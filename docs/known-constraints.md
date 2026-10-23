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
  layout changes. The four content tables (`tdoc_cr_annexes`, `tdoc_wid_contents`,
  `tdoc_ls_contents`, `tdoc_discussion_contents`) are created from the ORM metadata
  and start empty. The intermediate branch schema (`tdoc_documents`,
  `document_text`) was never deployed and has no migration; a local database
  created from it must be recreated.
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
  and a successful reparse creates the version-2 content rows.
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

## TDoc extraction and content

- **URL templates locked in for `R5s` (TTCN email CR) and `R5w`
  (TTCN workshop CR) branches** against offline fixtures. The `R5-`
  and `C6-` templates return `None` until exercised against the live
  site — treat `None` as "not yet supported", not as an error. See the
  inline docstring at
  `src/doc3gpp/scraping/tdoc_zip_source.py:_build_tdoc_zip_url`.
- **`python-docx` is an opt-in extra.** Without `doc3gpp[extract]`
  installed, the CLI prints a friendly install hint and exits 1.
- **Only CR, WID, LS, and discussion TDocs are parsed.**
  Every other type, including `other`, DRAFT, and BB, is never downloaded or stored. The type guard raises `TDocTypeUnsupportedError` before any download for
  an unsupported id. `--tdoc` reports it as skipped, and the raw read names the eligible types.
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
  and nested-ZIP members are never converted. A CR ZIP must have exactly
  one converted member with the CR cover; that main document alone
  produces cover/TTCN/body-change metadata. Other members are CR annexes stored as
  content in `tdoc_cr_annexes`. A TDoc whose only main-named member is a `.doc` fails
  with `main document for <id> is not .docx`, so its main cannot be converted.
  CR annexes and every family document are FTS5-searchable and semantically chunked.
  The CR main body is not full-text indexed; see the CR full-text constraint below.
- **TDoc ZIP resource caps are TOML-only.** `[tdoc_parse]`
  `max_documents_per_zip` defaults to `100` and
  `max_uncompressed_size_kb` defaults to `102400` aggregate supported
  member bytes; `0` disables either. Limit violations are skips, while
  zero/multiple main candidates and supported-member conversion errors
  fail the archive.
- **Markdown cache is a derived multi-member ZIP.**
  `cache/markdown/<cache_file>` contains one `.md` entry per stored
  document plus a versioned manifest. SQL content in the four content tables
  is authoritative for retrieval and FTS5 rebuilds, so cache purge or
  FIFO eviction does not delete SQL content or change completeness. The CR main
  exists only in this archive, so purging it makes raw CR main Markdown unavailable
  until the TDoc is re-downloaded.
  Legacy plain UTF-8, gzip, and single-member Markdown ZIPs remain
  readable as incomplete version-1 content only; they never prove a
  complete version-2 extraction.
- **A complete CR whose cache archive was purged is not re-selected by a normal `tdoc parse`,** because pending selection is SQL-only; recover it with `doc3gpp tdoc parse --tdoc <id> --force` or a `doc3gpp tdoc show --tdoc <id> --format raw` read, which re-runs extraction and rewrites the archive.
- **Semantic content chunks are capped.** Chunks for CR annexes and family
  documents are labelled per document.
  Chunks are re-derived from the stored Markdown. `semantic_search.max_content_chunks_per_tdoc`
  (TOML-only, default `16`) counts across a TDoc's content in order, so text past the cap
  is not embedded, but FTS5 still finds it. The CR main gets no content chunks. The
  TDoc-level metadata chunks (`max_chunks_per_tdoc`, default `8`) are unchanged.
- **CR main text is not full-text indexed.** A term that appears only in the CR main
  body does not find the TDoc. Its cover, change, and TTCN fields are still indexed,
  and CR annexes are indexed. This is the documented consequence of the content design
  (spec §8, decision 24) and is an accepted trade-off.
- **Semantic freshness ignores chunking settings.** The content-hash freshness check
  does not detect a change to `semantic_search.chunk_size` or `chunk_overlap`. After
  changing either, run `doc3gpp tdoc index --rebuild-embeddings`.
- **`--stale-only` does not select a metadata-only TDoc that is missing from the vector
  index and was uploaded before the last completed rebuild's snapshot day.** A rebuild
  stamps `last_indexed_uploaded_date` with the newest upload date it saw. `--stale-only`
  selects TDocs uploaded on or after that day, so an earlier-dated title-only TDoc that is
  synced later stays missing. A TDoc with no content documents has nothing for the content
  check to compare. Use `--rebuild-embeddings` to repair these rows.
- **Each `--stale-only` run re-embeds the TDocs uploaded on the snapshot day.** The cutoff
  is inclusive, so already-indexed TDocs uploaded on the newest upload day are re-embedded
  on every run, until a later upload date appears in `tdocs`. Their vectors are unchanged,
  so the cost is embedding requests only, and `IndexStatus.is_stale` ignores them. A TDoc
  with empty embed text is never indexed, so the same cutoff re-selects it on each run.
- A rebuild started before this change has no persisted snapshot, so its `--resume` takes the newest upload date seen at resume time until the next fresh `--rebuild-embeddings`.
- **A corrupt content blob is skipped by `tdoc index` rebuilds.** If a stored content
  row cannot be decoded, `upsert_for_tdoc` logs a warning and the rebuild loop
  continues. The TDoc's old FTS rows remain and its stored hash still matches, so
  `--stale-only` does not retry it; a full `doc3gpp tdoc index --rebuild` is needed.
  Content is written only through the strict repository, so corruption is not expected.
- **A failed annex keeps the extraction incomplete.** Each normal `tdoc parse` retries
  it and prints the warning again, so an annex that always fails is retried on every run.
- **Non-3GPP `--from-url` ids come from the whole URL.** The direct-mode id is the
  first TDoc-shaped match in the whole URL string, so a directory segment can trigger the
  TSG and meeting sync. The type-detection specification's step 1 reads the file name
  instead. This is an open mismatch for review; the current behaviour is documented under
  Direct-mode auto-sync in `docs/cli.md`.
- **CR raw reads re-run extraction.** `tdoc show --format raw --tdoc <id>` runs
  extraction first, so an incomplete TDoc is re-downloaded on each raw read when its
  ZIP is not cached. URL reads never extract.
- **Superseded ZIP URLs stay indexed.** Full-text and semantic indexes read content from
  every ZIP URL stored for a TDoc. When `tdocs.ftp_url` changes, the text from the old URL
  stays searchable until its rows are removed. A `tdoc index --rebuild` does not remove
  them, because rebuilds read every stored URL.
- **LIKE-based vector filters run after the knn window.** Meeting name and title,
  release, and spec filters are applied after the vector window, because the vector
  index rejects LIKE on metadata columns. Only `tdoc_id`, `tsg`, `meeting_id`, and the
  `since` and `until` date range are applied inside the window. Semantic-only search
  widens the window until enough TDocs match or the 4096-row cap is reached, so a
  very selective LIKE filter can still return fewer results. Hybrid search does not
  widen, so the same filter can return fewer results there.

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
