# spec sync `--per-version-details` flag — design

**Date:** 2026-08-13
**Status:** Approved

## Problem

`SpecService.sync` and `SpecService.sync_spec` always run the two
**per-version** follow-up fetches after the spec detail page has been
parsed:

- **ETSI PDF** — `_maybe_fetch_etsi_pdf` (`spec_service.py:398`):
  HTTP GET to `https://portal.etsi.org/.../Report_WorkItem.asp?WKI_ID=...`
  to extract the "download as PDF" link, gated on recency (`< 90 d`) and
  `pdf_url is None`.
- **CR list** — `_maybe_fetch_crs` (`spec_service.py:419`):
  HTTP GET to `https://portal.3gpp.org/ChangeRequests.aspx?q=1&versionId=...`
  to extract the comma-joined `tdoc_id` list, gated on recency OR
  `crs is None/empty`.

For a spec with N versions both branches always submit N + N = `2N`
futures to the follow-up executor. The detail page itself plus the
per-version follow-ups make a spec sync noticeably slower and noisier
on the upstream, and most users only care about the detail-page fields
(`status`, `radio_tech`, `wis`, `rapporteurs`, `ftp_url`, `upload_date`,
`release`).

There is no per-call knob to opt out of the follow-ups. `--force`
bypasses the **per-spec skip rule**; it does not gate the follow-ups.
The new flag does.

## Approach

Add one boolean CLI/MCP/web flag — `--per-version-details` /
`per_version_details` — that defaults to `False` and gates only the
two per-version HTTP follow-ups (ETSI PDF + CR list) on the
`spec sync` path. When `False` the follow-ups are not submitted, but
the **stored** `pdf_url` and `crs` values on existing
`spec_versions` rows are preserved (not cleared) on a re-sync.

The detail page itself is **always** re-fetched; the header fields and
the version-table fields that come from the detail page
(`status`, `radio_tech`, `initial_release`, `wis`, `rapporteurs`,
`version`, `ftp_url`, `meeting_id`, `meeting_name`, `upload_date`,
`release`, `version_id`, `wki_id`) are still parsed and upserted. Only
the two `fetch_*` calls that run *after* `parse_spec_detail` are
gated.

The service-layer seam is a single early-return in
`_fetch_followups_concurrently` plus a back-fill of both `pdf_url` and
`crs` from the DB onto freshly-parsed versions (regardless of the
flag) so a flag-OFF re-sync that re-runs `parse_spec_detail` and
`upsert_versions` cannot clobber the stored `pdf_url` / `crs` with
the freshly-parsed `None`s.

The flag is plumbed through end-to-end:

- **Service:** `SpecService.sync(tsg, *, force, per_version_details, ...)`,
  `SpecService.sync_spec(spec_id, *, force, per_version_details, ...)`,
  `_sync_one_spec(..., per_version_details)`, and
  `_fetch_followups_concurrently(..., per_version_details)`.
- **CLI:** `doc3gpp spec sync [--tsg|--spec-id] [--force] [--per-version-details]`.
- **Web:** new checkbox "Also fetch per-version details (ETSI PDF + CR list)"
  on the spec detail page sync form, posted as
  `per_version_details: bool` to `POST /jobs/sync/specs`.
- **Job handler:** reads `per_version_details` from `job.params`,
  forwards to `services.spec.sync(...)` / `sync_spec(...)`.
- **MCP:** new `per_version_details: bool = False` arg on the
  `sync_specs` tool, forwarded into `params`.

`Settings.sync.auto_sync` is **unchanged** and is not affected by
this design. No read-side CLI command auto-syncs specs today — the
existing `trigger_auto_sync` orchestrator (`cli_auto_sync.py`) only
fires `meeting_service.sync` and
`tdoc_sync_coordinator.sync_for_meeting_id`. The `spec list` /
`spec show` paths use `SpecService.list_recent` / `get` /
`list_versions`, which are read-only and do not trigger syncs. So
no auto-sync call site picks up the new default; this design is
purely a change to the explicit `doc3gpp spec sync` flow.

## Changes

### 1. Service — `src/doc3gpp/services/spec_service.py`

**`SpecService.sync(tsg, *, force=False, per_version_details=False, on_progress=None)`**

- Thread `per_version_details` into each per-worker
  `_sync_one_spec(spec, canonical, followup_executor, client, per_version_details)`
  submission.
- Docstring updated to note the flag.

**`SpecService.sync_spec(spec_id, *, force=False, per_version_details=False, on_progress=None)`**

- Same — thread the flag into the single `ThreadPoolExecutor(max_workers=1)`
  + `_sync_one_spec(...)` call.

**`_sync_one_spec(spec, canonical, followup_executor, client, per_version_details)`**

- `_backfill_pdf_urls(versions)` is renamed to
  `_backfill_followup_fields(versions)` and extended to also copy
  `crs` from the persisted `spec_versions` rows onto the freshly-parsed
  versions. Runs **regardless of `per_version_details`** so a
  flag-OFF re-sync that re-runs `upsert_versions` writes the stored
  `pdf_url` / `crs` back, not the freshly-parsed `None`s. This is
  what preserves existing rows on a default sync.
- `_fetch_followups_concurrently(versions, followup_executor, client, per_version_details)`
  is called with the flag.

**`_backfill_followup_fields(self, versions: list[SpecVersion]) -> None`**

- New body: build `by_version = {v.version: (v.pdf_url, v.crs) for v in persisted if v.pdf_url or v.crs}`,
  then for each `v` in the freshly-parsed list, if `v.pdf_url is None`
  copy the stored `pdf_url` and if `v.crs is None` copy the stored
  `crs`. The old `_backfill_pdf_urls` test (single-pdf-url back-fill)
  is subsumed; the helper now covers both columns.
- Runs on **every** sync (both `True` and `False` flag values).

**`_fetch_followups_concurrently(versions, executor, client, per_version_details)`**

- New `per_version_details: bool` parameter.
- When `per_version_details is False`, return immediately without
  submitting any futures. The `followup_executor` is still constructed
  in the caller (so the no-op path is symmetric with the
  with-follow-ups path).
- When `True`, the existing per-version `executor.submit(self._safe_fetch_etsi_pdf, v, client)`
  + `executor.submit(self._safe_fetch_crs, v, client)` loop runs
  unchanged. The two `_maybe_fetch_*` methods are unchanged.

**`_safe_fetch_etsi_pdf` / `_safe_fetch_crs` / `_maybe_fetch_etsi_pdf` / `_maybe_fetch_crs`**

- Unchanged. The 90-day recency window and the empty-`crs` back-fill
  rule inside `_maybe_fetch_crs` still apply when the flag is `True`
  — they were never the bug, just the order of operations.

### 2. CLI — `src/doc3gpp/cli.py` (`spec_sync`, line 3857)

- New option:

  ```python
  per_version_details: bool = typer.Option(
      False,
      "--per-version-details",
      "-d",
      help=(
          "Also fetch per-version follow-ups (ETSI PDF link + CR list). "
          "Default off so the sync stays cheap; existing stored "
          "pdf_url and crs values are preserved either way."
      ),
  )
  ```

- The `--tsg` and `--spec-id` branches both forward
  `per_version_details=per_version_details` to
  `service.sync(...)` / `service.sync_spec(...)`. No change to the
  tqdm progress bars.
- The CLI docstring is updated to mention the new default and the
  flag.

### 3. HTTP job route — `src/doc3gpp/web/routes/jobs.py`

- `_SyncSpecsBody` (line 116) gains `per_version_details: bool = False`.
- `post_sync_specs` (line 183) writes
  `params["per_version_details"] = body.per_version_details` alongside
  the existing `force` and the chosen `tsg` / `spec_id`.
- The JSON body shape mirrors the existing `{tsg|spec_id, force}`
  envelope: an unknown field is silently dropped, so older clients
  that POST without `per_version_details` keep working (and pick up
  the new default `False`).

### 4. Job handler — `src/doc3gpp/web/workers/handlers.py` (`_sync_specs`, line 128)

- Read `per_version_details = bool(job.params.get("per_version_details", False))`.
- Forward to
  `services.spec.sync_spec(spec_id, force=force, per_version_details=per_version_details, on_progress=on_progress)`
  and
  `services.spec.sync(tsg, force=force, per_version_details=per_version_details, on_progress=on_progress)`.
- Progress mapping (`list_parsed` / `spec_done` → `progress(...)`)
  unchanged.

### 5. MCP tool — `src/doc3gpp/web/mcp_server.py` (`sync_specs`, line 480)

- New arg:

  ```python
  per_version_details: Annotated[
      bool,
      Field(
          description=(
              "Also fetch per-version follow-ups (ETSI PDF link + CR list). "
              "Defaults to false to keep the sync cheap; existing stored "
              "pdf_url and crs values are preserved either way."
          )
      ),
  ] = False,
  ```

- `params` for the `tsg` branch and the `spec_id` branch both gain
  `params["per_version_details"] = per_version_details`. The
  `message` strings are unchanged.

### 6. Web spec detail page — `templates/spec_show.html` + `static/js/spec_sync.js`

**`spec_show.html`:**

- Inside `#spec-sync-form`, after the existing
  `<input type="checkbox" name="force"> Force sync`, add:

  ```html
  <label class="inline-check">
    <input type="checkbox" name="per_version_details">
    Also fetch per-version details (ETSI PDF + CR list)
  </label>
  ```

  Unchecked by default. The submit button + queued hint + job target
  are unchanged.

**`spec_sync.js`:**

- `buildBody` now reads both checkboxes:

  ```js
  buildBody: function (form) {
    var force = form.querySelector('input[name="force"]').checked;
    var perVersion = form.querySelector('input[name="per_version_details"]').checked;
    return JSON.stringify({
      spec_id: specId,
      force: force,
      per_version_details: perVersion,
    });
  },
  ```

  Tolerates a missing `per_version_details` input (older cached HTML
  rendered before this change) by defaulting to `false` via
  `form.querySelector(...) && ...checked`.

### 7. Auto-sync

`Settings.sync.auto_sync` (the implicit trigger from `meeting list`,
`tdoc list`, `tdoc show`, and database-mode `tdoc parse`) already
delegates to `SpecService.sync` / `sync_spec`. The new default `False`
on those service methods means auto-synced spec sweeps also skip
follow-ups, which is the intended (cheap) behaviour. No code change.

### 8. Tests

**`tests/unit/test_spec_service.py`:**

- `test_sync_skips_etsi_fetch_when_pdf_url_already_persisted` and
  `test_sync_skips_etsi_fetch_for_stale_versions` — update to pass
  `per_version_details=True` to the service (the new default would
  otherwise swallow the follow-up call). Add a parallel test
  `test_sync_skips_followups_when_per_version_details_false` that
  asserts `etsi_calls == []` and `cr_calls == []` AND the stored
  `pdf_url` / `crs` are preserved on a second sync.
- New test `test_sync_preserves_stored_crs_when_per_version_details_false`:
  a stored version with `crs="R5-260001,R5-260002"` and
  `pdf_url="..."` is re-synced with the flag off; the freshly-parsed
  `None` is overwritten by the back-fill, so the second sync's
  `upsert_versions` writes the original values back. Verified by
  reading `repo.list_versions(...)` after the second sync.
- New test `test_sync_spec_passes_per_version_details_through`:
  single-spec path forwards the flag (monkeypatched follow-up mocks
  prove the call).
- New test `test_sync_followup_executor_not_used_when_flag_false`:
  optional — assert the follow-up executor receives zero submissions
  when the flag is off (proves the gate lives in
  `_fetch_followups_concurrently`, not in `_maybe_fetch_*`).
- Existing `test_sync_smoke` updated to pass
  `per_version_details=True` so the follow-up mocks continue to be
  invoked; the test's surface (status, counts) is unchanged.

**`tests/integration/test_spec_cli.py`:**

- The `_ProgressFakeSpecService.sync` signature gains
  `per_version_details` so the CLI's new keyword arg does not break
  the test fake. Existing tests that only check `force` are
  unaffected; the fake stores the flag but does not act on it.
- New test `test_spec_sync_per_version_details_flag`: invoke the CLI
  with `--per-version-details` and assert the fake recorded
  `per_version_details=True` (the test fake stores the flag on the
  call); and without the flag, `per_version_details=False` (the
  default).

**`tests/unit/test_web_jobs_routes.py`:**

- Existing `POST /jobs/sync/specs` tests for `tsg` and `spec_id`
  unchanged.
- New test `test_post_sync_specs_forwards_per_version_details`:
  body with `per_version_details: true` → `job.params ==
  {"tsg": "R5", "force": False, "per_version_details": True}`; body
  with the field omitted → `per_version_details == False` in
  `params`.

**`tests/unit/test_job_worker.py`:**

- `SYNC_SPECS` job handler test with `per_version_details=True` in
  `params` dispatches the flag to the fake `services.spec.sync(...)`
  or `sync_spec(...)`.

**`tests/integration/test_mcp_end_to_end.py`:**

- The `sync_specs` tool end-to-end test passes
  `per_version_details=True` and asserts the enqueued
  `Job.params["per_version_details"] is True`. A second case
  without the arg asserts `False`.

**`tests/unit/test_web_routes.py`:**

- `test_get_spec_show_renders_sync_form` is updated to assert the
  new checkbox is present in the rendered HTML
  (`name="per_version_details"`).

### 9. Docs (per AGENTS.md doc-sync convention)

- `README.md` — `spec sync` blurb mentions the new default and the
  `--per-version-details` flag.
- `AGENTS.md` — `spec sync` row in the "Architecture boundaries /
  Workflows" table mentions the new default.
- `docs/cli.md` — `spec sync` reference adds `--per-version-details`,
  notes the default, and the "Existing stored `pdf_url` and `crs`
  values are preserved either way" guarantee.
- `docs/web-server.md` — spec detail page sync form now lists
  `force` + `per_version_details` checkboxes.
- `docs/conventions.md` — under the CLI conventions section, note
  the "default-OFF follow-ups" rule for spec sync.
- `docs/code-map.md` — `SpecService` row mentions the
  `per_version_details` keyword on `sync` / `sync_spec`.
- `doc3gpp.toml.example` — no change (the flag is per-call, not a
  setting).

## Out of scope

- No setting in `doc3gpp.toml` for the new flag — it's a per-call
  knob, mirroring `--force`. Users who want the old behaviour globally
  can wrap the CLI or the MCP tool.
- No change to the per-spec skip rule (`specs.last_synced_at`) — the
  flag is orthogonal to `force`.
- No change to the 90-day recency window or the empty-`crs`
  back-fill inside `_maybe_fetch_crs` — those gates still apply when
  the flag is `True`.
- No UI surface for "partial" follow-ups (ETSI but not CR, or vice
  versa). One bool gates both, as agreed during brainstorming.
- No DB migration — `spec_versions.pdf_url` and `spec_versions.crs`
  column shapes are unchanged; the back-fill is a Python-side
  read-modify-write of the freshly-parsed `SpecVersion` objects
  before `upsert_versions`.
