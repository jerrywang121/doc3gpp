# Periodic Job Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emit real, throttled progress lines from long-running background jobs so the web UI shows live status instead of sitting at "polling…".

**Architecture:** Add a `[server] progress_interval_seconds` knob (default 5.0). Throttle the worker's `progress()` closure uniformly in `job_worker.py` (emit at most once per interval, flush pending on completion, thread-safe for `asyncio.to_thread` callers). Thread an optional `on_progress: Callable[[str], None] | None = None` param through the sync services and batch extract methods, and wire `on_progress=progress` from the handlers. No frontend change — the UI already polls DB `log_lines` every 2s.

**Tech Stack:** Python 3.10+, pydantic-settings, asyncio, pytest.

## Global Constraints

- `on_progress` params default to `None` so all existing callers (CLI, web, tests) are unaffected.
- `progress_interval_seconds` lives in `[server]` (with sibling worker knobs), range 0.1..60.0, default 5.0.
- Follow existing layered architecture: services never touch the worker; handlers are thin adapters.
- No comments unless required; match existing docstring style.
- The worker `progress()` closure MUST be thread-safe: `_parse_tdoc_url` calls `extract_from_url_batch` via `asyncio.to_thread`, and `asyncio.Queue` is not thread-safe. Schedule onto the loop via `loop.call_soon_threadsafe` when called from a non-loop thread.
- The first `progress()` call MUST always emit immediately (initialize `last_emit = -interval`).
- The pending flush in `finally` MUST run BEFORE `unregister_queue` so flushed SSE events reach still-attached consumers.

---

### Task 1: Settings knob

**Files:**
- Modify: `src/doc3gpp/settings/schema.py` (`ServerSettings`, after `poll_interval_seconds` ~line 607)
- Modify: `doc3gpp.toml.example` (in the `[server]` block, after `poll_interval_seconds` ~line 253)
- Test: `tests/unit/test_server_settings.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Settings.server.progress_interval_seconds: float` (default 5.0, ge=0.1, le=60.0)

- [ ] **Step 1: Write the failing test**

```python
def test_progress_interval_seconds_default() -> None:
    from doc3gpp.settings.schema import Settings
    assert Settings().server.progress_interval_seconds == 5.0

def test_progress_interval_seconds_toml_override(tmp_path) -> None:
    from doc3gpp.settings.schema import Settings
    cfg = tmp_path / "doc3gpp.toml"
    cfg.write_text("[server]\nprogress_interval_seconds = 2.5\n")
    s = Settings(_env_file=None)
    # Load via the TOML loader path used by the app; assert the override.
    from doc3gpp.settings.loader import get_settings
    # (see existing test_server_settings.py for the exact TOML-loading helper)
    assert s.server.progress_interval_seconds == 2.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_server_settings.py -v`
Expected: FAIL with `AttributeError: 'ServerSettings' object has no attribute 'progress_interval_seconds'`

- [ ] **Step 3: Write minimal implementation**

In `src/doc3gpp/settings/schema.py`, inside `ServerSettings`, after `poll_interval_seconds`:

```python
progress_interval_seconds: float = Field(
    default=5.0,
    ge=0.1,
    le=60.0,
    description=(
        "Minimum interval between periodic progress log lines emitted "
        "by the job worker for long-running jobs. At most one line per "
        "interval is written to the job log / SSE stream; the final "
        "pending line is always flushed on completion."
    ),
)
```

In `doc3gpp.toml.example`, in the `[server]` block after `poll_interval_seconds`:

```toml
# Minimum interval between periodic progress log lines emitted by the
# job worker for long-running jobs. At most one line per interval is
# written to the job log / SSE stream; the final pending line is always
# flushed on completion. Range 0.1..60.0 seconds.
# progress_interval_seconds = 5.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_server_settings.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/settings/schema.py doc3gpp.toml.example tests/unit/test_server_settings.py
git commit -m "feat: add server.progress_interval_seconds knob"
```

---

### Task 2: Worker-side throttle (thread-safe, first-emits)

**Files:**
- Modify: `src/doc3gpp/web/workers/job_worker.py` (`_claim_and_run`, the `progress` closure ~line 254)
- Test: `tests/unit/test_job_worker.py`

**Interfaces:**
- Consumes: `settings.server.progress_interval_seconds` (Task 1)
- Produces: thread-safe throttled `progress()` that flushes pending on completion

- [ ] **Step 1: Write the failing test**

Add two tests to `tests/unit/test_job_worker.py`. Use a fake handler that records the `progress` callback and calls it twice, plus a threaded variant:

```python
def test_progress_throttle_flushes_pending_on_completion() -> None:
    """Rapid progress calls coalesce during run but all flush at completion."""
    repo = _make_repo()
    state = _make_state(repo)
    calls: list[str] = []

    def handler(job, services, settings, *, progress, cancel_event):
        progress("first")
        progress("second")
        return {"ok": True}

    worker = JobWorker(state, repo=repo, handlers={JobKind.SYNC_MEETINGS: handler})
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"})
    _run_worker_once(worker, repo)
    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.SUCCEEDED
    assert any("first" in line for line in done.log_lines)
    assert any("second" in line for line in done.log_lines)


def test_progress_from_thread_is_safe() -> None:
    """progress() called from asyncio.to_thread lands in log_lines."""
    repo = _make_repo()
    state = _make_state(repo)

    def handler(job, services, settings, *, progress, cancel_event):
        import asyncio as _a
        _a.get_running_loop().run_in_executor(None, lambda: progress("from-thread"))
        return {"ok": True}

    worker = JobWorker(state, repo=repo, handlers={JobKind.SYNC_MEETINGS: handler})
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"})
    _run_worker_once(worker, repo)
    done = repo.get(job.id)
    assert done is not None
    assert any("from-thread" in line for line in done.log_lines)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_job_worker.py -k "throttle or from_thread" -v`
Expected: FAIL (the second test may pass today since the current closure is not throttled; the first test's "second" line may be missing if throttled — but today both pass, so the throttle test must assert the *coalescing* behavior. Adjust the first test to assert that during a run with a long interval the intermediate line is NOT present until flush. See note below.)

> **Note on the throttle test:** With the current (unthrottled) closure, both `first` and `second` appear immediately, so the test above passes today. To make it a genuine failing test, set `progress_interval_seconds` to a large value (e.g. `60.0`) via `Settings(server=ServerSettings(progress_interval_seconds=60.0))`, and assert that after the handler returns, BOTH lines are present (flush) — this passes today too. The real regression guard is the thread-safety test. Keep both; the throttle test documents the flush contract.

- [ ] **Step 3: Write minimal implementation**

Replace the `progress` closure in `_claim_and_run` (currently lines ~254-260) with:

```python
loop = asyncio.get_running_loop()
interval = float(self._state.settings.server.progress_interval_seconds)
state = {"last_emit": -interval, "pending": []}

def _emit(message: str) -> None:
    state["last_emit"] = loop.time()
    line = f"[{_iso_now()}] {message}"
    try:
        self._repo.append_log(job.id, line=line)
    except Exception:
        logger.exception("failed to append log for job %s", job.id)
    self._enqueue(queue, {"event": "log", "data": {"line": line}})

def _do_progress(message: str) -> None:
    now = loop.time()
    if now - state["last_emit"] >= interval:
        _emit(message)
    else:
        state["pending"].append(message)

def progress(message: str) -> None:
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if current is loop:
        _do_progress(message)
    else:
        loop.call_soon_threadsafe(_do_progress, message)
```

In the existing `finally` block, **before** `self._state.jobs.unregister_queue(job.id)`, add:

```python
for msg in state["pending"]:
    _emit(msg)
state["pending"].clear()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_job_worker.py -v`
Expected: PASS (all existing tests still pass — the flush in `finally` preserves their `log_lines` assertions)

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/web/workers/job_worker.py tests/unit/test_job_worker.py
git commit -m "feat: throttle job progress lines in worker (thread-safe)"
```

---

### Task 3: Thread `on_progress` through sync services

**Files:**
- Modify: `src/doc3gpp/services/meetings_service.py` (`sync`, ~line 47)
- Modify: `src/doc3gpp/services/tdoc_service.py` (`sync_tdoc_list`, ~line 85)
- Modify: `src/doc3gpp/services/tdoc_file_service.py` (`sync_from_meeting_ftp`, ~line 28)
- Modify: `src/doc3gpp/services/tdoc_sync_coordinator.py` (`sync_for_meeting_id`, `sync_for_meeting_name`, `sync_all_tracked_meetings`, `_sync_for_meeting`)
- Test: `tests/unit/test_tdoc_sync_coordinator.py`, `tests/unit/test_cli_auto_sync.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `on_progress: Callable[[str], None] | None = None` on each method

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_tdoc_sync_coordinator.py`:

```python
def test_sync_for_meeting_id_reports_progress() -> None:
    """sync_for_meeting_id invokes on_progress with a meeting line."""
    # Build a coordinator with fakes (see existing fixtures in this file).
    lines: list[str] = []
    outcome = coordinator.sync_for_meeting_id(135, on_progress=lines.append)
    assert any("syncing meeting 135" in line for line in lines)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_tdoc_sync_coordinator.py -k progress -v`
Expected: FAIL with `TypeError: sync_for_meeting_id() got an unexpected keyword argument 'on_progress'`

- [ ] **Step 3: Write minimal implementation**

Add `on_progress: Callable[[str], None] | None = None` to each method and emit lines, guarding each call with `if on_progress is not None:`:

- `MeetingService.sync`: after `upsert_many`, `on_progress(f"meeting sync for TSG {canonical_tsg}: {written} rows stored")`
- `TDocService.sync_tdoc_list`: after `upsert_many`, `on_progress(f"tdoc list for meeting {meeting_id}: {stored} rows stored")`
- `TDocFileService.sync_from_meeting_ftp`: after `upsert_many`, `on_progress(f"aux files for meeting: {written} stored")`
- `TDocSyncCoordinator._sync_for_meeting`: at start, `on_progress(f"syncing meeting {meeting.meeting_id} ({meeting.name})")`; thread the param through `sync_for_meeting_id`, `sync_for_meeting_name`, `sync_all_tracked_meetings` into `_sync_for_meeting`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_tdoc_sync_coordinator.py tests/unit/test_cli_auto_sync.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/meetings_service.py src/doc3gpp/services/tdoc_service.py src/doc3gpp/services/tdoc_file_service.py src/doc3gpp/services/tdoc_sync_coordinator.py tests/unit/test_tdoc_sync_coordinator.py
git commit -m "feat: thread on_progress through sync services"
```

---

### Task 4: Thread `on_progress` through batch extract + auto-sync

**Files:**
- Modify: `src/doc3gpp/services/tdoc_cr_service.py` (`extract_from_url_batch` ~line 909, `extract_many` ~line 698)
- Modify: `src/doc3gpp/cli_auto_sync.py` (`trigger_auto_sync` ~line 251, `sync_tsg_internal` ~line 115, `sync_meeting_internal` ~line 138)
- Test: `tests/unit/test_tdoc_cr_service_ftp_batch.py`, `tests/unit/test_cli_auto_sync.py`

**Interfaces:**
- Consumes: `on_progress` param pattern from Task 3
- Produces: `on_progress` on `extract_from_url_batch`, `extract_many`, `trigger_auto_sync`, `sync_tsg_internal`, `sync_meeting_internal`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_tdoc_cr_service_ftp_batch.py`:

```python
def test_extract_from_url_batch_reports_progress() -> None:
    """extract_from_url_batch invokes on_progress with parsed counts."""
    # Build a service with fakes yielding N file URLs (see existing fixtures).
    lines: list[str] = []
    result = service.extract_from_url_batch(url, on_progress=lines.append)
    assert any("parsed" in line for line in lines)
```

Add to `tests/unit/test_cli_auto_sync.py`:

```python
def test_trigger_auto_sync_reports_progress() -> None:
    """trigger_auto_sync invokes on_progress with per-sync + summary lines."""
    lines: list[str] = []
    trigger_auto_sync(
        auto_sync_enabled=True,
        meeting_service=fake_meeting,
        tdoc_sync_coordinator=fake_coordinator,
        tsg="R5",
        on_progress=lines.append,
    )
    assert any("auto-sync:" in line for line in lines)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_tdoc_cr_service_ftp_batch.py tests/unit/test_cli_auto_sync.py -k progress -v`
Expected: FAIL with `TypeError: ... got an unexpected keyword argument 'on_progress'`

- [ ] **Step 3: Write minimal implementation**

- `extract_from_url_batch`: add `on_progress: Callable[[str], None] | None = None`; in the `for file_url in file_urls` loop, after each iteration `if on_progress is not None: on_progress(f"parsed {i}/{len(file_urls)} files")` (where `i` is the 1-based index).
- `extract_many`: add the param; after each `self.extract(...)` `if on_progress is not None: on_progress(f"parsed {i}/{len(total_ids)} TDocs")`.
- `sync_tsg_internal`: add `on_progress` param; after the sync, `if on_progress is not None: on_progress(f"TSG {canonical_tsg} sync: {outcome.reason}")`.
- `sync_meeting_internal`: add `on_progress` param; after the sync, `if on_progress is not None: on_progress(f"meeting {meeting_id} sync: {outcome.reason}")`.
- `trigger_auto_sync`: add `on_progress` param; thread into both internals; after the loops, `if on_progress is not None: on_progress(f"auto-sync: {meeting_syncs_done} TSG sync(s), {tdoc_syncs_done} tdoc sync(s) done")`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_tdoc_cr_service_ftp_batch.py tests/unit/test_cli_auto_sync.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/tdoc_cr_service.py src/doc3gpp/cli_auto_sync.py tests/unit/test_tdoc_cr_service_ftp_batch.py tests/unit/test_cli_auto_sync.py
git commit -m "feat: thread on_progress through batch extract and auto-sync"
```

---

### Task 5: Wire handlers

**Files:**
- Modify: `src/doc3gpp/web/workers/handlers.py` (`_sync_meetings` ~line 75, `_sync_tdocs` ~line 98, `_sync_tdocs_all` ~line 125, `_parse_tdoc_url` ~line 308/325)
- Test: `tests/unit/test_job_worker.py`

**Interfaces:**
- Consumes: `on_progress` params from Tasks 3–4
- Produces: handlers pass `on_progress=progress`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_job_worker.py`:

```python
def test_parse_tdoc_url_passes_on_progress() -> None:
    """The PARSE_TDOC_URL handler passes a callable on_progress to the service."""
    repo = _make_repo()
    captured: dict = {}

    class _FakeUrlService:
        def extract_from_url_batch(self, url, **kwargs):
            captured["on_progress"] = kwargs.get("on_progress")
            from doc3gpp.models.tdoc_cr import DirectParseBatchResult
            return DirectParseBatchResult(results=[], failures={}, skipped={})

    state = _make_state(repo, url_service=_FakeUrlService())
    job = repo.create(JobKind.PARSE_TDOC_URL, {"url": "https://www.3gpp.org/ftp/tsg_ran/WG2_RL2/TSGR2_135/"})
    worker = JobWorker(state, repo=repo)
    _run_worker_once(worker, repo)
    assert callable(captured.get("on_progress"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_job_worker.py -k parse_tdoc_url_passes -v`
Expected: FAIL with `assert callable(None)` (handler does not pass `on_progress` today)

- [ ] **Step 3: Write minimal implementation**

Pass `on_progress=progress` at each call site:

- `_sync_meetings`: `services.meeting.sync(url, tsg=tsg, force=force, on_progress=progress)`
- `_sync_tdocs`: `coordinator.sync_for_meeting_id(int(meeting_id), force=force, on_progress=progress)` and `coordinator.sync_for_meeting_name(str(meeting_name), force=force, on_progress=progress)`
- `_sync_tdocs_all`: `services.tdoc_sync.sync_all_tracked_meetings(force=force, on_progress=progress)`
- `_parse_tdoc_url`: `trigger_auto_sync(..., on_progress=progress)` and `services.tdoc_cr.extract_from_url_batch(..., on_progress=progress)`

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_job_worker.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/web/workers/handlers.py tests/unit/test_job_worker.py
git commit -m "feat: wire on_progress into job handlers"
```

---

### Task 6: Docs + full verification

**Files:**
- Modify: `docs/web-server.md` (if it documents `[server]` knobs), `docs/cli.md` (if it documents `[server]` knobs), `AGENTS.md` (if the server settings table lists knobs)

- [ ] **Step 1: Update docs** — document `progress_interval_seconds` in the `[server]` settings reference wherever the sibling knobs (`poll_interval_seconds`, `cleanup_interval_seconds`) are documented.

- [ ] **Step 2: Run full suite**

Run: `./scripts/test_sqlite.sh`
Expected: all unit + integration tests pass

- [ ] **Step 3: Lint**

Run: `ruff check .`
Expected: no errors

- [ ] **Step 4: Commit**

```bash
git add docs/ AGENTS.md
git commit -m "docs: document server.progress_interval_seconds"
```

---

## Self-Review Notes

- **Spec coverage:** knob (T1), throttle (T2), service threading (T3–T4), handler wiring (T5), docs (T6) — all covered.
- **Type consistency:** `on_progress: Callable[[str], None] | None = None` used consistently across all tasks; `progress_interval_seconds` referenced as `settings.server.progress_interval_seconds` in T2.
- **Thread-safety:** T2 makes the `progress` closure schedule onto the loop via `call_soon_threadsafe` when called from a non-loop thread (required because `_parse_tdoc_url` calls `extract_from_url_batch` via `asyncio.to_thread`).
- **First-emit:** `last_emit = -interval` guarantees the first call always emits.
- **Flush ordering:** pending flush runs before `unregister_queue` in `finally`.
- **Surfaces verified:** web forms, MCP tools, CLI direct calls (`on_progress=None` → no change), SSE consumers, DB `log_lines` polling, spec-sync event-based `on_progress` (unchanged, different API), `_parse_tdocs` per-batch progress (throttled), `_rebuild_search` (out of scope, future work), `_cache_purge` (short, no change), existing unit tests (flush preserves assertions), `mark_running("starting")` (unaffected), cancellation (finally flushes).
