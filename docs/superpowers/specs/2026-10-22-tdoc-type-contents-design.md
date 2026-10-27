# TDoc content parsing: CR annexes, WID, LS, and discussion

Date: 2026-10-22 (revised 2026-10-23)  
Status: **Approved by the user on 2026-10-23 (items 18 and 20–25 confirmed; migration baseline corrected to `main`). Plan revised 2026-10-23 to `docs/superpowers/plans/2026-10-22-tdoc-type-contents.md`; decisions P7, P12, and P13 are approved by the user. Execution awaits the user's choice of method.**  
Branch: `feat/tdoc-multifile-markdown` (current checkout, no worktree)  
Depends on: the committed multi-file TDoc work (commit `cb37051`: `tdoc_extracts`, `tdoc_documents`, the Markdown archive, and the ZIP helpers). See `docs/superpowers/specs/2026-10-22-tdoc-multi-file-zip-design.md`. This design renames `tdoc_documents` to `tdoc_cr_annexes`.

## Revision log (2026-10-23)

- **Changed.** CR main Markdown is no longer stored in SQL. Its key parts (cover page, TTCN details, change blocks) stay in SQL and remain searchable. Its full Markdown is read from the derived cache archive.
- **Changed.** CR annexes (every non-main `.docx` in a CR ZIP) are stored in SQL with their file name and full Markdown, and are searchable through FTS5 and semantic search.
- **Changed.** `tdoc_documents` is renamed to `tdoc_cr_annexes`. Its content column is compressed like the family tables.
- **Changed.** The FTS5 column `document_text` is renamed `content_text`.
- **Changed.** All four content tables use one shape (section 5). Markdown archive names use `_sanitized_source_stem` for every type.
- **Changed.** CR completeness, `document_count`, and the CR cache-hit rule are reworked (section 5).

## 1. Purpose and confirmed requirements

`tdoc parse` currently stores the Markdown of CR main documents and their supporting documents in `tdoc_documents`. The main document is parsed for its cover page, TTCN details, and change blocks. This change extends SQL storage of full Markdown to four content kinds:

| Scope | `tdocs.type` values (case-insensitive) | SQL content table | Main document in SQL |
| --- | --- | --- | --- |
| CR annexes | `CR` | `tdoc_cr_annexes` (renamed from `tdoc_documents`) | No. Read from the cache archive. |
| WID | `WID new`, `WID revised` | `tdoc_wid_contents` | Yes |
| LS | `LS in`, `LS out` | `tdoc_ls_contents` | Yes |
| discussion | `discussion` | `tdoc_discussion_contents` | Yes |

Confirmed by the user:

- For a CR TDoc, the main document's full Markdown is not stored in SQL. Its key details are stored (cover page, change blocks, TTCN details) and are searchable through FTS5. When the full main Markdown is needed, it comes from the cached Markdown archive.
- For a CR TDoc, every other `.docx` in the ZIP (an annex) is stored in SQL with its file name and full Markdown, for FTS5 and semantic search.
- For WID, LS, and discussion TDocs, the main document and every annex are stored in SQL with their file names and full Markdown.
- All other types are ignored this round, including `other`. `other` TDocs are never downloaded.
- Content tables are fixed and created with the schema. No runtime DDL.
- Content lives in the main database, not in a sidecar file.
- With no `--type`, `tdoc parse` selects CR plus the three families. Web and MCP parse use the same selection.
- Main document: the `.docx` whose basename starts with the TDoc number. It must convert, for every type. A failing annex is skipped with a source-named warning (tolerant annexes).
- Semantic search uses chunked embeddings of the stored documents.
- "Support list" means extending `tdoc list`: parse state is visible and filterable.
- Raw Markdown is retrievable from the CLI, HTTP, web, and MCP.
- Work on the current branch. Use Superpowers SDD and TDD during implementation. Do not commit without a separate request.

Items marked **Proposed** were not explicitly confirmed. They are collected in section 12.

## 2. Scope and non-goals

In scope: parse routing and selection; ZIP download and cache reuse; `.docx` discovery and main selection; tolerant annex conversion; per-type SQL content for CR annexes and the three families; the `tdoc_documents` to `tdoc_cr_annexes` rename; parent metadata; the Markdown archive; FTS5 document rows and the `content_text` column; chunked semantic index; `tdoc list` parse state; content retrieval on CLI, HTTP, web, and MCP; migrations; documentation.

Non-goals:

- Any change to CR cover-page, TTCN, and body-change parsing and storage. The CR changes in this design are limited to: the CR main Markdown is not stored in SQL, CR annexes are stored, and CR annexes are indexed (sections 5, 8, 9).
- Any other type, including `other`.
- `.doc`, PDF, spreadsheet, or nested-ZIP conversion.
- Direct-mode parsing for these families beyond the type detection in section 6. Family types found in direct mode depend on decision 11.
- A separate contents database file.
- Storing the CR main document's Markdown in SQL (user decision).
- Changes to `tdoc_files`.

## 3. Current state and what changes

- `doc3gpp tdoc parse` defaults `--type` to `CR` (`src/doc3gpp/cli.py:2446`).
- Web and MCP `parse_tdocs` have no CR default. They pass every matching type to the extractor (`web/workers/handlers.py:369`, `web/mcp_server.py:918`). A non-CR item raises `TDocTypeUnsupportedError` (`services/tdoc_cr_service.py:1431-1445`), which the pipeline classifies as **failed** (`services/tdoc_pipeline.py:69-85`).
- `TDocCrService.prepare_extract` (`services/tdoc_cr_service.py:621`) requires a CR cover through `_parse_bundle`. The family path does not call `_parse_bundle`.
- Today `tdoc_documents` holds every converted `.docx` of a CR ZIP: the main document at `file_order` 0, followed by the supporting documents (content-only). Writer: `SQLAlchemyTDocExtractionRepository.replace_bundle` (`storage/repositories/tdoc_extraction_sql.py:47`).
- FTS5 document rows come only from `tdoc_extracts` joined to `tdoc_documents` (`storage/repositories/search_sql.py:692`, `_build_index_text`). The FTS column is `document_text`.
- Semantic embed text is built from title, meeting, WIs, and CR sidecars only (`storage/repositories/vector_sql.py:505`, `_build_embed_text`). It does not use document bodies.
- `TDocContentService.get` (`services/tdoc_content_service.py:78`) reads CR SQL documents, with a legacy cache fallback for version-1 rows.

## 4. Type routing and selection

- A TDoc's family is found by case-insensitive exact match of `tdocs.type` against the table in section 1. CR uses the CR path. Any other type is ineligible.
- A single registry module (**Proposed:** `models/tdoc_content_families.py`) defines each family, its accepted type values, and its table name. Parse selection, the pipeline, the content service, search, and the schema registry all read from it. CR is listed in the registry with the CR annex table.
- Default selection (no `--type`): CR plus all three families.
- An explicit `--type` (rich LIKE filter) is intersected with eligible types. `other` and other ineligible types never enter the download queue.
- `tdoc parse --tdoc <id>` for an ineligible type goes to the **skipped** bucket with a reason that names the type. **Proposed.**

## 5. Durable content model

```text
tdocs (tdoc_id)
  └── tdoc_extracts (ftp_url)                    one row per immutable ZIP URL (existing parent)
        ├── tdoc_cr_annexes                      one row per CR annex .docx (renamed from tdoc_documents)
        ├── tdoc_wid_contents                    one row per .docx member (WID: main + annexes)
        ├── tdoc_ls_contents                     one row per .docx member (LS: main + annexes)
        └── tdoc_discussion_contents             one row per .docx member (discussion: main + annexes)
```

All four content tables have the same shape. **Changed:** this shape is shared, and `file_order` and `markdown_file` are not stored.

| Column | Type | Notes |
| --- | --- | --- |
| `ftp_url` | String(1024) | Part of the primary key. FK to `tdoc_extracts.ftp_url`, `ON DELETE CASCADE`. Normalized with `normalize_ftp_path`, as in `tdoc_extracts`. |
| `doc_filename` | String(1024) | Part of the primary key. Full, case-preserving member path inside the ZIP (for example, `annex/info.docx`). |
| `document_id` | String(64) | `document_id_for(ftp_url, doc_filename)` (SHA-256 identity, `models/tdoc_identity.py`). Used by FTS and chunk IDs (plan decision P1). |
| `tdoc_id` | String(64) | FK to `tdocs.tdoc_id`, `ON DELETE CASCADE`, indexed. |
| `content` | LargeBinary | Gzip JSON of the Markdown string, written with `storage/compression.py::compress_json`. Same encoding as `tdoc_cr_change_details.changes`. **Changed:** used by CR annexes too. |
| `content_hash` | String(64) | SHA-256 of the UTF-8 Markdown. FTS and vector freshness use it without decompressing every document. |

**Proposed:** `file_order` and `markdown_file` are not columns. The read path orders members (main first, then the path sort key `_path_sort_key` in `parsers/tdoc_zip.py`) and derives `markdown_file` from `document_id` and `_sanitized_source_stem` (section 8).

Main document and parent:

- `tdoc_extracts.doc_filename` is the main document's path for every type. It is a non-key attribute, so one ZIP URL has exactly one parent row. `tdoc_extracts` keeps `ftp_url` as its only primary key.
- For a family TDoc, the main document has a content row whose `doc_filename` equals the parent's.
- For a CR TDoc, the main document has **no** content row. Its Markdown is only in the cache archive. No CR annex row may equal the parent's `doc_filename`.

Parent metadata reuses `tdoc_extracts` with its existing meaning, plus:

- `cache_file`: the derived ZIP name.
- `extraction_version = 2`.
- `document_count`: **Changed (Proposed).** The number of content rows stored for this URL. For CR this is the annex count (main is not stored). For a family it is the main plus annex count.
- `skipped_sources` (**Proposed**, new nullable Text column, JSON array of member paths that failed to convert). `NULL` or `[]` means none.

Completeness:

- **Family:** the parent exists with `extraction_version = 2`, `skipped_sources` is empty, `document_count` equals the content-row count, and exactly one content row matches the parent's `doc_filename`. Any other state is pending for normal parsing.
- **CR (Proposed, replaces the main-row rule):** the parent exists with `extraction_version = 2`, `skipped_sources` is empty, `document_count` equals the annex-row count, no annex row equals the parent's `doc_filename`, and the CR cover-page row exists (unchanged CR sidecar rule). Zero annexes is a complete extraction.
- **CR cache hit (Proposed):** a cache hit also requires the derived archive `markdown/<cache_file>` to exist. Otherwise a normal parse re-downloads and rewrites the archive, because the archive is the only copy of CR main Markdown.
- `TDocExtractMeta.complete` (`models/tdoc_cr.py:306`) currently requires `document_count > 0`. That rule changes with the CR rule above.

Layering follows the existing rules. Domain values are ORM-free dataclasses in `models/`. Protocols are in `repository/protocols.py`. SQL and atomic replacement live in `storage/repositories/`. Services never touch ORM objects.

## 6. Extraction flow

Preparation performs no SQL writes. For one eligible TDoc:

1. Validate the TDoc ID and load its `tdocs` row. Require `ftp_url`. A missing URL raises `TDocNotYetOnFTPError` (skipped bucket).
2. Unless `--force` is given, return a cache hit when a complete SQL extraction exists for the canonical URL. For CR, the derived archive must also exist (section 5). No download happens on a hit.
3. Download or reuse the source ZIP with `download_tdoc_zip`, which caches it under `zips/<cache_file>` and enforces `tdoc_parse.max_tdoc_size_kb`.
4. List `.docx` members with `enumerate_docx_members` (`parsers/tdoc_zip.py:181`). Directories, `__MACOSX`, Word lock files, and non-`.docx` members are excluded. Nested ZIPs are not parsed. Enforce `tdoc_parse.max_documents_per_zip` and `tdoc_parse.max_uncompressed_size_kb` while reading. Violations go to the skipped bucket.
5. Select the main document from members whose basename starts with the TDoc number, compared case-insensitively:
   - Exactly one match: it is the main document.
   - Several matches: if exactly one equals `<tdoc_id>.docx` (case-insensitive), that member is main. Otherwise the archive fails.
   - No match: the archive fails with a "no main document" reason.
6. Convert the main document. Conversion failure fails the archive. Nothing is stored. For CR, the main document still feeds the existing cover, TTCN, and body-change parsers.
7. Convert each remaining `.docx` in path-sort order. A conversion failure logs a warning that names the source, adds the path to `skipped_sources`, and stores no content for it.
8. Build the Markdown archive with `build_markdown_archive` and install it atomically under `markdown/<cache_file>`. The archive holds every converted member, including the CR main document.
9. Return the prepared bundle: the parent row and the content rows. For CR, the content rows are the annexes only.

Persistence runs on the serialized writer in one transaction. It deletes existing rows for `ftp_url` from the content tables of this TDoc's type, inserts the stored rows, and upserts the parent (`cache_file`, `doc_filename`, `extraction_version = 2`, `document_count`, `skipped_sources`). The CR sidecars are persisted as today. After the commit, best-effort FTS and vector hooks run as they do for CR (`_index_after_parse`, `_embed_after_parse`).

Failure and safety rules:

- A main-document failure, a missing or ambiguous main document, a corrupt archive, or an identity error puts the TDoc in the **failed** bucket. Previously committed SQL content is kept.
- A failed forced reparse keeps the earlier successful SQL content.
- Reject absolute and traversing member paths and duplicate normalized identities. Never extract members to the filesystem.
- **Proposed:** a tolerant-annex result counts as success (`ok`). Parse output lists the skipped sources as warnings.
- A `skipped_sources` entry makes the extraction incomplete. The next normal parse retries it.
- **Direct-mode type detection.** `--from-url` and `--from-path` determine the TDoc type with the procedure below, instead of assuming CR. The CR parser is hard-wired with `tdoc_type="CR"` (`services/tdoc_cr_service.py:461`, `parsers/direct_extractor.py:572`).

### Direct-mode type detection

User-specified procedure for `tdoc parse --from-url` and `--from-path`. The determined type chooses the parse path.

1. Extract the TDoc ID from the file name, for example `R5-123456`.
2. Sync the meeting list for the TSG of that ID (`R5-123456` → `R5`), using `extract_tsg_from_tdoc_id_or_pattern` and `sync_tsg_internal` (`cli_auto_sync.py`).
3. Find the meeting whose range brackets the ID, using `resolve_meeting_id_for_tdoc_id`. The stored columns are `meetings.start_doc` and `meetings.end_doc` (`models/schema_info.py:129-130`).
4. Sync that meeting's TDoc list (`sync_meeting_internal`, or `TDocSyncCoordinator.sync_for_meeting_id`).
5. Read `tdocs.type` for the TDoc ID. If found, that is the type.
6. If not found, convert the file's main document to Markdown (inside a ZIP, the `.docx` whose name starts with the TDoc ID; for a standalone `.docx`, the file itself) and check the first non-empty lines:
   - WID family: a line containing `3GPP™ Work Item Description` within the first 20 non-empty lines.
   - LS family: a line matching `^\s*Title:\s+(Reply\s+){0,1}LS\s+on\s+` within the first 10 non-empty lines.
   - discussion: a line matching `^\s*Title:\s+Discussion\s+on\s+` within the first 10 non-empty lines.
   - CR: the structural CR-cover check (decision 12).
7. If the type is still unknown, drop the file. It is not stored or parsed, and it is reported as skipped with the reason "type could not be determined".

Notes:

- Steps 2–4 reuse the helpers that `--from-url` already runs through `trigger_auto_sync` when `sync.auto_sync` is on (`docs/cli.md:1439`). `--from-path` does not run them today. `AGENTS.md` says direct modes never trigger auto-sync, while `docs/cli.md:1439` says `--from-url` does. That contradiction must be resolved in the documentation update.
- Step 6 can identify a family, but a family write needs a `tdocs` row (foreign key). A type found only by sniffing is stored only when a `tdocs` row exists (decision 11).
- Once the type is known: CR goes to the CR parse; a family goes to decision 11; any other type, including `other`, is skipped with a reason.

## 7. Cache and persistence

- The derived Markdown archive is one ZIP at `<cache root>/markdown/<cache_file>`. It has one `.md` member per converted document (CR main, CR annexes, family main and annexes) plus a versioned manifest, as in the multi-file design. Readers select members explicitly.
- SQL is authoritative for every stored document **except the CR main document**. The CR main Markdown is only in the archive.
- Purging the cache does not change SQL content, FTS, or semantic results. It makes raw CR main Markdown unavailable until a re-download (`tdoc parse --tdoc <id> --force`, or a normal parse under the CR cache-hit rule in section 5).
- Auto-indexing runs after commit. An index failure does not invalidate stored content. `tdoc index` reports the gap. Rebuilds read from SQL without downloading.

## 8. Search and list

### Naming (Changed, Proposed)

- Markdown archive member names use `_sanitized_source_stem` (`parsers/tdoc_zip.py:234-238`: basename without `.docx`, characters outside `[A-Za-z0-9._-]` replaced by `_`, fallback `document`) for every type, CR and family alike. The archive member is `documents/<document_id>/<stem>.md`.
- Chunk IDs use one format for CR annexes and family documents (section 8, semantic).

### FTS5 (`tdoc_search`)

- **Changed:** the document-text column `document_text` is renamed `content_text`, in the FTS5 DDL, in `_SNIPPET_COLUMN_NAMES` (`settings/schema.py:436-446`), and in previews (`previews.content_text`). The nine positional BM25 weights are unchanged. The FTS table is recreated on layout change, so `tdoc index --rebuild` is needed after the rename.
- Document rows for a TDoc come from its content table: CR annexes from `tdoc_cr_annexes`, family members from the family table. The CR main document has no document row.
- `document_id`, `source_file` (= `doc_filename`), `extraction_version` (2), and `content_hash` come from the row.
- **Consequence (flagged for review):** a CR TDoc's main body is no longer full-text indexed. Its cover page, change blocks, and TTCN details are still indexed through `_cover_text`, `_change_text`, and `_ttcn_text`. Terms that appear only in the CR main body are not found.
- `_build_index_text` and index freshness read the union of the four content tables (`tdoc_cr_annexes`, `tdoc_wid_contents`, `tdoc_ls_contents`, `tdoc_discussion_contents`). `upsert(tdoc_id)` replaces the TDoc's metadata and document rows from SQL.
- Ranking, source grouping before pagination, `matched_documents`, and the nine BM25 weights follow the multi-file design, section 7.

### Semantic

- The existing TDoc-level metadata vector stays for all TDocs. Its chunks are `<tdoc_id>#<i>`, capped by `semantic_search.max_chunks_per_tdoc` (default 8).
- **Changed (Proposed):** content chunks cover every stored document of a TDoc: CR annexes, and family main and annexes. The CR main document gets no content chunks. Chunk ID format: `<tdoc_id>#<document_id>#<content_hash>#<chunk_index>`, where `chunk_index` is the position within that document.
- Content chunks are stored in the **existing** `vec_tdoc_embeddings` table (`chunk_id`, `tdoc_id`, `chunk_index`, `embedding`). No new chunk or vector table is added. Chunk text is re-derived from the stored Markdown at index time.
- Chunking reuses `semantic_search.chunk_size` (512 whitespace tokens) and `semantic_search.chunk_overlap` (24).
- Content chunks are capped by `semantic_search.max_content_chunks_per_tdoc`, default `16`, counted across the TDoc's content documents in order (main first for a family, path order for CR annexes).
- `vec_tdoc_embeddings` is replaced per TDoc by `upsert_chunks` (`storage/repositories/vector_sql.py:208`). Metadata and content chunks are built in one `index_for_tdoc` call. `upsert_chunks` accepts labelled chunks (chunk ID, chunk index, embedding).
- The existing `max_chunks_per_tdoc` (8) caps metadata chunks only. Applied to full documents, it would embed only the start of each TDoc.
- Hybrid search fuses TDoc-level metadata hits and chunk hits per TDoc with the existing reciprocal-rank fusion. Chunk hits populate `matched_documents` with the best chunk per document, and `best_chunk_id` points to the best chunk.
- Semantic-only results return TDoc rows. For TDocs with content chunks, `matched_documents` comes from chunk hits. A match on CR metadata alone leaves it empty.
- `tdoc index` reports chunk freshness by content hash. `--rebuild-embeddings` rebuilds chunk vectors from SQL without downloading.

### `tdoc list`

- The type filter already exists. Eligible type values are accepted in the same way.
- New output field `parsed` (boolean: a complete extraction exists, per section 5 for each type) and `document_count` (integer: stored content rows, or null when not parsed). **Proposed:** these apply to CR too.
- New filter `--parsed true|false` on CLI `tdoc list`, the web list, and MCP `list_tdocs`.
- Output follows the existing rules for table, JSON, and Markdown, including `--compact`.

## 9. Content retrieval and public surfaces

- `TDocContentService.get` resolves the parent by `tdoc_id` or exact `ftp_url`, then chooses the source by type.
- **Changed, CR:** documents are the CR main document, read from the archive member for `doc_filename` (`is_main` true), followed by the annexes from `tdoc_cr_annexes` in path order. If the archive is missing, or the main member cannot be read, the request fails with the existing cache-miss error and the re-parse hint (`tdoc parse --tdoc <id> --force`). It does not return a partial envelope. **Proposed.** Version-1 legacy rows keep the existing legacy cache path.
- **Family:** all rows from the family table, main first.
- Envelope (the multi-file shape, plus `skipped_sources`):

  ```json
  {"tdoc_id": "...", "ftp_url": "...", "extraction_version": 2, "complete": true,
   "skipped_sources": [],
   "documents": [{"document_id": "...", "source_file": "main.docx", "is_main": true, "markdown": "..."}]}
  ```

- Document order is main first, then the path sort key. `source_file` selects one member. An unknown source is a not-found error with no fallback.
- `complete` is false when `skipped_sources` is non-empty. Incomplete content follows the raw output contract below. It never falls back to a stale cache.
- CLI: `tdoc show --tdoc <id> --format raw [--source-file <path>]` uses the named-block rules for multiple documents. The error for an ineligible type names the type and lists the eligible types, replacing "only available for CR-type TDocs". On a missing extraction, the CLI may run extraction as it does for CR. HTTP and MCP reads never download.
- HTTP: `GET /tdocs/{tdoc_id}/content` (`source_file`, `format=json`) and `GET /tdocs/by-url/content?ftp_url=...`.
- MCP: `get_tdoc_content`, with the same selectors, formats, and JSON parity as HTTP. JSON uses the shared serializers.

### Raw output contract

The same renderer serves every text surface: `render_tdoc_documents_markdown(documents, include_source_headers=True)` in `services/tdoc_content_service.py:438`. The JSON envelope serializer is `tdoc_content_to_dict` (`:419`).

- **One document** (single main, or `--source-file`): its Markdown body, unchanged.
- **Several documents**: main first, then the other members in path order. Each block starts with `<!-- doc3gpp-source: <JSON string of source_file> -->` on its own line, followed by that body. Blocks are joined by line breaks. `>` in a source name is escaped to `\u003e`.
- **CLI** `tdoc show --tdoc <id> --format raw [--source-file <path>]`: writes that text to stdout or `--output`. Incomplete content prints a stderr warning.
- **HTTP** `GET /tdocs/{tdoc_id}/content?format=markdown` (and `/tdocs/by-url/content?ftp_url=...`): returns `text/markdown; charset=utf-8`. Incomplete content adds `X-Doc3gpp-Extraction-Complete: false`.
- **MCP** `get_tdoc_content` with `format=markdown` (default): returns the same text. Incomplete content raises an error with a re-parse hint (`web/mcp_server.py:502-512`). Use `format=json` to read the `complete` flag.
- **JSON** (HTTP `format=json`, MCP `format=json`, CLI envelope): the envelope above, with `complete` and `skipped_sources`.
- **Not a raw dump**: structured `tdoc show` metadata never includes bodies.

## 10. Migration and legacy data

The migration baseline is `main` (59273b5), the schema before any change on this branch. The intermediate schema from this branch (`tdoc_documents`, `document_text`) was never deployed and has no migration. Local databases created by that intermediate state are unsupported and must be recreated.

- `create_schema` creates `tdoc_cr_annexes`, `tdoc_wid_contents`, `tdoc_ls_contents`, and `tdoc_discussion_contents`. They start empty.
- `tdoc_extracts` already exists on `main`, with no version, count, or skipped-source columns. The branch's `_migrate_tdoc_extract_completeness` (`storage/db/migrate.py:488-514`) adds `extraction_version INTEGER NOT NULL DEFAULT 1` and `document_count INTEGER NOT NULL DEFAULT 0`. This design adds a nullable `skipped_sources TEXT` column to the same additions tuple. The step is idempotent and sqlite-aware. No rename and no backfill are needed.
- Pre-branch CR TDocs keep their cover, TTCN, and change rows, which stay valid. Their `tdoc_extracts` rows are version 1 with count 0, and they have no annex rows. Under the CR completeness rule (section 5) they are incomplete, so `parsed` is false and a normal `tdoc parse` (which uses `exclude_parsed`) selects each one once. The re-parse reuses the cached ZIP when present, so cached TDocs need no network. Each is converted once and becomes version 2. The per-run cap is `tdoc_parse.max_batch` (default 100), so a large backlog spans several runs.
- Until a pre-branch CR TDoc is re-parsed, raw content reads its version-1 legacy single-member cache through the existing legacy path, reported as incomplete. `tdoc list --parsed true` shows it as false until then.
- The FTS5 `tdoc_search` layout changes: `content_text` replaces the branch-only `document_text`, and `main` has no document column at all. The existing layout-mismatch path drops and recreates `tdoc_search`. Run `tdoc index --rebuild` after the upgrade. Content chunks for semantic search come from `tdoc index --rebuild-embeddings` or from a re-parse.
- The schema registry (`models/schema_info.py`) gains the new tables and columns. It has no `tdoc_documents` entries, because `main` never had that table.
- Family TDocs have no rows on `main`. Normal `tdoc parse` treats them as pending. `--force` reprocesses them.

## 11. Verification requirements (TDD)

Each implementation task demonstrates its regression first, then implements the minimum behavior, runs the relevant tests, and refactors.

Required coverage:

- Eligibility: `other` is never downloaded. Default selection is CR plus the families. Explicit `--type` intersects with eligibility. Web and MCP selection matches the CLI.
- Main selection: prefix matching is case-insensitive. Zero, multiple, and ambiguous cases are covered, including the exact-name tie-break.
- Tolerant annex: one annex fails, the others are stored, `skipped_sources` is set, the parent is incomplete, and the next normal parse retries.
- Path safety, resource limits, and a corrupt main document, which fails the archive with no writes.
- Atomic replacement, removed documents, cascade deletion, and parent cardinality.
- CR: the main Markdown is not written to SQL. CR annexes are stored with compressed content. A CR TDoc with zero annexes is complete. The CR cache-hit rule requires the archive.
- The Markdown archive contains every converted member and the manifest. Cache purge leaves SQL raw retrieval (annexes and family) and FTS rebuilds working.
- FTS: a term that appears only in an annex (CR or family) finds the parent and reports that annex in `matched_documents`. A term only in the CR main body is not found (documented consequence). Metadata-only parents remain searchable. The `content_text` column is populated.
- Semantic: a chunk hit finds annex text (CR and family). `--rebuild-embeddings` works with the cache purged. Hybrid carries `matched_documents`.
- `tdoc list`: the `parsed` and `document_count` fields and the `--parsed` filter, for CR and families.
- Content: JSON envelope, source selection, incomplete reporting, named raw blocks, CR main from the archive, cache-missing error, CLI, HTTP, and MCP parity, and no URL-read downloads.
- Migration: from the pre-branch baseline (`main`), `create_schema` creates the four content tables empty, `tdoc_extracts` gains a nullable `skipped_sources` column and the existing `extraction_version` and `document_count` defaults, pre-branch CR TDocs report `parsed=false` until re-parsed, and the `tdoc_search` layout is recreated. `create_schema` is idempotent. The intermediate-branch schema (`tdoc_documents`, `document_text`) is not migrated.
- Pipeline: ineligible types are classified as skipped. CR regressions are unchanged except the intended changes above.

Documentation updates in the same change set: `README.md`, `AGENTS.md`, `docs/architecture.md`, `docs/cli.md`, `docs/web-server.md`, `docs/conventions.md`, `docs/code-map.md`, `docs/known-constraints.md` (remove "non-CR types are not handled"), the schema registry, and the TOML example. The text that says `tdoc_documents` owns inner-file content must be rewritten for `tdoc_cr_annexes` and the family tables. Settings follow the TOML-only policy.

## 12. Decisions for review

Decisions 1–13 are from the approved version. Decisions 14–24 are new in this revision.

1. `content_hash` column on each content table, for freshness checks. (Approved version, kept.)
2. Reuse `tdoc_extracts` as the parent, plus a new `skipped_sources` column. (Kept.)
3. A tolerant-annex result counts as `ok` with warnings. Partial extractions are retried by normal parsing. (Kept, Proposed.)
4. Zero or ambiguous main documents go to the failed bucket. Exact `<tdoc_id>.docx` is the tie-break. (Kept.)
5. Ineligible types go to the skipped bucket. (Kept, Proposed.)
6. Direct modes (`--from-path`, `--from-url`) use the type-detection procedure in section 6. Family types found there depend on decision 11. (Kept.)
7. `tdoc list` gains `parsed`, `document_count`, and `--parsed`, and applies to CR too. (Kept, Proposed.)
8. Content chunks reuse `chunk_size` and `chunk_overlap`, and are stored in `vec_tdoc_embeddings`. The per-TDoc content cap is `semantic_search.max_content_chunks_per_tdoc`, default `16` (confirmed). (Kept, extended by 18.)
9. For TDocs with content chunks, semantic results carry chunk-based `matched_documents`. (Kept, Proposed.)
10. Direct-mode type detection follows the seven-step procedure in section 6. The CR parse runs only when the determined type is CR. (Kept.)
11. **Family type in direct mode (confirmed).** Store the family content through the family path. Persist it when a `tdocs` row exists, and keep the result in memory otherwise. (Kept.)
12. **CR fallback (confirmed).** Step 6 also runs the structural CR-cover check, so a CR missing from `tdocs` parses in memory. (Kept.)
13. **Sync gating (confirmed).** Steps 2–4 run only when `sync.auto_sync` is on, with the existing skip rules. When it is off, direct modes use the local DB and content sniffing. (Kept.)
14. **CR main Markdown is not stored in SQL (confirmed by the user).** Its key details (cover page, change blocks, TTCN details) are stored and searchable. The full Markdown comes from the derived cache archive. (Changed.)
15. **CR annexes are stored in SQL with file name and full Markdown (confirmed by the user).** They are FTS5-searchable and semantically indexed. (Changed.)
16. **Compressed `content` in all four content tables (confirmed by the user).** CR annexes are re-encoded from plain `markdown_text` by the migration. (Changed.)
17. **Renames (requested by the user).** `tdoc_documents` becomes `tdoc_cr_annexes`. FTS column `document_text` becomes `content_text`, with previews `previews.content_text`. The positional BM25 weights are unchanged. (Changed.)
18. **CR annexes get content chunks in the semantic index (Proposed, please confirm).** The user's request to make their content searchable is read as covering semantic search. The CR main gets metadata chunks only. (Changed.)
19. **Shared naming (requested by the user, Proposed details).** Archive names use `_sanitized_source_stem` for every type. Chunk IDs use one format for CR annexes and families. (Changed.)
20. **Common content shape (Proposed).** Each content table stores `document_id` and `content_hash`, and not `file_order` or `markdown_file`. The read path derives order and archive names. (Changed.)
21. **`document_count` and completeness (Proposed).** `document_count` is the number of stored content rows. CR completeness replaces the main-row rule with an annex-count rule, and zero annexes is complete. `TDocExtractMeta.complete` changes to match. (Changed.)
22. **CR cache-hit requires the archive (Proposed).** Without the archive, raw CR main Markdown cannot be served, so a parse re-downloads. (Changed.)
23. **CR raw content (Proposed).** Raw CR documents combine the archive main with SQL annexes. A missing archive gives the cache-miss error with a re-parse hint, not a partial envelope. (Changed.)
24. **Full-text consequence (flagged for review).** A CR main body's text is no longer full-text indexed. Only its cover, change, and TTCN fields are. This reduces CR search coverage and is the user's decision, but it must be accepted explicitly. (Changed.)
25. **Migration (Proposed).** In place: rename the table, delete CR main rows, re-encode annex text, recompute `document_count`. The alternative (rebuild from the archive or re-download) is listed in section 10. (Changed.)

## 13. Review and handoff

Review focus: decisions 14–25, especially the CR main-document rule (14), CR annex searchability and indexing (15, 18), the CR full-text consequence (24), the completeness and cache-hit rules (21, 22), and the migration (25). Decisions 3, 4, 9, 10–13 carry over from the approved version.

The implementation plan `docs/superpowers/plans/2026-10-22-tdoc-type-contents.md` predates this revision. It must be revised before execution: it still uses `tdoc_documents` (Task 3 deletes family-URL rows from it; Task 9 unions it), it still uses the `PurePosixPath(...).stem` naming (must become `_sanitized_source_stem`), it stores `file_order` and `markdown_file` for family rows, and it still uses `document_text`. Its decisions P1–P8 still apply, with P2 extended to CR annexes and P6 reworked for the CR completeness rule. P7 (direct modes) is unchanged and still needs the user's review.

Do not auto-commit the spec, plan, or implementation. Repository policy requires a separate user request to commit.
