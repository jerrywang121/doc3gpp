# Cross-Surface TDoc Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make database-ID, CLI local-folder, CLI/web FTP-folder, direct-file, HTTP, and MCP TDoc ingestion use one bounded parallel preparation pipeline with one serialized persistence lane per process.

**Architecture:** Move the generic bounded executor from the web layer into `services.pipeline_runtime`, then build a framework-neutral `TDocPipeline` runner around ordered items, preparation callbacks, optional persistence callbacks, cancellation, and one exception classifier. The web application owns one runtime for all queued jobs; each CLI batch invocation owns and drains one runtime; HTTP and MCP remain thin enqueue/read adapters.

**Tech Stack:** Python 3.10+, `concurrent.futures`, `asyncio`, SQLAlchemy 2.0, Pydantic v2/pydantic-settings, Typer, pytest, SQLite WAL/read connections.

**Spec:** `docs/superpowers/specs/2026-10-19-cross-surface-tdoc-pipeline-design.md`

## Global Constraints

- Scope is all TDoc surfaces only; do not change spec-sync or spec-document pipeline semantics beyond consuming the moved runtime.
- Preparation may perform network, filesystem, cache, conversion, and parser work; repository mutation must happen only on the serialized writer lane.
- The runtime has one bounded preparation pool and exactly one persistence worker; database writes must never run concurrently within a process.
- `prepare_workers` defaults to the CPU-aware bounded value equivalent to `min(32, (os.cpu_count() or 1) + 4)`; pending capacity is `prepare_workers * 2`.
- `server.max_concurrent_jobs` continues to control active web jobs and no longer controls per-item preparation workers.
- Web owns one process-wide runtime; each CLI invocation creates, drains, and shuts down one runtime; MCP creates no runtime and continues enqueueing existing jobs.
- Async callers must never perform a blocking future wait or blocking capacity wait on the event loop.
- FTP discovery remains deterministic breadth-first; preparation is the parallel phase. Results and local output remain in source order.
- `TDocNotYetOnFTPError` and `TDocTooLargeError` are skips; other per-item exceptions use `ExceptionClass: message` failure reasons; one persistence failure must not stop later items.
- Preserve `BatchExtractResult`, `DirectParseBatchResult`, local output/counters, CLI exit codes, HTTP/MCP request/enqueue envelopes, queued job status policy, cache-key identity, and cancellation semantics.
- Do not add a per-command worker flag in this change.
- Preserve the three pre-existing uncommitted bugfix files (`docs/web-server.md`, `src/doc3gpp/web/workers/handlers.py`, and `tests/unit/test_pipeline_handlers.py`); never reset or discard their hunks. Stage overlapping files by reviewed hunks when committing.

## Review Focus

- **Capacity wait while the event loop is busy:** an async caller waiting beyond `prepare_workers * 2` must allow unrelated event-loop work to run; pin this in `tests/unit/test_pipeline_runtime.py::test_async_capacity_wait_does_not_block_event_loop` and `tests/unit/test_tdoc_pipeline.py::test_async_runner_applies_backpressure_without_blocking`.
- **Writer failure isolation:** one failed persistence callback must become one failed outcome while later prepared items still persist; pin this in `tests/unit/test_tdoc_pipeline.py::test_writer_failure_is_isolated_to_one_item`.
- **Cancellation during a saturated folder/database batch:** no new preparation starts after cancellation, active preparation can finish, submitted writes drain, and resources are not disposed early; pin this in runner cancellation tests and the web/CLI batch integration tests.
- **Direct URL completion variants:** cache hits, FK-miss/in-memory parses, and prepared FK hits must preserve the existing `DirectParseBatchResult` and never send already-complete results through the writer; pin this in `tests/unit/test_tdoc_cr_service_ftp_batch.py` and `tests/integration/test_tdoc_parse_ftp_batch_sqlite.py`.
- **Revision/cache/output ordering:** concurrent preparation must not corrupt cache artifacts, must preserve revision-specific keys, and must emit local files in sorted source order with existing skip counters; pin this in `tests/unit/test_tdoc_cache.py`, `tests/unit/test_tdoc_parse_direct.py`, and the local/FTP integration tests.

## File Map

### Runtime and shared runner

- **Create:** `src/doc3gpp/services/pipeline_runtime.py` — framework-neutral bounded preparation executor, single writer executor, capacity permits, async/sync capacity acquisition, and shutdown.
- **Create:** `src/doc3gpp/services/tdoc_pipeline.py` — ordered item/outcome DTOs, common classifier, synchronous/async runner, and progress/cancellation plumbing.
- **Modify:** `src/doc3gpp/web/workers/pipeline.py` — compatibility re-export of the service runtime; no second runtime implementation.
- **Modify:** `tests/unit/test_pipeline_runtime.py` and `tests/unit/test_tdoc_pipeline.py` — runtime/runner contract tests.

### Configuration and ownership

- **Modify:** `src/doc3gpp/settings/schema.py` — add `PipelineSettings` and `Settings.pipeline`.
- **Modify:** `src/doc3gpp/web/app.py` — create the process-wide runtime from `[pipeline] prepare_workers` while retaining `server.max_concurrent_jobs` for active jobs.
- **Modify:** `src/doc3gpp/web/state.py` — use the service runtime type and drain it without blocking the event loop during shutdown.
- **Modify:** `tests/unit/test_settings.py`, `tests/unit/test_server_settings.py`, and relevant web lifecycle tests — default/override and ownership assertions.

### TDoc service adapters

- **Modify:** `src/doc3gpp/services/tdoc_cr_service.py` — route `extract_many`, `extract_from_url_batch`, and direct one-item methods through the shared runner while retaining synchronous compatibility signatures and public result types.
- **Modify:** `src/doc3gpp/scraping/cache.py` — serialize cache installation and size enforcement/eviction without removing atomic file replacement.
- **Modify:** `tests/unit/test_tdoc_cr_service.py`, `tests/unit/test_tdoc_cr_service_ftp_batch.py`, and `tests/unit/test_tdoc_cache.py` — service parity, writer isolation, URL variants, and concurrent cache safety.

### Web and CLI adapters

- **Modify:** `src/doc3gpp/web/workers/handlers.py` — replace private batch preparation/persistence loops with the async shared runner for TDoc IDs and FTP folders; preserve the earlier detail-map fix.
- **Modify:** `src/doc3gpp/cli.py` — create/drain one runtime per command invocation; route database, local-folder, FTP-folder, and direct single-file paths through the runner while preserving rendering and exit behavior.
- **Modify:** `tests/unit/test_pipeline_handlers.py`, `tests/unit/test_tdoc_parse_cli.py`, `tests/unit/test_tdoc_parse_direct.py`, `tests/integration/test_tdoc_parse_local_batch_sqlite.py`, `tests/integration/test_tdoc_parse_ftp_batch_sqlite.py`, and `tests/integration/test_parse_tdoc_url_job_sqlite.py` — reachability, overlap, ordering, skips, failures, and compatibility.
- **Modify:** `src/doc3gpp/web/routes/jobs.py` and `src/doc3gpp/web/mcp_server.py` only if adapter typing/tests require it; request/enqueue shapes must remain unchanged.

### Documentation and operational contract

- **Modify:** `docs/architecture.md`, `docs/cli.md`, `docs/web-server.md`, `doc3gpp.toml.example`, `AGENTS.md`, `README.md`, and `docs/conventions.md` only where the shared runner, `[pipeline]` setting, lifecycle, or surface behavior is currently summarized inaccurately.

---

### Task 1: Extract and harden the shared pipeline runtime

**Files:**
- Create: `src/doc3gpp/services/pipeline_runtime.py`
- Modify: `src/doc3gpp/web/workers/pipeline.py`
- Test: `tests/unit/test_pipeline_runtime.py`

**Interfaces:**
- Produces `PipelineRuntime(prepare_workers: int, max_pending: int | None = None)` with `max_pending` defaulting to `prepare_workers * 2`.
- Produces `PipelinePermit.release() -> None`, idempotent and held from preparation submission through either persistence completion or complete-result handling.
- Produces `PipelineRuntime.reserve() -> PipelinePermit` for synchronous callers and `async def reserve_async() -> PipelinePermit` for async callers.
- Produces `submit_prepare(permit: PipelinePermit, fn: Callable[..., T], *args: object, **kwargs: object) -> Future[T]`.
- Produces `submit_persist(permit: PipelinePermit, fn: Callable[[T], R], prepared: T) -> Future[R]`.
- Produces `shutdown(*, wait: bool = True, cancel_pending_preparation: bool = False) -> None` and `async shutdown_async(*, cancel_pending_preparation: bool = False) -> None`, with the async adapter delegating the blocking drain off the event loop.

- [ ] **Step 1: Write failing runtime tests.** Add tests for preparation-worker count, one-worker persistence serialization, shared pending capacity, idempotent permit release, async capacity waiting, and shutdown draining the writer. In the async capacity test, hold the only permit, start `reserve_async()`, yield to an unrelated coroutine, then release and assert the waiter completes.
- [ ] **Step 2: Run the runtime tests to verify the expected failures.**

  Run: `PATH="$PWD/.venv/bin:$PATH" python -m pytest tests/unit/test_pipeline_runtime.py -q`

  Expected: the new API/tests fail against the current web-only runtime or missing service module.
- [ ] **Step 3: Implement `PipelineRuntime` in `src/doc3gpp/services/pipeline_runtime.py`.** Use one bounded preparation executor, one persistence executor with `max_workers=1`, and one process-wide capacity counter represented by permits. `reserve_async()` must wait through an awaitable/non-event-loop-blocking path; `submit_*` must not acquire a second hidden capacity slot. Keep shutdown ordering preparation first, then writer drain, and make permit release safe in callbacks/finally blocks.
- [ ] **Step 4: Replace the web implementation with a compatibility re-export.** Make `src/doc3gpp/web/workers/pipeline.py` import and expose the service-level `PipelineRuntime`, preserving existing internal import paths and avoiding two runtime classes.
- [ ] **Step 5: Run the focused runtime tests.**

  Run: `PATH="$PWD/.venv/bin:$PATH" python -m pytest tests/unit/test_pipeline_runtime.py -q`

  Expected: all runtime tests pass.
- [ ] **Step 6: Commit the runtime extraction.** Stage only the new runtime, compatibility re-export, and runtime tests; do not stage unrelated pre-existing working-tree hunks.

  Commit: `git commit -m "refactor: move pipeline runtime into services"`

### Task 2: Add the shared ordered TDoc runner and classifier

**Files:**
- Create: `src/doc3gpp/services/tdoc_pipeline.py`
- Modify: `tests/unit/test_tdoc_pipeline.py`

**Interfaces:**
- Produces `PipelineItem[T](key: str, value: T)` with source-order identity.
- Produces `PipelineOutcome[R](key: str, status: Literal["success", "skipped", "failed"], value: R | None, reason: str | None)`.
- Produces `PipelineProgress(completed: int, total: int, key: str, outcome: PipelineOutcome[object])`.
- Produces `classify_tdoc_exception(exc: Exception) -> tuple[Literal["skipped", "failed"], str]`, mapping `TDocNotYetOnFTPError` and `TDocTooLargeError` to skipped and all other non-cancellation exceptions to failed with `ExceptionClass: message`.
- Produces `TDocPipeline(runtime: PipelineRuntime)` with:
  - `run_sync(items: Sequence[PipelineItem[I]], prepare: Callable[[I], P | R], *, persist: Callable[[P], R] | None = None, is_complete: Callable[[P | R], bool] | None = None, is_cancelled: Callable[[], bool] | None = None, on_progress: Callable[[PipelineProgress], None] | None = None) -> list[PipelineOutcome[R | P]]`.
  - `async run_async(...) -> list[PipelineOutcome[R | P]]` with the same parameters and no blocking future/capacity waits on the event loop.

- [ ] **Step 1: Write failing runner tests.** Add tests named `test_prepare_tasks_overlap_but_persist_tasks_are_serialized`, `test_writer_failure_is_isolated_to_one_item`, `test_outcomes_are_returned_in_source_order`, `test_async_runner_applies_backpressure_without_blocking`, and `test_cancellation_stops_new_submissions_and_drains_writer`. Use events/barriers rather than sleeps where possible; assert later writer items run after an earlier writer failure and that skips/failures carry exact reason strings.
- [ ] **Step 2: Run the runner tests to verify the expected failures.**

  Run: `PATH="$PWD/.venv/bin:$PATH" python -m pytest tests/unit/test_tdoc_pipeline.py -q`

  Expected: FAIL because the ordered runner/outcome API is not present.
- [ ] **Step 3: Implement the common classifier and runner.** Keep the existing exception definitions/re-exports stable and avoid a top-level import cycle with `TDocCrService` (the classifier may resolve the existing `TDocNotYetOnFTPError` lazily). Schedule no more than the runtime’s available permits; submit preparation concurrently; transfer a prepared value to the single writer without increasing capacity; treat `is_complete(value)` as a finished result and release its permit without writing; convert exceptions to outcomes; propagate cancellation after stopping new submissions and draining submitted writes; reconstruct the final list in source order; emit progress as items reach terminal outcomes.
- [ ] **Step 4: Run focused runner tests.**

  Run: `PATH="$PWD/.venv/bin:$PATH" python -m pytest tests/unit/test_tdoc_pipeline.py -q`

  Expected: all new and existing TDoc pipeline tests pass.
- [ ] **Step 5: Commit the runner.**

  Commit: `git commit -m "feat: add shared tdoc pipeline runner"`

### Task 3: Add shared configuration and wire runtime ownership

**Files:**
- Modify: `src/doc3gpp/settings/schema.py`
- Modify: `src/doc3gpp/web/app.py`
- Modify: `src/doc3gpp/web/state.py`
- Modify: `tests/unit/test_settings.py`, `tests/unit/test_server_settings.py`, and affected web lifecycle tests

**Interfaces:**
- Produces `PipelineSettings.prepare_workers: int` with default `min(32, (os.cpu_count() or 1) + 4)` and validation for a positive bounded worker count.
- Produces `Settings.pipeline: PipelineSettings`, loaded from `[pipeline]` with the project’s existing settings precedence rules.
- `build_app()` creates one `PipelineRuntime(prepare_workers=settings.pipeline.prepare_workers)` for the process and stores it in the existing service/job state.

- [ ] **Step 1: Add failing settings and lifecycle tests.** Pin the CPU-aware default, TOML `[pipeline] prepare_workers` override, `server.max_concurrent_jobs` independence, one runtime shared by web services/jobs, and runtime shutdown after the worker stops.
- [ ] **Step 2: Run the focused settings/lifecycle tests and confirm red.**

  Run: `PATH="$PWD/.venv/bin:$PATH" python -m pytest tests/unit/test_settings.py tests/unit/test_server_settings.py tests/unit/test_job_worker.py -q`

  Expected: the new setting/ownership assertions fail before implementation.
- [ ] **Step 3: Implement `PipelineSettings` and `Settings.pipeline`.** Follow existing Pydantic settings patterns and use a default factory so the CPU count is evaluated safely without introducing an environment-dependent import-time constant.
- [ ] **Step 4: Wire web lifespan and non-blocking shutdown.** Build the runtime from `settings.pipeline.prepare_workers`, retain `server.max_concurrent_jobs` as the worker’s active-job limit, expose the same runtime to all queued services, and await runtime draining through a thread/async adapter before disposing engines and clients.
- [ ] **Step 5: Run focused tests.**

  Run: `PATH="$PWD/.venv/bin:$PATH" python -m pytest tests/unit/test_settings.py tests/unit/test_server_settings.py tests/unit/test_job_worker.py -q`

  Expected: PASS.
- [ ] **Step 6: Commit configuration and ownership wiring.**

  Commit: `git commit -m "feat: configure shared pipeline workers"`

### Task 4: Refactor `TDocCrService` batch and direct APIs onto the runner

**Files:**
- Modify: `src/doc3gpp/services/tdoc_cr_service.py`
- Modify: `tests/unit/test_tdoc_cr_service.py`
- Modify: `tests/unit/test_tdoc_cr_service_ftp_batch.py`

**Interfaces:**
- Extend `extract_many(..., runner: TDocPipeline | None = None) -> BatchExtractResult`; an omitted runner creates and drains a one-worker compatibility runtime so existing library callers keep working.
- Extend `extract_from_url_batch(..., runner: TDocPipeline | None = None) -> DirectParseBatchResult`; discovery remains synchronous/deterministic, while discovered file preparation uses the runner.
- Extend `extract_from_url(..., runner: TDocPipeline | None = None) -> DirectParseResult` and `extract_from_bytes(..., runner: TDocPipeline | None = None) -> DirectParseResult` with keyword-only runner parameters so direct URL and local-byte calls can use the same one-item preparation contract without changing existing positional/keyword behavior.
- Keep `PreparedTDocExtract`, `PreparedDirectTDoc`, `prepare_extract`, `persist_prepared_extract`, `prepare_from_url`, `persist_prepared_url`, `BatchExtractResult`, and `DirectParseBatchResult` intact.

- [ ] **Step 1: Add failing service tests.** Add a database-batch overlap test with a two-worker runner and a recording preparation callback, a serialized-persistence assertion, a writer-failure/later-item test, and URL-batch tests proving deterministic BFS discovery plus concurrent file preparation. Add parity assertions for FTP-unavailable and oversized skips, ordinary failures, cache hits, FK misses, and revision-specific URLs.
- [ ] **Step 2: Run focused service tests to verify red.**

  Run: `PATH="$PWD/.venv/bin:$PATH" python -m pytest tests/unit/test_tdoc_cr_service.py tests/unit/test_tdoc_cr_service_ftp_batch.py -q`

  Expected: new runner-injection/overlap assertions fail while existing compatibility tests remain the baseline.
- [ ] **Step 3: Adapt `extract_many` to build ordered `PipelineItem`s and invoke `TDocPipeline.run_sync`.** Use `prepare_extract` for preparation and `persist_prepared_extract` for prepared DTOs; recognize an existing `ExtractResult` as complete; convert ordered outcomes into the existing success/failure/skipped maps; preserve normalized keys, progress text, cancellation, and all current flags.
- [ ] **Step 4: Adapt `extract_from_url_batch` and one-item methods.** Keep `collect_3gpp_file_urls` breadth-first and ordered; pass each URL to `prepare_from_url`; send only `PreparedDirectTDoc` values to `persist_prepared_url`; treat returned `DirectParseResult` values as complete; convert outcomes to the existing URL-keyed batch result. Ensure `TDocTooLargeError` is skipped and all other non-cancellation exceptions are failed.
- [ ] **Step 5: Run focused service tests and adjacent regressions.**

  Run: `PATH="$PWD/.venv/bin:$PATH" python -m pytest tests/unit/test_tdoc_cr_service.py tests/unit/test_tdoc_cr_service_ftp_batch.py tests/unit/test_tdoc_pipeline.py -q`

  Expected: PASS, including the existing service behavior tests.
- [ ] **Step 6: Commit the service adapters.**

  Commit: `git commit -m "refactor: run tdoc service batches through shared pipeline"`

### Task 5: Make cache writes safe under concurrent preparation

**Files:**
- Modify: `src/doc3gpp/scraping/cache.py`
- Modify: `tests/unit/test_tdoc_cache.py`
- Modify: `tests/unit/test_cache_cli.py` only if shared cache fixtures require updates

**Interfaces:**
- Preserve `TDocCache.put_bytes`, `get_bytes`, `enforce_size_limit`, `purge`, `purge_subdir`, `status`, and cache-key/path behavior.
- Add internal locking around the atomic install plus size-accounting/eviction critical section; reads remain lock-light and file replacement remains atomic.

- [ ] **Step 1: Write failing concurrency tests.** Start concurrent `put_bytes` calls with a small limit and distinct revision keys; assert every retained artifact is a complete readable file, no revision key aliases another, and the reported/actual cache size respects the limit after enforcement.
- [ ] **Step 2: Run the cache tests to verify red or expose the race.**

  Run: `PATH="$PWD/.venv/bin:$PATH" python -m pytest tests/unit/test_tdoc_cache.py -q`

  Expected: the new race-sensitive assertions fail or demonstrate unguarded size enforcement before the lock is added.
- [ ] **Step 3: Implement the minimal re-entrant cache critical section.** Guard replacement and eviction/accounting operations together so another preparation cannot evict a file while a concurrent writer is accounting for it; do not hold the lock across network/download or parsing work.
- [ ] **Step 4: Run cache and service regressions.**

  Run: `PATH="$PWD/.venv/bin:$PATH" python -m pytest tests/unit/test_tdoc_cache.py tests/unit/test_tdoc_cr_service_ftp_batch.py -q`

  Expected: PASS.
- [ ] **Step 5: Commit cache safety.**

  Commit: `git commit -m "fix: serialize concurrent tdoc cache eviction"`

### Task 6: Replace web TDoc batch loops with the async shared runner

**Files:**
- Modify: `src/doc3gpp/web/workers/handlers.py`
- Modify: `tests/unit/test_pipeline_handlers.py`
- Modify: `tests/unit/test_spec_doc_pipeline.py`
- Modify: `tests/integration/test_parse_tdoc_url_job_sqlite.py`
- Modify: `tests/unit/test_job_worker.py` if shared-runtime/cross-job fixtures need assertions

**Interfaces:**
- `_parse_tdocs` continues returning `requested`, `successes`, `failures`, `skipped`, `failure_details`, and `skipped_details`; its selection, `max_batch`, `force`, `full`, cancellation, and job-status behavior do not change.
- `_parse_tdoc_url` continues returning its existing counts/files envelope; its folder branch must use the shared async runner after deterministic discovery, and its single-file branch must use the same one-item contract.

- [ ] **Step 1: Add failing web tests.** Extend the existing handler regression to prove preparation overlap, serialized persistence, cross-batch detail aggregation, and expected skip classification. Add a folder-job test with two delayed fake FTP files and assert they overlap rather than run serially while returned files remain discovery-ordered. Add a shared-runtime test showing two queued jobs never execute persistence callbacks concurrently. Update the existing spec-document pipeline test to prove its one-item preparation/persistence path uses a permit without changing `SpecDocBatchResult` success/skip/failure semantics.
- [ ] **Step 2: Run focused handler/job tests to verify red.**

  Run: `PATH="$PWD/.venv/bin:$PATH" python -m pytest tests/unit/test_pipeline_handlers.py tests/unit/test_job_worker.py tests/integration/test_parse_tdoc_url_job_sqlite.py -q`

  Expected: new overlap/shared-runtime assertions fail against the remaining private loops.
- [ ] **Step 3: Refactor `_parse_tdocs`.** Construct ordered items, call `TDocPipeline.run_async` with `prepare_extract` and `persist_prepared_extract`, recognize `ExtractResult` as complete, and fold outcomes into the existing summary/detail maps. Remove duplicate exception classification from the handler while preserving the earlier bugfix’s output fields.
- [ ] **Step 4: Refactor `_parse_tdoc_url`.** Preserve URL validation, auto-sync, depth selection, and file discovery; run `prepare_from_url` concurrently through the shared runtime; serialize only `persist_prepared_url`; convert outcomes into `DirectParseBatchResult`/existing `files` output; keep progress and cancellation behavior.
- [ ] **Step 5: Adapt `_parse_spec_docs` to the new runtime API without changing its semantics.** Reserve one permit asynchronously, submit its existing `prepare_parse` and `persist_prepared_parse` callbacks through the runtime, release the permit in `finally`, and preserve its existing `SpecDocBatchResult` and all-failed behavior. This is runtime plumbing, not a spec-document pipeline redesign.
- [ ] **Step 6: Run focused web tests and HTTP/MCP reachability tests.**

  Run: `PATH="$PWD/.venv/bin:$PATH" python -m pytest tests/unit/test_pipeline_handlers.py tests/unit/test_spec_doc_pipeline.py tests/unit/test_job_worker.py tests/unit/test_web_jobs_routes.py tests/integration/test_parse_tdoc_url_job_sqlite.py tests/integration/test_mcp_end_to_end.py -q`

  Expected: PASS; HTTP and MCP request/enqueue payloads remain unchanged while completed jobs expose the shared detail maps.
- [ ] **Step 7: Commit web integration.** Stage the new handler hunks without discarding or silently rewriting the earlier uncommitted detail-map fix.

  Commit: `git commit -m "refactor: use shared tdoc runner for web jobs"`

### Task 7: Give CLI database-mode parsing one configured runtime

**Files:**
- Modify: `src/doc3gpp/cli.py`
- Modify: `tests/unit/test_tdoc_parse_cli.py`
- Modify: `tests/integration/test_tdoc_cr_sqlite.py` or the closest existing DB-mode parse integration fixture

**Interfaces:**
- Add a CLI-owned context/helper that creates `PipelineRuntime(settings.pipeline.prepare_workers)`, exposes `TDocPipeline(runtime)`, and always drains/shuts down it in `finally`.
- Keep `doc3gpp tdoc parse` filter selection, pending-row rules, `max_batch`, prompts, summary lines, output fields, and exit codes unchanged.

- [ ] **Step 1: Add failing CLI tests.** Extend the fake service/runtime fixtures so `test_tdoc_parse_partial_failure`, `test_tdoc_parse_ftp_url_null_lands_in_skip_bucket`, and `test_tdoc_parse_summary_counts_split_correctly` assert runner injection and unchanged output/exit behavior. Add a preparation-overlap test using two database TDocs and a configured worker count greater than one.
- [ ] **Step 2: Run focused CLI tests to verify red.**

  Run: `PATH="$PWD/.venv/bin:$PATH" python -m pytest tests/unit/test_tdoc_parse_cli.py -q`

  Expected: the runner-injection/overlap assertions fail before CLI runtime ownership is added.
- [ ] **Step 3: Implement CLI runtime ownership around the database-mode command.** Create the runtime once for the command invocation, pass its runner to `TDocCrService.extract_many`, and close it in `finally`, including exceptions and interrupts. Do not instantiate one runtime per TDoc or change the public command flags.
- [ ] **Step 4: Run focused CLI tests and DB integration tests.**

  Run: `PATH="$PWD/.venv/bin:$PATH" python -m pytest tests/unit/test_tdoc_parse_cli.py tests/integration/test_tdoc_cr_sqlite.py -q`

  Expected: PASS with unchanged CLI output and exit-code assertions.
- [ ] **Step 5: Commit CLI database-mode integration.**

  Commit: `git commit -m "feat: parallelize cli tdoc database batches"`

### Task 8: Parallelize CLI local, FTP-folder, and direct-file modes

**Files:**
- Modify: `src/doc3gpp/cli.py`
- Modify: `tests/unit/test_tdoc_parse_direct.py`
- Modify: `tests/unit/test_tdoc_parse_cli.py`
- Modify: `tests/integration/test_tdoc_parse_local_batch_sqlite.py`
- Modify: `tests/integration/test_tdoc_parse_ftp_batch_sqlite.py`

**Interfaces:**
- Local folder parsing uses the runner with no persistence callback for sorted target paths; output writing remains a source-order, CLI-thread operation after outcomes complete.
- FTP-folder parsing passes the CLI runner into `extract_from_url_batch`; breadth-first URL discovery remains unchanged and URL-keyed results remain deterministic.
- Direct single-file `--from-path`/`--from-url` uses the one-item runner contract and preserves current success/failure/size-skip output and exit codes.

- [ ] **Step 1: Add failing local/FTP/direct tests.** Add a local-folder overlap test with a barrier, assert output files are emitted in sorted source order despite completion order, and retain existing output-exists/size/failure counters. Add FTP-folder overlap and URL-order tests. Add direct one-file tests that verify the runner is used without changing emitted records or `TDocTooLargeError` handling.
- [ ] **Step 2: Run focused batch tests to verify red.**

  Run: `PATH="$PWD/.venv/bin:$PATH" python -m pytest tests/unit/test_tdoc_parse_direct.py tests/unit/test_tdoc_parse_cli.py tests/integration/test_tdoc_parse_local_batch_sqlite.py tests/integration/test_tdoc_parse_ftp_batch_sqlite.py -q`

  Expected: new overlap/runner assertions fail while existing compatibility tests establish the baseline.
- [ ] **Step 3: Refactor local-folder mode.** Filter existing outputs and explicit size skips using current rules, submit remaining file reads/parses to the runner, classify per-file outcomes, then emit completed records in original sorted order and preserve all counters/exit conditions.
- [ ] **Step 4: Refactor FTP-folder and direct-file modes.** Build one runner for the invocation, pass it through the service URL batch wrapper, and use one-item runner calls for direct sources. Ensure `finally` drains the writer/runtime before the command exits and that no DB write is performed for local-only parses.
- [ ] **Step 5: Run focused batch/integration tests.**

  Run: `PATH="$PWD/.venv/bin:$PATH" python -m pytest tests/unit/test_tdoc_parse_direct.py tests/unit/test_tdoc_parse_cli.py tests/integration/test_tdoc_parse_local_batch_sqlite.py tests/integration/test_tdoc_parse_ftp_batch_sqlite.py -q`

  Expected: PASS, including source ordering, revision/cache identity, size skips, failures, and exit codes.
- [ ] **Step 6: Commit CLI folder/direct integration.**

  Commit: `git commit -m "feat: parallelize cli tdoc folder parsing"`

### Task 9: Prove cross-surface compatibility, search responsiveness, and shared writer safety

**Files:**
- Modify: `tests/integration/test_search_during_pipeline_write.py`
- Modify: `tests/integration/test_parse_tdoc_url_job_sqlite.py`
- Modify: `tests/unit/test_pipeline_handlers.py`, `tests/unit/test_job_worker.py`, and relevant CLI/service tests as needed

**Interfaces:**
- No new public API; this task closes the acceptance gaps across runtime ownership, search reads, concurrent jobs, cancellation, and HTTP/MCP reachability.

- [ ] **Step 1: Add/strengthen integration tests.** Delay one serialized persistence command and assert an independent search read completes; run two jobs against one web runtime and assert writer callbacks do not overlap; assert web cancellation and shutdown drain submitted work; assert CLI and web result maps classify the same skip/failure cases.
- [ ] **Step 2: Run the focused cross-surface tests.**

  Run: `PATH="$PWD/.venv/bin:$PATH" python -m pytest tests/integration/test_search_during_pipeline_write.py tests/integration/test_parse_tdoc_url_job_sqlite.py tests/unit/test_pipeline_handlers.py tests/unit/test_job_worker.py tests/unit/test_tdoc_cr_service.py tests/unit/test_tdoc_cr_service_ftp_batch.py -q`

  Expected: PASS with no concurrent database mutations and no event-loop starvation.
- [ ] **Step 3: Commit cross-surface regression coverage.**

  Commit: `git commit -m "test: cover cross-surface tdoc pipeline guarantees"`

### Task 10: Synchronize documentation and complete verification

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/cli.md`
- Modify: `docs/web-server.md`
- Modify: `doc3gpp.toml.example`
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `docs/conventions.md` only if the shared lifecycle/concurrency rule is not covered elsewhere

- [ ] **Step 1: Update documentation.** Document the service-level runner, one runtime per web process/CLI invocation, serialized writer guarantee, parallel preparation behavior for DB/local/FTP batches, `[pipeline] prepare_workers`, deterministic discovery/output order, and unchanged MCP/HTTP enqueue contracts. Preserve the prior uncommitted web result-detail documentation while extending it.
- [ ] **Step 2: Review the completed implementation against the approved spec.** Check every goal/non-goal, data flow, classifier rule, cache/search safety rule, cancellation/shutdown rule, test category, and acceptance criterion; correct omissions before running the final suite.
- [ ] **Step 3: Run focused tests for all changed surfaces.**

  Run: `PATH="$PWD/.venv/bin:$PATH" python -m pytest tests/unit/test_pipeline_runtime.py tests/unit/test_tdoc_pipeline.py tests/unit/test_tdoc_cr_service.py tests/unit/test_tdoc_cr_service_ftp_batch.py tests/unit/test_tdoc_cache.py tests/unit/test_pipeline_handlers.py tests/unit/test_tdoc_parse_cli.py tests/unit/test_tdoc_parse_direct.py tests/unit/test_web_jobs_routes.py tests/unit/test_job_worker.py tests/integration/test_tdoc_parse_local_batch_sqlite.py tests/integration/test_tdoc_parse_ftp_batch_sqlite.py tests/integration/test_parse_tdoc_url_job_sqlite.py tests/integration/test_search_during_pipeline_write.py -q`

  Expected: PASS.
- [ ] **Step 4: Run the full offline suite.**

  Run: `PATH="$PWD/.venv/bin:$PATH" ./scripts/test_sqlite.sh`

  Expected: all offline tests pass; online tests remain opt-in.
- [ ] **Step 5: Run changed-file lint and whitespace checks.**

  Run: `.venv/bin/ruff check <all changed Python files>` and `git diff --check`.

  Expected: changed Python files have no Ruff violations and the diff has no whitespace errors. Report unrelated pre-existing repository-wide lint failures separately rather than modifying unrelated files.
- [ ] **Step 6: Request final code review before integration.** Review the complete diff, all task commits, preserved prior bugfix hunks, and full-suite evidence; do not commit or merge additional changes until review findings are resolved.
