# Multi-file TDoc ZIP Markdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` to implement this plan
> task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert every supported DOCX member of a TDoc ZIP into durable,
source-identifiable Markdown while retaining one parent CR metadata record and
making FTS5, hybrid search, and raw retrieval source-aware.

**Architecture:** Add a document-level domain/content layer below the existing
URL-keyed `tdoc_extracts` parent. A shared ZIP parser and extraction bundle
feeds an atomic SQL replacement repository; SQL content is authoritative for
retrieval and index rebuilds, while the on-disk Markdown ZIP remains a derived
cache. Extend the existing parent-level FTS5 search with one metadata row and
one source row per document, group physical matches back to one TDoc result,
and expose the source collection through a shared content service used by the
CLI, HTTP, and MCP adapters.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0, SQLite FTS5, Pydantic v2,
Typer, FastAPI, FastMCP, `python-docx`, pytest, pytest-xdist, and the existing
serialized TDoc pipeline writer.

**Spec:** `docs/superpowers/specs/2026-10-22-tdoc-multi-file-zip-design.md`

## Global Constraints

- Exactly one converted member contains the CR cover; only that main member
  produces `tdoc_cr_cover_page`, TTCN, and body-change sidecars.
- Keep one `tdoc_extracts` parent row per immutable ZIP `ftp_url`; supporting
  members are content/provenance rows, not TDoc metadata or `tdoc_files` rows.
- Store Markdown text in SQL so retrieval and FTS5 rebuilding work after cache
  eviction; the Markdown ZIP and manifest are derived artifacts.
- Supported archive conversion is `.docx` only; do not add `.doc`, PDF,
  spreadsheet, or nested-ZIP converters.
- Preserve existing direct-mode FK behavior, auto-sync ordering, batch
  classifier, serialized writer boundary, and TDoc-level semantic embeddings.
- Resource limits are TOML-only: `max_documents_per_zip = 100` and
  `max_uncompressed_size_kb = 102400`; `0` disables each limit.
- Existing eight-element `search.bm25_weights` values remain accepted by
  appending the ninth `document_text` weight `1.0`; new serialized defaults
  contain nine values.
- Parent search pagination and `row_count` remain logical-TDoc semantics;
  physical FTS rows must never leak as duplicate parent results.
- Legacy plain UTF-8, gzip, and single-member Markdown cache artifacts remain
  readable but never become complete version-2 SQL extractions.
- CLI, HTTP, and MCP JSON payloads use the shared serializer and compact JSON
  rules where byte equality is part of the existing contract.
- Use TDD in every task: write a focused failing test, run it to prove the
  failure, implement the minimum change, then rerun the focused test and
  relevant regression tests.
- Do not add dependencies, reset databases destructively, repair the recorded
  unrelated auto-sync test, or commit automatically. The repository policy
  requires a separate user request for commits.

## Review Focus

These five input classes are most likely to expose mistakes not obvious from
the happy path; each is assigned to the task that owns its behavior:

1. A ZIP whose main CR member is not alphabetically first, with duplicate
   basenames in different directories — Task 1 tests source identity, main
   selection, and deterministic ordering.
2. A valid supporting DOCX with no CR cover, plus a corrupt supported member —
   Task 3 tests content-only support files and all-or-nothing extraction.
3. An old version-1 row or evicted cache with no derived Markdown artifact —
   Task 2/3 tests pending completeness and SQL-first retrieval/rebuild.
4. A query matching several supporting files in one TDoc — Task 5/6 tests
   source grouping, parent pagination, snippets, and hybrid provenance.
5. An exact URL/source selector containing encoded path characters — Task 7/8
   tests selector isolation, JSON parity, named Markdown output, and no
   implicit URL-read extraction.

---

## File Map and Dependency Order

The implementation follows these boundaries; later tasks consume the named
interfaces from earlier tasks.

1. **Domain and archive primitives:** new `tdoc_document` value objects and
   `tdoc_zip` enumeration/manifest helpers; no SQL or network access.
2. **Durable storage:** ORM, migrations, aggregate extraction repository, and
   schema registry entries for parent completeness and document content.
3. **Extraction orchestration:** shared conversion/main-selection flow and all
   database/direct/local/batch preparation paths.
4. **Content retrieval/rendering:** SQL-first content service and common raw
   Markdown renderer.
5. **FTS5 physical index:** nine indexed columns, metadata/document rows,
   currentness checks, rebuild behavior, and BM25 grouping.
6. **Public search model:** source matches through FTS5, facade, hybrid merge,
   CLI, HTTP, MCP, and templates.
7. **Documentation and end-to-end hardening:** settings examples, architecture
   references, schema snapshots, corpus upgrade guidance, and final coverage.

Files that need coordinated edits are listed in the tasks instead of being
changed opportunistically. No task changes `tdoc_files`.

## Task 1: Domain Models, ZIP Enumeration, and Multi-member Cache Artifact

**Files:**
- Create: `src/doc3gpp/models/tdoc_document.py`
- Create: `src/doc3gpp/parsers/tdoc_zip.py`
- Modify: `src/doc3gpp/models/tdoc_cr.py` (`TDocExtractMeta`,
  `DirectParseResult`)
- Modify: `src/doc3gpp/parsers/cr/helpers.py` (retain the legacy wrapper,
  delegate single-file behavior to the new enumeration primitive)
- Modify: `src/doc3gpp/parsers/__init__.py` and
  `src/doc3gpp/parsers/cr_parser.py` exports/documentation
- Test: Create `tests/unit/test_tdoc_zip.py`
- Test: Modify `tests/unit/test_cr_parser.py` and
  `tests/unit/test_tdoc_cr_model.py`

**Interfaces:**
- Produces `TDocDocument`, `TDocParseBundle`, and `MarkdownArchive` value
  objects for Tasks 2–4.
- Produces `document_id_for(ftp_url: str, source_file: str) -> str`, using the
  compact UTF-8 JSON array serialization specified by the spec and SHA-256.
- Produces `ephemeral_document_id_for(payload_hash: str, source_file: str) -> str`
  for direct/local results that must not invent a stored FTP URL.
- Produces
  `enumerate_docx_members(zip_bytes: bytes, *, max_documents: int = 100,
  max_uncompressed_bytes: int = 0) -> tuple[tuple[str, bytes], ...]`.
- Produces
  `build_markdown_archive(documents: Sequence[TDocDocument], *,
  ftp_url: str, main_source_file: str) -> bytes` and
  `read_markdown_archive(raw: bytes) -> MarkdownArchive`.
- Changes `DirectParseResult` to carry
  `documents: tuple[TDocDocument, ...]` and an explicit `main_source_file`
  instead of a scalar Markdown body; later tasks update consumers.

- [ ] **Step 1: Write failing enumeration and identity tests.**

  Add tests that construct an in-memory ZIP containing `z/main.docx`,
  `a/shared.docx`, `b/shared.docx`, a directory, `__MACOSX/` content, a Word
  lock file, and an unsupported `.pdf`. Assert that all supported DOCX members
  are returned, full paths are preserved, duplicate basenames remain distinct,
  and the order is deterministic. Assert that absolute/traversing names,
  duplicate normalized names, document-count overflow, and aggregate
  uncompressed-size overflow raise the specified validation/limit errors.

- [ ] **Step 2: Run the focused tests and verify the expected failure.**

  Run:

  ```bash
  pytest tests/unit/test_tdoc_zip.py tests/unit/test_cr_parser.py -q
  ```

  Expected: the new tests fail because the multi-member enumeration,
  validation, and identity interfaces do not yet exist.

- [ ] **Step 3: Implement the archive primitives.**

  In `tdoc_zip.py`, read ZIP metadata without extracting members to disk,
  normalize only `/` versus `\` separators, reject unsafe/duplicate names,
  exclude directories, `__MACOSX`, and Word lock files, accept `.docx` only,
  enforce both declared and actual byte limits, and return sorted members.
  Add `TDocDocument` fields `document_id`, `source_file`, `markdown_file`,
  `file_order`, `markdown_text`, and `content_hash`; validate non-empty safe
  source names and hashes in `__post_init__`. Add `TDocParseBundle` with
  `documents`, `main_source_file`, and the main `TDocCRParseResult`.

  Build derived ZIP members below `documents/<document_id>/`, add a versioned
  JSON manifest containing the URL, main source, ordered mappings, and hashes,
  and make the reader select `.md` members explicitly. Preserve legacy
  plain/gzip/single-member decoding through a separate compatibility result;
  never treat the manifest as Markdown.

  Add `extraction_version: int = 1` and `document_count: int = 0` to
  `TDocExtractMeta`. Update its documentation to say `doc_filename` is the
  main source path. Replace scalar direct-result documentation with the
  document collection contract while retaining source-kind/FK semantics.

- [ ] **Step 4: Run the focused tests and verify they pass.**

  Run:

  ```bash
  pytest tests/unit/test_tdoc_zip.py tests/unit/test_cr_parser.py tests/unit/test_tdoc_cr_model.py -q
  ```

  Expected: PASS, including the existing single-member selector compatibility
  tests and new multi-member/archive-manifest cases.

## Task 2: SQL Content Model, Migration, and Atomic Extraction Repository

**Files:**
- Modify: `src/doc3gpp/storage/db/models.py` (`TDocExtractOrm`)
- Create: `src/doc3gpp/storage/repositories/tdoc_extraction_sql.py`
- Modify: `src/doc3gpp/repository/protocols.py` (new extraction/document
  protocol)
- Modify: `src/doc3gpp/storage/repositories/tdoc_cr_sql.py` metadata mapping
- Modify: `src/doc3gpp/storage/db/migrate.py` (parent-column migration and
  `tdoc_documents` schema bootstrap)
- Modify: `src/doc3gpp/models/schema_info.py`
- Modify: `src/doc3gpp/services/factory.py` repository construction
- Test: Create `tests/integration/test_tdoc_documents_sqlite.py`
- Test: Modify `tests/unit/test_tdoc_cr_model.py` and
  `tests/unit/test_schema_info.py`

**Interfaces:**
- Produces `TDocDocumentRepository`/`TDocExtractionRepository` protocol
  methods:
  `replace_bundle(details, ttcn, changes, meta, documents) -> None`,
  `list_documents(ftp_url: str) -> tuple[TDocDocument, ...]`,
  `get_document(ftp_url: str, source_file: str) -> TDocDocument | None`, and
  `get_documents_for_tdoc(tdoc_id: str) -> tuple[TDocDocument, ...]`.
- `replace_bundle` is the single transaction boundary for the parent cover,
  optional sidecars, `tdoc_extracts`, and all `tdoc_documents` rows.
- `tdoc_documents` has the spec-defined columns and unique constraints on
  `(ftp_url, source_file)` and `(ftp_url, file_order)`, with a cascading FK to
  `tdoc_extracts.ftp_url`.
- Existing `TDocCrDetailRepository` read methods remain available to current
  show/sidecar callers; the new aggregate repository owns multi-file writes and
  document reads.

- [ ] **Step 1: Write failing SQL model and transaction tests.**

  Assert that schema creation exposes `extraction_version` and
  `document_count` on `tdoc_extracts` and all `tdoc_documents` columns. Insert
  one parent plus main/support documents and assert round-trip order, hashes,
  duplicate-basename isolation, and cascade deletion. Assert that replacing a
  successful bundle removes a deleted supporting document and clears stale
  TTCN/body sidecars, while a failed replacement leaves the previous bundle
  intact. Assert existing version-1 rows are readable as `version=1`,
  `document_count=0`, `complete=False`.

- [ ] **Step 2: Run the focused SQL tests and verify the expected failure.**

  Run:

  ```bash
  pytest tests/integration/test_tdoc_documents_sqlite.py tests/unit/test_tdoc_cr_model.py tests/unit/test_schema_info.py -q
  ```

  Expected: FAIL because the new ORM columns/table, migration, protocol, and
  repository do not yet exist.

- [ ] **Step 3: Implement ORM, migration, protocol, and repository.**

  Add `TDocDocumentOrm` and parent completeness columns with safe defaults.
  Write an idempotent migration helper that checks existing SQLite table
  columns before `ALTER TABLE`; call it from `create_schema("main")` before
  `Base.metadata.create_all`. Keep old rows at version 1/count 0. Add the
  schema registry descriptions and expected values.

  Implement `SQLAlchemyTDocExtractionRepository.replace_bundle` with one
  engine transaction: upsert the main details and metadata, delete/recreate
  URL-owned sidecars and document rows as required by the bundle, insert the
  ordered documents, and commit version 2/count only with the complete set.
  Reuse domain conversion helpers, not ORM objects, at service boundaries.
  Expose the repository from the factory for both CLI and web service builds.

- [ ] **Step 4: Run the focused tests and verify they pass.**

  Run the same command from Step 2. Expected: PASS, including migration
  idempotence, rollback, cascade, and schema-registry assertions.

## Task 3: Shared Multi-document Extraction Pipeline

**Files:**
- Modify: `src/doc3gpp/parsers/direct_extractor.py`
- Modify: `src/doc3gpp/services/tdoc_cr_service.py`
- Modify: `src/doc3gpp/services/factory.py`
- Modify: `src/doc3gpp/settings/schema.py` (`TDocParseSettings`)
- Modify: `src/doc3gpp/scraping/tdoc_zip_source.py` limit propagation
- Modify: `src/doc3gpp/web/workers/handlers.py` result handling
- Test: Modify `tests/unit/test_tdoc_cr_service.py`,
  `tests/unit/test_tdoc_parse_direct.py`, and
  `tests/unit/test_tdoc_parse_url_batch.py`
- Test: Modify `tests/integration/test_tdoc_parse_direct_sqlite.py`,
  `tests/integration/test_tdoc_parse_local_batch_sqlite.py`, and
  `tests/integration/test_tdoc_parse_ftp_batch_sqlite.py`

**Interfaces:**
- Changes `direct_parse_bytes` to
  `direct_parse_bytes(payload: bytes, *, filename: str, full: bool = False,
  max_bytes: int = 0, max_documents: int = 100,
  max_uncompressed_bytes: int = 0) -> TDocParseBundle`.
- Changes `PreparedTDocExtract` to carry `documents` and the main source,
  and `PreparedDirectTDoc` to carry the same collection; no prepared object
  has a scalar authoritative Markdown field.
- `TDocCrService` receives the aggregate extraction repository and uses
  `replace_bundle` from `persist_prepared_extract` and `persist_prepared_url`.
- A successful archive has one main `TDocCRParseResult`; supporting documents
  are converted content only. A direct bare `.docx` is a one-document bundle
  and still requires its CR header.

- [ ] **Step 1: Write failing multi-document parser/service tests.**

  Build DOCX fixtures in memory (using the existing test helpers) where the
  main cover document sorts after two supporting files. Assert all three
  Markdown bodies, stable source paths/IDs/order, main-only CR/TTCN/change
  metadata, and one parent extraction metadata record. Assert a support file
  without a CR header succeeds. Assert zero or multiple cover candidates,
  corrupt supported members, unsafe members, and limit violations enter the
  correct failure/skip path without replacing a prior SQL bundle.

  Assert database URL, direct URL, local path, folder batch, and queued URL
  paths all use the same bundle shape. Assert non-3GPP/FK-miss direct paths
  remain in-memory and do not invent a stored FTP URL.

- [ ] **Step 2: Run the focused parser/service tests and verify the expected failure.**

  Run:

  ```bash
  pytest tests/unit/test_tdoc_cr_service.py tests/unit/test_tdoc_parse_direct.py tests/unit/test_tdoc_parse_url_batch.py -q
  ```

  Expected: FAIL because direct parsing still selects one member, service
  preparation still expects scalar Markdown, and the aggregate write hook is
  not wired.

- [ ] **Step 3: Implement shared enumeration, conversion, and main selection.**

  Make `direct_parse_bytes` call the Task 1 enumerator for ZIPs, convert each
  DOCX sequentially, and run `is_cr_header_present` on each converted body.
  Require exactly one candidate; pass only that candidate to the existing CR
  parser. Build the ordered `TDocParseBundle`, with source hashes and the main
  source path. Keep a bare DOCX as a one-member bundle.

  Add the two TOML-only settings with the exact defaults and propagate them
  through factory/service/direct/FTP paths. Replace the single Markdown cache
  write/read with the multi-entry archive builder and manifest. Treat any
  supported-member conversion failure as an archive failure and preserve old
  SQL content until the aggregate transaction succeeds.

  Update `prepare_extract`, `_prepare_from_3gpp_url`, `prepare_from_bytes`,
  `persist_prepared_extract`, `persist_prepared_url`, and batch/worker result
  adapters. A non-force cache hit is complete only when parent version/count
  and SQL document rows agree with exactly one main source; old rows continue
  through the reparse path.

- [ ] **Step 4: Run parser/service and integration tests.**

  Run:

  ```bash
  pytest tests/unit/test_tdoc_cr_service.py tests/unit/test_tdoc_parse_direct.py tests/unit/test_tdoc_parse_url_batch.py tests/integration/test_tdoc_parse_direct_sqlite.py tests/integration/test_tdoc_parse_local_batch_sqlite.py tests/integration/test_tdoc_parse_ftp_batch_sqlite.py tests/integration/test_parse_tdoc_url_job_sqlite.py -q
  ```

  Expected: PASS for multi-member conversion, one-main metadata ownership,
  rollback/pending behavior, all direct/batch paths, and existing one-member
  regressions.

## Task 4: SQL-first Content Service and Raw Markdown Rendering

**Files:**
- Create: `src/doc3gpp/models/tdoc_content.py`
- Create: `src/doc3gpp/services/tdoc_content_service.py`
- Modify: `src/doc3gpp/services/factory.py`
- Modify: `src/doc3gpp/cli.py` (`_emit_record_raw`, TDoc raw paths, direct/batch
  raw emitters, `tdoc show` validation)
- Modify: `src/doc3gpp/web/deps.py` and `src/doc3gpp/web/state.py`
- Test: Create `tests/unit/test_tdoc_content_service.py`
- Test: Modify `tests/unit/test_tdoc_parse_cli.py` and
  `tests/integration/test_tdoc_parse_direct_sqlite.py`

**Interfaces:**
- `TDocContentDocument` contains `document_id`, `source_file`, `is_main`, and
  `markdown`.
- `TDocContent` contains `tdoc_id`, `ftp_url`, `extraction_version`,
  `complete`, and ordered `documents`.
- `TDocContentService.get(*, tdoc_id: str | None = None,
  ftp_url: str | None = None, source_file: str | None = None) -> TDocContent`.
  Exactly one of `tdoc_id`/`ftp_url` is required; `source_file` is scoped to
  that URL and never treated as a filesystem path.
- `tdoc_content_to_dict(content: TDocContent) -> dict[str, Any]` is the shared
  JSON payload function for HTTP/MCP/CLI structured output.
- `render_tdoc_documents_markdown(documents: Sequence[TDocContentDocument],
  *, include_source_headers: bool) -> str` emits one body unchanged or named
  comment-delimited blocks in main-first order, with JSON-safe source names.

- [ ] **Step 1: Write failing content/retrieval tests.**

  Seed SQL parent/document rows and assert TDoc and exact-URL selectors return
  all rows in order, source selection returns exactly one row, unknown sources
  fail without falling back, and SQL content wins when the cache is missing.
  Assert version-1 legacy cache fallback is incomplete, uses the known source
  only, and does not claim version 2. Test single-body versus named multi-body
  rendering, trailing-newline behavior, and source names containing comment or
  Unicode characters.

- [ ] **Step 2: Run focused content tests and verify the expected failure.**

  Run:

  ```bash
  pytest tests/unit/test_tdoc_content_service.py tests/unit/test_tdoc_parse_cli.py -q
  ```

  Expected: FAIL because no SQL-first content service or collection renderer
  exists and CLI consumers still read one cache string.

- [ ] **Step 3: Implement the content service and shared renderer.**

  Resolve TDoc IDs through the existing TDoc repository, normalize exact URLs,
  read SQL documents first, and validate version-2 completeness/count/main
  invariants. Only when SQL content is absent use the Task 1 legacy cache
  decoder; do not conceal inconsistent version-2 SQL with a stale cache.
  Keep URL reads side-effect free. Let the TDoc-ID CLI raw path call the
  existing extraction service before retrieval, while the URL path remains a
  deterministic snapshot.

  Replace scalar raw consumers in `cli.py` and direct/batch output paths with
  the shared renderer. Add `--source-file` to raw TDoc show, reject it for
  non-raw formats, and preserve existing TDoc/FTP selector XOR behavior.

- [ ] **Step 4: Run focused content and raw-output regressions.**

  Run the commands from Step 2 plus:

  ```bash
  pytest tests/unit/test_tdoc_cr_service.py tests/integration/test_tdoc_cr_sqlite.py tests/integration/test_tdoc_parse_direct_sqlite.py -q
  ```

  Expected: PASS for SQL/cache precedence, legacy compatibility, exact source
  selection, and raw output without breaking existing single-member behavior.

## Task 5: FTS5 Document Rows, BM25 Grouping, and Index Freshness

**Files:**
- Modify: `src/doc3gpp/storage/db/migrate.py` (`_create_search_schema` and
  explicit existing-table migration/recreation)
- Modify: `src/doc3gpp/settings/schema.py` (`_SNIPPET_COLUMN_NAMES`,
  `SearchSettings`)
- Modify: `src/doc3gpp/models/search.py` (new `MatchedDocument` value)
- Modify: `src/doc3gpp/storage/repositories/search_sql.py`
- Modify: `src/doc3gpp/repository/protocols.py` search signatures/docs
- Modify: `src/doc3gpp/services/search_service.py`
- Modify: `doc3gpp.toml.example`
- Test: Modify `tests/integration/test_search_schema.py`,
  `tests/integration/test_search_index_lifecycle.py`,
  `tests/integration/test_search_previews_per_column.py`, and
  `tests/integration/test_search_filters.py`
- Test: Create `tests/integration/test_tdoc_multifile_search_sqlite.py`

**Interfaces:**
- FTS5 retains the eight existing indexed fields and appends indexed
  `document_text`; row metadata adds unindexed `row_kind`, `document_id`,
  `source_file`, `extraction_version`, and `content_hash`.
- `SQLAlchemySearchIndexRepository.upsert(tdoc_id: str) -> None` replaces one
  metadata row plus all current document rows for that TDoc from SQL.
- `SearchIndexRepository.search(...) -> list[SearchHit]` returns one grouped
  parent hit; each document match is represented by the new
  `MatchedDocument` value.
- `count_tdocs_to_index`, `rebuild_batch`, and `status` compare expected SQL
  document IDs/hashes/version/count with FTS rows, not only uploaded dates.

- [ ] **Step 1: Write failing FTS5/schema/settings tests.**

  Assert a fresh schema has the new unindexed provenance fields and nine
  indexed fields, and an existing eight-weight settings value is normalized
  with a ninth `1.0` value while an invalid length is rejected. Seed one
  metadata-only TDoc and one extracted TDoc with two documents; assert an
  attachment-only term finds the parent with the matching source, metadata
  matches remain single parent hits, document snippets use the document
  weight, and source rows do not satisfy a phrase/conjunction across files.
  Assert replacement removes stale physical rows, `row_count` counts parents,
  and content hash/version changes make status stale even when upload dates do
  not change.

- [ ] **Step 2: Run focused search tests and verify the expected failure.**

  Run:

  ```bash
  pytest tests/integration/test_search_schema.py tests/integration/test_search_index_lifecycle.py tests/integration/test_search_previews_per_column.py tests/integration/test_tdoc_multifile_search_sqlite.py -q
  ```

  Expected: FAIL because the current virtual table has eight indexed columns,
  one row per TDoc, eight-weight validation, and no document-content source
  grouping.

- [ ] **Step 3: Implement schema migration, indexing, grouped search, and freshness.**

  Replace the old FTS virtual table explicitly when the indexed-column layout
  is incompatible, preserving the meta sidecar and filter indexes. Add the
  compatibility weight normalizer and exact nine-column snippet/score binding.

  Make `upsert` build the existing metadata text plus SQL-backed document
  rows, delete all old rows for the parent, and write provenance/hashes in one
  transaction. Implement `search` with a match CTE that ranks physical rows,
  selects parent IDs before applying limit/offset, then loads all matching
  document rows for those parents. Keep FTS expressions row-local; rank a
  parent by its best row and tie-break by TDoc ID/source identity.

  Update rebuild/stale-only/status logic to compare physical row counts,
  source IDs, hashes, and extraction versions while preserving parent cursors
  and logical progress. Use SQL document content only; no cache/network read
  is allowed during index rebuild.

- [ ] **Step 4: Run FTS5 regressions and verify they pass.**

  Run the command from Step 2 plus:

  ```bash
  pytest tests/integration/test_search_filters.py tests/integration/test_search_weighted_bm25.py tests/integration/test_search_after_parse.py -q
  ```

  Expected: PASS for old metadata search, new document search, snippets,
  grouped pagination, migration, rebuild, and freshness behavior.

## Task 6: Search DTOs, Facade, Hybrid Provenance, and Renderers

**Files:**
- Modify: `src/doc3gpp/models/unified_search.py`
- Modify: `src/doc3gpp/services/tdoc_search_facade.py`
- Modify: `src/doc3gpp/services/semantic_search_service.py` (`rrf_merge`)
- Modify: `src/doc3gpp/cli.py` unified TDoc search renderer
- Modify: `src/doc3gpp/web/templates/partials/search_results_table.html`
- Test: Modify `tests/unit/test_tdoc_search_facade.py`,
  `tests/unit/test_semantic_search_service.py`,
  `tests/unit/test_unified_search_models.py`, and
  `tests/unit/test_cli_search.py`
- Test: Modify `tests/integration/test_search_sem_end_to_end.py` and
  `tests/integration/test_web_search_end_to_end.py`

**Interfaces:**
- Consumes `MatchedDocument` from Task 5 with fields `document_id`, `ftp_url`,
  `source_file`, `score`, and `previews`.
- `SearchHit.matched_documents: tuple[MatchedDocument, ...]` is ordered by
  document score/source identity.
- `TDocSearchResult.matched_documents` is carried through all modes; FTS5 and
  hybrid may populate it, semantic-only and filter-only return an empty tuple.
- `tdoc_search_result_to_dict` always serializes a JSON `matched_documents`
  array without changing the existing top-level fields.
- `rrf_merge` continues to fuse one grouped TDoc FTS hit against the best
  vector hit and carries the grouped FTS source list unchanged.

- [ ] **Step 1: Write failing DTO/fusion/rendering tests.**

  Assert FTS5 facade mapping preserves two matching source files, semantic-only
  and filter-only mappings return `[]`, JSON serialization includes source
  identity and exact URL, and RRF does not overwrite one source list when
  merging by TDoc ID. Assert CLI table/Markdown/JSON output displays source
  matches and the HTML table links to source-aware content URLs.

- [ ] **Step 2: Run focused DTO/fusion tests and verify the expected failure.**

  Run:

  ```bash
  pytest tests/unit/test_tdoc_search_facade.py tests/unit/test_semantic_search_service.py tests/unit/test_unified_search_models.py tests/unit/test_cli_search.py -q
  ```

  Expected: FAIL because current DTOs have no matched-document field and
  serializers/fusion/renderers discard file provenance.

- [ ] **Step 3: Implement source-aware public search mapping.**

  Add immutable matched-document values, map repository hits in
  `TDocSearchFacade`, and keep semantic rank/fusion parent-level. Update CLI
  fields and preview formatting without exposing internal FTS row kind. In
  the web table, render each source path as a URL-encoded link to the exact
  URL/source content endpoint while retaining the parent TDoc link.

- [ ] **Step 4: Run focused and end-to-end search tests.**

  Run the command from Step 2 plus:

  ```bash
  pytest tests/integration/test_search_sem_end_to_end.py tests/integration/test_web_search_end_to_end.py tests/integration/test_search_query_sem_rerank.py -q
  ```

  Expected: PASS with unchanged parent ranking/filter semantics and preserved
  source lists in FTS5 and hybrid modes.

## Task 7: HTTP Content Route, MCP Content Tool, and HTML Source Sections

**Files:**
- Modify: `src/doc3gpp/web/routes/tdocs.py`
- Modify: `src/doc3gpp/web/mcp_server.py`
- Modify: `src/doc3gpp/web/deps.py` (`get_tdoc_content_service`),
  `src/doc3gpp/web/state.py` (`ServiceContainer`/`WebState`), and
  `src/doc3gpp/web/app.py` (`build_state` service construction)
- Modify: `src/doc3gpp/web/templates/tdoc_content.html`
- Modify: `src/doc3gpp/web/templates/partials/search_results_table.html`
- Test: Modify `tests/integration/test_web_end_to_end.py`,
  `tests/integration/test_mcp_end_to_end.py`, and
  `tests/unit/test_web_routes.py`

**Interfaces:**
- Extend `GET /tdocs/{tdoc_id}/content` with optional `source_file` and
  `format=html|markdown|json`, retaining HTML as the default.
- Add `GET /tdocs/by-url/content?ftp_url=...` before the parameterized route,
  with the same optional source/format contract and no extraction side effect.
- Extend MCP `get_tdoc_content(tdoc_id: str | None = None,
  ftp_url: str | None = None, source_file: str | None = None,
  format: str = "markdown") -> str`; enforce exactly one parent selector.
- HTTP JSON and MCP JSON use `tdoc_content_to_dict` and compact
  `ensure_ascii=False` serialization so response bytes match.

- [ ] **Step 1: Write failing HTTP/MCP contract tests.**

  Seed a multi-document SQL extraction and assert HTTP JSON equals MCP JSON
  byte-for-byte, exact source selection returns one document, all-document
  Markdown uses named blocks, HTML labels main/support sources, and unknown
  source/selector/format errors are clear. Assert exact URL reads do not call
  the scraper or extraction service, and legacy incomplete HTTP responses
  carry the completion header while MCP text-only reads direct callers to JSON
  or reparse.

- [ ] **Step 2: Run focused web/MCP tests and verify the expected failure.**

  Run:

  ```bash
  pytest tests/integration/test_web_end_to_end.py tests/integration/test_mcp_end_to_end.py tests/unit/test_web_routes.py -q
  ```

  Expected: FAIL because routes/tools accept only one TDoc ID and decode one
  cache body independently.

- [ ] **Step 3: Implement shared web/MCP adapters.**

  Inject/use `TDocContentService`, register the URL route before
  `/{tdoc_id}/content`, and use the shared payload/renderer. Render HTML as
  distinct source-labelled sections with source links. Keep HTTP/MCP error
  mapping and existing missing-cache parse hints. Do not duplicate cache
  decoding or silently trigger auto-sync from URL content reads.

- [ ] **Step 4: Run web/MCP regressions and verify they pass.**

  Run the command from Step 2 plus:

  ```bash
  pytest tests/integration/test_web_search_end_to_end.py tests/integration/test_mcp_end_to_end.py -q
  ```

  Expected: PASS for HTML, Markdown, JSON, exact URL/source selection, parity,
  and no-side-effect URL reads.

## Task 8: CLI/Worker/Pipeline Completion and Corpus Upgrade Behavior

**Files:**
- Modify: `src/doc3gpp/cli.py` all remaining direct/batch result consumers
- Modify: `src/doc3gpp/web/workers/handlers.py`
- Modify: `src/doc3gpp/services/tdoc_pipeline.py` only where result typing or
  raw output collection requires it
- Modify: `src/doc3gpp/models/jobs.py` only if failure details need a new
  source-aware shape
- Test: Modify `tests/unit/test_tdoc_parse_cli.py`,
  `tests/unit/test_tdoc_pipeline.py`, and
  `tests/integration/test_parse_tdoc_url_job_sqlite.py`
- Test: Create `tests/integration/test_tdoc_multifile_upgrade_sqlite.py`

**Interfaces:**
- Every direct/local/folder/queued parse result exposes the same ordered
  document collection and source-specific failure detail.
- Existing `BatchExtractResult` bucket semantics remain unchanged; limit
  violations are skipped, conversion/cover ambiguity failures are failed.
- Version-1 parent rows are discovered as pending; version-2 rows skip only
  when document count, main source, and child content are complete.

- [ ] **Step 1: Write failing pipeline/upgrade tests.**

  Assert raw folder/batch outputs cannot collide when two source documents
  share a basename, queued jobs report member-specific outcomes, a version-1
  row is selected for reparse, a forced reparse replaces removed support rows,
  and an index rebuild after cache purge uses SQL content without a download.

- [ ] **Step 2: Run focused pipeline tests and verify the expected failure.**

  Run:

  ```bash
  pytest tests/unit/test_tdoc_parse_cli.py tests/unit/test_tdoc_pipeline.py tests/integration/test_parse_tdoc_url_job_sqlite.py -q
  ```

  Expected: FAIL because remaining consumers still access `.markdown`, and
  completeness/upgrade logic has not been propagated through the pipeline.

- [ ] **Step 3: Update all remaining adapters and pending/force behavior.**

  Replace scalar result access with the shared collection renderer/content
  service, preserve deterministic discovery/output order, and include source
  paths in failure details. Ensure the serialized writer performs one complete
  aggregate replacement before the existing best-effort FTS/vector hooks.
  Do not add per-file semantic embeddings or change job IDs/contracts beyond
  the source details needed by this feature.

- [ ] **Step 4: Run focused pipeline/upgrade tests and verify they pass.**

  Run:

  ```bash
  pytest tests/unit/test_tdoc_parse_cli.py tests/unit/test_tdoc_pipeline.py tests/integration/test_parse_tdoc_url_job_sqlite.py tests/integration/test_tdoc_parse_local_batch_sqlite.py tests/integration/test_tdoc_parse_ftp_batch_sqlite.py tests/integration/test_tdoc_multifile_upgrade_sqlite.py -q
  ```

  Expected: PASS for direct, folder, queued, version-1 upgrade, force
  replacement, and cache-evicted rebuild behavior.

## Task 9: Documentation, Schema Registry, Settings Examples, and Final Coverage

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/architecture.md`, `docs/cli.md`, `docs/web-server.md`,
  `docs/code-map.md`, `docs/conventions.md`, and
  `docs/known-constraints.md` where the changed contracts are documented
- Modify: `doc3gpp.toml.example`
- Modify: `src/doc3gpp/models/schema_info.py` for the complete new table/field
  registry
- Test: Modify `tests/unit/test_schema_info.py`,
  `tests/integration/test_search_schema.py`,
  `tests/integration/test_web_end_to_end.py`, and
  `tests/integration/test_mcp_end_to_end.py`

**Interfaces:**
- Documentation names `tdoc_documents`, extraction version/completeness,
  source-aware FTS results, `--source-file`, HTTP/MCP selectors, and the two
  TOML-only archive limits.
- Schema output describes every new table/field and remains consistent across
  CLI, HTTP, and MCP.

- [ ] **Step 1: Write failing documentation/schema assertions.**

  Add assertions for the new schema registry table/fields, nine search
  columns/weights, settings example keys/defaults, and CLI/web help or payload
  examples for multi-document content.

- [ ] **Step 2: Run the focused documentation/schema tests and verify failure.**

  Run:

  ```bash
  pytest tests/unit/test_schema_info.py tests/integration/test_search_schema.py -q
  ```

  Expected: FAIL until registry and examples match the implementation.

- [ ] **Step 3: Update documentation and examples.**

  Document that `tdoc_files` remains FTP auxiliary-file metadata, while
  `tdoc_extracts` is the one parent and `tdoc_documents` stores inner content.
  Include the source-aware search/raw examples, legacy upgrade workflow, cache
  eviction behavior, no implicit URL-read extraction, and recorded unrelated
  baseline test issue. Keep docs synchronized with actual names and defaults;
  do not promise semantic supporting-document coverage.

- [ ] **Step 4: Run the focused documentation/schema tests and verify they pass.**

  Run:

  ```bash
  pytest tests/unit/test_schema_info.py tests/integration/test_search_schema.py tests/integration/test_web_end_to_end.py tests/integration/test_mcp_end_to_end.py -q
  ```

  Expected: PASS with schema/setting/documentation references consistent.

## Whole-branch Verification Handoff

After all tasks are implemented and individually reviewed by the
subagent-driven workflow, run the repository's required verification commands
from a clean task state:

```bash
ruff check .
./scripts/test_sqlite.sh
```

The known baseline auto-sync test can still fail because its calendar scraper
patch is incomplete and the environment may return HTTP 403; report it
separately rather than modifying unrelated code. Verify the feature-specific
unit/integration targets independently and inspect `git diff`/`git status`.
Do not claim the branch complete without fresh command output, and do not
commit unless the user explicitly requests it.

## Execution Handoff

Plan complete and saved to
`docs/superpowers/plans/2026-10-22-tdoc-multifile-markdown.md`. The user has
already specified subagent-driven development plus TDD; after plan approval,
invoke `superpowers:subagent-driven-development`, then use a fresh implementer
and reviewer for each task followed by whole-branch review. Do not begin
implementation or run the full verification suite before the user reviews
this plan.
