# Spec sync — DynaReport direct fetch on `--spec-id` miss

**Status:** design draft (awaiting user review before writing-plans)
**Date:** 2026-08-12
**Author:** brainstorming session

> **Superseded** — the per-TSG `tsgs.spec_last_sync` skip rule
> described in §2 and §4 of this design was replaced by a
> per-spec `specs.last_synced_at` skip rule in
> [`docs/superpowers/plans/2026-08-13-per-spec-skip-rule.md`](../../plans/2026-08-13-per-spec-skip-rule.md).
> The DynaReport direct fetch on `--spec-id` miss, the
> `SpecUnknownOnUpstreamError` / `UnknownTsgError` exception types,
> the error mapping in `web/errors.py`, and the test coverage all
> stand as written; only the skip-rule source moved. The original
> design text is preserved below for historical reference.

## 1. Problem

`doc3gpp spec sync --spec-id <id>` currently refuses to run when the
spec is not already in the local `specs` table:

```text
$ doc3gpp spec sync --spec-id 38.523-1
Error: Unknown spec id '38.523-1'. Run 'doc3gpp spec sync --tsg <tsg>' first.
```

To get a fresh single spec, the user has to know which TSG owns it and
run the whole-TSG sweep first (`spec sync --tsg R5`), which is wasteful
when only one spec is wanted. The DynaReport detail page at
`https://www.3gpp.org/DynaReport/{no_dot}.htm` already carries the
three fields needed to insert a header row (`title`, `type`, and the
primary responsible group), so we can do a direct fetch and bootstrap
the row on demand.

This spec adds a service-level fallback in `SpecService.sync_spec`:
when the DB lookup misses, fetch the DynaReport detail page directly,
parse the three header fields, normalise the responsible group to a
seeded TSG short name, and funnel the freshly-built `Spec` through the
existing `_sync_one_spec` pipeline. The web / MCP entry points that
delegate to `sync_spec` (`POST /jobs/sync/specs` and the
`sync_specs` MCP tool) inherit the new behaviour automatically.

User-confirmed decisions during brainstorming:

* Unknown / legacy responsible groups (e.g. `RAN AH*`) → refuse the
  sync with `typer.BadParameter` rather than persist `tsg=NULL` or
  auto-create the TSG row.
* Empty / 404 detail body → refuse the sync with `typer.BadParameter`.
* Implementation lives at the service layer (not CLI-only) so the web
  and MCP paths benefit too.

## 2. Layering

### 2.1 New parser functions in `parsers/spec_parser.py`

Two pure functions, both added to the existing module so all
detail-page parsing stays in one place:

* `parse_dynareport_header(html: str) -> SpecHeaderFields` — extracts
  `title` (`#titleVal`), `type` (`#typeVal` — `Technical specification
  (TS)` → `TS`, `Technical report (TR)` → `TR`; a free-text `TS` /
  `TR` token passes through unchanged via the same `\b(TS|TR)\b`
  regex used by `_extract_type_token` at `parsers/spec_parser.py:61`),
  and `tsg_long_name` (the text of the second `<td>` in the row that
  contains `#PrimaryResponsibleGroupLbl`). Returns a `NamedTuple`
  `SpecHeaderFields(title: str | None, type: str | None, tsg_long_name:
  str | None)` with all three fields `None` when missing. `NamedTuple`
  is used instead of a `@dataclass` to keep the parser module free of
  any new domain class — the existing module does not currently
  declare dataclasses, and the intermediate is a parser-private DTO
  that does not flow past `SpecService.sync_spec`.

* `normalise_tsg_long_name(long_name: str) -> str | None` — collapses
  the long label to a `tsgs.short_name` row. Algorithm:

  1. Strip whitespace, collapse multi-spaces, uppercase.
  2. Match `^(RAN|CT|SA)\s*(?:WG)?\s*(\d+)$` → `{R|C|S}{digits}`
     (e.g. `RAN 5` → `R5`, `CT 1` → `C1`, `SA WG2` → `S2`).
  3. Match `^(RT|RP|CP|SP)$` exactly → return as-is (plenary, no
     number).
  4. Anything else (including `RAN AH*`, `RAN` with no number, free
     text) → `None`.

  `SpecHeaderFields` and `normalise_tsg_long_name` are exported
  alongside the existing `parse_spec_list` / `parse_spec_detail` from
  `parsers.spec_parser`.

### 2.2 `scraping/spec_source.py`

Add one helper:

* `fetch_dynareport_detail(spec_id_dotted: str, client) -> str` —
  composes the URL via the existing `build_spec_detail_url`
  (`{spec_id_no_dot}.htm` = dotted id with the dot stripped), then
  delegates to the same `client.get_text` path. No new HTTP constants
  — the existing `_SPEC_DETAIL_URL_TEMPLATE` is the single source of
  truth.

### 2.3 `services/spec_service.py`

`sync_spec(spec_id, *, force, on_progress)` is refactored:

1. Try `repository.get(spec_id)`. If present, take the existing path
   (use the stored `tsg`, honour the per-TSG skip rule, hand off to
   `_sync_one_spec`).
2. If absent, fetch the DynaReport detail page directly via
   `fetch_dynareport_detail`. Parse with
   `parse_dynareport_header`. If any of the three required fields
   (`title`, `type`, `tsg_long_name`) is missing, raise
   `SpecUnknownOnUpstreamError`. Otherwise normalise the long name via
   `normalise_tsg_long_name`; if the result is `None`, raise
   `SpecUnknownOnUpstreamError` (the upstream label was unrecognised,
   e.g. `RAN AH1`).
3. Validate the normalised short name against
   `self._tsg_repository.get_by_short_name(canonical)` (the repo is
   the existing field on `SpecService`; no new dependency on
   `TsgService` — the in-service lookup is sufficient and matches the
   existing `_is_sync_skipped` call shape at
   `services/spec_service.py:222`). If the lookup returns `None`,
   raise `UnknownTsgError`. The validation runs **before** the FK
   insert so `tsgs.short_name` is guaranteed to exist when the row
   lands. If `self._tsg_repository is None` (a `SpecService`
   constructed without a TSG repo, e.g. a unit test), the validation
   step is skipped and the FK insert is allowed to surface any
   integrity error — matches the existing pattern where the TSG repo
   is treated as optional throughout.
4. Build an in-memory `Spec(spec_id=spec_id, type=type, title=title,
   tsg=canonical)` (status / radio_tech / initial_release / wis /
   rapporteurs left as their default-`None` — they are filled by the
   existing `parse_spec_detail` call inside `_sync_one_spec`).
5. Honour the per-TSG skip rule (now possible because the TSG is
   known). `--force` bypasses as before.
6. Hand off to `_sync_one_spec(header, ...)` — the rest of the
   pipeline (detail-page parse, ETSI / CR follow-ups, two-phase
   `last_synced_at` write, ~~`tsgs.spec_last_sync` stamp~~) is
   unchanged. **Superseded** — the `tsgs.spec_last_sync` stamp is
   gone; the per-worker pipeline now stamps the spec's own
   `specs.last_synced_at` only. See
   [`docs/superpowers/plans/2026-08-13-per-spec-skip-rule.md`](../../plans/2026-08-13-per-spec-skip-rule.md).

`_sync_one_spec` does **not** need to know the row came from the
fallback path. It receives a `Spec` with `tsg` set and behaves
identically.

## 3. Errors

Two new exception types, both in `services/spec_service.py`:

* `SpecUnknownOnUpstreamError(LookupError)` — detail page is 404,
  body has no `#titleVal` / `#typeVal` / `#PrimaryResponsibleGroupLbl`,
  or the long group name fails normalisation (e.g. `RAN AH1`).
  Message: `"spec {id!r} is unknown on the 3GPP DynaReport upstream
  ({reason}); nothing to sync"`.

* `UnknownTsgError(ValueError)` — normalised short name is not a
  row in `tsgs`. Message: `"spec {id!r} has unknown TSG short name
  {short!r} (normalised from {long!r}); run 'doc3gpp tsg seed' or
  'doc3gpp tsg list' to inspect the reference table"`.

CLI (`src/doc3gpp/cli.py:3905`): the existing pre-flight block is
removed. Today, lines 3905-3910 of `cli.py` do an early
`service.get(spec_id)` lookup and raise `typer.BadParameter` if the
row is missing; with this spec, that responsibility moves into
`SpecService.sync_spec`, so the CLI's pre-flight is dead code. The
new shape is: always set up the tqdm progress bar, always call
`service.sync_spec(spec_id, force=force, on_progress=...)`, and
wrap that call in a `try / except` for the two new error types. Both
are re-raised as `typer.BadParameter` with the message preserved. The
existing mutual-exclusion check (lines 3900-3903) and the rest of the
command body (the `--tsg` branch and the no-selector fallback) are
unchanged.

Web (`src/doc3gpp/web/errors.py`): the three existing lookup
tables (`_MCP_RESOURCE_BY_EXC`, `_ERROR_SLUGS`, `_STATUS_BY_EXC` at
`web/errors.py:95,131,146`) are extended with two new rows:

* `SpecUnknownOnUpstreamError` → resource `("spec", MCP_CODE_NOT_FOUND)`,
  slug `spec_unknown_on_upstream`, HTTP `404`.
* `UnknownTsgError` → resource `("spec", MCP_CODE_INVALID_PARAMS)`,
  slug `unknown_tsg`, HTTP `400`.

`SpecNotFoundError` and its existing mapping are unchanged.

The `POST /jobs/sync/specs` route (`web/routes/jobs.py`) surfaces the
error message inside the job's `result_summary` / `error` columns; the
handler in `web/workers/handlers.py:_sync_specs` re-raises from
`services.spec.sync_spec`, and the worker's exception path writes the
exception message to the job record unchanged.

MCP (`web/mcp_server.py`): the `sync_specs` tool's `?format=json`
shape byte-matches; the new exception types route through the
extended `map_mcp_error` table.

## 4. What does NOT change

* `SpecService.sync(tsg=...)` — whole-TSG sweep, unchanged.
* `_sync_one_spec`, `_backfill_pdf_urls`, `_fetch_followups_concurrently`,
  `_maybe_fetch_etsi_pdf`, `_maybe_fetch_crs` — unchanged.
* The `specs` and `spec_versions` schema — unchanged. No migration.
* `SQLAlchemySpecRepository.upsert` / `upsert_versions` — unchanged.
* `tsgs.spec_last_sync` skip rule semantics — unchanged.
  **Superseded** — the per-TSG skip was replaced by a per-spec
  `specs.last_synced_at` skip in
  [`docs/superpowers/plans/2026-08-13-per-spec-skip-rule.md`](../../plans/2026-08-13-per-spec-skip-rule.md);
  the column itself was dropped and the TSG repo no longer
  carries the helper.
* The `--force` flag, the `--tsg` / `--spec-id` mutual-exclusion
  check, the no-selector fallback that iterates
  `list_distinct_tsgs` — unchanged.

## 5. Testing

### 5.1 Unit tests (`tests/unit/test_spec_parser.py`)

* `parse_dynareport_header` — assert each of the three fields is
  extracted from a small HTML fragment, and that a fragment missing
  the `#titleVal` / `#typeVal` / `#PrimaryResponsibleGroupLbl` span
  returns `None` for that field.
* `normalise_tsg_long_name` — parametrised matrix:
  * `RAN 1` → `R1`, `RAN WG1` → `R1`, `RAN5` → `R5`
  * `CT 1` → `C1`, `CT 3` → `C3`
  * `SA 2` → `S2`, `SA WG6` → `S6`
  * `RT` → `RT`, `RP` → `RP`, `CP` → `CP`, `SP` → `SP`
  * `RAN AH1` → `None`, `RAN` → `None`, ` ""` → `None`,
    `"bogus"` → `None`

### 5.2 Unit tests (`tests/unit/test_spec_service.py`)

* `sync_spec` with a missing row but a successful DynaReport fetch
  builds a `Spec` from the parsed header, calls `_sync_one_spec`,
  and persists the row via the existing stub repo's `upsert`.
* `sync_spec` raises `SpecUnknownOnUpstreamError` when the detail
  HTML is empty / 404.
* `sync_spec` raises `UnknownTsgError` when the normalised short name
  is not in `self._tsg_repository` (i.e. the stub repo returns
  `None` from `get_by_short_name`).

### 5.3 Integration tests (`tests/integration/test_spec_cli.py`)

* `test_spec_sync_spec_id_fetches_from_dynareport_when_missing` —
  pre-seed a SQLite DB with an empty `specs` table; stub
  `fetch_dynareport_detail` to return a fixture HTML carrying
  `#titleVal` (long), `#typeVal` ("Technical specification (TS)"),
  and a primary-responsible-group cell ("RAN 5"); assert the CLI
  succeeds, the `specs` row is created, and the output reads
  `Spec sync complete for {spec_id}: 1 spec, N versions stored`.
* `test_spec_sync_spec_id_unknown_tsg_bad_parameter` — fixture with
  a `RAN AH1` group; assert exit code != 0 and the message names the
  unknown short name.
* `test_spec_sync_spec_id_404_bad_parameter` — stub
  `fetch_dynareport_detail` to raise or return empty; assert
  `BadParameter` and a `Spec unknown on upstream` message.
* `test_spec_sync_spec_id_stored_row_unchanged` — pre-seed a
  `specs` row, stub `fetch_dynareport_detail` to never be called
  (it must not be), and assert the existing path was taken. (Reuses
  the existing `test_spec_sync` style.)

### 5.4 Test fixtures

A new fixture HTML file
`tests/fixtures/spec_pages/dynareport_header.html` carrying the three
target spans + a typical version table. The existing
`R5_detail_portal.html` is a *portal* page (different DOM) and is
**not** a valid input for `parse_dynareport_header` — the test
fixture is separate.

## 6. Out of scope

* Auto-creating TSG rows for unknown groups (rejected: pollutes the
  reference table).
* Persisting the row with `tsg=NULL` (rejected: breaks FK joins).
* Re-running the whole-TSG sweep to discover a spec — the whole
  point of the change is to avoid that.
* Re-using the parsed header to skip the second `parse_spec_detail`
  call. The detail-page parse is the source of truth for
  `status` / `radio_tech` / `initial_release` / `wis` /
  `rapporteurs` and the version rows; the new header parse is a
  bootstrap for the case where the DB has nothing. Keeping two
  separate parses matches the existing architecture.
