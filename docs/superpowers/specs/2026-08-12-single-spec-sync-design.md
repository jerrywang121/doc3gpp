# Single-spec sync + sync-all-in-DB — design

**Date:** 2026-08-12
**Status:** Approved (subsequently amended — see note below)

> **Superseded** — the per-TSG `tsgs.spec_last_sync` skip rule
> described in this design was replaced by a per-spec
> `specs.last_synced_at` skip rule in
> [`docs/superpowers/plans/2026-08-13-per-spec-skip-rule.md`](../../plans/2026-08-13-per-spec-skip-rule.md).
> The `--spec-id` selector, `SpecService.sync_spec`, the
> `SpecRepository.list_distinct_tsgs` repo method, the no-selector
> fallback, the HTTP / MCP / job / web hookups, and the test
> coverage all stand as written; only the skip-rule source
> (`tsgs.spec_last_sync` → `specs.last_synced_at`) and the TSG
> stamp moved. The original design text is preserved below for
> historical reference.

## Problem

`spec sync` currently syncs only **per-TSG** (or every TSG found in the
`meetings` table). There is no way to sync a **single spec** — the common
case of "one spec was updated, refresh just it" — and the no-`--tsg`
fallback is anchored to meetings rather than to the specs actually stored.

Additionally, the web spec **detail** page has no sync button at all, even
though the spec-sync job (`POST /jobs/sync/specs`) already exists and the
web surface exposes sync buttons for meetings and TDocs.

## Approach

Add a `--spec-id` selector to `spec sync` (mutually exclusive with
`--tsg`), change the no-selector fallback to iterate the distinct TSGs of
the **specs** table, expose the single-spec path on the HTTP job route,
job handler and MCP tool, and add a sync button (with a `--force`
checkbox) to the web spec detail page that enqueues the single-spec job
and auto-refreshes on completion — all reusing the existing
`bindJobPolling` helper.

## Changes

### 1. Service — `src/doc3gpp/services/spec_service.py`

**`SpecService.sync_spec(spec_id: str, *, force: bool = False, on_progress=None) -> SyncOutcome`**

Syncs one spec without fetching the list page:

- Look up the stored spec via `self._repository.get(spec_id)` to recover
  its `tsg` (needed for the `specs` FK and for `parse_spec_detail`).
  A miss raises `ValueError("spec <id> is not in the database...")` —
  a single spec can only be synced when it is already stored.
- Honour the per-TSG `tsgs.spec_last_sync` skip rule (unless `force`),
  identical to `sync()` — a single spec whose TSG was synced within
  `sync.spec_sync_interval` is skipped with a `skipped` `SyncOutcome`.
  **Superseded** — the skip rule is now per-spec and keyed on
  `specs.last_synced_at`; see
  [`docs/superpowers/plans/2026-08-13-per-spec-skip-rule.md`](../../plans/2026-08-13-per-spec-skip-rule.md).
- Open one `ScraperClient`, call `self._sync_one_spec(spec, canonical,
  followup_executor, client)` with a single-worker `ThreadPoolExecutor`
  for the ETSI/CR follow-ups, fire `"spec_done"` on progress.
- Stamp `tsgs.spec_last_sync` at the end (same as `sync()`).
  **Superseded** — the per-TSG stamp is gone; the per-worker
  pipeline now stamps the spec's own `specs.last_synced_at`.
- Return `SyncOutcome` with `status`, `reason`, `synced_count` (0 or 1)
  and `version_count`.

Refactor the shared "skip-check + client + stamp" tail of `sync()` into a
small private helper so `sync()` and `sync_spec()` stay in lock-step.

**`SpecService.list_distinct_tsgs() -> list[str]`** — distinct TSG short
names from the `specs` table, delegating to a new
`SpecRepository.list_distinct_tsgs()`. Ordered alphabetically.

### 2. Repository — `repository/protocols.py` + `storage/repositories/spec_sql.py`

Add `list_distinct_tsgs()` to `SpecRepository` (Protocol) and implement it
in `SQLAlchemySpecRepository` mirroring
`meeting_sql.list_distinct_tsgs`: `select(distinct(SpecORM.tsg))`
excluding `NULL`, `order_by(SpecORM.tsg)`.

### 3. CLI — `src/doc3gpp/cli.py` (`spec sync`)

- Add `--spec-id` option (dotted id, e.g. `36.579-5`).
- Add an XOR guard: passing both `--tsg` and `--spec-id` raises
  `typer.BadParameter`.
- `--spec-id` given → look up the spec (`service.get`); miss raises
  `typer.BadParameter("Unknown spec id ...")`; else call
  `service.sync_spec(spec_id, force=force)` with the same tqdm bar
  (desc `spec <spec_id>`).
- `--tsg` given → existing behavior.
- Neither → iterate `service.list_distinct_tsgs()` (specs table) instead
  of `build_meeting_service().list_distinct_tsgs()`; empty → "No stored
  specs with a TSG found; nothing to sync."
- Help text and docstring updated to document all three selectors.

### 4. HTTP job route — `src/doc3gpp/web/routes/jobs.py`

`_SyncSpecsBody` becomes `{tsg: str | None = None, spec_id: str | None =
None, force: bool = False}`. `post_sync_specs` requires **exactly one** of
`tsg` / `spec_id` (else `InvalidFilterError` → 400) and writes
`{"tsg": ...}` or `{"spec_id": ...}` plus `force` into `job.params`.

### 5. Job handler — `src/doc3gpp/web/workers/handlers.py` (`_sync_specs`)

Read `spec_id` OR `tsg` from params. With `spec_id` → call
`services.spec.sync_spec(spec_id, force=force, on_progress=...)`; with
`tsg` → current `sync(...)` path. Progress mapping and return summary
unchanged (`status` / `reason` / `synced_count` / `version_count`). Raise
`ValueError` when neither is present.

### 6. MCP tool — `src/doc3gpp/web/mcp_server.py` (`sync_specs`)

Add `spec_id` param. Require exactly one of `tsg` / `spec_id` (else
`InvalidFilterError`). Enqueue `{"spec_id": ...}` or `{"tsg": ...}` +
`force`.

### 7. Web spec detail page — `templates/spec_show.html` + `static/js/spec_sync.js`

Add a "Sync" card mirroring the TDoc Parse card:

- `<form id="spec-sync-form" action="/jobs/sync/specs" method="post"
  data-spec-id="{{ spec.spec_id }}">` with a `force` checkbox,
  a submit button "Sync this spec", a `spec-sync-queued` hint
  (hidden), and `<div id="spec-sync-job-target"></div>`.
- Include `job_poller.js` + new `spec_sync.js`.
- `spec_sync.js` mirrors `tdoc_parse.js`: `bindJobPolling(form, {
  queuedSelector: ".spec-sync-queued", targetSelector:
  "#spec-sync-job-target", contentType: "application/json",
  buildBody })` POSTing `{spec_id, force}`. `bindJobPolling` already
  shows the queued hint, polls via the job-status partial, hides the
  hint and reloads the page on terminal state.

### 8. Tests

- `tests/unit/test_spec_service.py`: `sync_spec` — happy path, skip rule,
  unknown-spec `ValueError`.
- `tests/unit/test_spec_repository_filters.py` (or new): `list_distinct_tsgs`.
- `tests/integration/test_spec_cli.py`: `--spec-id` syncs one spec;
  `--spec-id` unknown raises; `--tsg`+`--spec-id` both rejected;
  no-selector iterates specs-table TSGs (not meetings).
- `tests/unit/test_web_jobs_routes.py`: `sync/specs` with `spec_id`
  creates the job; both `tsg`+`spec_id` → 400; neither → 400.
- `tests/unit/test_job_worker.py`: `SYNC_SPECS` job with `spec_id`
  dispatches to `sync_spec` (fake service records the call).
- `tests/integration/test_mcp_end_to_end.py`: `sync_specs` with
  `spec_id` enqueues the expected params.
- `tests/unit/test_web_routes.py`: `GET /specs/{id}` renders the sync
  form (button + force checkbox present).

### 9. Docs (per AGENTS.md doc-sync convention)

- `docs/cli.md`: `spec sync` options (`--tsg` / `--spec-id` / `--force`),
  the XOR rule, and the no-selector fallback (specs table).
- `docs/web-server.md`: job route body now accepts `spec_id`; document the
  spec detail page sync button.
- `docs/code-map.md`: `SpecService` / `SpecRepository` rows mention
  `sync_spec` + `list_distinct_tsgs`.
- `docs/architecture.md`: spec sync section documents the single-spec
  path.
- `README.md`, `AGENTS.md`: CLI / job-surface summaries mention
  `--spec-id`.

## Out of scope

- No per-TSG bulk "sync all specs" on the web surface beyond the existing
  `--tsg` path.
- No `sync_spec` MCP tool name change — the existing `sync_specs` tool
  gains a `spec_id` param instead.
- No change to the `tsgs.spec_last_sync` semantics (single-spec sync
  still stamps the owning TSG). **Superseded** — `tsgs.spec_last_sync`
  was removed entirely; the skip rule is per-spec and the only stamp
  is on the spec's own `specs.last_synced_at` (see
  [`docs/superpowers/plans/2026-08-13-per-spec-skip-rule.md`](../../plans/2026-08-13-per-spec-skip-rule.md)).
