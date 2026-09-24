# Architecture

> Last reviewed: 2026-08-13

The project is implemented as a layered Python package under `src/doc3gpp/`,
shipped both as a library (SDK) and a CLI. Each layer depends only on the
layer below it; cross-layer imports flow strictly downward.

Current scope:

- SQLite storage backend (sole backend).
- Calendar scraping from the 3GPP DynaReport meetings pages.
- TDoc list scraping from the 3GPP portal
  (`GenerateDocumentList.aspx?meetingId={meeting_id}`); auxiliary TDoc
  files still come from the per-meeting FTP folders.
- Work-item (WI) scraping from the per-TSG DynaReport WI pages.
- TDoc CR extraction pipeline (download zip → on-disk cache → python-docx
  render → markdown cache → cover-page parser → persist).
- Calendar / TDoc / WI / TDoc-CR persistence in SQLAlchemy.
- RAN5 testcase status snapshots (History zip → workbook → `testcases` /
  `testcase_status` / `testcase_sources` rows; `testcase sync` / `list`
  / `show`).
- Spec-document corpus (`spec doc parse` / `toc show` /
  `search query` / `search sem`): version-zip download → `.docx` →
  blocks → ordered files → per-version TOC + chunk rows in the
  separate specdata sqlite file, searchable via FTS5 + hybrid vector
  search.

## Layers

The seven layers sit between the CLI entry point and the database driver.
Each layer owns one concern; everything above depends only on the layer
immediately below it (with `services` reaching down into `storage` via
the `repository/` Protocols rather than touching the concrete ORM).

```
                  ┌──────────────────────────────────────┐
                  │            cli.py  (Typer)           │
                  └──────────────────┬───────────────────┘
                                     │
                  ┌──────────────────▼───────────────────┐
                  │      services/  (orchestration)      │
                  │    Protocol-typed repos in / out      │
                  └──────────────────┬───────────────────┘
                                     │
                  ┌──────────────────▼───────────────────┐
                  │   repository/  (abstract contracts)  │
                  └──────────────────┬───────────────────┘
                                     │
   ┌────────────┐ ┌─────────────────▼─┐ ┌──────────────────┐
   │ settings/  │ │    storage/        │ │   models/        │
   │ (config)   │ │   (ORM + repos +  │ │  (domain DTOs)   │
   │            │ │    engine)        │ │                  │
   └────────────┘ └─────────┬─────────┘ └──────────────────┘
                           │
                  ┌────────▼─────────┐
                  │    parsers/       │  ←─┐
                  │  (HTML/Excel →    │    │ shared inputs:
                  │   domain objects) │    │ bytes / str
                  └────────┬──────────┘    │
                           │               │
                  ┌────────▼──────────┐    │
                  │  scraping/        │ ───┘
                  │ (network I/O)     │
                  └───────────────────┘
```

Per-layer modules:

- `settings/` — schema and loader for environment-driven and TOML config;
  exposes `get_settings()` (cached) with `Settings` (root) +
  `SyncSettings` / `OutputSettings` / `OutputFieldsSettings` /
  `CacheSettings` / `TDocParseSettings` / `SpecDocSettings` (TOML
  `[spec_doc]` block: `max_zip_size_kb = 0`, `max_chunk_chars = 1500`,
  `chunk_overlap = null` → reuse `semantic_search.chunk_overlap`,
  `auto_index_on_parse` / `auto_embed_on_parse`, 6-entry `bm25_weights`)
  sub-models.
    - `src/doc3gpp/settings/schema.py`
    - `src/doc3gpp/settings/loader.py`
    - `src/doc3gpp/settings/config_source.py` (TOML discovery)
- `scraping/` — HTTP/FTP transport. Knows about URLs and bytes; never
  parses content.
    - `scraping/client.py` — `ScraperClient` (retry/backoff, UA, `httpx`)
    - `scraping/calendar_source.py` — DynaReport meetings HTML
    - `scraping/ftp_source.py` — FTP-directory listings for auxiliary TDoc files
    - `scraping/portal_source.py` — `GenerateDocumentList.aspx` TDoc-list XLSX
    - `scraping/wi_source.py` — DynaReport WI list HTML per TSG
    - `scraping/testcase_source.py` — RAN5 TTCN status History listing
      (`HISTORY_URL`) + status-zip download (network only; filename
      grammar parsed by `parse_status_filename`, latest picked by
      `select_latest` on `(year, week, revision)`)
    - `scraping/tdoc_zip_source.py` — TDoc zip URL builder + downloader
      (`R5s` TTCN + `R5w` Workshop branches)
    - `scraping/spec_doc_source.py` — spec-document transport (network
      only): `resolve_spec_doc_version` (numeric sort on
      `SpecVersion.version`, optional `release` / `version` pins) +
      `fetch_spec_doc_zip` (zip bytes for one `SpecVersion.ftp_url`)
    - `scraping/cache.py` — `TDocCache` (two-subtree on-disk cache for
      zip + markdown, size-based FIFO eviction)
- `parsers/` — `bytes|str` → domain objects. No network I/O.
    - `parsers/calendar_parser.py`, `parsers/html_parsers.py`,
      `parsers/normalizers.py` — meetings HTML → `Meeting`
    - `parsers/tdoc_parser.py`, `parsers/tdoc_file_parser.py` — TDoc
      list XLSX → `TDoc` / `TDocFile`
    - `parsers/wi_parser.py` — DynaReport HTML → `Wi`
    - `parsers/testcase_parser.py` — status workbook (`*.xlsx`,
      `read_only=True, data_only=True`) → `(list[TestCase],
      list[TestCaseStatus], list[str])`. Constants: `SHEET_TO_GROUP`
      (6 sheets → `5G` / `LTE` / `IMS` / `UTRA` / `POS` / `MCX`),
      `PATH_RANK` (single source of truth, reused by the repo sort and
       the CLI `path=gcf/ttcn` rendering), `STATUS_PAIRS` (`(gcf_idx, ttcn_idx,
      path)` per group), `FIXED_SPEC` (`5G` → `38.523-1`, `LTE` →
      `36.523-1`). `extract_workbook` picks the lexically-first
      `*.xlsx` member (zero → `TestcaseWorkbookNotFoundError`). Header
      row is row 1; each status pair's headers are validated
      (`_is_gcf_header` / `_is_ttcn_header`), invalid pairs are
       skipped with a warning. Rows with an empty TC are skipped; a TC
       with zero emitted pairs still yields its header. Identity is
       `(testcase_id, group)`, so the same TC id on several sheets
       yields one header row per group (each with its own group-scoped
       status rows) — the live workbook contains such duplicates, so
       the docs describe what the code does today (see
       `docs/3gpp-knowledge.md`).
    - `parsers/docx_converter.py` — `.docx` → markdown via the
      optional `python-docx` extra (raises
      `PythonDocxNotInstalledError` when missing). The legacy `.doc`
      binary format is rejected at the wrapper boundary because
      python-docx only supports the OOXML container. Also exposes
      `convert_document_to_blocks` (`.docx` → `HeadingBlock` /
      `ParagraphBlock` / `TableBlock` list) consumed by the spec-doc
      parse pipeline.
    - `parsers/spec_doc.py` — spec-zip parsing (no network):
      `list_spec_docx` (`.docx` members; `.doc` entries warn-skip),
      `order_spec_files` (front-matter TOC file first, then
      section-tuple sort, TOC tiebreak, unnumbered lexicographic
      tail), `extract_spec_toc` (headings → `SpecDocTocEntry` /
      `SpecDocTocFile` rows per `(spec_id, version)`).
    - `parsers/spec_doc_chunker.py` — pure block → `ChunkDraft` splitter:
      `chunk_blocks(blocks, chunk_size=512, chunk_overlap=24,
      max_chunk_chars=1500)` (break priority paragraph → sentence →
      table-row; table rows atomic; overlap prepends trailing tokens).
    - `parsers/cr_parser.py` — thin re-export shim around
      `parsers/cr/`, exposing `parse_cr_details(markdown) ->
      TDocCRParseResult(cover, ttcn)`. The actual implementations
      (`CRParserBase` / `CRParser` / `TTCNCRParser`,
      `cover_page.py`, `header.py`, `helpers.py`,
      `ttcn_sections.py`) live in the `parsers/cr/` subpackage; the
      shim delegates to `build_default_registry()` so the public
      surface stays a single import.
- `models/` — pure domain dataclasses (`@dataclass(slots=True)`),
  passed between layers; never leak ORM attributes.
    - `models/meeting.py`, `models/tdoc.py`, `models/tsg.py`,
      `models/wi.py`, `models/tdoc_file.py`, `models/tdoc_cr.py`
      (`TDocCRDetails` slim cover-page dataclass, `TDocCRTTCNDetails`
      sidecar, `TDocCRParseResult` parser bundle, `TDocExtractMeta`
      cache-pointer sidecar, `DirectParseResult` direct-mode outcome)
    - `models/testcase.py` — `TestCase` header, `TestCaseStatus`
      `(testcase_id, group, path)` triple, `TestCaseWithStatuses` list-view DTO
      (`statuses: list[TestCaseStatus]`), `TestCaseDetail` detail-view
      DTO (`statuses: list[TestCaseStatus]`), `TestCaseSource` sync
      ledger, plus `TestcaseSourceNotFoundError` (`LookupError`) and
      `TestcaseWorkbookNotFoundError` (`ValueError`)
    - `models/spec_doc.py` — spec-document DTOs (`SpecDocSource` sync
      ledger, `SpecDocToc` + `SpecDocTocEntry` / `SpecDocTocFile`,
      `ChunkDraft` chunker output vs `SpecDocChunk` persisted with
      `chunk_id = {spec_id}@{version}#{chunk_index}`, chunk-level
      `SpecDocSearchFilters` / `SpecDocHit` / `SpecDocSemanticHit`,
      `SpecDocBatchResult(successes/skipped/failures)`) plus the
      `SpecDocError` hierarchy (`SpecDocUnknownSpecError`,
      `SpecDocUnknownVersionError`, `SpecDocNoDocxError`,
      `SpecDocTooLargeError`)
- `repository/` — abstract `Protocol` contracts used by services.
    - `repository/protocols.py` — `MeetingRepository`,
      `TDocRepository` (+ `get_by_id`), `TsgRepository`,
      `WiRepository`, `TDocFileRepository`,
      `TDocCrDetailRepository` (slim cover-page repo + the
      `tdoc_extracts` sidecar, written through a separate
      `upsert_extract_meta` method),
      `TDocCrTTCNDetailRepository` (TTCN sidecar),
      `TestCaseRepository` (`upsert_many` / `replace_statuses` (delete
      + insert per id) / `list` with text filters + any-path
      `status`/`gcf_status` `EXISTS` / `get` / `list_statuses`
      (`PATH_RANK`-sorted) / `get_source` / `record_download` /
      `record_parsed`),
      `SpecDocRepository` (`get_source` / `record_download` /
      `record_parsed` / `get_toc` / `upsert_toc` / `replace_chunks` /
      `list_chunks`),
      `SpecDocSearchRepository` (`upsert_for_version` /
      `remove_for_version` / `search` / `rebuild_batch` /
      `count_versions_to_index` / resume-cursor + `status`),
      `SpecDocVectorRepository` (`upsert_for_version` /
      `remove_for_version` / `knn`)
- `services/` — orchestration. Constructed via `services/factory.py`
  (`build_*` helpers); the CLI never imports a concrete SQL repository
  directly.
    - `services/meetings_service.py`, `services/tdoc_service.py`,
      `services/tsg_service.py`, `services/wi_service.py`
    - `services/tdoc_file_service.py` — auxiliary FTP files
    - `services/tdoc_sync_coordinator.py` — cross-service orchestration
      for `tdoc sync`
    - `services/tdoc_cr_service.py` — end-to-end CR extraction pipeline
      (accepts `cr_ttcn_repository` in its constructor; fans the
      parser's `TDocCRParseResult` out across the cover-page repo,
      the TTCN sidecar repo, and the extract-metadata repo)
    - `services/factory.py` — `build_meeting_service`,
      `build_tdoc_service`, `build_tdoc_file_service`,
      `build_tdoc_sync_coordinator`, `build_tdoc_cr_service`,
      `build_tsg_service`, `build_wi_service`, `build_testcase_service`
      (`TestCaseService(SQLAlchemyTestCaseRepository())`),
      `build_tdoc_repository`, `build_tdoc_cr_repository`,
      `build_tdoc_cr_ttcn_repository`,
      `build_spec_doc_service` (main-DB `SQLAlchemySpecRepository` +
      specdata `SQLAlchemySpecDocRepository` + FTS5/vector hooks +
      one shared embedder), `build_spec_doc_search_service` (`None`
      when `[search].enabled` is false or FTS5 is unavailable),
      `build_spec_doc_semantic_service` (`None` when the semantic
      stack is disabled, FTS5 is unavailable, or no embedder is
      configured)
    - `services/testcase_service.py` — `TestCaseService.sync(*,
      force=False, on_progress=None)` (latest-History-zip →
      `upsert_many` + per-`(id, group)` `replace_statuses`, with the
      file-identity skip rule on `testcase_sources.parsed_at`),
      `list_recent` (headers + `{path: ttcn_status}` dicts),
      `get` (`TestCaseDetail | None`, optional group) + `get_all`
      (every stored group for an id)
    - `services/spec_doc_service.py` — `SpecDocService.fetch`
      (resolve via `resolve_spec_doc_version` → zip-cache hit or
      `fetch_spec_doc_zip` → `record_download`) / `parse`
      (fetch-if-missing + convert + order + TOC + per-file
      `chunk_blocks` → `upsert_toc` + `replace_chunks` + markdown
      cache + `record_parsed` + best-effort auto-index/auto-embed) /
      `parse_many` (immutable `(spec_id, version)` skip → per-spec
      `parse`; oversized → `skipped`, unknown/no-docx/other →
      `failures`; never aborts; optional `on_progress`) / `get_toc`
      (stored rows only; miss → `SpecDocUnknownVersionError`).
      Cache layout: `{cache.dir}/specs/zips/<spec_id>/<version>.zip`
      + `{cache.dir}/specs/markdown/<spec_id>/<version>/<file_order>-<stem>.md`.
    - `services/spec_doc_search_service.py` — `SpecDocSearchService`
      (`upsert_for_version` / `remove_for_version` best-effort,
      `search(query, filters)` raw-text FTS5, `rebuild` generator with
      `batch_size` / `resume` / `stale_only` via `spec_doc_search_meta`,
      `status`).
    - `services/spec_doc_semantic_service.py` —
      `SpecDocSemanticService` (chunk-level `rrf_merge`, `k=60`;
      `search` always embeds, FTS5 opt-in via `fts5_query`;
      `index_for_version` / `remove_for_version`).
- `storage/` — SQLAlchemy ORM models, engine / session factory,
  backend-specific options, concrete Protocol implementations.
    - `storage/db/models.py` — ORM classes (including
      `TDocCrDetailOrm` slim cover-page, `TDocCrTtcnDetailOrm`
      TTCN sidecar, `TDocExtractOrm` cache-pointer sidecar)
    - `storage/compression.py` — shared gzip JSON helpers
      (`compress_json` / `decompress_json`) used by the cover-page
      repo and the TTCN sidecar repo for any binary JSON column
      (the sidecar's `required_changes` blob today; tolerant
      decoding covers future binary detail columns)
    - `storage/db/session.py` — `get_engine`, `get_session_factory`
      (cached, main DB), `get_testcase_engine`,
      `get_testcase_session_factory`,
      `resolve_testcase_database_url` (cached, testcase DB), and
      `get_specdata_engine`, `get_specdata_session_factory`,
      `resolve_specdata_database_url` (cached, specdata DB — sibling
      `<main-stem>_specdata.db` unless `specdata_database_url` is set)
    - `storage/db/base.py` — declarative `Base`
    - `storage/db/testcase_base.py` — declarative `TestCaseBase`
      (owns the three testcase ORMs: `testcases`, `testcase_status`,
      `testcase_sources`)
    - `storage/db/specdata_base.py` — declarative `SpecDataBase`
      (owns the three spec-doc ORMs in `storage/db/models.py`:
      `spec_doc_sources`, `spec_doc_tocs`, `spec_doc_chunks`)
    - `storage/db/migrate.py` — `create_schema(scope)` (calls
      `Base.metadata.create_all` for `"main"`,
      `TestCaseBase.metadata.create_all` for `"testcase"`,
      `SpecDataBase.metadata.create_all` + `_create_specdata_search_schema`
      (`spec_doc_search` + `spec_doc_search_meta`) +
      `_create_specdata_vector_schema` (`vec_spec_doc_embeddings` +
      `vec_spec_doc_meta`) for `"specdata"`, all three for `"all"`)
    - `storage/db/migrations/` — placeholder for future Alembic
    - `storage/backends/sqlite.py` — engine kwargs
    - `storage/repositories/{meeting,tdoc,tsg,wi,tdoc_file,tdoc_cr}_sql.py`
      and `storage/repositories/tdoc_cr_ttcn_sql.py` — concrete
      `SQLAlchemy*Repository` classes (the cover-page repo also
      owns `tdoc_extracts` writes via `upsert_extract_meta`)
    - `storage/repositories/testcase_sql.py` —
      `SQLAlchemyTestCaseRepository` (owns `testcases` +
      `testcase_status` + `testcase_sources`; text cols via
      `apply_text_filter`, `group` exact upper-cased, `status` /
      `gcf_status` via any-path `EXISTS`, `list_statuses` Python-sorted
      by `PATH_RANK`). Bound to the testcase session factory
      (`get_testcase_session_factory()`), not the main one.
    - `storage/repositories/spec_doc_sql.py` —
      `SQLAlchemySpecDocRepository` (owns `spec_doc_sources` +
      `spec_doc_tocs` (gzip-compressed TOC JSON) + `spec_doc_chunks`;
      `record_download` / `record_parsed` ledger, `upsert_toc`,
      `replace_chunks` (delete-then-insert per pair, `chunk_id`
      assigned `{spec_id}@{version}#{index}`), `list_chunks`
      (rich-filtered)). Bound to the specdata session factory
      (`get_specdata_session_factory()`).
    - `storage/repositories/spec_doc_search_sql.py` —
      `SQLAlchemySpecDocSearchRepository` (owns the 6-column
      `spec_doc_search` FTS5 table + `spec_doc_search_meta`; FTS5 rows
      carry the `normalize_query` form; `section_title` stores
      `section_no + " " + title`; ranking via `bm25(...)` with
      `SpecDocSettings.bm25_weights`, one `snippet(...)` per
      `weight > 0` column, surfaced in `previews` only on a match).
    - `storage/repositories/spec_doc_vector_sql.py` —
      `SQLAlchemySpecDocVectorRepository` (owns
      `vec_spec_doc_embeddings` + `vec_spec_doc_meta`, gated on
      sqlite + sqlite-vec; `upsert_for_version` /
      `remove_for_version` / `knn` with exact `=` on `spec_id` /
      `version` and plain `LIKE` on `release` / `section` via a
      `spec_doc_chunks` JOIN).

Data flow by database: testcase traffic →
`SQLAlchemyTestCaseRepository` → testcase factory
(`get_testcase_session_factory()` → `get_testcase_engine()` →
`testcase_database_url`); spec-document traffic →
specdata factory (`get_specdata_session_factory()` →
`get_specdata_engine()` → `specdata_database_url`); everything else →
main factory (`get_session_factory()` → `get_engine()` →
`database_url`).

| `services/search_service.py` | orchestration; injected with a `SearchIndexRepository` impl + `EmbeddingReranker` via `services/factory.build_search_service` |
| `storage/db/fts5_query.py` | `normalize_query` index-time pre-processor (T3); applies TDoc-ID base+full duplication + spec-id `dot→underscore` rejoin |
| `storage/repositories/search_sql.py` | SQL impl of `SearchIndexRepository`; FTS5 DDL/DML; gzip decompression at index time |

## Runtime Data Flow

The CLI composes a service via the factory, the service drives the
scrapers + parsers + repos through the Protocols, and the repos own the
SQLAlchemy session. There are six primary end-to-end flows; the
"meeting-based TDoc sync" flow is itself composed of two sub-flows,
and the TDoc CR extraction is the deepest.

### Testcase sync

1. `doc3gpp testcase sync [--force]` calls
   `TestCaseService.sync(force=force, on_progress=...)`.
2. `list_history_files()` fetches the upstream `History/` folder HTML
   and returns every anchor whose URL-decoded basename matches the
   status-file grammar; `select_latest` picks the max `(year, week,
   revision)` tuple.
3. The service reads `TestCaseRepository.get_source(filename)`. When
   the row exists and already carries `parsed_at` (and `--force` is
   absent) the run returns `skipped` — file-identity skip rule, no
   interval, no bootstrap seed, no network beyond the listing.
4. Otherwise `fetch_testcase_zip` downloads the zip once,
   `record_download` writes the `testcase_sources` row BEFORE parsing,
   `extract_workbook` pulls the lexically-first `*.xlsx` member, and
    `parse_testcase_workbook` returns `(cases, statuses, notes)`.
5. `upsert_many(cases)` writes the headers, each `(id, group)` pair's
   statuses are replaced via `replace_statuses` (delete-then-insert
   scoped to the pair, so stale paths disappear), and
   `record_parsed` stamps `parsed_at` + counts.
6. `doc3gpp testcase list [filters]` reads cached headers via
   `TestCaseRepository.list(...)` (rich filter grammar on text cols;
   `group` exact upper-cased; `--status` / `--gcf-status` any-path
   `EXISTS` on `ttcn_status` / `gcf_ptcrb`) then nests one
   status-row list (`{path, gcf_ptcrb, ttcn_status}` objects, `null`
   preserved) per header via `list_statuses`. Pagination via `--limit` (default 50) /
   `--offset` (default 0); columns from
   `settings.output.fields.testcase` (default `testcase_id, title,
   spec, group, release, statuses`), overridable with `--fields`
   (`all` = all 9 list fields).
7. `doc3gpp testcase show --testcase <id> [--group <g>]` renders one
   id across every stored group (or one `(id, group)` row with
   `--group`) via `TestCaseService.get_all` / `get` plus scoped
   `list_statuses` (`PATH_RANK`-sorted); a miss raises
    `typer.BadParameter(f"Testcase {id!r} not found")`. JSON always
    emits an array of flat per-`(id, group)` objects with nested
    `statuses` (one element when a single group matches); status
    rows carry `{path, gcf_ptcrb, ttcn_status}` with no `group`.

### Meetings sync\n\n1. `doc3gpp meeting sync --tsg <short>` validates `<short>` against\n   the `tsgs` table (auto-seeded if empty).\n2. `MeetingService.sync` checks `tsgs.meeting_last_sync` against\n   `Settings.sync.meeting_sync_interval` (default `24h`) and skips\n   the upstream fetch when the last sync is still fresh. `--force`\n   bypasses this check.\n3. On a non-skipped run: `fetch_calendar` (DynaReport HTML) →\n   `parse_3gpp_calendar` (HTML → `Meeting` list). Every parsed\n   `Meeting` is then stamped with `Meeting.tsg = <short>` (canonicalised\n   to upper case) before being handed to\n   `SQLAlchemyMeetingRepository.upsert_many`. The FK constraint\n   requires the parent row to exist in `tsgs`, so the auto-seed in\n   step 1 is a hard prerequisite.\n4. `SQLAlchemyMeetingRepository.upsert_many` writes the rows; a final\n   `delete_with_end_before(cutoff)` pass trims out-of-window rows.\n5. `doc3gpp meeting list --tsg <pattern>` is a SQL ``LIKE`` lookup on\n     the indexed `meetings.tsg` column (case-insensitive on input). Rows\n     without an owning TSG are excluded.

### TDoc list sync (per meeting)

1. `doc3gpp tdoc sync --meeting-id <id>` (or `--meeting <name>`)
   resolves the meeting and reads its stored `meeting_id` and `ftp_url`.
2. `TDocSyncCoordinator.sync_for_meeting_id` applies two skip rules
   in order, **but only when the meeting has been synced before**
   (`meetings.tdoc_list_last_sync IS NOT NULL`): closed window
   (`meetings.end_date` older than
   `Settings.sync.tdoc_list_closed_window`, default `90d`) and recent
   local sync (`meetings.tdoc_list_last_sync` newer than
   `Settings.sync.tdoc_list_sync_interval`, default `30m`). A meeting
   that has never been synced is allowed to fetch even when its
   `end_date` is older than the closed window — the rule exists to
   avoid re-fetching meetings whose TDocs we already have, not to gate
   the first sync. `--force` bypasses both rules.
3. On a non-skipped run, the coordinator orchestrates:
      - `TDocService.sync_tdoc_list` →
        `fetch_tdocs_from_portal` →
        `read_tdoc_sheet` (XLSX → `TDoc` list) →
        `SQLAlchemyTDocRepository.upsert_many`.
      - `TDocFileService.sync_from_meeting_ftp` uses the freshly-persisted
        TDoc IDs as the prefix list to recognise attachments under
        `Inbox/`, `Docs/`, `Tdocs/`, `Review/`.
4. `SQLAlchemyTDocFileRepository.upsert_many` persists revision / review
   / support files keyed by the unique `ftp_url`.

### TDoc list sync (bulk / no selector)

1. `doc3gpp tdoc sync` (no `--meeting-id` and no `--meeting`) calls
   `TDocSyncCoordinator.sync_all_tracked_meetings`.
2. The coordinator reads the distinct non-null `meeting_id` values from
   the `tdocs` table via
   `SQLAlchemyTDocRepository.list_distinct_meeting_ids` (sorted ascending,
   orphaned TDocs excluded).
3. For each meeting ID, it resolves the record via
   `MeetingService.get_by_id` and runs the same per-meeting sync path as
   the single-meeting flow above (closed window and sync interval checks
   apply individually). `--force` bypasses both checks for every meeting
   in the run.
4. A single meeting failure (`MeetingNotFoundError`) is recorded in
   `BulkSyncOutcome.failures` and does not abort the sweep; iteration
   continues so a partial sweep still completes.
5. The CLI prints a single summary block (no per-meeting lines):
   `TDoc bulk sync: N meeting(s) processed / Synced / Skipped / Failed`
   plus the per-failure detail. Exit code is `1` only when every meeting
   failed; otherwise `0`.

### TDoc CR extraction

1. `doc3gpp tdoc parse` is filter-driven. At least one filter must be
   supplied (`--tdoc` as a LIKE pattern, `--meeting-id`, `--meeting`, or
   any text/date filter); the CLI validates `--meeting-id` when present,
   applies `type == "CR"` by default when no explicit `--type` is
   supplied, and in normal mode the SQL query excludes rows already
   present in `tdoc_cr_cover_page` before applying the batch cap, so the
   preview and confirmation list only pending TDocs. With `--force`, the
   exclusion is disabled and every match (including already-parsed rows)
   becomes a candidate. If the pending set is empty, the CLI prints
   `Nothing to extract — every match is already parsed.` and exits `0`
   (successful no-op).
2. `TDocCrService.extract(tdoc_id, *, force=False)`:
    - Pre-resolves the candidate download URL(s) via
      `resolve_download_url(tdoc_id, build_ftp_url(tdocs.ftp_url))`
      (combining the stored `tdocs.ftp_url` rebuilt to an absolute URL
      via `build_ftp_url`, and the template URL), then probes
      `TDocCrRepository.get_by_url` (normalised via `normalize_ftp_path`)
      per candidate. A hit short-circuits with
      `ExtractResult.from_cache = True` and skips the network.
    - Else, `download_tdoc_zip` first checks the on-disk zip cache via
      the `ftp_url`-derived cache key (regardless of `force`) and
      returns the cached path on a hit. On a miss, it resolves the
      TDoc id to its 3GPP URL via the template table (`R5s` → TTCN
      email CR, `R5w` → workshop CR), hits `ScraperClient.get_bytes`,
      and stages the zip in `TDocCache.put_bytes(key, payload, "zips")`.
      The function tries the stored `tdocs.ftp_url` (rebuilt to an
      absolute URL) first, falling back to the template on a terminal
      HTTP error.
    - `extract_docx_from_zip` returns `(filename, docx_bytes)`.
    - The markdown for that exact `docx_bytes` is looked up by the
      shared `cache_file` (URL-derived via
      `scraping.cache_keys.derive_cache_file`) in
      `TDocCache.get_bytes(cache_file, "markdown")`; on miss,
      `convert_document_to_markdown` runs (raises
      `PythonDocxNotInstalledError` if `python-docx` is not installed)
      and the result is written to `markdown/<cache_file>` as a **real
      ZIP archive** (single entry named `<docx stem>.md`,
      `zipfile.ZIP_DEFLATED`) — so the `.zip` extension matches a
      format that `unzip` / 7z / WinZip understand directly. The
      writer is `_wrap_markdown_zip`; the reader
      (`_decompress_markdown`) magic-byte-sniffs the on-disk bytes
      (`PK\x03\x04` for the new format, `\x1f\x8b` for the legacy
      gzip blob, plain UTF-8 for the pre-gzip era) so previously
      written cache files still decode transparently. The same gzip
      JSON convention continues to apply to the SQL-side
      `tdoc_cr_ttcn_details.required_changes` blob via the shared
      helpers in `storage/compression.py` (`compress_json` /
      `decompress_json`) — same `compresslevel=9`, same tolerant
      fallback (`None` / empty / gzip / JSON / Unicode errors all
      resolve to `None` plus a warning, and legacy uncompressed
      blobs decode transparently).
    - `parse_cr_details(markdown, tdoc_id=...)` returns a typed
      `TDocCRParseResult(cover, ttcn)` — the slim cover-page fields
      bundled with an optional `TDocCRTTCNDetails` sidecar (populated
      only when `tdoc_id` matches `R5s\d{6}` and the parser ran the
      TTCN overview + corrections sub-parsers; non-TTCN CRs get
      `ttcn=None`). The sidecar is built with the derived
      `changed_functions` aggregate via
      `parsers/cr/ttcn_functions.py::extract_changed_functions` at the
      `TDocCRTTCNDetails(...)` construction site in
      `parsers/cr/cr_parsers.py:161`, so every successful TTCN
      parse persists the sorted + deduped `<module>.<function>`
      list alongside `required_changes`. Partial-extraction markers
      apply: when only the module basename is recoverable the entry is
      recorded as `'<module>.'` (trailing-dot sentinel); when only the
      function name is recoverable it is recorded as `'.<function>'`
      (leading-dot sentinel); when neither is recoverable the
      correction is dropped.
    - The service fans the result out across THREE independent
      upserts in `TDocCrService.extract_many` /
      `TDocCrService.extract_from_url`: the slim cover-page row in
      `tdoc_cr_cover_page` (`cr_repo.upsert(cover)`), the optional
      TTCN sidecar in `tdoc_cr_ttcn_details`
      (`cr_ttcn_repo.upsert(ttcn)` — only when `ttcn is not None`),
      and the cache metadata row in `tdoc_extracts`
      (`cr_repo.upsert_extract_meta(extract_meta)`). All three are
      keyed by the relative `ftp_url`; multiple extracts at distinct
      URLs for the same `tdoc_id` write distinct rows, one per
      revision. The fan-out replaces the previous
      `TDocCrRepository.upsert(details, extract_meta)` two-table
      transaction; partial failure is now possible across the three
      tables, but the unbuffered HTTP fetch we already trust keeps
      that window negligible.
    - Writes a `tdoc_cr_change_details` row when the parser detects
      `<ins>`/`<del>` revision marks in the body (non-TTCN CRs only).
    - Returns `ExtractResult(details, extract_meta, from_cache=False)`.
  3. `doc3gpp tdoc show --tdoc <id>` resolves the parent `tdoc` row
    via `TDocRepository.get_by_id` (PK lookup on `tdocs.tdoc_id`),
    then performs THREE URL-keyed reads against the immutable
    `tdoc.ftp_url`:
    1. `SQLAlchemyTDocCrRepository.get_by_url(tdoc.ftp_url)` — the
       slim cover-page row from `tdoc_cr_cover_page`.
    2. `SQLAlchemyTDocCrRepository.get_extract_meta_by_url(tdoc.ftp_url)`
       — the cache metadata row from `tdoc_extracts`. The
       `extracted_at` display value is sourced from this row
       (both `tdoc_cr_cover_page` and `tdoc_cr_ttcn_details` no longer
       carry their own timestamps after the slimming).
    3. `SQLAlchemyTDocCrTtcnRepository.get_by_url(tdoc.ftp_url)` —
       the TTCN sidecar from `tdoc_cr_ttcn_details`, gated on
       `is_ttcn_tdoc(tdoc.tdoc_id)` so non-TTCN CRs never hit the
       sidecar table.

    The bundled `TDocShowRecord(tdoc, cover, ttcn, extracted_at)`
    is rendered by `table` / `json` / `markdown` to three separate
    sections: `cover`, the optional `ttcn` block, and the standalone
    `extracted_at` line. Optional keys are **omitted** (not emitted
    as `null`) in the JSON payload when the corresponding row is
    absent. The legacy `details` / `parser_version` fields no longer
    appear in any output. The `raw` format delegates to
    `TDocCrService.extract()` and writes the converted `.docx`
    markdown (DB-cache short-circuit, otherwise download + render +
    persist).

### Spec sync (list + detail)

`spec sync --spec-id <id>` and the web spec detail page's Sync button
use `SpecService.sync_spec` — they recover the spec's TSG from the
stored row and fetch only that spec's detail page + versions (no list
page). With no `--tsg` and no `--spec-id`, the fallback iterates the
distinct TSGs of the `specs` table (via `SpecService.list_distinct_tsgs`)
and syncs each through the `--tsg` path below.

1. `doc3gpp spec sync --tsg <short>` validates `<short>` against
   the `tsgs` table (auto-seeded if empty) and resolves the TSG via
   `TsgService`.
2. The list page is fetched unconditionally on a `--tsg` sweep (no
   TSG-level skip rule). The **per-spec** skip is enforced per
   worker: each `_sync_one_spec` short-circuits the upstream
   fetch for any spec whose own `specs.last_synced_at` is within
   `Settings.sync.spec_sync_interval` (default `24h`). `--force`
   bypasses the per-spec check.
3. On a non-skipped run: `fetch_spec_list(canonical)` produces the
   DynaReport list HTML, `parse_spec_list(html, canonical)` parses
   the spec table into `list[Spec]` (one header per `<tr>`).
4. The service then fans out across the per-spec detail pages in a
   `ThreadPoolExecutor` capped at `min(32, cpu+4)` workers. Each
   worker:
   - Resolves the per-spec URL via `build_spec_detail_url(slug)`
     (`slug = spec_id.replace(".", "")`) and calls `fetch_spec_detail`.
   - Parses the detail page into `(header, list[SpecVersion])` via
     `parse_spec_detail`.
   - For each version, runs the optional follow-ups gated in
     `SpecService._maybe_fetch_etsi_pdf` (only when `wki_id` is set
     and `pdf_url` is empty — fetches the ETSI deliverable HTML and
     extracts the "download as PDF" URL) and
     `SpecService._maybe_fetch_crs` (only when the upload date is
     within the last 90 days **or** the cached `crs` is empty —
     fetches the per-version CR list HTML and extracts the
     comma-joined TDoc ids). Both gates are bypassed by the
     `--per-version-details` flag (default `False`); when set, every
     version is re-fetched on every run while existing cached
     `pdf_url` / `crs` values on populated rows are preserved.
   - Upserts the header row via `SQLAlchemySpecRepository.upsert` and
     every version row via `SQLAlchemySpecRepository.upsert_versions`.
   - Per-spec ETSI / CR failures log a warning and the sweep
     continues — one bad spec must not abort the whole sync.
5. On success, each worker's `_sync_one_spec` stamps the spec's
   own `specs.last_synced_at` so the next sweep can short-circuit
   that spec's re-fetch within the interval (per-spec throttle;
   no TSG-level gate).
6. `doc3gpp spec list [filters]` reads cached header rows via
   `SpecRepository.list(...)` (rich filter grammar via the same
   `_apply_text_filter` / `_apply_date_filter` helpers used by the
   other list commands). No network traffic.
7. `doc3gpp spec show <spec-id>` resolves the header via
   `SpecRepository.get(spec_id)`; a miss raises `SpecNotFoundError`
   (rendered as `typer.BadParameter` pointing at
   `doc3gpp spec sync --tsg <tsg>`). On hit, the version rows come
   from `SpecRepository.list_versions(spec_id)` and the CLI
   renders header + versions in the requested format (table,
   JSON, or markdown). The JSON payload splits the data into
   `{"spec": {...}, "versions": [{...}]}` so downstream consumers
   don't need to scan a flat list.

### Spec-document corpus (parse → TOC → search)

1. `doc3gpp spec doc parse --spec <id>... [--release R] [--version V]
   [--force]` calls `SpecDocService.parse_many`. The version resolves via
   `resolve_spec_doc_version` — numeric sort on `SpecVersion.version`
   (segment-wise ints, non-numeric → 0), optional `release` / `version`
   pins, newest wins; a miss raises `SpecDocUnknownSpecError` (no
   `spec_versions` rows — run `spec sync --spec-id` first) or
   `SpecDocUnknownVersionError`. `parse_many` calls `parse`, which fetches
   the ZIP when absent via internal `fetch_spec_doc_zip` or uses the cache at
   `{cache.dir}/specs/zips/<spec_id>/<version>.zip`, records the download,
   and checks the zip size against `[spec_doc] max_zip_size_kb` (default `0`
   = unlimited). `--force` makes the internal fetch re-download before
   parsing, including when the source row already has `parsed_at`.
   Each spec short-circuits
   into the `skipped` bucket when its `(spec_id, version)` source row
   already carries `parsed_at` (immutable ledger; `--force` re-parses);
   `parse` itself short-circuits the same way. `parse` =
   fetch-if-missing + `list_spec_docx` (empty zip →
   `SpecDocNoDocxError` failure; `.doc` entries warn-skip) +
   `convert_document_to_blocks` + `order_spec_files` + `extract_spec_toc`
   → `upsert_toc` + per-file `chunk_blocks` → `replace_chunks` +
   per-file markdown cache +
   `{cache.dir}/specs/markdown/<spec_id>/<version>/<file_order>-<stem>.md`
   + `record_parsed` + best-effort auto-index (`upsert_for_version`,
   gated on `auto_index_on_parse`) / auto-embed (`index_for_version`,
   gated on `auto_embed_on_parse`). Oversized zips land in `skipped`
   (`SpecDocTooLargeError`); unknown spec/version and empty zips land in
   `failures`; one spec never aborts the batch. The CLI prints
   `ok` / `skipped <reason>` / `failed <reason>` (stderr) bucket lines
   only — its `--format/--output/--compact` flags are accepted but
   ignored.
2. `doc3gpp spec doc toc show --spec <id> --version <v>` calls
   `SpecDocService.get_toc` (stored rows only, no network; miss →
   `SpecDocUnknownVersionError` rendered as `BadParameter`).
3. `doc3gpp spec doc search query "QUERY" [filters]` calls
   `SpecDocSearchService.search(query, filters)` → repo builds the
   `MATCH` internally via `SearchQueryBuilder` + pushes the rich
   filters down + ranks with `bm25(spec_doc_search, weights)` over
   `(text, section_title, table_title, spec_id, version, release)`
   (`[spec_doc] bm25_weights`, default `(5.0, 5.0, 5.0, 1.0, 1.0,
   1.0)`) + one `snippet(...)` per `weight > 0` column (surfaced in
   `previews` only on a match) → `list[SpecDocHit]` (chunk-level).
   The repo also exposes `upsert_for_version` / `remove_for_version` /
   `rebuild` (`batch_size` / `resume` / `stale_only` via
   `spec_doc_search_meta`) / `status`, but there is no
   `spec doc search index` CLI today — auto-index on parse is the only
   writer path from the CLI (the search-corrupt hint names the index
   command aspirationally).
4. `doc3gpp spec doc search sem QUERY [--fts5-query Q]
   [--fts5-weight 0.5] [filters]` calls
   `SpecDocSemanticService.search` → the positional `QUERY` is always
   embedded (vector path); the opt-in FTS5 side feeds `fts5_query`
   verbatim (no `SearchQueryBuilder` preprocessing). With
   `--fts5-query` both sides fan out to `limit * fanout_multiplier`
   and fuse via chunk-level `rrf_merge` (`k=60`, vector weight
   `1 - fts5_weight`); without it pure vector KNN returns dressed as
   `SpecDocSemanticHit` (`rank_fts5=None`, `hit=None` for vector-only
   chunks). Vector-side `spec_id`/`version` are exact `=`,
   `release`/`section` plain `LIKE` (no rich grammar — pass plain
   values for exact agreement). Requires
   `[semantic_search].embedding_base_url`.

### Cache + CLI

- `doc3gpp cache status` → `TDocCache.status()` (file count, total
  bytes, limit, per-subdir breakdown; non-mutating).
- `doc3gpp cache purge [--yes] [--scope {markdown,zips,all}]`
  (default scope: `markdown` — only the rendered sidecars; gated by
  `CacheSettings.purge_confirm`, configurable only via TOML since
  `DOC3GPP_CACHE__PURGE_CONFIRM` is outside the env-var allowlist)
  → `TDocCache.purge_subdir(scope)` for the scoped case, or
  `TDocCache.purge()` for `--scope all`.
- `doc3gpp tdoc search query "QUERY" [filters]` → `SearchService.search(query,
  filters)` → `repo.search` (FTS5 MATCH + filters + bm25) →
  `EmbeddingReranker.rerank` (`PassthroughReranker` for v1) →
  `list[SearchHit]` → CLI formatter. The ranking stage uses
  `bm25(tdoc_search, ...)` with the configurable column-weight
  vector in `Settings.search.bm25_weights` (see the `tdoc_search`
  schema below for the column order); the same `bm25_weights`
  vector drives snippet selection — one `snippet(...)` per
  `weight > 0` column, and the result surfaces in the hit's
  `previews` map only when the snippet contains a match. Fires
  the stale-index hint on the side (one-shot, gated on `--quiet`).
- `doc3gpp tdoc search index --rebuild` → `SearchService.rebuild(...)`
  generator → `repo.rebuild_batch(...)` per batch → per-row
  `repo.upsert(tdoc_id)` → updates `tdoc_search_meta` cursor.
  Resumable via `--resume`; cheap incremental via `--stale-only`.
- `doc3gpp tdoc search sem QUERY [filters]` →
  `SemanticSearchService.search` → original `QUERY` embedding (vector
  path, always on) → opt-in FTS5 path (the explicit `--fts5-query`
  string is preprocessed by `SearchQueryBuilder` — no stopword strip)
  → when `--fts5-query` is supplied, FTS5 fan-out (`2N`) and vector
  KNN fan-out (`2N`) flow through `rrf_merge` and the result is
  truncated to `--limit` (default 20); when `--fts5-query` is absent,
  FTS5 + RRF are skipped and pure vector KNN results return, dressed
  as `SemanticSearchHit` with `rank_fts5=None`. `--fts5-weight`
  (0.0..1.0, default 0.5) blends the two ranks via
  `rrf = 1/(k + rank_fts5) * fts5_weight + 1/(k + rank_vec) *
  (1 - fts5_weight)` (`k=60`); the flag is ignored when `--fts5-query`
  is omitted. `tdoc search query` (FTS5-only) is unchanged.
- `doc3gpp tdoc search index --rebuild-embeddings [--stale-only] [--batch N]
  [--resume] [--quiet]` → `SemanticSearchService.rebuild_embeddings`
  → on a fresh (non-resume) run drops + recreates `vec_tdoc_embeddings`
  at the live embedder dim and stamps `embedding_dim` + `embedding_model`
  in `vec_meta`; iterates every `tdocs` row, calls `index_for_tdoc` per
  id (build embed text → chunk → embed → upsert); updates `vec_meta`
  for resume + staleness. A resume run fails fast when the live
  dim/model no longer matches the stored values (no silent cross-model
  mixing). `--rebuild-all` runs both FTS5 and vector rebuilds in sequence.
  Requires `[semantic_search].embedding_base_url`; without it the command
  reports unavailable. A model/dim mismatch against a populated index
  fails fast with the same rebuild hint (swapping models forces a rebuild
  even when dims collide).

### Web layer + MCP + Jobs

The web layer (`src/doc3gpp/web/`) is a thin adapter over the same service
+ repository layer the CLI uses. A single process serves an HTMX/Jinja2 HTML
interface and a Streamable HTTP MCP endpoint on one port (`127.0.0.1:8765`
by default). The MCP mount answers each POST with a plain `application/json`
body (`json_response=True`), the mode every modern MCP client (including the
TypeScript SDK) expects; the legacy SSE-streamed response is rejected by
those clients with "Legacy MCP SSE endpoints are not supported". It is
disabled by default (`[server] enabled = false`); every
`doc3gpp server` subcommand refuses to run while disabled.

- `doc3gpp server start` → uvicorn runs `doc3gpp.web.app:build_app`
  (`--factory`); the FastAPI lifespan calls `build_state(settings)` to
  compose a `WebState` (engine + `ServiceContainer` of every service + job
  repository), starts the `JobWorker`, mounts the MCP sub-app at `/mcp`
  (only when `server.enabled` and `mcp.enabled`), and disposes the engine on
  shutdown. `GET /healthz` returns `{"ok": true}`.
- Browser → HTTP routes → `web/deps.py` request-scoped helpers → the same
  services → the same repositories. Routes render HTML via Jinja2 or return
  the CLI-equivalent JSON with `?format=json`.
- AI client → `POST /mcp` (JSON-RPC) → `mcp_server.build_mcp_server(state)`
  exposes the same read tools and enqueue the same jobs. For any read query,
  the MCP tool result bytes are **byte-for-byte identical** to the equivalent
  `?format=json` HTTP route (the job-enqueue envelope is the only exception —
  it adds a `message` key). See `docs/web-server.md`.
- `POST /jobs/...` / MCP job tools → enqueue a row in the SQLite `jobs` table
  → the single `JobWorker` (`web/workers/job_worker.py`) claims it
  (`queued → running → succeeded|failed|cancelled`), resolves a handler from
  `web/workers/handlers.py::JobHandlers.KIND_TO_HANDLER`, streams progress as
  `[{iso}]` log lines, and publishes Server-Sent Events to
  `GET /jobs/{id}/events`. Cancellation is cooperative via an asyncio event.
  The `jobs` table is created by schema bootstrap (no migration step).
- The worker polls for new `QUEUED` rows every
  `Settings.server.poll_interval_seconds` (default `1.0`s,
  range `0.05..60.0`) and runs retention cleanup on the separate
  `Settings.server.cleanup_interval_seconds` cadence (default `300`s).
  The two cadences are independent — flipping the cleanup cadence does
  not change pickup latency, and vice-versa. Earlier v1 conflated them
  and the 5-minute cleanup cadence became a 5-minute pickup delay for
  every freshly enqueued parse / sync / cache-purge request; the
  dedicated `poll_interval_seconds` knob is the fix. `mark_running` is
  idempotent (issues `UPDATE ... WHERE status = 'queued'` and treats
  `rowcount == 0` as a no-op) and returns a `(claimed, job)` pair so a
  worker that loses the claim race skips the handler instead of
  executing the job twice; the worker also sweeps any rows stuck at
  `RUNNING` on startup and marks them `FAILED` with
  `error="orphaned_after_restart"` so the nav badge can't get stuck on
  a job the new process never claimed.

Current TDoc search HTTP surfaces are `GET /tdocs/search`,
`GET /tdocs/search/sem`, and `POST /jobs/tdocs/search/rebuild`. The old
unscoped search routes were removed without redirects. The corresponding MCP
tools are `search_tdoc`,
`semantic_search_tdoc`, and `rebuild_tdoc_search_index`; the old plural
TDoc tool aliases were removed. Spec-document search remains under
`/spec-docs/search`, `/spec-docs/search/sem`, `search_spec_docs`, and
`semantic_search_spec_docs`.


## Database Schema

Tables live in `src/doc3gpp/storage/db/models.py`. Schema bootstrap is
`Base.metadata.create_all` via `doc3gpp db init`.

- `tdocs`:
    - `tdoc_id` (PK), `title`, `meeting_id` (FK → `meetings.meeting_id`),
      `ftp_url`, `source`, `type`, `status`, `reservation_date`,
      `uploaded_date`, `cr_cat`, `is_revision_of`, `revised_to`,
      `release`, `spec`, `version`, `related_wis`, `cr_num`,
      `cr_pack`. `ftp_url` is a regular nullable text column (no
      DB-level `UNIQUE` constraint); the upload pipeline maintains it
      as a 1:1 invariant (one `tdoc_id` per `ftp_url`), so the new
      `TDocRepository.get_by_ftp_url` reads with
      `ORDER BY tdoc_id ASC LIMIT 1` as a deterministic fallback if
      the invariant is ever violated.
    - `tdocs.tdoc_for` / `abstract` / `secretary_remarks` / `ls_to` /
      `ls_cc` / `original_ls` — six optional XLSX metadata columns
      captured per meeting TDoc-list sync; see
      `docs/superpowers/specs/2026-08-19-tdocs-xlsx-metadata-design.md`.
- `tdoc_files`:
    - `id` (PK), `tdoc_id` (FK → `tdocs.tdoc_id`, no cascade),
      `type` (`revision` / `review` / `support`), `file`, `ftp_url`
      (unique, the upsert key; stored as a path relative to the
      canonical 3GPP FTP root), `uploaded_date`.
- `spec_doc_sources` (specdata DB — `SpecDataBase`, not main `Base`):
    - `(spec_id, version)` composite PK, `release` (nullable),
      `ftp_url` (absolute version-zip URL), `downloaded_at` /
      `parsed_at` (nullable UTC — `parsed_at` is the immutable-skip
      key), `chunk_count` / `docx_count` (`Integer`, default 0).
- `spec_doc_tocs` (specdata DB):
    - `(spec_id, version)` composite PK, `release` (nullable),
      `toc_json_gzip` (nullable gzip JSON `{entries, files}`),
      `file_order_json` (nullable JSON `{source_file: file_order}`),
      `docx_count` (`Integer`), `created_at` (nullable UTC).
- `spec_doc_chunks` (specdata DB):
    - `chunk_id` (PK, `{spec_id}@{version}#{chunk_index}`), `spec_id`,
      `version`, `release` (nullable), `file_order` / `chunk_index`
      (`Integer`), `source_file` (bare `.docx` name),
      `section_no` / `section_title` / `table_no` / `table_title`
      (nullable), `text` (chunk body, no further splitting applied).
- `spec_doc_search` (specdata DB, FTS5): 7-column virtual table —
  `chunk_id` (UNINDEXED cid 0) + 6 indexed columns `(text,
  section_title, table_title, spec_id, version, release)` (cids 1..6)
  holding the `normalize_query` form. Backed by the chunk-level
  `SQLAlchemySpecDocSearchRepository` (one FTS5 row per chunk,
  `section_title` = `section_no + " " + title`).
- `spec_doc_search_meta` (specdata DB): sidecar for spec-doc rebuild
  resume + staleness (`last_indexed_parsed_at`,
  `last_rebuild_at`, resume cursor).
- `vec_spec_doc_embeddings` (specdata DB, sqlite-vec): virtual table
  keyed on `chunk_id` (`{spec_id}@{version}#{index}`); one row per
  chunk embedding. Gated on the sqlite + sqlite-vec support matrix
  (`_create_specdata_vector_schema`); silently skipped when the
  extension is unavailable.
- `vec_spec_doc_meta` (specdata DB): vector sidecar mirroring the
  `vec_meta` contract for the spec-doc vector table.
- `tdoc_cr_cover_page`:
    - `ftp_url` (PK, immutable download URL stored relative to the
      3GPP FTP root) + `tdoc_id` (non-PK FK → `tdocs.tdoc_id` with
      `ondelete="CASCADE"`, indexed for the per-tdoc lookup), one
      column per parsed cover-page field (`spec`, `cr_num`, `rev`,
      `version`, `title`, `source`, `tsg`, `related_wis`, `date`,
      `cr_cat`, `release`, `reason_for_change`,
      `consequences_if_not_approved`, `clauses_affected`,
      `other_comments`, `revision_history`, `extracted_tdoc_id`).
      Identity is the URL because 3GPP assets are byte-for-byte
      identical for the lifetime of the URL while a single `tdoc_id`
      may map to multiple URLs across revisions — every revision's
      parsed record is preserved. The table is **slim** post-Wave-1:
      it carries cover-page fields only. TTCN-specific fields
      (`testcase`, `ue`, `ss`, `ats_version`, `ttcn_release`,
      `test_suite`, `required_changes`) and the per-row timestamps
      (`extracted_at`, `parser_version`) have moved to
      `tdoc_extracts` (for timestamps) and `tdoc_cr_ttcn_details`
      (for the TTCN slice).
- `tdoc_cr_ttcn_details`:
    - `ftp_url` (PK, immutable download URL — same identity
      convention as `tdoc_cr_cover_page`) + `tdoc_id` (non-PK FK →
      `tdocs.tdoc_id` with `ondelete="CASCADE"`, indexed for the
      per-tdoc lookup), six overview columns (`testcase`, `ue`,
      `ss`, `ats_version`, `ttcn_release`, `test_suite`) plus
      `required_changes` (`LargeBinary(16 MB)` — gzip-compressed
      UTF-8 JSON list of correction dicts, written via
      `storage/compression.py`) and `changed_functions`
      (`Text`, nullable — a `\n`-joined `list[str]` of
      `"<module_basename>.<function_name>"` entries, sorted +
      deduped by `parsers/cr/ttcn_functions.py::extract_changed_functions`
      at parse time; deliberately **not** gzip-compressed so the
      column stays queryable via `LIKE`). The serialization contract
      is `"\n".join(...)` on write and `value.split("\n")` on read
      (`NULL` and empty both round-trip to `[]`). Partial-extraction
      markers apply: when only the module basename is recoverable the
      entry is recorded as `'<module>.'` (trailing-dot sentinel);
      when only the function name is recoverable it is recorded as
      `'.<function>'` (leading-dot sentinel); when neither is
      recoverable the correction is dropped. One row per
      immutable URL, so multiple revisions of the same `tdoc_id`
      still land at distinct URLs and occupy distinct rows. No
      `extracted_at` or `parser_version` — the sidecar is purely the
      parsed payload; timestamps and parser versioning live in
      `tdoc_extracts`.
- `tdoc_search`: FTS5 virtual table keyed on `tdoc_id`; uses stock sqlite `unicode61` tokenizer + Python-side `normalize_query` (T3); indexes title, ftp_url, meeting context, related WIs, and the concatenated text of `tdoc_cr_cover_page` / `tdoc_cr_change_details` / `tdoc_cr_ttcn_details` (gzip blobs decompressed in Python). Filter push-down is supported by three composite indexes that back the `search` / `tdoc list` predicate columns: `idx_tdocs_release_spec` (`tdocs.release`, `tdocs.spec`), `idx_tdocs_uploaded_date` (`tdocs.uploaded_date`), and `idx_meetings_name_tsg` (`meetings.name`, `meetings.tsg`).
- `tdoc_search_meta`: Sidecar for rebuild resume + staleness tracking (`last_rebuild_at`, `last_indexed_uploaded_date`, `last_rebuild_last_tdoc_id`, `last_indexed_at`).
- `vec_tdoc_embeddings`: sqlite-vec virtual table keyed on `(tdoc_id, chunk_id)`; one row per embedding chunk produced by the remote OpenAI-compatible embeddings API. Schema is gated on the sqlite + sqlite-vec support matrix (created by `_create_vector_schema` in `storage/db/migrate.py`; silently skipped when the sqlite-vec extension is unavailable). Dimensions match the live embedder dim at rebuild time; `vec_meta` tracks `embedding_dim` + `embedding_model` (a missing model row on a legacy DB counts as a mismatch — rebuild is the upgrade path).
- `vec_meta`: Sidecar for vector-index rebuild resume + staleness tracking — single row, mirrors the `tdoc_search_meta` contract for the vector table (`last_rebuild_at`, `last_indexed_uploaded_date`, `last_rebuild_last_tdoc_id`, `last_indexed_at`) plus the `embedding_dim` / `embedding_model` identity rows stamped on every non-resume rebuild.
- `tdoc_cr_change_details`:
    - `ftp_url` (PK, immutable download URL — same identity
      convention as `tdoc_cr_cover_page` and `tdoc_cr_ttcn_details`)
      + `tdoc_id` (non-PK FK → `tdocs.tdoc_id` with
      `ondelete="CASCADE"`, indexed for the per-tdoc lookup), plus
      two payload columns: `clauses` (`Text`, nullable — a
      `\n`-joined `list[str]` of clause numbers observed across the
      body that belong to a captured change block; sorted + deduped
      on write, deliberately **not** gzip-compressed so the column
      stays queryable via `LIKE`) and `changes` (`LargeBinary(16 MB)`,
      nullable — gzip-compressed UTF-8 JSON array of the captured
      change blocks, each block being a list of the original markdown
      lines that surround the `<ins>` / `<del>` revision marks;
      written via `storage/compression.py`). The serialization
      contract is `"\n".join(...)` on write and `value.splitlines()`
      on read (`NULL` and empty both round-trip to `[]`). One row
      per immutable URL, so multiple revisions of the same
      `tdoc_id` still land at distinct URLs and occupy distinct
      rows. No `extracted_at` or `parser_version` — the sidecar is
      purely the parsed payload; timestamps and parser versioning
      live in `tdoc_extracts`. Non-TTCN CRs only — a TTCN CR
      leaves no `tdoc_cr_change_details` row because the TTCN
      sidecar already carries the per-function change aggregate.
- `tdoc_extracts`:
    - `ftp_url` (PK, matches `tdoc_cr_cover_page.ftp_url`) + `tdoc_id`
      (non-PK FK → `tdocs.tdoc_id` with `ondelete="CASCADE"`, indexed
      for the per-tdoc lookup), `cache_file` (String(255), indexed),
      `doc_filename`, `extracted_at`, `parser_version`. Cache-pointer
      sidecar — the two child tables share the URL as their identity
      but have **no FK between themselves**: the on-disk cache can be
      purged (deleting every `tdoc_extracts` row) without dropping the
      parsed `tdoc_cr_cover_page` history, and the parsed record can be
      rebuilt (deleting `tdoc_cr_cover_page`) without invalidating the
      cached zip/markdown. The `cache_file` column stores the unified
      basename (derived from `ftp_url` via `derive_cache_file()`); the
      on-disk paths are reconstructed as `{cache.dir}/zips/<cache_file>`
      and `{cache.dir}/markdown/<cache_file>`.
- `meetings`:
    - `meeting_id` (PK), `name`, `title`, `location`, `start_date`,
      `end_date`, `ftp_url`, `start_doc`, `end_doc`, `tsg` (nullable
      FK → `tsgs.short_name`, indexed for the `meeting list --tsg` filter).
- `tsgs`:
    - `short_name` (PK), `tsg_name` (unique), `description`, `url`,
      `meeting_last_sync`. Seeded on `db init`; validates `--tsg`
      in `meeting sync`, `wi sync`, and `spec sync`. The spec sync
      skip rule is **per-spec** and lives on `specs.last_synced_at`
      (no TSG-level gate).
- `wis`:
    - `(wi_id, tsg_short)` composite PK, `acronym`, `release`, `name`.
      `tsg_short` FK → `tsgs.short_name`; composite PK keeps the natural
      identifier stable across multi-TSG ownership.
- `specs`:
    - `spec_id` (PK, dotted form e.g. `36.579-5`), `type` (`TS` / `TR`),
      `title`, `status`, `radio_tech`, `initial_release`, `tsg`
      (FK → `tsgs.short_name`, indexed for the `--tsg` filter),
      `wis` (nullable text — comma-joined related WI ids, extracted
      from the DynaReport detail page; deliberately **not** a join
      table so the schema stays flat), `rapporteurs` (nullable text —
      comma-joined company names from the detail page rapporteurs
      grid), `last_synced_at`. One row per dotted spec id.
- `testcases`:
    - `(testcase_id, group)` composite PK (`String(64)` /
      `String(8)`, indexed, upper-cased: `5G` / `LTE` / `IMS` /
      `UTRA` / `POS` / `MCX`), `title`, `ats`
      (`String(128)`), `feature` (`String(256)`), `release`
      (`String(32)`), `wis` (`String(512)`, comma-joined),
      `spec` (`String(32)`).
      One row per `(testcase_id, group)` — the same TC id on
      several workbook sheets yields one header row per group (see
      the parser note in the per-layer modules above).
- `testcase_status`:
    - `(testcase_id, group, path)` composite PK; `(testcase_id,
      group)` FK → `testcases(testcase_id, group)`
      (`ondelete="CASCADE"`), `path`
      (`String(16)` — `FR1` / `FR2` / `FR1+FR2` / `FDD` / `TDD` /
      `IPCAN-4G` / `EUTRA` / `IPCAN-5G` / `NR5GC` / `default`;
      single-path groups store the literal `'default'`),
      `gcf_ptcrb` (nullable text), `ttcn_status` (nullable text,
      indexed). One row per `(testcase_id, group, path)`;
      `replace_statuses` deletes + re-inserts per `(id, group)` on
      every sync.
- `testcase_sources`:
    - `filename` (PK, `String(256)` — upstream basename, e.g.
      `TTCN CR Agreement Status 2024-wk32.zip`), `year`, `week`,
      `revision` (all `Integer`, parsed from the filename),
      `downloaded_at` / `parsed_at` (tz-aware `DateTime`, nullable),
      `testcase_count` / `status_count` (`Integer`, default 0).
      Sync ledger: `parsed_at` is the file-identity skip key
      (`record_download` writes before parsing; `record_parsed`
      stamps after upserts).
- `spec_versions`:
    - `(spec_id, version)` composite PK, `release`, `ftp_url`, `pdf_url`,
      `meeting_id` (nullable), `meeting_name`, `upload_date`, `crs`
      (nullable comma-joined TDoc ids from the per-version CR list
      page), `version_id`, `wki_id`, `last_synced_at`.
      `spec_id` FK → `specs.spec_id` with `ondelete="CASCADE"` so
      wiping a header row clears every cached version. No FK to
      `meetings` — `meeting_id` is a snapshot of the upstream page
      and the meeting row may not exist in our database yet.

Cascading FK deletes are deliberately inconsistent across the schema:
`tdoc_cr_cover_page` / `tdoc_cr_ttcn_details` / `tdoc_extracts` cascade
on `tdocs.tdoc_id` deletion (they are derived artefacts of the parent
TDoc and are safe to wipe with it), while `tdoc_files` does not
(revision files survive a TDoc re-sync). The `tdoc_cr_cover_page`,
`tdoc_cr_ttcn_details`, and `tdoc_extracts` tables have **no FK
between each other**: the cache sidecar can be purged without
dropping parsed detail history, the parsed detail can be rebuilt
without invalidating the cached zip/markdown, and the TTCN sidecar
lives independently of the cover-page row at the same URL (a
non-TTCN extract leaves no `tdoc_cr_ttcn_details` row). The
`test_cascade_delete_via_fk` ORM test exercises the cascade
end-to-end via a `PRAGMA foreign_keys=ON` connect listener (SQLite
default is OFF).

## Backend Selection

The backend is selected from the allowlisted
`DOC3GPP_DATABASE_URL` env var (or its TOML counterpart):

- sqlite: `sqlite+pysqlite:///~/.local/share/doc3gpp/doc3gpp.db`

Engine kwargs are applied in `src/doc3gpp/storage/db/session.py` via:

- `src/doc3gpp/storage/backends/sqlite.py`

Every SQLite connection runs in **WAL journal mode** with a 5s
`busy_timeout` (set in the `Engine "connect"` listener alongside
`PRAGMA foreign_keys=ON`). WAL lets concurrent readers/writers proceed
without blocking and a busy writer waits for the lock instead of failing
immediately — this makes the thread-pool spec sync safe against
mid-write interruption (a Ctrl-C that previously tore the DB header
page). A side effect is that a live DB has `-wal` / `-shm` sidecar
files; `db reset` removes stale sidecars before recreating the schema.

## CLI Surface

Implemented command groups in `src/doc3gpp/cli.py` (12 sub-apps incl.
the `spec doc` / `toc` / `search` triplet, 41 commands) plus the `server` group in `cli_server.py`
(6 commands):

- `db`:
    - `check` — `--scope main|testcase|specdata|all` (default `all`); connects
      per scope and prints the selected URL(s)
    - `init` — `--scope main|testcase|specdata|all` (default `all`); creates the
      selected schema(s) via `create_schema(scope)` and seeds the
      `tsgs` reference table when main is in scope
    - `reset` — `--scope main|testcase|specdata|all` (default `all`);
      SQLite-only destructive reset; deletes the selected DB file(s)
      and sidecars, clears every engine cache, recreates the selected
      schema(s), and re-seeds `tsgs` when main is in scope
- `meeting`:
    - `sync` — validates `--tsg` against the reference table
    - `list` — filters by `--tsg`, `--name`, `--location`, `--year`,
      `--tdoc`, `--limit`, `--offset`. `--name` / `--location` are raw
      SQL LIKE patterns (use `%` / `_` explicitly). `--tdoc` accepts a
      9-character CR-shape id (e.g. `R5-260013`, `R5s260009`,
      `R5w260013`) and resolves to the meeting whose `start_doc` /
      `end_doc` range brackets the TDoc; prefix match is case-insensitive.
- `tdoc`:
    - `sync` — exactly one of `--meeting-id` or `--meeting`; delegates to
      `TDocSyncCoordinator`
    - `list` — filters by `--tdoc`, `--meeting`, `--meeting-id`,
      `--source`, `--spec`, `--wi`, `--title`, `--cr-cat`, `--status`,
      `--type`, `--revision-of`, `--revised-to`, `--ftp-url`, `--release`,
      `--version`, `--cr-num`, `--cr-pack`, `--uploaded-date`.
      Text-column filters accept the rich grammar from
      `src/doc3gpp/cli_filters.py` (`null` / `not-null` / `!<pattern>` for
      `NOT LIKE` / SQL `LIKE`); `--uploaded-date` additionally accepts
      `OP 'YYYY-MM-DD'` parameterised comparisons — the same surface
      `tdoc parse` exposes.
    - `parse` — `--tdoc` (LIKE pattern on `tdoc_id`), `--meeting-id`,
      `--meeting`, plus every text-column filter and `--uploaded-date`;
      `--force` re-extracts already-parsed rows, `--full` is reserved
      for the parser's `full=True` mode. End-to-end filter-driven:
      candidates are the intersection of every supplied predicate, with
      CR-type as the implicit default and a `max_batch` cap.
    - `search query`, `search index`, and `search sem` — TDoc FTS5,
      index-maintenance, and hybrid semantic search commands nested under
      `tdoc search`.
    - `show` — `--tdoc` (mutually exclusive with `--ftp-url`); renders
      the matching TDoc, the slim cover-page row from
      `tdoc_cr_cover_page` (URL-keyed on `tdoc.ftp_url`), the
      `extracted_at` timestamp from `tdoc_extracts` (same URL), and,
      when the TDoc is a TTCN CR, a `[TTCN Details]` block from
      `tdoc_cr_ttcn_details`. Every matching `tdoc_files` row
      (`tdoc_id`-keyed read, no URL match) renders under an
      `[Auxiliary Files]` block (table), `## Auxiliary Files`
      section (markdown), or `files` key (JSON). JSON payload keys
      are `tdoc` (always), `cover` (omitted when absent), `ttcn`
      (omitted when absent), `extracted_at` (omitted when absent),
      `files` (omitted when no auxiliary files exist). `--ftp-url`
      resolves the URL across `tdocs` / `tdoc_cr_cover_page` /
      `tdoc_cr_ttcn_details` / `tdoc_files` directly (no parent
      TDoc needed) and bundles the result into a separate
      `TDocShowRecordByUrl(ftp_url, tdoc, cover, ttcn, extracted_at,
      files)` DTO rendered under a `# FTP URL` / `[FTP URL]`
      anchor.
- `tsg`:
    - `list`, `show`, `seed`
- `testcase`:
    - `sync` — `[--force]`; resolves the latest `TTCN CR Agreement
      Status` History zip, downloads once, upserts headers +
      statuses; file-identity skip on `testcase_sources.parsed_at`
      (no interval).
    - `list` — filters by `--testcase`, `--title`, `--ats`,
      `--feature`, `--release`, `--wis`, `--spec`, `--group`
      (exact: `5G` / `LTE` / `IMS` / `UTRA` / `POS` / `MCX`),
      `--status` (any-path `ttcn_status` `EXISTS`), `--gcf-status`
      (any-path `gcf_ptcrb` `EXISTS`), `--limit` (default 50,
      range 1..500), `--offset` (default 0), `--fields` (all 9
      list fields; default from `output.fields.testcase`).
       JSON carries a nested `statuses` list of
       `{path, gcf_ptcrb, ttcn_status}` objects (`null` preserved);
       table / markdown stringify it as `path=gcf/ttcn;…`
       (`PATH_RANK`-sorted, `-` for `None`).
     - `show` — `--testcase ID` (required) + optional `--group`
       (exact: same 6 values; unknown → `BadParameter`); without
       `--group` every stored group renders. Renders the header(s)
       plus every `(path, gcf_ptcrb, ttcn_status)` row (no `group`
       in status rows); JSON is always an array of flat
       per-`(id, group)` objects with nested `statuses`; miss →
       `BadParameter`.
- `wi`:
    - `sync` — `--tsg`
    - `list` — filters by `--tsg`, `--name`, `--acronym`, `--release`
 - `spec doc` (nested under `spec` as `spec doc ...`):
     - `parse` — repeatable `--spec` (at least one required), `--release`,
       `--version`, `--force/-f`; resolves the numeric-newest or pinned
       version, fetches the ZIP when missing, and records the download;
       `--force` re-downloads and re-parses, including already-parsed pairs.
       Also accepts `--format/--output/--compact` (currently ignored); prints
       `ok` / `skipped <reason>` / `failed <reason>` (stderr) bucket lines.
    - `toc show` — `--spec` + `--version` (both required), `--release`,
      `--fields`, `--format/--output/--compact`.
    - `search query QUERY` — `--spec/--release/--version/--section`,
      `--limit` (default 20), `--offset` (default 0), `--fields`,
      `--format`, `--compact`.
    - `search sem QUERY` — `--fts5-query`, `--fts5-weight` (default 0.5),
      `--spec/--release/--version/--section`, `--limit` (default 20),
      `--format`, `--compact`.
    - `schema` — `--format/--output/--compact`; 27 rows, no filters, no DB.
- `config`:
    - `init` — bootstrap a default TOML at `--target {auto,project,user}`
      (default `auto`: `./doc3gpp.toml` from a project root, otherwise
      `~/.config/doc3gpp/config.toml`); refuses when `DOC3GPP_CONFIG`
      is set; `--force/-f` overwrites an existing file.
    - `path` — which TOML file is in effect (or
      `"(no config file found)"`)
    - `show` — fully-resolved `Settings` as JSON for diffing against
      `doc3gpp.toml.example`
    - `set` — `<key> <value>` write one setting into the active TOML
      config file (refuses when none is in use; run `config init`
      first); `--dry-run` prints the resulting TOML without writing.
- `cache`:
    - `status` — file count, total bytes, limit, per-subdir breakdown
    - `purge` — `[--scope {markdown,zips,all}]` (default `markdown`)
      selects which subtree to evict; `[--yes]` skips the interactive
      confirm; gated by `CacheSettings.purge_confirm` (TOML-only)
- `server`:
    - `start` — `--host`, `--port`, `--open/--no-open`, `--reload`;
      backgrounded (PID + log file, `/healthz` wait) unless `--reload`
    - `stop` — SIGTERM → 10s → SIGKILL; removes the PID file
    - `status` — PID, uptime, OS service state, HTTP/MCP URLs, last job
    - `logs` — `--job <id>` (from DB), `-f/--follow` (tail -f); mutually
      exclusive
    - `install systemd|launchd` — `--user/--system`, `--no-start`, `--dry-run`
    - `uninstall systemd|launchd` — refuses non-managed units
      (`InstallNotManagedError`)
    - Every subcommand requires `[server] enabled = true`.

Every `* list` command also accepts `--format table|json|markdown`
and `-o/--output PATH`. `meeting list`, `tdoc list`, `tsg list`, and
`testcase list` also
accept `--fields`; `wi list` and `spec list` use their configured
`output.fields.wi` / `output.fields.spec` lists without a per-command
`--fields` override. `tdoc show` additionally
accepts `--format {table,json,markdown,raw}` (the `raw` mode reads the
converted `.docx` markdown body straight from the cache, bypassing the
DB-row render — `--format raw` on the `--ftp-url` path is a
deterministic cache read because the URL is the row identity), and
the direct-mode `tdoc parse --from-path/--from-url` also accepts
`--format raw` for local-batch use.

The `--compact` flag threads a `compact: bool` through four renderer
seams — `_emit_records` (list commands), `_emit_record` (direct-mode
tdoc parse), the four `_render_tdoc_show_*` functions, and the four
`_render_tdoc_show_by_url_*` functions. `_resolve_compact(compact)`
resolves the CLI flag against `Settings.output.compact` (CLI > settings).
Table and raw formats are explicit no-ops; JSON swaps to
`separators=(",", ":")` with no trailing newline; Markdown drops every
CommonMark decorator and emits `key: value` lines with blank-line
section separators.

## Composition

The CLI layer never instantiates a concrete `SQLAlchemy*Repository`
directly; everything goes through `services/factory.py::build_*`. The
factory wires:

- `get_settings()` (cached; `cache_clear()` in tests that mutate
  allowlisted `DOC3GPP_*` env vars)
- `get_engine()` / `get_session_factory()` (cached; same clear
  contract) for the main DB, and `get_testcase_engine()` /
  `get_testcase_session_factory()` (cached; same clear contract)
  for the testcase DB
- `ScraperClient()` — single instance per CLI invocation

`_build_cache` in the CLI constructs `TDocCache(settings.cache.dir,
size_limit_bytes=settings.cache.size_limit_mb * 1024 * 1024)` directly
for the `cache status` / `cache purge` commands, which don't need the
service stack.

The web layer composes the same stack in-process: `web/app.py::build_state`
calls the factory builders and wraps them in a `ServiceContainer`, and
`web/deps.py` exposes request-scoped `Depends` helpers that read the
`app.state.web` container. `cli_server.py` never instantiates repositories
directly either — `server logs --job` uses `SQLAlchemyJobRepository()` and
`server status` uses it best-effort, while `server start` shells out to
uvicorn with `build_app`.

## Testing Layout

- `tests/unit/` — pure-Python unit tests that mock external calls. Coverage
  is concentrated in:
    - parser fixtures (`test_calendar_parser.py`,
      `test_cr_parser.py`, `test_tdoc_parser.py`,
      `test_tdoc_file_parser.py`, `test_wi_parser.py`,
      `test_docx_converter.py`)
    - scraping + cache contracts (`test_tdoc_cache.py`,
      `test_tdoc_zip_source.py`, `test_ftp_source.py`,
      `test_scraper_client.py`)
    - repositories (CRUD + filter combinations for concrete
      `SQLAlchemy*Repository` classes)
    - services (`test_meetings_service_sync.py`,
      `test_tdoc_service_sync.py`, `test_tdoc_sync_coordinator.py`)
    - CLI (`test_meeting_cli*`, `test_tdoc_cli_fields.py`,
      `test_tdoc_sync_cli.py`, `test_tdoc_parse_cli.py`,
      `test_cache_cli.py`, `test_wi_cli.py`, `test_tsg_cli.py`,
      `test_db_reset_cli.py`)
- `tests/integration/` — sqlite-only by default; online
  opt-in. Coverage includes:
    - `test_sqlite_backend.py`, `test_sdk_integration.py`,
      `test_cli_sqlite.py`, `test_db_reset_sqlite.py`
    - `test_meeting_service_sqlite.py`,
      `test_tdoc_sqlite.py`, `test_tdoc_file_sqlite.py`,
      `test_tdoc_cr_sqlite.py`, `test_tsg_sqlite.py`, `test_wi_sqlite.py`
    - web + MCP end-to-end (`test_web_end_to_end.py`,
      `test_mcp_end_to_end.py`, `test_cli_server.py`)
    - spec-doc corpus (`test_spec_doc_blocks.py`,
      `test_spec_doc_chunker.py`, `test_spec_doc_models.py`,
      `test_spec_doc_parser.py`, `test_spec_doc_settings.py`,
      `test_spec_doc_source.py` unit; `test_spec_doc_service.py`,
      `test_spec_doc_repo.py`, `test_spec_doc_search_repo.py`,
      `test_spec_doc_search_service.py`, `test_spec_doc_cli.py`,
      `test_spec_doc_web.py` integration)
    - `test_online_3gpp_calendar.py`, `test_online_tdoc_parse.py`,
      `test_online_tdoc_fetch_r5.py`, `test_online_spec_doc_sync.py`
      (live 3GPP endpoints, `@pytest.mark.online`; the spec-doc test
      resolves the numeric-newest version at runtime — no pins)
- `tests/fixtures/tdoc_cr_doc/` — 7 CR zip fixtures
  (`C6-250028.zip`, `R5-227476.zip`, `R5-253079.zip`,
  `R5s260009.zip`, `R5s260051.zip`, `R5s260135.zip`,
  `R5s260176.zip`). Regression corpus for `cr_parser` and
  `tdoc_cr_service`.
- Pytest markers: `online`. The sqlite profile is
  `pytest -m "not online"`; `./scripts/test_sqlite.sh`
  is the canonical wrapper.

## Cross-cutting design rules

These are enforced by code review (see `AGENTS.md` §Conventions for the
original convention list) and re-stated here for the architecture
readers:

- **Ruff clean at every phase boundary** — `ruff check src/doc3gpp
  tests` before merging.
- **No `as any` / `# type: ignore`** — use typed code paths instead.
- **Schema bootstrap is create-all, not versioned migrations.** `db init`
  is the intended schema boundary for normal use, while `meeting sync`,
  `wi sync`, and `tsg seed` still call `create_schema()` idempotently for
  fresh-database ergonomics. `tdoc sync` and `tdoc parse` assume the schema
  already exists. Existing installs need `doc3gpp db reset --yes` (SQLite)
  or a backend-native migration/reset after ORM shape changes.
- **Protocol ↔ impl signature parity** — when changing a filter
  signature on any repo, update both the Protocol and the impl.
- **CLI depends on `services/factory.py` only** — never instantiate a
  concrete `SQLAlchemy*Repository` from `cli.py`.
- **Settings caching** — `get_settings`, `get_engine`,
  `get_testcase_engine`, and `get_specdata_engine` are
  `@lru_cache(maxsize=1)`; any test that
  mutates an allowlisted `DOC3GPP_*` env var must call `cache_clear()`
  on all four in teardown (the `sqlite_env` fixture is the canonical
  pattern — it pins both database URLs and clears all three caches).

## Out of scope (today)

The full list of open constraints — schema bootstrap policy, settings
caching, hardcoded FTP root, calendar-parser coupling, TDoc source
coverage, R5-/C6- URL-template status, `python-docx` opt-in, and the
test-surface limits — lives in
[`docs/known-constraints.md`](known-constraints.md). That file is the
single source of truth; update it in the same change set when a
constraint is lifted.

Out-of-scope features that have not been implemented yet:

- TDoc types other than CR (LS, DRAFT, BB, etc.).
- Workplan / spec status extraction.
- Alembic / versioned migrations (the schema bootstrap is
  `Base.metadata.create_all` via `db init`).
