# Unified Search and Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the split TDoc/spec-document search and index interfaces with resource-scoped unified search/index APIs across the CLI, web/REST, and MCP surfaces.

**Architecture:** Keep the existing FTS5 and vector services as low-level ranking/indexing components. Add one facade per searchable resource to select `fts5`, `semantic`, `hybrid`, or `filter` mode and convert all modes into a flattened immutable result DTO. Add resource index coordinators that compose the existing FTS5 rebuilds with vector rebuilds, then make CLI, HTTP, and MCP adapters depend on those facades instead of duplicating dispatch and serialization logic.

**Tech Stack:** Python 3.10+, Typer, FastAPI/Starlette, MCP SDK, SQLAlchemy 2, SQLite FTS5, sqlite-vec, Pydantic v2, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-26-unified-search-index-design.md`

## Global Constraints

- The public migration is replacement-only: no aliases, redirects, or deprecation shims for removed commands, routes, or MCP tools.
- `text` only selects `fts5`; `semantic` only selects `semantic`; both select `hybrid`; neither selects `filter`. Empty and whitespace-only inputs are absent.
- Hybrid weight is read only from `Settings.semantic_search.fts5_weight`; no CLI, HTTP, or MCP request accepts `fts5_weight`.
- Public search rows are flattened and contain one `score`, one `search_mode`, and `previews`; public `rank_*`, nested `hit`, `rrf_score`, and `min_chunk_distance` fields are removed.
- `previews` is an object for FTS5 and JSON `null` for semantic, hybrid, and filter modes.
- Result array order is final rank: FTS5 order, vector-distance order, RRF order, or deterministic filter order.
- TDoc public results include `type`, `status`, and `best_chunk_id`; spec-document public results include all chunk metadata fields.
- Field-only search must use source SQL and must not initialize or invoke FTS5 or embeddings.
- HTTP JSON and MCP JSON remain bare arrays for search results and use the existing compact `ensure_ascii=False` serialization parity.
- Background index jobs must not perform synchronous rebuild work in request handlers.
- Use test-first development for every behavior change: write a failing test, run it, implement the smallest fix, then run the focused test again.
- Do not commit changes unless the user explicitly requests a commit.

---

## File Map

Create these focused modules:

- `src/doc3gpp/models/unified_search.py` — public search mode enum, flattened TDoc/spec-doc result DTOs, and cross-surface result dictionaries.
- `src/doc3gpp/services/tdoc_search_facade.py` — TDoc four-mode dispatch, metadata conversion, and filter-only mapping.
- `src/doc3gpp/services/spec_doc_search_facade.py` — spec-document four-mode dispatch, vector-only metadata completion, and filter-only mapping.
- `src/doc3gpp/services/index_service.py` — shared index status/action DTOs and validation helpers without leaking ORM details.
- `src/doc3gpp/services/tdoc_index_service.py` — TDoc FTS5/vector status and rebuild orchestration.
- `src/doc3gpp/services/spec_doc_index_service.py` — spec-document FTS5/vector status and rebuild orchestration.
- `tests/unit/test_unified_search_models.py` — exact public result field and serialization contract.
- `tests/unit/test_tdoc_search_facade.py` — TDoc mode selection and score/previews behavior.
- `tests/unit/test_spec_doc_search_facade.py` — spec-doc mode selection and vector metadata behavior.
- `tests/unit/test_index_services.py` — action validation/status/rebuild coordination.

Modify these existing modules:

- Domain/protocol/storage: `src/doc3gpp/models/search.py`, `src/doc3gpp/models/spec_doc.py`, `src/doc3gpp/repository/protocols.py`, `src/doc3gpp/storage/repositories/search_sql.py`, `tdoc_sql.py`, `vector_sql.py`, `spec_doc_sql.py`, `spec_doc_search_sql.py`, `spec_doc_vector_sql.py`.
- Services/factory/state: `src/doc3gpp/services/search_service.py`, `semantic_search_service.py`, `spec_doc_search_service.py`, `spec_doc_semantic_service.py`, `factory.py`, `src/doc3gpp/web/state.py`, `web/deps.py`, `web/app.py`.
- CLI: `src/doc3gpp/cli.py` and CLI-focused tests under `tests/unit/test_cli_search.py`, `test_cli_search_sem.py`, `test_spec_doc_cli.py`, and integration search tests.
- Web: `src/doc3gpp/web/routes/search.py`, `routes/spec_docs.py`, `routes/jobs.py`, `web/workers/handlers.py`, `models/jobs.py`, `web/mcp_server.py`, templates under `src/doc3gpp/web/templates/`, and search/sync JavaScript.
- Documentation: `README.md`, `AGENTS.md`, `docs/cli.md`, `docs/architecture.md`, `docs/web-server.md`, `docs/code-map.md`, current API examples, and the new spec references.

The historical design documents remain unchanged; current-contract documentation must be updated.

---

### Task 1: Define the Flattened Public Result Contract

**Files:**
- Create: `src/doc3gpp/models/unified_search.py`
- Create: `tests/unit/test_unified_search_models.py`
- Modify: `src/doc3gpp/models/search.py` only if a shared search mode type must be re-exported
- Modify: `src/doc3gpp/models/spec_doc.py` only for imports/re-exports, not internal legacy ranking DTOs

**Interfaces:**
- Produces `SearchMode(str, Enum)` with values `fts5`, `semantic`, `hybrid`, and `filter`.
- Produces frozen, slotted `TDocSearchResult` with fields in this order: `tdoc_id`, `score`, `search_mode`, `previews`, `title`, `meeting`, `tsg`, `uploaded_date`, `ftp_url`, `wis`, `type`, `status`, `best_chunk_id`.
- Produces frozen, slotted `SpecDocSearchResult` with fields in this order: `chunk_id`, `score`, `search_mode`, `previews`, `spec_id`, `version`, `release`, `sections`, `tables`, `chunk_index`, `text`.
- Produces `tdoc_search_result_to_dict(result)` and `spec_doc_search_result_to_dict(result)` that preserve field order, convert `SearchMode` to its string value, serialize dates/datetimes with `isoformat()`, and preserve `None`.
- No public result dictionary contains `rank_fts5`, `rank_vec`, `rrf_score`, `min_chunk_distance`, or nested `hit`.

- [ ] **Step 1: Write the failing tests**

Add tests that construct each DTO and assert exact dictionary keys and values. Include one result per mode and assert:

```python
def test_tdoc_result_has_one_score_and_mode_specific_previews():
    result = TDocSearchResult(
        tdoc_id="R1-25-0001",
        score=None,
        search_mode=SearchMode.FILTER,
        previews=None,
        title="Title",
        meeting="RAN1#120",
        tsg="RAN",
        uploaded_date="2025-01-10",
        ftp_url="ftp/path.zip",
        wis="38.331",
        type="CR",
        status="Agreed",
        best_chunk_id=None,
    )
    assert list(tdoc_search_result_to_dict(result)) == [
        "tdoc_id", "score", "search_mode", "previews", "title", "meeting",
        "tsg", "uploaded_date", "ftp_url", "wis", "type", "status",
        "best_chunk_id",
    ]
    assert tdoc_search_result_to_dict(result)["previews"] is None
```

Add the equivalent spec-document key-order assertion and an explicit test that a date/datetime field is serialized to ISO text.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```text
python -m pytest tests/unit/test_unified_search_models.py -q
```

Expected: collection or assertion failures because the module, enum, DTOs, and serializers do not exist.

- [ ] **Step 3: Implement the DTOs and serializers**

Use frozen/slotted dataclasses. Keep `previews` typed as `dict[str, str] | None`. Use a private scalar helper for `date`, `datetime`, and `None`; do not serialize `None` as an empty object or empty string. Use `search_mode.value` in dictionaries.

- [ ] **Step 4: Run focused tests**

Run the same pytest command and expect all tests to pass.

- [ ] **Step 5: Check the diff**

Run `git diff --check`. Do not commit.

---

### Task 2: Complete TDoc Metadata and Source-Filter Repository Paths

**Files:**
- Modify: `src/doc3gpp/models/search.py`
- Modify: `src/doc3gpp/repository/protocols.py`
- Modify: `src/doc3gpp/storage/repositories/search_sql.py`
- Modify: `src/doc3gpp/storage/repositories/vector_sql.py`
- Modify: `src/doc3gpp/storage/repositories/tdoc_sql.py`
- Modify: `tests/integration/test_search_filters.py`
- Modify: `tests/integration/test_search_sem_end_to_end.py`
- Modify: `tests/integration/test_tdoc_sqlite.py`

**Interfaces:**
- `SearchHit` gains `type: str | None` and `status: str | None`; its internal FTS5 score field remains `score` until facade conversion.
- `TDocMeta` gains `type` and `status`.
- `SearchIndexRepository.get_tdocs_metadata()` returns the new fields for vector-only metadata.
- `TDocRepository.list_for_search(filters: SearchFilters) -> list[TDocWithMeeting]` performs direct SQL filtering with the same search filter grammar and deterministic `tdoc_id` ordering.

- [ ] **Step 1: Write failing metadata tests**

Extend the integration fixture to seed a TDoc with `type="CR"` and `status="Agreed"`. Assert both `SQLAlchemySearchIndexRepository.search()` and `get_tdocs_metadata()` return those fields. Add a vector-only semantic fixture assertion that the metadata stub carries them.

Add a source-filter repository test:

```python
rows = repo.list_for_search(
    SearchFilters(tdoc_id="R1-%", release="Rel-18", limit=10)
)
assert [row.tdoc.tdoc_id for row in rows] == ["R1-25-0001"]
assert rows[0].tdoc.type == "CR"
assert rows[0].tdoc.status == "Agreed"
```

- [ ] **Step 2: Run focused tests to verify failure**

Run:

```text
python -m pytest tests/integration/test_search_filters.py tests/integration/test_search_sem_end_to_end.py -q
```

Expected: constructor/attribute or missing repository-method failures.

- [ ] **Step 3: Implement metadata selection and source filtering**

Update the FTS5 SQL SELECT list and row offsets to select `t.type` and `t.status`. Update vector metadata SQL and `_build_fts5_stub`. Add `list_for_search` to the protocol and SQL repository. Preserve the existing rich filter semantics:

- `tsg` is an upper-case exact meeting TSG match.
- `meeting` applies the existing name/title OR rich filter.
- `meeting_id`, `tdoc_id`, `release`, and `spec` retain their current exact/rich behavior.
- `since` maps to `uploaded_date >=`; `until` maps to `uploaded_date <=`.
- `limit` is applied after filtering and ordering.

Use `ORDER BY tdoc_id ASC` for filter mode. Do not route this method through FTS5.

- [ ] **Step 4: Run focused tests to verify green**

Run the focused commands from Step 2 plus:

```text
python -m pytest tests/integration/test_search_filters.py tests/integration/test_search_sem_end_to_end.py tests/integration/test_tdoc_sqlite.py -q
```

- [ ] **Step 5: Run lint on touched modules**

Run `ruff check src/doc3gpp/models/search.py src/doc3gpp/repository/protocols.py src/doc3gpp/storage/repositories/search_sql.py src/doc3gpp/storage/repositories/vector_sql.py src/doc3gpp/storage/repositories/tdoc_sql.py`.

---

### Task 3: Add Spec-Document Source Metadata Lookup and Vector Rebuild Protocols

**Files:**
- Modify: `src/doc3gpp/repository/protocols.py`
- Modify: `src/doc3gpp/storage/repositories/spec_doc_sql.py`
- Modify: `src/doc3gpp/storage/repositories/spec_doc_vector_sql.py`
- Modify: `src/doc3gpp/services/spec_doc_semantic_service.py`
- Modify: `src/doc3gpp/services/spec_doc_search_service.py`
- Create: `tests/unit/test_spec_doc_vector_metadata.py`
- Modify: `tests/integration/test_spec_doc_search_service.py`
- Modify: `tests/integration/test_spec_doc_search_repo.py`

**Interfaces:**
- `SpecDocRepository.get_chunks_by_ids(chunk_ids: list[str]) -> dict[str, SpecDocChunk]` returns all requested existing chunk metadata in one batch.
- `SpecDocRepository.list_for_search(filters: SpecDocSearchFilters) -> list[SpecDocChunk]` supports optional rich filters over `spec_id`, `release`, `version`, `sections`, and `tables`, then applies `offset`/`limit` in deterministic `(spec_id, version, chunk_index)` order.
- `SpecDocVectorRepository` gains `rebuild_batch`, `count_versions_to_index`, `get_resume_cursor`, `set_resume_cursor`, `clear_resume_cursor`, and `status`, parallel to `SpecDocSearchRepository`.
- `SpecDocSemanticService.search()` enriches vector-only `SpecDocSemanticHit.hit` with a full `SpecDocHit` carrying all metadata, `score=0.0`, and empty internal previews. The facade later converts public previews to `None`.
- `SpecDocSemanticService.rebuild_embeddings(batch_size, stale_only, quiet, resume)` yields `RebuildProgress` for parsed source-version pairs.

- [ ] **Step 1: Write failing vector-only metadata tests**

Extend the existing `RecordingVectorRepository` test setup with a fake chunk repository containing a vector-only chunk. Assert the returned semantic hit has a non-`None` `hit` and that `hit.spec_id`, `version`, `release`, `sections`, `tables`, `chunk_index`, and `text` are populated.

Add a repository test for `get_chunks_by_ids` with two IDs and one missing ID. Assert one SQL batch result and silent omission of the missing ID.

Add a test that `SpecDocSemanticService.rebuild_embeddings(..., resume=True)` reads the vector cursor and calls `index_for_version` only for later pairs.

- [ ] **Step 2: Run tests to verify failure**

Run:

```text
python -m pytest tests/integration/test_spec_doc_search_service.py tests/integration/test_spec_doc_search_repo.py -q
```

Expected: missing constructor dependency/method failures and `hit is None` for vector-only chunks.

- [ ] **Step 3: Implement chunk metadata and vector rebuild support**

Inject `SpecDocRepository` into `SpecDocSemanticService` through an optional constructor argument with the production factory supplying `SQLAlchemySpecDocRepository`. Keep test doubles valid by accepting `None` and constructing the SQL repository only when the service is used without an injected repository.

Implement batched chunk lookup with SQLAlchemy `IN` parameters. Do not expose ORM rows. In the semantic service, parse missing chunk IDs from vector results, fetch metadata once, and use `dataclasses.replace` or new `SpecDocHit` construction to fill the internal hit.

Add vector metadata keys under `vec_spec_doc_meta` for cursor, last rebuild time, and last indexed parsed-at watermark. Implement status using vector row count and parsed source timestamps. Implement rebuild enumeration from `spec_doc_sources` where `parsed_at IS NOT NULL`, with `stale_only` comparing against the stored watermark and `resume` comparing the `spec_id@version` cursor.

Update all vector mismatch/rebuild hints to use `doc3gpp spec doc index --rebuild-embeddings`.

- [ ] **Step 4: Run tests to verify green**

Run the commands from Step 2 and:

```text
python -m pytest tests/integration/test_spec_doc_search_service.py tests/integration/test_spec_doc_search_repo.py tests/unit/test_spec_doc_models.py -q
```

- [ ] **Step 5: Run lint and inspect protocol consistency**

Run `ruff check src/doc3gpp/repository/protocols.py src/doc3gpp/storage/repositories/spec_doc_sql.py src/doc3gpp/storage/repositories/spec_doc_vector_sql.py src/doc3gpp/services/spec_doc_semantic_service.py` and confirm every changed Protocol method exists on its SQL implementation.

---

### Task 4: Implement TDoc and Spec-Document Unified Search Facades

**Files:**
- Create: `src/doc3gpp/services/tdoc_search_facade.py`
- Create: `src/doc3gpp/services/spec_doc_search_facade.py`
- Create: `tests/unit/test_tdoc_search_facade.py`
- Create: `tests/unit/test_spec_doc_search_facade.py`
- Modify: `src/doc3gpp/services/search_service.py` to accept the existing `snippet_tokens` override on pure FTS5 calls
- Modify: `src/doc3gpp/services/spec_doc_search_service.py` only as required for that same override

**Interfaces:**

```python
class TDocSearchFacade:
    def search(
        self,
        *,
        text: str | None,
        semantic: str | None,
        filters: SearchFilters,
        snippet_tokens: int | None = None,
    ) -> list[TDocSearchResult]: ...
```

```python
class SpecDocSearchFacade:
    def search(
        self,
        *,
        text: str | None,
        semantic: str | None,
        filters: SpecDocSearchFilters,
        snippet_tokens: int | None = None,
    ) -> list[SpecDocSearchResult]: ...
```

Constructors receive the FTS5 service, semantic service, source repository, and settings. They normalize input using `value.strip()` and never pass whitespace-only values to a low-level service.

- [ ] **Step 1: Write failing mode-dispatch tests**

Use small recording FTS, semantic, and source-repository fakes. Pin these calls and outputs:

```python
facade.search(text="handover", semantic=None, filters=filters)
# calls FTS5 only; result.search_mode == SearchMode.FTS5

facade.search(text=None, semantic="handover procedure", filters=filters)
# calls vector only; result.search_mode == SearchMode.SEMANTIC

facade.search(text="handover", semantic="handover procedure", filters=filters)
# calls hybrid semantic service with text as fts5_query and configured weight

facade.search(text=None, semantic=None, filters=filters)
# calls source repository only; result.search_mode == SearchMode.FILTER
```

Assert all public TDoc fields are present. Assert `previews` is the FTS5 mapping only for FTS5 mode and is `None` for semantic, hybrid, and filter. Assert TDoc `type`, `status`, and `best_chunk_id` behavior. Assert scores come from `SearchHit.score`, `SemanticSearchHit.min_chunk_distance`, `SemanticSearchHit.rrf_score`, and `None` respectively.

Add spec-document tests for the same four modes, including a vector-only internal hit whose metadata is fully flattened and whose public `previews` is `None`.

- [ ] **Step 2: Run tests to verify failure**

Run:

```text
python -m pytest tests/unit/test_tdoc_search_facade.py tests/unit/test_spec_doc_search_facade.py -q
```

Expected: import or assertion failures because the facades do not exist.

- [ ] **Step 3: Implement TDoc mode dispatch and conversion**

Use the following exact dispatch logic:

```python
text_value = text.strip() if text and text.strip() else None
semantic_value = semantic.strip() if semantic and semantic.strip() else None
if text_value and semantic_value:
    mode = SearchMode.HYBRID
elif text_value:
    mode = SearchMode.FTS5
elif semantic_value:
    mode = SearchMode.SEMANTIC
else:
    mode = SearchMode.FILTER
```

For hybrid, call the semantic service with `fts5_query=text_value`, `query=semantic_value`, `limit=filters.limit`, and `fts5_weight=settings.semantic_search.fts5_weight`. For semantic, pass `fts5_query=None`. For FTS5, call the FTS service with the configured snippet override. For filter, call `list_for_search`.

Map internal results to `TDocSearchResult`. For semantic results use `min_chunk_distance` as `score`; for hybrid use `rrf_score`; always discard internal previews in non-FTS5 modes. Keep the result list order returned by the low-level service.

- [ ] **Step 4: Implement spec-document mode dispatch and conversion**

Use the same mode selection. For semantic/hybrid, request `filters.limit + filters.offset` from the vector service with offset zero and slice the final list by the requested offset, because the current vector service has no offset parameter. For FTS5 and filter, preserve repository offset semantics.

Map every `SpecDocHit` metadata field into `SpecDocSearchResult`. Use `min_chunk_distance` for semantic score, `rrf_score` for hybrid score, FTS5 `score` for FTS5, and `None` for filter. Set `previews=None` outside FTS5.

- [ ] **Step 5: Run focused tests to verify green**

Run:

```text
python -m pytest tests/unit/test_tdoc_search_facade.py tests/unit/test_spec_doc_search_facade.py tests/unit/test_semantic_search_service.py tests/integration/test_spec_doc_search_service.py -q
```

- [ ] **Step 6: Run lint on new services**

Run `ruff check src/doc3gpp/models/unified_search.py src/doc3gpp/services/tdoc_search_facade.py src/doc3gpp/services/spec_doc_search_facade.py`.

---

### Task 5: Add Resource Index Coordinators and Wire Services

**Files:**
- Create or modify: `src/doc3gpp/models/index.py` if a bundle status DTO is needed
- Create: `src/doc3gpp/services/tdoc_index_service.py`
- Create: `src/doc3gpp/services/spec_doc_index_service.py`
- Create: `tests/unit/test_index_services.py`
- Modify: `src/doc3gpp/services/factory.py`
- Modify: `src/doc3gpp/web/state.py`
- Modify: `src/doc3gpp/web/deps.py`
- Modify: `src/doc3gpp/web/app.py`
- Modify: `tests/unit/test_factory_semantic.py`, `tests/unit/test_web_app.py`, and `tests/unit/test_job_worker.py` for new optional container fields

**Interfaces:**

Define one shared action shape:

```python
@dataclass(frozen=True, slots=True)
class IndexRequest:
    rebuild: bool = False
    rebuild_embeddings: bool = False
    rebuild_all: bool = False
    batch: int | None = None
    resume: bool = False
    stale_only: bool = False
```

Define `TDocIndexService.status()`, `TDocIndexService.rebuild(request, quiet, on_progress)`, `SpecDocIndexService.status()`, and `SpecDocIndexService.rebuild(request, quiet, on_progress)`. `status()` returns a resource bundle with FTS5 and vector component statuses. `rebuild()` validates that action flags are not contradictory, runs FTS5 before vector for `rebuild_all`, and returns processed counts.

Add these `ServiceContainer` fields with `None` defaults for old test constructors: `tdoc_search`, `spec_doc_search_facade`, `tdoc_index`, and `spec_doc_index`. Keep existing low-level `search`, `semantic_search`, `spec_doc_search`, and `spec_doc_semantic` fields because parse hooks and existing tests use them.

- [ ] **Step 1: Write failing coordinator tests**

Use recording low-level services to assert:

- no action returns status and performs no rebuild;
- `rebuild=True` calls only FTS5;
- `rebuild_embeddings=True` calls only vector;
- `rebuild_all=True` calls FTS5 then vector;
- `batch=None` resolves from settings;
- progress callbacks receive resource-prefixed messages;
- invalid action combinations produce the existing CLI/HTTP validation error.

Add factory/state tests asserting both facades share the web process embedder and that missing optional semantic services leave the facades unavailable only for semantic modes.

- [ ] **Step 2: Run tests to verify failure**

Run:

```text
python -m pytest tests/unit/test_index_services.py tests/unit/test_factory_semantic.py tests/unit/test_web_app.py -q
```

- [ ] **Step 3: Implement index coordinators and wiring**

Build TDoc facades from `SearchService`, `SemanticSearchService`, `TDocRepository`, and settings. Build spec facades from `SpecDocSearchService`, `SpecDocSemanticService`, `SpecDocRepository`, and settings. Do not make the facades instantiate concrete repositories directly.

The factory must return `None` for an unavailable FTS5 or semantic/vector subsystem in the same way as current builders. The resource index service must still expose status for an available component and report an unavailable component explicitly rather than crashing the status page.

Update `build_state()` to construct one TDoc facade, one spec-doc facade, and both index services. Update dependency helpers with `get_tdoc_search_facade`, `get_spec_doc_search_facade`, `get_tdoc_index_service`, and `get_spec_doc_index_service`.

- [ ] **Step 4: Run tests to verify green**

Run the focused command from Step 2 and:

```text
python -m pytest tests/unit/test_job_worker.py tests/integration/test_search_extras_disabled.py tests/integration/test_semantic_extras_disabled.py -q
```

- [ ] **Step 5: Run lint**

Run `ruff check src/doc3gpp/models/index.py src/doc3gpp/services/tdoc_index_service.py src/doc3gpp/services/spec_doc_index_service.py src/doc3gpp/services/factory.py src/doc3gpp/web/state.py src/doc3gpp/web/deps.py src/doc3gpp/web/app.py`.

---

### Task 6: Migrate CLI Commands and Unified Renderers

**Files:**
- Modify: `src/doc3gpp/cli.py`
- Modify: `tests/unit/test_cli_search.py`
- Modify: `tests/unit/test_cli_search_sem.py`
- Modify: spec-doc CLI tests discovered by `test_spec_doc_cli.py` or `test_spec_doc_search_cli.py`
- Modify: `tests/integration/test_search_query_sem_rerank.py` and related tests to the replacement contract

**Interfaces:**
- Register `@tdoc_app.command("search")` for the unified TDoc search function.
- Register `@tdoc_app.command("index")` for TDoc index maintenance.
- Register `@spec_doc_app.command("search")` for unified spec-doc search.
- Register `@spec_doc_app.command("index")` for spec-doc index maintenance.
- Remove `tdoc_search_app` command registrations for `query`, `sem`, and `index`; remove the spec-doc search subgroup registrations for `query` and `sem`.
- Add `_render_unified_tdoc_results(results, format, compact)` and `_render_unified_spec_doc_results(results, format, compact, fields)` using `tdoc_search_result_to_dict` and `spec_doc_search_result_to_dict`.

- [ ] **Step 1: Write failing CLI surface tests**

Add Typer runner tests that assert:

```python
assert invoke(["tdoc", "search", "--text", "handover", "--format", "json"]).exit_code == 0
assert invoke(["tdoc", "index"]).exit_code == 0
assert invoke(["spec", "doc", "search", "--semantic", "handover"]).exit_code == 0
assert invoke(["spec", "doc", "index"]).exit_code == 0
assert invoke(["tdoc", "search", "query", "handover"]).exit_code != 0
assert invoke(["tdoc", "search", "--fts5-weight", "0.5"]).exit_code != 0
```

Add mocked facade tests for all four modes. Parse JSON and assert the exact unified keys, `search_mode`, score, and `previews` values. Assert `--format table`, `--format markdown`, and `--compact` preserve the existing output conventions while showing `score` and `search_mode`.

Add spec-doc tests proving `--offset`, `--fields`, `--format`, `--output`-compatible behavior, and `--compact` remain accepted.

- [ ] **Step 2: Run tests to verify failure**

Run:

```text
python -m pytest tests/unit/test_cli_search.py tests/unit/test_cli_search_sem.py tests/integration/test_search_query_sem_rerank.py -q
```

Expected: old command registration or missing facade/rendering failures.

- [ ] **Step 3: Implement TDoc CLI migration**

Replace the current `search_command` signature with optional `--text` and `--semantic`, retain the existing TDoc filters, `--limit`, `--format`, `--compact`, `--snippet-tokens`, `--explain`, and `--quiet`. Remove `--sem-query`, hidden `--rerank`, `--fts5-query`, and `--fts5-weight` from the public function.

Move the current index body to `@tdoc_app.command("index")`, route it through `TDocIndexService`, and update every error/status/rebuild hint to `doc3gpp tdoc index`. Keep progress and quiet behavior.

Use the facade result list for all three renderers. JSON must emit every unified field, not the old semantic nested object. Markdown must emit `search_mode`, `score`, core metadata, and previews only when the value is not `None`. Table must include `tdoc_id`, `score`, `search_mode`, `title`, `meeting`, `tsg`, `uploaded_date`, `ftp_url`, `wis`, `type`, `status`, and a preview column when applicable.

- [ ] **Step 4: Implement spec-document CLI migration**

Replace `spec_doc_search_app` with `@spec_doc_app.command("search")`. Add `--text`, `--semantic`, the existing spec/release/version/sections/tables filters, `--limit`, `--offset`, `--fields`, `--format`, and `--compact`. Use `SpecDocSearchFacade` and render the unified DTO for all modes.

Add `@spec_doc_app.command("index")` using `SpecDocIndexService` and the same six maintenance options as TDoc. Update corruption hints to `doc3gpp spec doc index --rebuild`.

- [ ] **Step 5: Run focused tests to verify green**

Run:

```text
python -m pytest tests/unit/test_cli_search.py tests/unit/test_cli_search_sem.py tests/unit/test_unified_search_models.py tests/integration/test_search_query_sem_rerank.py tests/integration/test_spec_doc_search_service.py -q
```

- [ ] **Step 6: Run CLI help checks**

Run:

```text
python -m doc3gpp.cli tdoc --help
python -m doc3gpp.cli spec doc --help
```

Confirm help contains `tdoc search`, `tdoc index`, `spec doc search`, and `spec doc index`, and does not expose the removed nested `query`, `sem`, or `search index` commands.

---

### Task 7: Migrate Web Search, Index Status, and Background Jobs

**Files:**
- Modify: `src/doc3gpp/web/routes/search.py`
- Modify: `src/doc3gpp/web/routes/spec_docs.py`
- Modify: `src/doc3gpp/web/routes/jobs.py`
- Modify: `src/doc3gpp/web/workers/handlers.py`
- Modify: `src/doc3gpp/models/jobs.py`
- Modify: `src/doc3gpp/web/templates/search_results.html`
- Modify: `src/doc3gpp/web/templates/partials/search_form.html`
- Modify: `src/doc3gpp/web/templates/partials/search_results.html`
- Modify: `src/doc3gpp/web/templates/spec_docs_search.html`
- Modify: `src/doc3gpp/web/templates/partials/spec_doc_search_form.html`
- Modify: `src/doc3gpp/web/templates/partials/spec_doc_search_results.html`
- Modify: `src/doc3gpp/web/templates/partials/search_resource_tabs.html`
- Modify: `src/doc3gpp/web/templates/sync.html`, `src/doc3gpp/web/static/js/sync_hub.js`, and `src/doc3gpp/web/static/js/search.js`
- Modify: `tests/unit/test_web_routes.py`, `tests/integration/test_web_search_end_to_end.py`, `tests/unit/test_job_worker.py`, `tests/unit/test_sync_hub_page.py`

**Interfaces:**
- `GET /tdocs/search` depends on `TDocSearchFacade` and accepts `text`, `semantic`, existing filters, `limit`, and `format`.
- `GET /spec-docs/search` depends on `SpecDocSearchFacade` and accepts `text`, `semantic`, existing filters, `limit`, `offset`, and `format`.
- `GET /tdocs/index` and `GET /spec-docs/index` return status payloads.
- `POST /jobs/tdocs/index` and `POST /jobs/spec-docs/index` accept an index action body and return the standard 202 job envelope.
- Add `JobKind.INDEX_TDOCS` and `JobKind.INDEX_SPEC_DOCS` with handlers `_index_tdocs` and `_index_spec_docs`. Keep `REBUILD_SEARCH` only if needed to decode persisted historical rows; do not expose a route or form for it.

- [ ] **Step 1: Write failing route/job tests**

Add route tests asserting:

- `/tdocs/search?text=handover&format=json` returns the unified array;
- `/tdocs/search?semantic=handover&format=json` returns semantic rows with `previews: null`;
- `/tdocs/search?format=json&tdoc-id=R1-%` invokes filter mode and returns `score: null`;
- `/spec-docs/search?text=handover&format=json` and semantic/filter variants return unified rows;
- `/tdocs/search/sem` and `/spec-docs/search/sem` are not registered;
- HTMX requests receive result fragments rather than full pages;
- old `q`, `fts5_query`, and `fts5_weight` request parameters are not part of the new route contract.

Add job tests that POST both index endpoints, assert the new `JobKind`, and run each handler with a recording index service. Assert action/options are passed unchanged and progress is appended.

- [ ] **Step 2: Run tests to verify failure**

Run:

```text
python -m pytest tests/unit/test_web_routes.py tests/integration/test_web_search_end_to_end.py tests/unit/test_job_worker.py tests/unit/test_sync_hub_page.py -q
```

- [ ] **Step 3: Implement unified search routes**

Replace route parameters `q`, `sem`, and semantic-route-specific parameters with `text` and `semantic`. Normalize them in the route only for template context; delegate actual mode selection to the facade. Always serialize the facade result DTOs through the shared model dictionaries for JSON. Set template context `search_mode` from the returned mode or the resolved empty-input mode.

Remove both `/sem` route functions. Keep existing parse/show spec-doc routes untouched except for updated search links and corruption hints.

- [ ] **Step 4: Implement index routes and jobs**

Add Pydantic request body models with booleans `rebuild`, `rebuild_embeddings`, `rebuild_all`, `resume`, `stale_only`, and optional positive `batch`. Reject contradictory actions before enqueueing. Add status routes that return JSON for `format=json` and a small HTML status page/partial otherwise.

Add new worker handlers that call the resource index service in `asyncio.to_thread`, check cancellation between progress updates, and return processed summaries. Register them in `JobHandlers.KIND_TO_HANDLER`. Update the sync hub to submit the new TDoc/spec-doc index jobs and remove the old FTS5-only form.

- [ ] **Step 5: Update templates and browser behavior**

Use one search form per resource with text and semantic fields on the same page. Remove links/forms for `/tdocs/search/sem` and `/spec-docs/search/sem`, remove the FTS5-weight input, and render mode/score consistently. Render snippets only when `previews` is not `None`. Keep HTMX `#results` fragment behavior and existing pagination/filter fields.

- [ ] **Step 6: Run focused web tests to verify green**

Run:

```text
python -m pytest tests/unit/test_web_routes.py tests/integration/test_web_search_end_to_end.py tests/unit/test_job_worker.py tests/unit/test_sync_hub_page.py tests/unit/test_web_app.py -q
```

- [ ] **Step 7: Run frontend/static checks available in the repository**

Run `git diff --check` and the repository's existing Python template/static tests (`tests/integration/test_spec_doc_web.py` plus the web route tests). Do not introduce a new frontend toolchain.

---

### Task 8: Migrate MCP Tools and Cross-Surface Serialization

**Files:**
- Modify: `src/doc3gpp/web/mcp_server.py`
- Modify: `tests/integration/test_mcp_end_to_end.py`
- Modify: `tests/unit/test_web_routes.py` or the existing MCP serializer test module

**Interfaces:**
- Keep `search_tdoc` as the one TDoc search tool, but change it to optional `text` and `semantic` arguments plus existing filters.
- Keep `search_spec_docs` as the one spec-document search tool, with optional `text` and `semantic` plus existing filters.
- Remove `semantic_search_tdoc` and `semantic_search_spec_docs` registrations.
- Add `get_tdoc_index`, `index_tdocs`, `get_spec_doc_index`, and `index_spec_docs`.
- MCP search results are `_to_json([tdoc_search_result_to_dict(...)])` or `_to_json([spec_doc_search_result_to_dict(...)])` and must match HTTP JSON arrays.

- [ ] **Step 1: Write failing MCP tests**

Extend tool discovery tests to assert the unified search/index names and absence of the removed semantic tools. Add calls for text-only, semantic-only, hybrid, and filter-only search and compare parsed JSON to the corresponding TestClient HTTP JSON payload.

Add tests that pass `fts5_weight`, `fts5_query`, or `sem_query` and assert the tool schema/body rejects them. Add index status/enqueue tests that assert job kind, action options, and standard job links.

- [ ] **Step 2: Run tests to verify failure**

Run:

```text
python -m pytest tests/integration/test_mcp_end_to_end.py -q
```

- [ ] **Step 3: Implement unified MCP serializers and tools**

Delete the old `_fts5_hit_to_json`, `_semantic_hit_to_json`, and spec semantic serializers from the public path. Use the model-level dictionaries for all result modes. Remove request-level weight validation because settings supply the value.

Implement index status tools by calling the resource index services. Implement enqueue tools through `_enqueue` with `JobKind.INDEX_TDOCS` / `JobKind.INDEX_SPEC_DOCS` and JSON-safe action parameters.

- [ ] **Step 4: Verify HTTP/MCP byte parity**

Run the focused MCP test suite and assert:

```python
assert mcp_json == response.text
```

for compact JSON arrays in each search mode and for the same seeded data.

- [ ] **Step 5: Run lint**

Run `ruff check src/doc3gpp/web/mcp_server.py tests/integration/test_mcp_end_to_end.py`.

---

### Task 9: Update Documentation and Current Contract References

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/cli.md`
- Modify: `docs/architecture.md`
- Modify: `docs/web-server.md`
- Modify: `docs/code-map.md`
- Modify: `doc3gpp.toml.example` only if current examples mention request-level blend flags
- Modify: current tests/docstrings containing active old command or route hints

**Interfaces:**
- Documentation must describe only `tdoc index`, `tdoc search`, `spec doc index`, and `spec doc search` as current commands.
- Documentation must list `/tdocs/search`, `/spec-docs/search`, index status routes, index job routes, and unified MCP tools.
- Historical design files remain historical and are not rewritten.

- [ ] **Step 1: Write documentation contract checks**

Use repository text search in a test or verification command to identify active documentation references to removed public paths. The check must distinguish historical `docs/superpowers/specs/` files from current guides. Assert current guides contain the new commands and unified result field names.

- [ ] **Step 2: Update current documentation**

Replace active examples of `tdoc search query`, `tdoc search sem`, `tdoc search index`, `spec doc search query`, `spec doc search sem`, `/search/sem`, and request-level `fts5_weight` with the unified contracts. Document the four mode table, `score` semantics, `search_mode`, `previews: null` for non-FTS5 modes, TDoc `type`/`status`, and array-order ranking.

Update the architecture/code map entries for the new facade/index service modules and the new worker/job routes.

- [ ] **Step 3: Verify documentation references**

Run targeted searches over `README.md`, `AGENTS.md`, `docs/cli.md`, `docs/architecture.md`, `docs/web-server.md`, and `docs/code-map.md`, then run `git diff --check`.

---

### Task 10: Full Verification and Regression Cleanup

**Files:**
- Modify any remaining tests that assert old public names or old result keys.
- Do not modify unrelated user changes in the worktree.

- [ ] **Step 1: Run focused complete search/index coverage**

Run:

```text
python -m pytest \
  tests/unit/test_unified_search_models.py \
  tests/unit/test_tdoc_search_facade.py \
  tests/unit/test_spec_doc_search_facade.py \
  tests/unit/test_index_services.py \
  tests/unit/test_cli_search.py \
  tests/unit/test_cli_search_sem.py \
  tests/integration/test_search_filters.py \
  tests/integration/test_search_sem_end_to_end.py \
  tests/integration/test_spec_doc_search_service.py \
  tests/integration/test_web_search_end_to_end.py \
  tests/integration/test_mcp_end_to_end.py -q
```

Expected: all focused tests pass, including exact payload and old-surface rejection assertions.

- [ ] **Step 2: Run the complete offline suite**

Run `python -m pytest` or `./scripts/test_sqlite.sh`, using the repository's configured offline markers. If a failure is unrelated, record the exact test and verify it existed before the change rather than suppressing it.

- [ ] **Step 3: Run lint and whitespace checks**

Run:

```text
ruff check .
```

- [ ] **Step 4: Inspect the final worktree**

Run `git status --short` and `git diff --stat`. Confirm only intended source, test, template, JavaScript, and documentation files changed. Do not commit.

---

## Plan Self-Review

- **Spec coverage:** Tasks 1-4 cover unified DTOs, scores, modes, previews, metadata, vector-only enrichment, and filter-only SQL. Task 5 covers service wiring and index orchestration. Tasks 6-8 cover CLI, HTTP/jobs, templates, and MCP. Task 9 covers current documentation. Task 10 covers focused/full verification.
- **Placeholder scan:** No implementation step relies on `TBD`, `TODO`, an unspecified helper, or an unbounded "handle edge cases" instruction. Every new public symbol has a named module and signature.
- **Type consistency:** Facades return `list[TDocSearchResult]` / `list[SpecDocSearchResult]`; serializers consume those DTOs; adapters consume facades; index coordinators return status/action data to CLI, routes, workers, and MCP. `SpecDocSemanticService` remains an internal ranking service and is enriched before the spec facade converts it.
- **Compatibility boundary:** Internal FTS5/vector DTOs may retain their current fields for ranking implementation, but no adapter serializes those legacy fields. Public removed names are not reintroduced. Persisted historical `REBUILD_SEARCH` rows may remain decodable without exposing that kind through new routes/tools.
- **Testing gate:** Each task starts with a failing test and includes a focused command before the next task. Full offline tests, lint, and whitespace checks are required before claiming completion.
