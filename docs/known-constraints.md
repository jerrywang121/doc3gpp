# Known Constraints

> Last reviewed: 2026-08-13

Open / known limitations that future work should respect. This file is
the single source of truth — `docs/architecture.md` §Out of scope (today)
and the per-feature notes below should stay in lock-step with this list.

When a constraint is lifted, **edit this file** as part of the same
change set so the docs stay honest.

## Schema

- **No general Alembic migration framework.** `Base.metadata.create_all`
  via `storage/db/migrate.py` is the schema bootstrap; `db_auto_migrate`
  is TOML-only and does not run migrations. The bootstrap does contain
  explicit, idempotent compatibility migrations: the legacy
  `tdoc_cr_details` rename, the TDoc extraction completeness columns, and
  recreation of the TDoc FTS5 virtual table when its indexed/provenance
  layout changes. `tdoc_documents` is created from the ORM metadata.
  Other ORM shape changes without a dedicated migration may still require
  a destructive `doc3gpp db reset`; do not reset a database to upgrade the
  multi-file TDoc extraction layout.
- **Orphan testcase tables on upgrade.** After the testcase-DB split,
  pre-existing main DB files keep `testcases` / `testcase_status` /
  `testcase_sources` tables as orphans (harmless — nothing reads
  them; `Base.metadata.create_all` will not drop them). Reclaim the
  space with `doc3gpp db reset --scope main` (destructive!) or leave
  them. The testcase corpus re-syncs from scratch via
  `testcase sync` into the new file.
- **`meeting sync`, `wi sync`, and `tsg seed` call `create_schema()`**
  for fresh-database ergonomics. Idempotent but blurs the `db init`
  boundary; `tdoc sync` and `tdoc parse` already dropped the call.
  See [`docs/conventions.md`](conventions.md) for the canonical list.
- **TDoc multi-file completeness is not inferred from old cover/cache rows.**
  Existing `tdoc_extracts` rows migrate to `extraction_version=1` and
  `document_count=0`; normal `tdoc parse` discovery treats them as pending,
  and a successful reparse creates the version-2 `tdoc_documents` collection.
  Reparse the original ZIP (using its cache copy when available); an old
  single-member Markdown artifact cannot reveal which supporting members were
  present. No whole-corpus reset or standalone repair command is required.
- **Older cache-key schemas are a separate compatibility break.** The
  historical `tdoc_extracts` migration from `zip_path` / `markdown_path` to
  indexed `cache_file` is not backfilled by the multi-file completeness
  migration. Installs that predate `cache_file` still need to follow the
  existing schema-upgrade guidance; orphaned legacy `zips/<tdoc_lower>` and
  `markdown/<sha256>` files can be reclaimed with cache purge or FIFO eviction.

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
- **`TDoc` types other than CR (LS, DRAFT, BB, …) are not handled.**
  The extractor's type guard raises `TDocTypeUnsupportedError` for
  non-CR ids; future expansion is a separate change.
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
- **Only `.docx` ZIP members are converted.** `.doc`, PDF, spreadsheet,
  and nested-ZIP converters are out of scope. A ZIP must have exactly
  one converted member with the CR cover; that main document alone
  produces cover/TTCN/body-change metadata. Supporting members are
  valid content without CR headers, but do not get per-document
  semantic embeddings.
- **TDoc ZIP resource caps are TOML-only.** `[tdoc_parse]`
  `max_documents_per_zip` defaults to `100` and
  `max_uncompressed_size_kb` defaults to `102400` aggregate supported
  member bytes; `0` disables either. Limit violations are skips, while
  zero/multiple main candidates and supported-member conversion errors
  fail the archive.
- **Markdown cache is a derived multi-member ZIP.**
  `cache/markdown/<cache_file>` contains one `.md` entry per stored
  document plus a versioned manifest. `tdoc_documents.markdown_text`
  is authoritative for retrieval and FTS5 rebuilds, so cache purge or
  FIFO eviction does not delete SQL content or change completeness.
  Legacy plain UTF-8, gzip, and single-member Markdown ZIPs remain
  readable as incomplete version-1 content only; they never prove a
  complete version-2 extraction.
- **Semantic search remains TDoc-level.** Supporting-document text is
  covered by FTS5 source rows and may flow through hybrid results, but
  the existing vector index is unchanged and does not promise semantic
  supporting-document matches.

## Test surface

- **`online` tests access live `3gpp.org` + FTP** and are flaky.
  Always run with `-rs` to surface skip reasons; do not gate CI on
  them. They live behind the `online` pytest marker so the default
  profile (`pytest -m "not online"`) skips them.
- **No CI pipeline exists.** The project relies on local
  `scripts/test_sqlite.sh` runs. There is no `.github/workflows/`,
  no Makefile, no Dockerfile.

## Recorded pre-existing baseline issue

- A pre-feature baseline run reported 2,666 passed, 1 skipped, and one
  failure in `tests/integration/test_auto_sync_sqlite.py::test_tdoc_list_auto_sync_triggers_tdoc_sync`.
  The test leaves the calendar module's imported `ScraperClient` unmocked
  and received a live HTTP 403 in that environment. A one-off in-memory
  mock-binding correction made the individual test pass without repository
  edits. This unrelated test was intentionally recorded, not repaired as
  part of the TDoc multi-file feature.

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
