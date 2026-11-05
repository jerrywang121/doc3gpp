# Presentation Search Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose exact source paths and matching presentation chunks in specification and TDoc searches across CLI, web, REST, and MCP.

**Architecture:** Keep the existing spec chunk store and add its source path to the shared result DTO. Preserve TDoc's whole-document FTS ranking, adding a separate FTS presentation-chunk sidecar and an atomic vector chunk-text sidecar; merge source matches into the existing grouped TDoc result. Both sidecars have explicit freshness checks and are rebuilt from stored SQL content.

**Tech Stack:** Python 3.10+, SQLAlchemy 2, SQLite FTS5, optional sqlite-vec, Typer, FastAPI/Jinja2, MCP, pytest.

**Spec:** `docs/superpowers/specs/2026-11-04-presentation-search-provenance-design.md`

## Global Constraints

- Work on the existing `feat/mcp-skill-spec-annexes` branch. Preserve all pre-existing uncommitted changes, including `.superpowers/sdd/.gitignore`, `AGENTS.md`, the skill, and MCP tests.
- Do not commit or push: `docs/conventions.md` says this project does not auto-commit. Dispatch sequential implementer/reviewer agents with the same constraint.
- Use TDD: write each behavior test, run it to observe the expected failure, implement, then run it and neighboring tests. Do not rely on a passing test written after the change.
- FTS presentation indexing covers all chunks; semantic indexing keeps `max_content_chunks_per_tdoc` (default 16, shared across content documents). Vector ranking/fan-out remains unchanged.
- Preserve existing TDoc parent FTS ranking, result limits, filters, source identity, CR-main exclusion, and spec chunk semantics. Never return unverified chunk text.
- Source paths are stored case-preserving ZIP member paths. JSON must agree across compact CLI, REST, and MCP; HTML must escape paths and text.
- SQLite schema changes are additive; upgrade/rebuild from stored SQL content, without ZIP downloads, reparsing, or Markdown cache. Semantic rebuild may call the configured embedding service.

## Review Focus

1. A spec ID such as `38.331` is normalized for FTS but its returned chunk text must remain the original text: pin this in Task 2.
2. A presentation with zero searchable tokens must become current after indexing rather than remaining stale forever: pin this in Task 2.
3. Two immutable ZIP URLs containing the same member path must remain distinct in hybrid results: pin this in Task 4.
4. An old vector ID with unchanged content hash but changed chunk size must never resolve to the wrong text: pin this in Task 3.
5. A chunk or source containing `<script>` must be escaped in HTML while JSON preserves it literally: pin this in Task 5.

## File map and shared interfaces

- `models/spec_doc.py`, `models/unified_search.py`, spec search SQL/facades: carry spec chunk `source_file` end-to-end.
- `models/search.py`: define `MatchedChunk(chunk_id: str, chunk_index: int, text: str)` and append `matched_chunk: MatchedChunk | None = None` to `MatchedDocument`.
- `storage/db/migrate.py` and new `storage/repositories/tdoc_presentation_search_sql.py`: additive FTS chunk and freshness-ledger tables. `search_sql.py` integrates atomic write, read and staleness behavior.
- `models/semantic_search.py`, `repository/protocols.py`, `services/semantic_search_service.py`, and `storage/repositories/vector_sql.py`: store and batch-resolve exact vector chunk text with indexing-time provenance.
- `services/tdoc_search_facade.py` and `models/unified_search.py`: merge document matches and serialize the new nested chunk without changing the parent contract.
- Existing CLI and web renderers/templates expose the shared DTO; existing documentation and bundled skill describe the behavior and upgrades.

Use the existing content chunk ID `<tdoc_id>#<document_id>#<content_hash>#<index>`. Define the chunking fingerprint `semantic-whitespace-v1:<size>:<overlap>` in `models/semantic_chunking.py` so FTS and vector writes agree. Display the output of the existing whitespace-splitting `_chunks` helper, not FTS-normalized text; its size is a whitespace-token bound, not a byte bound. `matched_chunk` is always present in a serialized matched-document object, with either the object defined in the spec or `null`.

---

### Task 1: Specification search source paths

**Files:**
- Modify: `src/doc3gpp/models/spec_doc.py`, `src/doc3gpp/models/unified_search.py`
- Modify: `src/doc3gpp/storage/repositories/spec_doc_search_sql.py`, `src/doc3gpp/services/spec_doc_semantic_service.py`, `src/doc3gpp/services/spec_doc_search_facade.py`
- Test: `tests/integration/test_spec_presentation_parse.py`, `tests/integration/test_spec_doc_search_repo.py`, `tests/integration/test_spec_doc_search_service.py`, `tests/unit/test_spec_doc_search_facade.py`, `tests/unit/test_unified_search_models.py`

**Interfaces:** Extend `SpecDocHit` and `SpecDocSearchResult` with `source_file: str`; `spec_doc_search_result_to_dict(result: SpecDocSearchResult) -> dict[str, Any]` emits `source_file` after `release`. Existing spec chunk repository methods continue to return `SpecDocChunk.source_file`.

- [ ] **Step 1: Write failing tests.** Extend `test_mixed_zip_persists_full_presentation_and_indexes_its_text` to assert `slide_hits[0].source_file == "slides.pptx"`, `"slideonlysignal" in slide_hits[0].text`, and `len(presentation_chunks) > 1`. Add `test_spec_search_source_file_in_every_mode`, parameterized over FTS, semantic, hybrid, and filter-only with `Annexes/Radio Slides.PPTX` and `docs/Body.docx`; assert each returned result and serialized row carries the correct exact source and unchanged chunk text. Assert source retrieval does not invoke fetch/reparse/rebuild.
- [ ] **Step 2: Observe RED.** Run `.venv/bin/python -m pytest tests/integration/test_spec_presentation_parse.py tests/unit/test_spec_doc_search_facade.py tests/unit/test_unified_search_models.py -q`; the added assertions must fail because `source_file` is absent, not because setup fails.
- [ ] **Step 3: Implement propagation.** Select `c.source_file` from the existing spec chunk join, hydrate vector-only `SpecDocHit` from `SpecDocChunk`, propagate in every facade mode, and serialize the field. Preserve callers that construct DTOs positionally by appending defaulted fields where necessary; populate real stored rows explicitly.
- [ ] **Step 4: Verify.** Run `.venv/bin/python -m pytest tests/integration/test_spec_presentation_parse.py tests/integration/test_spec_doc_search_repo.py tests/integration/test_spec_doc_search_service.py tests/unit/test_spec_doc_search_facade.py tests/unit/test_unified_search_models.py -q`; expect all pass.

### Task 2: TDoc presentation FTS chunks

**Files:**
- Modify: `src/doc3gpp/models/semantic_chunking.py`, `src/doc3gpp/models/search.py`, `src/doc3gpp/storage/db/migrate.py`, `src/doc3gpp/storage/repositories/search_sql.py`
- Create: `src/doc3gpp/storage/repositories/tdoc_presentation_search_sql.py`
- Test: `tests/integration/test_tdoc_content_search_sqlite.py`, `tests/integration/test_tdoc_multifile_search_sqlite.py`, `tests/integration/test_search_index_lifecycle.py`, `tests/integration/test_search_schema.py`, `tests/unit/test_semantic_chunking.py`

**Interfaces:**
- `chunking_fingerprint(size: int, overlap: int) -> str` returns `semantic-whitespace-v1:<size>:<overlap>`.
- `MatchedChunk` and `MatchedDocument.matched_chunk` are as defined above. `SQLAlchemySearchIndexRepository.search(query: str, filters: SearchFilters, snippet_tokens: int | None = None) -> list[SearchHit]` keeps its signature and adds chunks to presentation matches.
- The new repository module operates on the caller's existing SQLAlchemy `Connection`; it must not start a second transaction for writes. FTS rows carry the stable chunk ID, TDoc/document/source identity, content hash, chunk index, fingerprint, normalized indexed content, and original unnormalized text. A per-document ledger records the fingerprint and expected chunk count, including zero.

- [ ] **Step 1: Write failing SQLite tests.** Reuse the family/CR helpers in `test_tdoc_content_search_sqlite.py`. Add `test_presentation_fts_finds_late_chunk` for `Annexes/Radio.PPTX` and `legacy/Deck.ppt` with `chunk_size=4`, `chunk_overlap=0`, and more than 16 chunks; assert the last term's exact source, full chunk text and index. In `test_presentation_fts_preserves_spec_id_text`, assert `38.331` is found and `"38.331" in chunk.text` and `"38_331" not in chunk.text`. Add `test_presentation_fts_cross_chunk_and_keeps_parent` with `alpha AND omega` in different non-overlapping chunks, asserting the parent match and `matched_chunk is None`; assert metadata-only matches have no documents and ranking/parent limit are unchanged. Add lifecycle tests now, before implementation: `test_presentation_fts_empty_is_current`, `test_presentation_fts_replace_rolls_back_both_indexes`, `test_presentation_fts_remove_and_stale_rebuild`, `test_presentation_fts_detects_chunk_setting_change`, `test_presentation_fts_stale_entry_returns_null`, and `test_presentation_fts_schema_is_additive_and_idempotent`. Assert exact sidecar/parent contents before and after each operation, stale/missing sidecars return no chunk, and stale-only selects the affected parent.
- [ ] **Step 2: Observe RED.** Run `.venv/bin/python -m pytest tests/integration/test_tdoc_content_search_sqlite.py tests/integration/test_search_index_lifecycle.py tests/integration/test_search_schema.py -k presentation -q`; expect the new tests to run and fail on missing `matched_chunk`, sidecar, or staleness behavior.
- [ ] **Step 3: Implement the sidecar.** Add idempotent FTS5-gated schema without altering `tdoc_search`. Give the chunk FTS table the same searchable column names as the existing table, with only `content_text` populated, so existing MATCH expressions/column qualifiers are valid. Split original decoded Markdown with the existing `_chunks`, store `normalize_query(chunk)` for MATCH and the original chunk for output. Use the existing grouped-document matches and select one matching chunk per document with FTS5 `bm25`, then chunk index/ID tie-breaks. Batch the lookup for selected documents.
- [ ] **Step 4: Integrate lifecycle.** In the existing upsert/remove transaction, replace corresponding sidecar and ledger rows; keep source URL identity through `document_id`. Extend stale-only selection and status to compare source hash, expected chunk count/IDs and fingerprint, including empty presentations. Chunk lookup must validate that same provenance and return null for stale or incomplete entries. A missing sidecar must leave existing whole-document search usable with null chunks and mark the index stale; do not suppress unrelated FTS corruption. Keep logical parent row counts unchanged.
- [ ] **Step 5: Verify.** Run `.venv/bin/python -m pytest tests/integration/test_tdoc_content_search_sqlite.py tests/integration/test_tdoc_multifile_search_sqlite.py tests/integration/test_search_index_lifecycle.py tests/integration/test_search_schema.py tests/unit/test_semantic_chunking.py -q`; expect all tests, including the lifecycle tests written in Step 1, to pass.

### Task 3: Exact vector chunk provenance

**Files:**
- Modify: `src/doc3gpp/models/semantic_search.py`, `src/doc3gpp/repository/protocols.py`, `src/doc3gpp/services/semantic_search_service.py`, `src/doc3gpp/storage/repositories/vector_sql.py`, `src/doc3gpp/storage/db/migrate.py`
- Test: `tests/integration/test_tdoc_content_semantic_sqlite.py`, `tests/integration/test_vector_index_lifecycle.py`, `tests/integration/test_stale_only_keeps_index_sqlite.py`, `tests/unit/test_semantic_search_service.py`

**Interfaces:**
- Append defaulted `text: str | None = None` and `chunking_fingerprint: str | None = None` to `EmbeddedChunk`; content entries carry the precise text passed to the embedder, metadata entries carry neither.
- Add `VectorIndexRepository.matched_chunk_texts(chunk_ids: Collection[str]) -> dict[str, MatchedChunk]` and the matching SQL implementation. It returns only rows whose stored fingerprint, document content hash, and current vector identity are verified; absent/unverifiable IDs are omitted. `SemanticSearchService.matched_chunk_texts` delegates to this batch method.

- [ ] **Step 1: Write failing tests.** Seed a `.pptx` content document and deterministic fake embeddings as in `test_tdoc_content_semantic_sqlite.py`; `test_presentation_vector_returns_exact_embedded_chunk` asserts `matched.chunk_id == embedded.chunk_id`, `matched.chunk_index == embedded.chunk_index` and `matched.text == text_passed_to_encode`. `test_presentation_vector_does_not_require_fts_sidecar` removes the FTS sidecar and still resolves the vector chunk. `test_presentation_vector_changed_chunk_size_is_stale` indexes once, changes `chunk_size` without rebuilding while keeping the source hash unchanged, asserts old chunk lookup is absent and status is stale, then rebuilds and asserts the new text is correct. Add `test_presentation_vector_legacy_entry_is_unverifiable`, `test_presentation_vector_replace_rolls_back_both_stores`, `test_presentation_vector_remove_and_stale_rebuild`, and `test_presentation_vector_shared_cap`: verify missing snapshot returns no chunk, rollback/remove keep stores consistent, stale-only selects the parent, and the default cap permits at most 16 content chunks across ordered documents.
- [ ] **Step 2: Observe RED.** Run `.venv/bin/python -m pytest tests/integration/test_tdoc_content_semantic_sqlite.py tests/integration/test_vector_index_lifecycle.py tests/integration/test_stale_only_keeps_index_sqlite.py -k presentation -q`; expect the named tests to run and fail on missing snapshot lookup, sidecar or freshness behavior.
- [ ] **Step 3: Implement storage.** Create an additive vector chunk-text sidecar keyed by chunk ID, with TDoc/document/content identity, index, fingerprint, and original embedded text. Populate it in `upsert_chunks` and `upsert_chunks_many` in the same transaction as vector rows; delete it in replacement/removal paths. Batch lookups within SQLite parameter limits and verify against current SQL content and current fingerprint. Never fabricate text for legacy vectors, missing sidecars, replaced content, or changed settings.
- [ ] **Step 4: Integrate freshness.** Update vector `status`, stale-only selection and planned expected chunk IDs/counts to detect missing provenance, altered chunk settings, content changes, and changed cap. Full and stale-only vector rebuilds use stored SQL Markdown and the configured embedder. A failed batch must not leave vector and text rows from different generations.
- [ ] **Step 5: Verify.** Run `.venv/bin/python -m pytest tests/integration/test_tdoc_content_semantic_sqlite.py tests/integration/test_vector_index_lifecycle.py tests/integration/test_stale_only_keeps_index_sqlite.py tests/unit/test_semantic_search_service.py -q`; expect all pass, including added rollback, remove, stale-only, and legacy-entry tests.

### Task 4: Merge TDoc matches and serialize chunks

**Files:**
- Modify: `src/doc3gpp/services/tdoc_search_facade.py`, `src/doc3gpp/models/unified_search.py`
- Test: `tests/unit/test_tdoc_search_facade.py`, `tests/unit/test_unified_search_models.py`, `tests/integration/test_tdoc_content_semantic_sqlite.py`

**Interfaces:** Consume `MatchedDocument.matched_chunk` from Task 2 and `SemanticDocumentHit.matched_chunk` from Task 3. Semantic candidates carry chunk text verified in the same SQLite read snapshot as KNN; do not resolve a scored semantic chunk by ID in a later read. `tdoc_search_result_to_dict(result: TDocSearchResult) -> dict[str, Any]` appends `matched_chunk` to each `matched_documents` object.

- [ ] **Step 1: Write failing tests.** Add `test_hybrid_unions_distinct_fts_and_vector_presentations`: build an FTS hit for `Annexes/Radio.PPTX` and a vector hit for `legacy/Deck.ppt`, asserting both exactly once. In `test_hybrid_same_document_preserves_preview_and_best_chunk`, assert a verified vector chunk is selected and FTS previews retained; if the vector snapshot is unavailable, use a verified FTS chunk instead. `test_hybrid_same_member_in_different_zip_urls` must retain both identities. `test_semantic_unverified_chunk_serializes_null` asserts no guessed text; filter-only and metadata-only hits have `matched_documents == ()`. Assert JSON has either `{"chunk_id": ..., "chunk_index": ..., "text": ...}` or `null`.
- [ ] **Step 2: Observe RED.** Run `.venv/bin/python -m pytest tests/unit/test_tdoc_search_facade.py tests/unit/test_unified_search_models.py -q`; expect the new merge/serialization assertions to fail.
- [ ] **Step 3: Implement.** Use each semantic hit's same-snapshot `matched_chunk`; never perform a later ID-only lookup to decorate a scored KNN result. Union document matches by `document_id` (which includes ZIP URL), choose a verified vector chunk over an FTS chunk for the same document and otherwise use the FTS chunk, retain FTS previews, and sort vector hits by `(distance, document_id)` followed by FTS-only hits in their existing FTS order. Keep existing parent-level reciprocal-rank score, ordering, and limit. Ensure metadata-only and filter-only results do not acquire a source.
- [ ] **Step 4: Verify.** Run `.venv/bin/python -m pytest tests/unit/test_tdoc_search_facade.py tests/unit/test_unified_search_models.py tests/integration/test_tdoc_content_semantic_sqlite.py -q`; expect all pass.

### Task 5: Surface parity and documentation

**Files:**
- Modify: `src/doc3gpp/cli.py`, `src/doc3gpp/settings/schema.py`, `src/doc3gpp/data/doc3gpp.toml.example`, `src/doc3gpp/web/templates/partials/search_results_table.html`, `src/doc3gpp/web/templates/partials/spec_doc_search_results_table.html`, and the search routes/MCP only if needed to pass the shared DTO through.
- Modify: `src/doc3gpp/data/skills/doc3gpp/SKILL.md`, `README.md`, `AGENTS.md`, `docs/cli.md`, `docs/web-server.md`; preserve their existing edits.
- Test: `tests/unit/test_cli_search.py`, `tests/integration/test_web_search_end_to_end.py`, `tests/integration/test_mcp_end_to_end.py`, `tests/integration/test_spec_presentation_parse.py`

**Interfaces:** Consume the shared serialized result from Tasks 1 and 4; do not add a separate REST or MCP response shape. Existing CLI options and HTTP/MCP tool signatures stay unchanged.

- [ ] **Step 1: Write failing surface tests.** Add `test_search_presentation_source_and_chunk_cli_formats` for both corpora and CLI table, Markdown, compact Markdown and compact JSON; assert the source path and full returned chunk text are visible with default fields. Assert `--fields source_file` works for spec searches and an explicit field selection omitting it remains honored. `test_search_presentation_cli_rest_mcp_parity` compares parsed compact CLI, REST, and MCP JSON for identical source/ID/text/null fields and ordering; REST and MCP bytes must also match exactly. `test_search_presentation_html_escapes_path_and_chunk` uses `Annexes/<script>.PPTX` and `<script>alert(1)</script>`: assert `&lt;script&gt;` is present and the injected executable element absent. Include real generated `.pptx` content and the existing legacy `.ppt` conversion test boundary; no external LibreOffice process is needed for the latter.
- [ ] **Step 2: Observe RED.** Run `.venv/bin/python -m pytest tests/unit/test_cli_search.py tests/integration/test_web_search_end_to_end.py tests/integration/test_mcp_end_to_end.py -k 'search and (source or chunk or presentation)' -q`; verify selected tests actually run and fail on missing displayed source/chunk.
- [ ] **Step 3: Render and document.** Add `source_file` to `SPEC_DOC_SEARCH_FIELDS`, `OutputFieldsSettings.spec_doc` defaults, and the commented example TOML defaults; retain explicit `--fields` and custom configuration behavior. Extend `_render_unified_tdoc_results` and the search templates to display the source and matched text, reusing shared DTO serializers and autoescaping. In `docs/web-server.md` and the packaged skill, replace any stale claim that semantic-only `matched_documents` is always empty; describe source paths, the `matched_chunk` object-or-null contract, hybrid union/fallback, null chunks for whole-document FTS expressions not satisfied by one chunk, and the shared semantic cap. Document FTS coverage, the shared semantic cap, null chunk cases, legacy vector rebuild, `doc3gpp db init --scope main`, `doc3gpp tdoc index --rebuild`, and `--rebuild-embeddings`; note presentations do not add OCR or spec TOC metadata. Keep `get_skill` reading the edited skill verbatim.
- [ ] **Step 4: Verify focused surfaces.** Run `.venv/bin/python -m pytest tests/unit/test_cli_search.py tests/integration/test_web_search_end_to_end.py tests/integration/test_mcp_end_to_end.py tests/integration/test_spec_presentation_parse.py -q`; expect all pass.
- [ ] **Step 5: Verify the entire branch.** Run `.venv/bin/python -m ruff check .` and `PATH="$PWD/.venv/bin:$PATH" ./scripts/test_sqlite.sh -n 0`; record exact results, including any pre-existing failures. Review `git diff` and `git status --short` without discarding user edits. Do not commit.
