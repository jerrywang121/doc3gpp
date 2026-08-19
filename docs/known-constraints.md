# Known Constraints

> Last reviewed: 2026-08-18

Open / known limitations that future work should respect. This file is
the single source of truth — `docs/architecture.md` §Out of scope (today)
and the per-feature notes below should stay in lock-step with this list.

When a constraint is lifted, **edit this file** as part of the same
change set so the docs stay honest.

## Schema

- **No Alembic migrations.** `Base.metadata.create_all` via
  `storage/db/migrate.py` (invoked by `db init`) is the only schema
  bootstrap. `db_auto_migrate` is a TOML-only field (see
  `doc3gpp.settings.schema.ALLOWED_ENV_VARS`) that does **not** run
  migrations. After pulling an ORM shape change, **existing SQLite
  installs must run `doc3gpp db reset --yes`** — otherwise the live
  schema stays out of sync and SQL repos raise `OperationalError`.
  The single exception is `src/doc3gpp/storage/db/migrate.py::_migrate_rename_tdoc_cr_details`,
  a one-shot idempotent rename that bridges legacy `tdoc_cr_details`
  callers to the current `tdoc_cr_cover_page` table; nothing else
  in the bootstrap is allowed to mutate an existing table in place.
- **`meeting sync`, `wi sync`, and `tsg seed` call `create_schema()`**
  for fresh-database ergonomics. Idempotent but blurs the `db init`
  boundary; `tdoc sync` and `tdoc parse` already dropped the call.
  See [`docs/conventions.md`](conventions.md) for the canonical list.
- **Cache schema break.** `tdoc_extracts` v2: hard schema break; dropped
  `zip_path` + `markdown_path` columns in favour of `cache_file`
  (String(255), indexed). Existing rows from older versions must be
  re-extracted with `--force` to repopulate the new column. Legacy
  on-disk files (`zips/<tdoc_lower>`, `markdown/<sha256>`) are orphaned
  and will be cleaned up by `cache purge` or FIFO eviction.

## Settings caching

- **`get_settings` is `@lru_cache(maxsize=1)`** and `ScraperClient`'s
  `__init__` reads settings once, so env changes mid-process do not
  propagate after the first call. Restart the process to pick up new
  `DOC3GPP_*` values (only the closed
  `doc3gpp.settings.schema.ALLOWED_ENV_VARS` subset is honoured by
  `Settings` — everything else is silently ignored). Tests must
  `cache_clear()` both loaders when mutating env via `monkeypatch` —
  see [`docs/conventions.md`](conventions.md) for the canonical fixture.

## Network / scraping

- **`https://www.3gpp.org/ftp/` is hardcoded** in the FTP scraper.
  If 3GPP moves the assets to a CDN, `meeting sync` will silently
  return empty rows rather than raise.
- **`ScraperClient.get_text` retries via a narrow exception surface**
  (specific `httpx` retryable subclasses + retryable HTTP status
  codes). Programming errors propagate immediately — see the
  anti-pattern in [`docs/conventions.md`](conventions.md).

## Calendar parser

- **Coupled to the current 3GPP DynaReport table layout.** Upstream
  HTML changes will silently break `meeting sync` (no error, just
  fewer rows). Re-pin tests against a recorded page snapshot when the
  table shape changes.

## TDoc list sync

- **TDoc lists are downloaded from the 3GPP portal.**
  `GenerateDocumentList.aspx?meetingId={meeting_id}` returns the XLSX
  directly; the URL template is configurable via
  `sync.tdoc_list_url_template`. Auxiliary TDoc files are still scanned
  from the meeting's FTP folders.
- **Skip rules are limited to the closed window and the local sync
  interval.** The upstream XLSX does not return a reliable
  `Last-Modified` header, so the coordinator no longer performs an
  mtime comparison. Use `--force` to bypass the remaining rules.
- **TDoc date parsing** accepts both ISO (`YYYY-MM-DD`) and the
  `DD/MM/YYYY HH:MM:SS` shape used upstream; both are stored as
  `Date`. Anything outside these shapes falls through to `NULL`.

## TDoc CR extraction

- **URL templates locked in for `R5s` (TTCN email CR) and `R5w`
  (TTCN workshop CR) branches** against offline fixtures. The `R5-`
  and `C6-` templates return `None` until exercised against the live
  site — treat `None` as "not yet supported", not as an error. See the
  inline docstring at
  `src/doc3gpp/scraping/tdoc_zip_source.py:_build_tdoc_zip_url`.
- **`python-docx` is an opt-in extra.** Without `doc3gpp[extract]`
  installed, the CLI prints a friendly install hint and exits 1.
- **`TDoc` types other than CR and LS (DRAFT, BB, …) are not handled.**
  The extractor's type guard raises `TDocTypeUnsupportedError` for
  unsupported ids. LS rows are supported on both the direct-mode
  `--from-url` path and DB-mode `tdoc parse` (which writes the
  `tdoc_cr_ls_details` sidecar via `LSParserBase`), and real local `.docx` /
  `.zip` files dispatched through `direct_parse_bytes` sniff the
  converted markdown via `is_ls_header_present` and route an
  LS-shaped body to `ThreeGPPLSParser.parse_ls` — a CR cover-shape
  keeps the CR family. IEEE / ETSI LS format variants are v2 stubs
  and are not registered in the parser registry.
- **`tdoc_cr_ls_details` is created lazily on first write, not on
  `doc3gpp db init`** (same lazy-bootstrap contract as
  `tdoc_cr_ttcn_details`): `TDocCrLSDetailOrm` is registered with
  `Base.metadata`, so a fresh install picks the table up via
  `create_schema()`, but existing installs gain it only when the LS
  repo writes its first row (`SQLAlchemyLSParserRepository._ensure_table_exists`
  runs `Base.metadata.create_all` on a missing-table probe).
- **`tdoc_cr_ls_details.response_to` migration.** The response-to
  cell collapsed to a single `response_to` column (raw cell text) —
  the legacy `response_to_title` / `response_to_group` /
  `response_to_doc` columns are gone. `create_schema()` runs
  `_migrate_ls_response_to_columns` for legacy databases — existing
  rows get `response_to` backfilled from the old columns, and the
  legacy columns are dropped on sqlite >= 3.35 (they remain as orphan
  columns on older sqlite but are never read/written).
- **`tdoc_cr_ttcn_details` is created lazily on first write, not on
  `doc3gpp db init`.** `TDocCrTtcnDetailOrm` is registered with
  `Base.metadata` so a fresh install picks it up via the standard
  `create_schema()` call, but existing installs that pre-date the
  sidecar gain the table only when `TDocCrService` writes the first
  TTCN row. Each repo implements `_ensure_table_exists()`: a
  `SELECT 1 FROM tdoc_cr_ttcn_details LIMIT 0` is attempted, and an
  `OperationalError("no such table")` triggers an idempotent
  `Base.metadata.create_all()` followed by a retry. The same lazy
  bootstrap covers `tdoc_cr_cover_page` on legacy DBs.
- **`tdoc_cr_ttcn_details.changed_functions` is a derived aggregate
  column.** It holds the sorted, deduplicated set of
  `"<module_basename>.<function_name>"` entries derived from each
  row's `required_changes` payload by
  `parsers/cr/ttcn_functions.py::extract_changed_functions` at parse
  time, persisted as a newline-delimited `Text` column (deliberately
  **not** gzip-compressed — the column is queryable via `LIKE`, e.g.
  `WHERE changed_functions LIKE 'NR5GC_Positioning_Functions.%'`).
  Pre-existing rows from before the column landed stay `NULL` after
  the lazy bootstrap; the repo's tolerant `_orm_to_details` reads
  `NULL` / empty as `[]` so the sidecar still round-trips, and the
  CLI renderers display the empty aggregate as `0 item(s)` (table)
  or `—` (markdown). For installs that pre-date this column, the
  same `_ensure_table_exists()` helper extends the lazy bootstrap
  with a column-probe step: a `SELECT changed_functions FROM
  tdoc_cr_ttcn_details LIMIT 0` is attempted after the table-exists
  probe, and an `OperationalError("no such column")` triggers an
  idempotent `ALTER TABLE tdoc_cr_ttcn_details ADD COLUMN
  changed_functions TEXT`. The probe is idempotent — a second call
  short-circuits as soon as the SELECT succeeds. The `--force`
  re-parse path (`doc3gpp tdoc parse --tdoc <id> --force`)
  recomputes and persists the aggregate on demand, since the
  derivation runs unconditionally inside
  `CRParserBase.parse()` at the `TDocCRTTCNDetails` construction
  site (`parsers/cr/cr_parsers.py:161`). A scripted bulk back-fill
  is not provided — iterate `tdoc list --cr-type TTCN` and invoke
  `tdoc parse --tdoc <id> --force --yes` per id.
  - **Partial-extraction markers.** The aggregate uses a 4-form
    output contract: when both the module basename and function name
    extract the entry is the full-match form `"<module>.<function>"`;
    when only the module basename is recoverable the entry is
    recorded as `'<module>.'` (trailing-dot sentinel); when only the
    function name is recoverable it is recorded as `'.<function>'`
    (leading-dot sentinel); when neither is recoverable the
    correction is dropped. The dot sentinels are unambiguous in SQL:
    `LIKE '%.'` finds module-only entries, `LIKE '.%'` finds
    function-only entries, and the joined dot inside the full-match
    form remains inert.
- **Markdown cache format changed.** `cache/markdown/<cache_file>`
  files are now real ZIP archives (single `<docx stem>.md` entry) so a
  plain `.zip` extension maps to a format `unzip` / 7z / WinZip can
  open. Pre-change files (gzip blob with `.zip` extension or plain
  UTF-8) stay readable via the magic-byte sniff in
  `tdoc_cr_service._decompress_markdown`; a re-extract via
  `doc3gpp tdoc parse --force` is only required if an operator wants
  the cache rewritten in the new shape.
- **`direct_parse_bytes` returns a loosely-typed tuple.**
  The helper exposes the third element of its return tuple as
  `object` (`tuple[str, str, object]`) to keep `parsers/`
  independent of the `models/` import cycle; the call sites that
  assign to `DirectParseResult.details` rely on a runtime
  `isinstance` check rather than a typed assignment. Pyright will
  flag the assignment as a soft error (`reportArgumentType` /
  `reportAssignmentType`) until the return type is tightened. The
  trade-off is intentional today — the wider fix is a follow-up.

## Test surface

- **`online` tests access live `3gpp.org` + FTP** and are flaky.
  Always run with `-rs` to surface skip reasons; do not gate CI on
  them. They live behind the `online` pytest marker so the default
  profile (`pytest -m "not online"`) skips them.
- **No CI pipeline exists.** The project relies on local
  `scripts/test_sqlite.sh` runs. There is no `.github/workflows/`,
  no Makefile, no Dockerfile.

## Configuration writer

- **`doc3gpp config set` rewrites the entire TOML file via `tomli_w`**
  (`src/doc3gpp/settings/config_writer.py:write_toml`); comments, blank
  lines, and key ordering are not preserved. Keep the file under VCS
  if formatting matters — diffs will look like full rewrites on the
  first edit.
- **`doc3gpp config init` writes the canonical template (full defaults)
  at the bootstrap target** (`--target auto|project|user`; default
  `auto`); `--force` overwrites unconditionally. Refuses while
  `DOC3GPP_CONFIG` is set so the env pin cannot mask the new file.
  The previous `doc3gpp config set --init` is removed — migrate by
  running `doc3gpp config init` (with `--force` when overwriting) then
  `doc3gpp config set <key> <value>` to edit individual keys.
