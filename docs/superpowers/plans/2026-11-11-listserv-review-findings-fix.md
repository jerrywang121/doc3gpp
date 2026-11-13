# LISTSERV Review Findings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for inline execution, or superpowers:subagent-driven-development if the user explicitly selects delegation. Steps use checkbox (`- [ ]`) syntax for tracking. Do not start until the user approves this plan.

**Goal:** Fix the two Important and three Minor LISTSERV branch-review findings without changing established public contracts.

**Architecture:** Keep the existing vector KNN path, falling back to an exact filtered SQLite scalar-distance scan when its bounded window cannot supply the requested distinct messages. Make lazy embedder ownership explicit across LISTSERV services and app/worker lifecycles; make the three smaller fixes at their existing adapter and sync boundaries.

**Tech Stack:** Python 3.10+, SQLAlchemy 2, SQLite/FTS5, optional sqlite-vec, FastAPI/MCP, Jinja, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-11-11-listserv-review-findings-fix-design.md`

## Global Constraints

- Preserve the dirty `feature/listserv-archives` checkout and all unrelated/user-owned files, including `node_modules/`, `package.json`, and `package-lock.json`; do not stage, commit, reset, change branches, or create a worktree.
- No schema changes, new dependency, fourth database, private/authenticated archives, attachment-byte downloads, or implicit read-time sync/parse.
- Preserve HTTP, MCP, CLI, SDK and UI payloads, result scores/provenance, filtering semantics, source-revision freshness, cancellation, and existing job-worker shutdown order.
- No public semantic/hybrid offset ceiling; permit arbitrary nonnegative offsets and keep hybrid FTS paging internal.
- Preserve public ETSI host only, identity/gzip/deflate only, decoded response limit 16 MiB, and no attachment downloads.
- Test first. Each task below is independently reviewable; no commits are authorized. Update affected maintained docs alongside behavior changes.
- Use existing environment and fixtures; optional semantic tests must run with `sqlite-vec` installed, not silently count as passing when skipped. No real network calls in tests.

## Review Focus

1. **Empty or exhausted vector corpus:** A deep offset or filters excluding every record returns `[]`, without over-limit KNN requests (Task 1).
2. **Ties and chunk crowding across the cap:** Equal-distance chunks and repeated URLs must not destabilize the best snippet or hybrid rank (Task 1).
3. **Unavailable or concurrently accessed lazy provider:** A provider returning `None` must not create a replacement client; concurrent web/jobs must share one initialized client (Task 2).
4. **Cleanup before lazy initialization or after construction failure:** Closing must not trigger initialization; owned clients close once, borrowed clients never close through a service (Task 2).
5. **Overlapping duplicates with different local dates:** The final page occurrence controls selection even when an earlier occurrence is in range; coverage and derived indexes stay correct (Task 5).

## File map and scope

The five findings can be implemented and reviewed separately in this one plan; Task 2 groups the tightly coupled service/factory/worker/app lifecycle changes. Tests live beside existing LISTSERV integration/unit tests. `docs/processes.md`, `docs/known-constraints.md`, `docs/web-server.md`, and `docs/mcp.md` are the primary maintained guides to update where their statements are affected; update `AGENTS.md` only if a contributor-facing lifecycle invariant changes. Keep existing file organization and preserve unrelated edits.

---

### Task 1: Complete deep semantic and hybrid vector windows

**Files:**
- Modify: `src/doc3gpp/storage/repositories/listserv_vector_sql.py` and `src/doc3gpp/services/listserv_semantic.py`.
- Test: `tests/integration/test_listserv_semantic_sqlite.py`.
- Modify docs if needed: `docs/processes.md`, `docs/known-constraints.md`.

**Interfaces:**
- Consumes: `SQLAlchemyListservVectorRepository.knn(query_vec: np.ndarray, limit: int) -> list[VectorCandidate]`, `_build_filter_clauses(filters)`, `_matched_chunk_texts(conn, ids)`, and existing `_group_vec_candidates`/hybrid RRF.
- Produces: `SQLAlchemyListservVectorRepository.exact_distinct(query_vec: np.ndarray, *, target_distinct: int, filters: ListservFilters) -> list[VectorCandidate]`; candidates represent distinct eligible URLs in the same vector rank order as the existing grouping path, each with its best chunk and snapshot-matched text.
- Keeps: `ListservSemanticService._distinct_email_rows(query_vec, *, target_distinct: int, fanout: int, filters: ListservFilters) -> list[VectorCandidate]` and all public signatures.

- [x] **Step 1: Write regression tests.** Use the existing SQLite/vector fixtures and bulk inserts, with an independent Python cosine-distance/RRF oracle, not the implementation's ranking helpers. Add:
  - `test_deep_semantic_and_hybrid_match_exact_oracle`: insert more than 4,096 eligible messages, query `offset=4096, limit=2`, and assert semantic URLs/distances/snippets plus hybrid URLs/RRF scores/provenance equal the oracle. Include an FTS-only hit, a vector-only hit, and an FTS+vector hit; hybrid candidate target remains `(offset + limit) * fanout`.
  - `test_exact_window_applies_filters_before_counting`: interleave eligible/ineligible messages with list ID, sender, subject, TDoc and displayed-local-date filters; assert a deep page equals the oracle over only eligible records. Include `%`, `!`, `null`/`not-null` where supported by existing fields.
  - `test_crowded_cap_ties_and_exhaustion`: place more than 4,096 top-ranked chunks in fewer messages than the requested distinct window, with equal-distance chunks for a URL and multiple URL ties. Assert the best chunk ID/text and message/rank ordering match the existing tie rules, a page beyond the corpus is `[]`, and zero eligible messages are `[]`.
  - `test_fast_and_exact_matched_chunk_text_use_read_snapshot`: use the existing two-connection synchronization pattern to replace a vector/text revision between vector scoring and text lookup; both paths must return text from the same revision as the scored vector, never a mixed pair. Spy on `knn` to assert every requested `k <= 4096`; a small ordinary window must use KNN without the exact fallback.
- [x] **Step 2: Run the new tests and confirm the deep/crowded cases fail for truncation.** Run `python -m pytest -q tests/integration/test_listserv_semantic_sqlite.py -k 'deep_semantic_and_hybrid_match_exact_oracle or exact_window_applies_filters_before_counting or crowded_cap_ties_and_exhaustion or fast_and_exact_matched_chunk_text_use_read_snapshot'`. Record any sqlite-vec skip as missing coverage, not success.
- [x] **Step 3: Implement the exact repository method.** In one explicitly established read transaction, score `vec_listserv_embeddings` with `vec_distance_cosine(embedding, :query_vector)` and join source message/detail rows using the same `_build_filter_clauses` as `matching_urls`. Order by the existing KNN order `(distance IS NULL, distance ASC, chunk_id ASC)`, consume rows until `target_distinct` distinct URLs are obtained or exhausted, and retain the first/best chunk per URL. Fetch matched text through `_matched_chunk_texts` on the same connection before the transaction ends. Handle zero target and empty corpus without SQL errors; retain compatibility checks and current null-distance handling. Do not materialize an incomplete unfiltered prefix.
- [x] **Step 4: Select the fallback.** In `_distinct_email_rows`, keep KNN widening up to 4096 for satisfiable windows; call `exact_distinct` when the requested distinct target exceeds 4096 or a full 4096-row KNN result has too few eligible distinct URLs. Return the KNN result if it exhausted the corpus. Preserve existing grouping, semantic final sort, hybrid RRF, scores, provenance and output.
- [x] **Step 5: Verify.** Run `python -m pytest -q tests/integration/test_listserv_semantic_sqlite.py tests/integration/test_listserv_index_sqlite.py`. Expected: all pass with semantic tests executed. Update docs to note the correctness fallback's potentially corpus-sized scoring/sort cost, with no offset ceiling.

### Task 2: Share and close LISTSERV embedders correctly

**Files:**
- Modify: `src/doc3gpp/services/listserv_semantic.py`, `listserv_search.py`, `listserv_index.py`, `listserv_sync.py`, `listserv_parse.py`, and `factory.py` under `src/doc3gpp/services/`.
- Modify: `src/doc3gpp/web/app.py`, `web/state.py`, `web/workers/handlers.py`, and `src/doc3gpp/cli_listserv.py`.
- Test: `tests/integration/test_listserv_index_sqlite.py`, `tests/unit/web/test_listserv_jobs.py`, `tests/unit/test_web_app.py`, `tests/unit/web/test_mcp_stdio_lifecycle.py`, and `tests/unit/test_cli_listserv.py`.
- Modify affected lifecycle guidance: `docs/processes.md`, `docs/known-constraints.md`, `docs/mcp.md`, and, if its invariant changes, `AGENTS.md`.

**Interfaces:**
- Consumes: existing `build_listserv_semantic_service(settings, *, embedder=None, vector_repo=None, fts_repository=None, source_repository=None)` and app `shared_embedder() -> object | None` lazy provider.
- Produces: idempotent `close(self) -> None` on `ListservSemanticService`, `ListservSearchService`, and `ListservIndexService`; sync/parse `close()` cascades to their owned index service and source. An explicitly injected semantic/index service is borrowed unless the builder explicitly transfers ownership.
- Extend each `build_listserv_{search,index,sync,parse}_service` with keyword-only `embedder_factory: Callable[[], Embedder | None] | None = None`; retain existing `embedder=` compatibility where present. The provider is resolved only on semantic use. An explicitly supplied provider returning `None` means unavailable, not “build another embedder.” Thread it via the web service container to worker builders.

- [x] **Step 1: Write lifecycle regressions.** Add tests asserting:
  - An internally built embedder is closed exactly once by repeated semantic/service `close()`; an injected embedder and injected semantic/index services are not closed by borrowers. Closing an unused lazy service never calls its factory.
  - For owned construction, `dim` unavailable, vector repository construction failure, and an unexpected propagated exception each close the created embedder once; a borrowed embedder stays open in the same failures.
  - App construction and index status do not call the provider, probe dimensions, or perform embedding requests. Search, index, sync and parse jobs use the same provider instance; concurrent first use builds only one instance. If the provider returns `None`, no independent client is created.
  - Sync and parse job success, exception and cancellation close their job-owned source/index, not the app embedder. HTTP and MCP stdio shutdown tests record `worker_drained` before `embedder_closed`, with one shared close; retain failed-drain resource preservation and stdio executor behavior.
  - CLI search and index close owned services on both successful output and raised search/index errors.
- [x] **Step 2: Run and observe failures.** Run `python -m pytest -q tests/integration/test_listserv_index_sqlite.py tests/unit/web/test_listserv_jobs.py tests/unit/test_web_app.py tests/unit/web/test_mcp_stdio_lifecycle.py tests/unit/test_cli_listserv.py -k 'embedder or listserv or shutdown or close'`. Confirm the new ownership/provider tests fail against existing behavior.
- [x] **Step 3: Implement owned versus borrowed cleanup.** In `build_listserv_semantic_service`, record ownership only when it creates the embedder; close that owned embedder on every unsuccessful construction path, including unexpected exceptions, without changing which errors propagate. Its `close()` closes only an owned embedder once. Search/index close only semantic services they created, without forcing lazy creation; sync/parse close only their owned index/source, once. Preserve borrowed injections.
- [x] **Step 4: Wire lazy provider and teardown.** Add the declared optional provider to builders; propagate it through `ServiceContainer` and `_run_listserv_batch`, using existing worker `_to_thread` handling. Serialize the web provider's first initialization so concurrent jobs/search cannot create two clients; do not resolve it during app construction or status reads. Keep it app-owned. Close app-owned LISTSERV wrappers after the worker drains and before the shared embedder, without changing HTTP/stdio shutdown or failed-drain semantics. Ensure CLI search/index use `try/finally` to close their built services on every exit.
- [x] **Step 5: Verify.** Run `python -m pytest -q tests/integration/test_listserv_index_sqlite.py tests/integration/test_listserv_parse_sqlite.py tests/integration/test_listserv_sync_sqlite.py tests/unit/web/test_listserv_jobs.py tests/unit/test_web_app.py tests/unit/web/test_mcp_stdio_lifecycle.py tests/integration/test_mcp_stdio_tool_shutdown.py tests/unit/test_cli_listserv.py`. Expected: all pass; shutdown sequence and no-network/laziness assertions run. Update affected lifecycle docs.

### Task 3: Correct LISTSERV corruption metadata and repair hint

**Files:**
- Modify: `src/doc3gpp/web/routes/listserv.py`, `src/doc3gpp/web/mcp_server.py`, `src/doc3gpp/web/errors.py`.
- Test: `tests/integration/test_listserv_web_sqlite.py`, `tests/unit/web/test_listserv_mcp.py`, `tests/unit/test_web_routes.py`.
- Modify docs if affected: `docs/web-server.md`, `docs/mcp.md`.

**Interfaces:**
- Consumes: `map_search_error(exc: Exception, *, resource: str) -> Exception` and existing `SearchIndexCorruptError`/web corruption mapper.
- Produces: LISTSERV HTTP/MCP corruption metadata with `resource="listserv"` and repair command `doc3gpp listserv index --rebuild`; existing error slugs/status/codes, existing metadata fields, and non-LISTSERV mappings remain unchanged.

- [x] **Step 1: Write failing tests.** Stub LISTSERV search to raise `SearchIndexCorruptError`; assert HTTP `/api/listserv/search` has the existing corruption status/slug, resource `listserv`, and LISTSERV rebuild command, not the spec-doc command. Assert the MCP `listserv_search` error data has resource `listserv`, that repair command, and the existing error code/slug and `id` field. Add controls asserting TDoc, spec-doc and generic corruption retain their original hint/code.
- [x] **Step 2: Run tests.** `python -m pytest -q tests/integration/test_listserv_web_sqlite.py tests/unit/web/test_listserv_mcp.py tests/unit/test_web_routes.py -k 'corrupt'`. Confirm the LISTSERV-specific assertions fail.
- [x] **Step 3: Implement mapping.** Map corruption at the LISTSERV HTTP and MCP search boundaries with `map_search_error(exc, resource="listserv")`. Extend `map_mcp_error` to recognize only `SearchIndexCorruptWebError(resource="listserv")`, return the existing `SearchIndexCorruptError` code, slug and `id` plus its resource, and add the LISTSERV repair hint. Add the LISTSERV HTTP hint in `map_domain_error`; preserve every other resource branch and exception field.
- [x] **Step 4: Verify.** Run `python -m pytest -q tests/integration/test_listserv_web_sqlite.py tests/unit/web/test_listserv_mcp.py tests/unit/test_web_routes.py tests/integration/test_listserv_mcp.py`. Expected: all pass. No docs changes were needed; neither guide discusses these repair hints.

### Task 4: Expose rich list-ID filtering in the search form

**Files:**
- Modify: `src/doc3gpp/web/templates/listserv.html` and `src/doc3gpp/web/routes/listserv.py`.
- Modify `src/doc3gpp/web/static/js/listserv.js` only if a real search-form serialization requirement emerges; the current search form uses native GET submission and the JS only handles sync/parse/index POST forms.
- Test: `tests/integration/test_listserv_ui.py`, `tests/integration/test_listserv_web_sqlite.py`, `tests/unit/test_listserv_forms_smoke.py`, `tests/unit/_listserv_forms_smoke.js`.
- Modify if affected: `docs/web-server.md`.

**Interfaces:**
- Consumes: existing stored list IDs in template context and existing search form parameter `list_id`.
- Produces: a free-text input named `list_id` backed by a `<datalist>` of known IDs; blank means no filter and all existing rich-filter tokens pass through unchanged.

- [x] **Step 1: Write failing tests.** In the rendered form assert an editable `list_id` text input references suggestions containing stored IDs. Exercise native GET submissions of `"SA%"`, `"!SA%"`, `"null"`, `"not-null"`, and `""`; assert the submitted nonblank values reach `ListservFilters.list_id` unchanged and a blank value becomes no filter. With stored records from `SA1`, `SA2`, and `CT1`, assert the UI `SA%` query renders exactly the `SA1` and `SA2` records.
- [x] **Step 2: Run tests.** `python -m pytest -q tests/integration/test_listserv_ui.py tests/integration/test_listserv_web_sqlite.py tests/unit/test_listserv_forms_smoke.py`. Confirm the new free-text/form assertions fail against the selector; observed 2 failed and 34 passed in 9.05s, with the expected missing-input and blank-filter failures.
- [x] **Step 3: Implement.** Replace only the search form's list-ID selector with the declared input and datalist; retain labels, accessible association, known suggestions, form name, and native GET submission. In `/listserv`, map an empty/whitespace-only `list_id` to no filter while preserving every nonblank value exactly. Do not change the JSON API, sync/parse exact-ID controls, or filter grammar.
- [x] **Step 4: Verify.** Reran the Step 2 command; all tests, including the JavaScript smoke test, passed (36 passed in 8.62s). Updated `docs/web-server.md` with the UI rich-filter grammar and blank-means-all behavior.

### Task 5: Deduplicate page rows before date filtering

**Files:**
- Modify: `src/doc3gpp/services/listserv_sync.py`.
- Test: `tests/integration/test_listserv_sync_sqlite.py`.
- Modify if affected: `docs/processes.md`.

**Interfaces:**
- Consumes: parsed page `list[ListservMessage]`, canonical message URL, existing `dates` predicate, repository `commit_page` and post-commit index update.
- Produces: one final-occurrence message per URL passed to the existing date filter/commit path; page coverage and next-page handling remain unchanged.

- [x] **Step 1: Write failing tests.** With a date filter for `2026-01-15`, parse the same canonical URL twice: first `2026-01-15`, last `2026-01-16`; assert no earlier row is inserted or indexed, an existing record/revision is not overwritten, and coverage/next URLs are still recorded. Reverse the dates and change final subject; assert only the final `2026-01-15` row is persisted/indexed once with the expected source revision. Include a timezone-bearing displayed date to ensure filtering uses local date, not UTC.
- [x] **Step 2: Run tests.** `python -m pytest -q tests/integration/test_listserv_sync_sqlite.py -k 'duplicate'`. Confirm the out-of-range final-occurrence case fails against the old filter order.
- [x] **Step 3: Implement.** Collapse each parsed page by canonical URL, keeping the final occurrence, before the existing date predicate; retain stable discovery behavior, page coverage/pagination, transaction boundaries and repository-level deduplication.
- [x] **Step 4: Verify.** Run `python -m pytest -q tests/integration/test_listserv_sync_sqlite.py tests/integration/test_listserv_repository_sqlite.py tests/integration/test_listserv_index_sqlite.py`. Expected: all pass. Update runtime docs if they describe page filtering/deduplication.

### Task 6: Final verification and handoff

**Files:** All files changed in Tasks 1–5 and their maintained guides; no additional product behavior.

**Interfaces:** Consumes all five completed tasks; produces fresh verification evidence and an unstaged, uncommitted worktree.

- [x] **Step 1: Check scope and whitespace.** `git diff --check` passed; `git diff --cached --quiet` confirms no staged changes. `git status --short --branch` still shows the intended dirty/untracked `feature/listserv-archives` work. No unrelated files were overwritten or staged/committed.
- [x] **Step 2: Run lint.** Full `ruff check .` reports 17 pre-existing `RUF100` unused-DTZ-`noqa` markers outside this remediation. Fixed the four lint findings in the Task 2 test file; `ruff check . --ignore RUF100` passes. Ruling: preserve existing markers rather than edit unrelated dirty feature files; cost if wrong: full lint remains nonzero until that cleanup is separately authorized.
- [x] **Step 3: Run complete offline SQLite suite.** The default xdist run had four subprocess/health-check timing failures; all four passed with `-n 0`, so the authoritative full suite was run serially as supported by the script. Initial serial run: 4006 passed, 1 skipped, 10 deselected, 13 warnings in 328.58s. After the whole-branch review fixes, reran `env PATH=/tmp/opencode/doc3gpp-listserv-review-venv/bin:$PATH ./scripts/test_sqlite.sh -n 0`: 4009 passed, 1 skipped, 10 deselected, 13 warnings in 308.42s (91% coverage), exit 0. The semantic/sqlite-vec and JavaScript form tests executed. Ruling: use documented serial mode; cost if wrong: parallel-only timing issues may be missed.
- [x] **Step 4: Check docs and contracts.** The full suite passed documented-link, CLI-help, config/schema and payload-contract tests. No public payload/schema changes were made. Report the single skip, warnings, lint limitation, and unchanged dirty/unstaged/uncommitted worktree accurately.
