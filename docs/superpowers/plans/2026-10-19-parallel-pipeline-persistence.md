# Parallel Pipeline Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Parallelize queued TDoc/spec preparation while routing every related database mutation through one global writer without blocking search reads.

**Architecture:** Add a process-scoped `PipelineRuntime` with a bounded preparation executor and a fixed one-worker persistence executor. Refactor TDoc, spec, and spec-document services into prepare/persist operations while retaining synchronous compatibility for CLI callers; queued handlers orchestrate futures through the runtime. SQLite readers use independent connections/WAL snapshots and never acquire the persistence lane.

**Tech Stack:** Python 3.10+, `asyncio`, `concurrent.futures`, SQLAlchemy 2, SQLite WAL, FastAPI lifespan, pytest.

**Spec:** `docs/superpowers/specs/2026-10-19-parallel-pipeline-persistence-design.md`

## Global Constraints

- Preparation may fetch, download, convert, parse, extract, and chunk concurrently.
- All queued TDoc/spec database save/update work uses one process-wide persistence lane with exactly one writer worker.
- Search reads never submit to or wait on the persistence queue.
- Persistence commands own short transactions and item-level atomicity; preparation performs no repository mutations.
- `server.max_concurrent_jobs` remains the preparation/job pressure limit; the persistence worker count is not configurable.
- Existing job kinds, route/MCP payloads, CLI service contracts, skip rules, and terminal summary fields remain compatible.
- Cancellation stops unsent work, allows already-submitted writes to finish atomically, and drains the writer before engine disposal.
- Use TDD, preserve layered architecture, and update relevant architecture/server documentation with the behavior change.

## Review Focus

- A slow persistence command must not serialize or starve search reads — pin with a concurrent SQLite search integration test.
- Two different jobs must never mutate the database concurrently — pin with a cross-job writer barrier test.
- A cancelled job must not discard a write already submitted to the writer — pin with a cancellation/drain test.
- A failed item must not terminate later items or the global writer — pin with failure-isolation tests.
- Cache-key collisions and multi-table TDoc/spec writes must remain safe — pin with per-item atomicity and cache identity tests.

---

### Task 1: Add the shared pipeline runtime

**Files:**
- Create: `src/doc3gpp/web/workers/pipeline.py`
- Modify: `src/doc3gpp/web/state.py`
- Modify: `src/doc3gpp/web/app.py`
- Modify: `src/doc3gpp/web/workers/job_worker.py`
- Test: `tests/unit/test_pipeline_runtime.py`
- Test: `tests/unit/test_job_worker.py`

**Interfaces:**
- Produces `PipelineRuntime(prepare_workers: int, max_pending: int | None = None)`.
- Produces `PipelineRuntime.submit_prepare(fn: Callable[..., T], *args, **kwargs) -> concurrent.futures.Future[T]`.
- Produces `PipelineRuntime.submit_persist(fn: Callable[[T], R], prepared: T) -> concurrent.futures.Future[R]`.
- Produces `PipelineRuntime.shutdown(*, wait: bool = True, cancel_pending_preparation: bool = False) -> None`.
- `JobWorkerHandle` owns an optional `pipeline: PipelineRuntime`; `WebState.jobs` exposes it to handlers.

- [ ] **Step 1: Write failing runtime tests** for overlapping preparation, strictly non-overlapping persistence, bounded pending submissions, writer failure recovery, and shutdown draining.
- [ ] **Step 2: Run `pytest tests/unit/test_pipeline_runtime.py -q`** and verify the new tests fail because the runtime does not exist.
- [ ] **Step 3: Implement `PipelineRuntime`** with one bounded preparation executor, `ThreadPoolExecutor(max_workers=1)` persistence executor, a pending-work semaphore, and future cleanup that releases capacity on completion.
- [ ] **Step 4: Add lifecycle wiring** so `build_app` creates the runtime before starting `JobWorker`, `JobWorkerHandle.shutdown()` drains it after the worker stops, and engine disposal occurs afterward.
- [ ] **Step 5: Add worker tests** proving existing non-pipeline jobs retain current behavior and shutdown signals pipeline cancellation without closing engines early.
- [ ] **Step 6: Run `pytest tests/unit/test_pipeline_runtime.py tests/unit/test_job_worker.py -q`** and verify PASS.
- [ ] **Step 7: Commit** with `git add src/doc3gpp/web/workers/pipeline.py src/doc3gpp/web/state.py src/doc3gpp/web/app.py src/doc3gpp/web/workers/job_worker.py tests/unit/test_pipeline_runtime.py tests/unit/test_job_worker.py && git commit -m "feat: add shared pipeline runtime"`.

### Task 2: Split TDoc extraction into preparation and persistence

**Files:**
- Modify: `src/doc3gpp/services/tdoc_cr_service.py`
- Modify: `src/doc3gpp/services/tdoc_sync_coordinator.py` (only if TDoc sync needs prepared list/file writes)
- Modify: `src/doc3gpp/models/tdoc_cr.py` (prepared DTOs if the existing DTOs cannot carry all write inputs)
- Test: `tests/unit/test_tdoc_cr_service.py`
- Test: `tests/unit/test_tdoc_cr_service_ftp_batch.py`
- Test: `tests/unit/test_tdoc_pipeline.py` (create)

**Interfaces:**
- Add an immutable `PreparedTDocExtract` value object containing the normalized URL/id, cover, optional TTCN/change sidecars, extract metadata, cache identity, and post-parse index inputs.
- Add `TDocCrService.prepare_extract(tdoc_id: str, *, force: bool, full: bool) -> PreparedTDocExtract | ExtractResult` with no repository mutation.
- Add `TDocCrService.persist_prepared_extract(prepared: PreparedTDocExtract) -> ExtractResult` that performs the existing cover/sidecar/meta/index writes on the writer thread.
- Keep `extract()` as a synchronous compatibility wrapper that calls prepare then persist directly.

- [ ] **Step 1: Write failing tests** asserting preparation performs download/conversion/parsing but no repository `upsert`, and persistence performs all TDoc writes in the existing order.
- [ ] **Step 2: Add tests** for DB-cache hits, missing FTP URLs, oversized files, TTCN sidecars, non-TTCN change details, direct URL FK misses, and cache-file identity.
- [ ] **Step 3: Run the focused tests** and verify the new prepare/persist tests fail.
- [ ] **Step 4: Extract the current fresh-extract body** into `prepare_extract`, returning a value DTO instead of writing cover/sidecar/meta rows or search/vector sidecars.
- [ ] **Step 5: Implement `persist_prepared_extract`** so one writer command performs the existing multi-table writes and post-parse index/embedding work with the same error semantics.
- [ ] **Step 6: Rewrite `extract()`** as the compatibility wrapper and preserve DB-cache short-circuit behavior.
- [ ] **Step 7: Extend URL-folder orchestration** so each discovered file can prepare independently and submit its prepared result to the shared writer without concurrent repository mutation.
- [ ] **Step 8: Run `pytest tests/unit/test_tdoc_cr_service.py tests/unit/test_tdoc_cr_service_ftp_batch.py tests/unit/test_tdoc_pipeline.py -q`** and verify PASS.
- [ ] **Step 9: Commit** with `git add ... && git commit -m "refactor: split tdoc preparation from persistence"`.

### Task 3: Split specification sync into preparation and persistence

**Files:**
- Modify: `src/doc3gpp/services/spec_service.py`
- Modify: `src/doc3gpp/models/spec.py` (only if a prepared aggregate is needed)
- Test: `tests/unit/test_spec_service.py`
- Test: `tests/unit/test_spec_pipeline.py` (create)

**Interfaces:**
- Add immutable `PreparedSpecSync` containing the parsed header, versions, canonical TSG, and sync metadata needed by the writer.
- Add `SpecService.prepare_spec(spec: Spec, canonical: str, client: ScraperClient, *, per_version_details: bool) -> PreparedSpecSync` with no repository mutation.
- Add `SpecService.persist_prepared_spec(prepared: PreparedSpecSync) -> int` that performs header/version/timestamp updates on the writer thread.
- Keep `sync()` and `sync_spec()` behavior-compatible by using the split operations synchronously when called outside queued jobs.

- [ ] **Step 1: Write failing tests** proving detail/follow-up fetches and parsing may complete without repository writes, while persistence performs all three existing header/version/timestamp writes.
- [ ] **Step 2: Add tests** for per-spec interval skips, `force`, follow-up failures, missing-spec bootstrap, and `last_synced_at` not being stamped after a partial write failure.
- [ ] **Step 3: Run `pytest tests/unit/test_spec_service.py tests/unit/test_spec_pipeline.py -q`** and verify the split tests fail.
- [ ] **Step 4: Extract detail/follow-up work** into `prepare_spec`, retaining the dedicated follow-up executor only for network work within preparation.
- [ ] **Step 5: Implement `persist_prepared_spec`** with the existing upsert order and timestamp safeguards; recheck current persisted identity/skip state immediately before mutation where required.
- [ ] **Step 6: Refactor `sync()` / `sync_spec()`** to preserve their synchronous public contracts while allowing queued handlers to submit one spec preparation and one persistence command at a time.
- [ ] **Step 7: Run focused spec tests** and verify PASS.
- [ ] **Step 8: Commit** with `git add ... && git commit -m "refactor: split spec preparation from persistence"`.

### Task 4: Split spec-document parse into preparation and persistence

**Files:**
- Modify: `src/doc3gpp/services/spec_doc_service.py`
- Modify: `src/doc3gpp/models/spec_doc.py` (prepared aggregate if needed)
- Test: `tests/unit/test_spec_doc_service_reads.py`
- Test: `tests/unit/test_spec_doc_pipeline.py` (create)

**Interfaces:**
- Add immutable `PreparedSpecDocParse` containing source identity, download metadata, ordered file blocks, TOC, chunk drafts, markdown-cache inputs, and index/embedding inputs.
- Add `SpecDocService.prepare_parse(spec_id: str, *, release: str | None, version: str | None, force: bool) -> PreparedSpecDocParse | SpecDocSource` with no database mutation.
- Add `SpecDocService.persist_prepared_parse(prepared: PreparedSpecDocParse) -> SpecDocSource` for source, TOC, chunks, parsed marker, and post-parse index/embedding writes.
- Keep `parse()` / `parse_many()` compatible by composing preparation and persistence synchronously.

- [ ] **Step 1: Write failing tests** proving ZIP fetch/cache, DOCX conversion, TOC extraction, and chunking do not call repository mutation methods.
- [ ] **Step 2: Add tests** for already-parsed skip, force reparse, oversized ZIP skip, unknown version failure, empty ZIP failure, and markdown cache output.
- [ ] **Step 3: Run the focused tests** and verify the new split tests fail.
- [ ] **Step 4: Extract selection/fetch/conversion/chunking** into `prepare_parse`, preserving release/version validation and cache behavior.
- [ ] **Step 5: Implement `persist_prepared_parse`** with one logical writer command covering download metadata, TOC/chunks, parsed marker, and post-parse indexes.
- [ ] **Step 6: Refactor `parse()` / `parse_many()`** to retain current batch result buckets and progress behavior.
- [ ] **Step 7: Run `pytest tests/unit/test_spec_doc_service_reads.py tests/unit/test_spec_doc_pipeline.py -q`** and verify PASS.
- [ ] **Step 8: Commit** with `git add ... && git commit -m "refactor: split spec document preparation from persistence"`.

### Task 5: Orchestrate queued handlers through the shared runtime

**Files:**
- Modify: `src/doc3gpp/web/workers/handlers.py`
- Modify: `src/doc3gpp/web/workers/job_worker.py` (only for shared runtime access/progress helpers)
- Test: `tests/unit/test_pipeline_handlers.py` (create)
- Test: `tests/integration/test_parse_tdoc_url_job_sqlite.py`

**Interfaces:**
- Add handler helpers that submit preparation futures and then persistence futures through `services` plus `state.jobs.pipeline`.
- Preserve handler return mappings for `PARSE_TDOCS`, `PARSE_TDOC_URL`, `SYNC_SPECS`, and `PARSE_SPEC_DOCS`.
- Use `asyncio.wrap_future`/equivalent awaiting so no blocking future `.result()` runs on the event loop.

- [ ] **Step 1: Write failing handler tests** with fake preparation barriers, asserting multiple items overlap and all persistence calls go through one shared writer.
- [ ] **Step 2: Add cross-job tests** proving a TDoc and spec job share the same writer and that writer failure only affects its item.
- [ ] **Step 3: Add cancellation tests** proving no new preparation is scheduled after cancellation while already-submitted writes complete.
- [ ] **Step 4: Run the focused handler tests** and verify failure before orchestration changes.
- [ ] **Step 5: Refactor `_parse_tdocs` and `_parse_tdoc_url`** to prepare per item, submit persistence, aggregate results, and retain current skip/failure/file summary fields.
- [ ] **Step 6: Refactor `_sync_specs`** to prepare each detail result in parallel and submit each spec aggregate to the global writer.
- [ ] **Step 7: Refactor `_parse_spec_docs`** to use the same prepare/persist flow while preserving its all-failed terminal-job rule and `failure_details` output.
- [ ] **Step 8: Run `pytest tests/unit/test_pipeline_handlers.py tests/integration/test_parse_tdoc_url_job_sqlite.py -q`** and verify PASS.
- [ ] **Step 9: Commit** with `git add ... && git commit -m "feat: parallelize queued tdoc and spec pipelines"`.

### Task 6: Ensure search reads do not block on pipeline writes

**Files:**
- Modify: `src/doc3gpp/storage/backends/sqlite.py`
- Modify: `src/doc3gpp/storage/db/session.py` (only if separate read engine/session configuration is needed)
- Modify: `src/doc3gpp/web/routes/` search adapters only where a blocking call is currently on the event loop
- Modify: `src/doc3gpp/web/mcp_server.py` search adapters only where needed
- Test: `tests/unit/test_sqlite_backend.py` (create or extend existing backend test)
- Test: `tests/integration/test_search_during_pipeline_write.py` (create)

**Interfaces:**
- Keep `configure_sqlite_engine()` as the single backend hook for `foreign_keys`, `journal_mode=WAL`, and `busy_timeout`.
- Search APIs remain independent of `PipelineRuntime`; no search method accepts a persistence lock or queue.

- [ ] **Step 1: Write a failing integration test** that holds a writer transaction on one item, executes a search on a separate connection, and asserts the search returns before the writer releases.
- [ ] **Step 2: Add a backend test** asserting SQLite connections use WAL and a busy timeout without changing non-SQLite behavior.
- [ ] **Step 3: Audit HTTP/MCP search call paths** and add `asyncio.to_thread`/read-executor dispatch where an async endpoint would otherwise execute blocking SQL directly.
- [ ] **Step 4: Keep writer/index transactions short** and ensure FTS/vector updates occur inside the writer command, not preparation.
- [ ] **Step 5: Run `pytest tests/unit/test_sqlite_backend.py tests/integration/test_search_during_pipeline_write.py -q`** and verify PASS.
- [ ] **Step 6: Commit** with `git add ... && git commit -m "feat: keep search reads responsive during pipeline writes"`.

### Task 7: Documentation, full verification, and branch review

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/web-server.md`
- Modify: `docs/conventions.md` if concurrency guidance belongs there
- Modify: `AGENTS.md` only if the durable architecture map needs a pointer
- Test: full existing test suite and all new tests

- [ ] **Step 1: Document** the preparation pool, global one-worker persistence lane, lifecycle, SQLite read behavior, and search responsiveness guarantee.
- [ ] **Step 2: Run `ruff check .`** and fix all lint failures.
- [ ] **Step 3: Install declared development dependencies if needed** (`pip install -e '.[dev]'`) and rerun `./scripts/test_sqlite.sh`; record any environment-only failures separately.
- [ ] **Step 4: Run the focused concurrency suite** covering runtime, handlers, TDoc, spec, spec-doc, SQLite, and search integration tests.
- [ ] **Step 5: Inspect `git diff --check`, `git status`, and the complete branch diff** for accidental API changes, unbounded queues, repository writes outside the writer, and event-loop blocking.
- [ ] **Step 6: Commit documentation and verification fixes** with `git add ... && git commit -m "docs: document parallel pipeline concurrency"`.
- [ ] **Step 7: Run the verification-before-completion checklist** and report exact commands/results, including any dependency or online-test limitations.
