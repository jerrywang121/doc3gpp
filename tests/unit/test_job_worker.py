"""Tests for the background job worker (:mod:`doc3gpp.web.workers`).

These are unit tests: they exercise the worker loop, the per-kind
handler registry, and the terminal-state transitions against an
in-memory sqlite ``jobs`` table plus a fake :class:`ServiceContainer`
whose service methods are stubs. No network calls are made.
"""
from __future__ import annotations

import asyncio
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from doc3gpp.models.jobs import JobKind, JobStatus
from doc3gpp.models.tdoc_cr import DirectParseBatchResult, DirectParseResult
from doc3gpp.repository.protocols import JobRepository
from doc3gpp.settings.schema import Settings
from doc3gpp.storage.db.base import Base
from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository
from doc3gpp.web.state import ServiceContainer, WebState
from doc3gpp.web.workers.job_worker import JobWorker


class _FakeMeetingService:
    """Fake ``MeetingService`` whose ``sync`` returns a canned outcome."""

    def __init__(self, *, fail: bool = False) -> None:
        from doc3gpp.models.sync import SyncOutcome

        if fail:
            self.sync = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        else:
            self.sync = lambda *a, **k: SyncOutcome(
                status="synced", reason="meeting sync ok", synced_count=3
            )


class _FakeSpecService:
    """Fake ``SpecService`` that records which method was called."""

    def __init__(self, *, fail: bool = False) -> None:
        from doc3gpp.models.sync import SyncOutcome

        self.calls: list[tuple[str, dict]] = []
        if fail:
            self.sync = self._raise
            self.sync_spec = self._raise
        else:
            self.sync = lambda *a, **k: self._record("sync", k, SyncOutcome(
                status="synced", reason="spec sync ok", synced_count=5, version_count=12))
            self.sync_spec = lambda *a, **k: self._record("sync_spec", k, SyncOutcome(
                status="synced", reason="spec sync ok", synced_count=1, version_count=2))

    def _raise(self, *a, **k):
        raise RuntimeError("boom")

    def _record(self, name, kwargs, outcome):
        self.calls.append((name, kwargs))
        return outcome


class _FakeTsgService:
    """Fake ``TsgService`` that knows a fixed set of short names."""

    def __init__(self, known: set[str] | None = None) -> None:
        self._known = known or {"R5", "SA2"}

    def is_known_short_name(self, short_name: str) -> bool:
        return short_name.upper() in self._known


def _make_state(
    repo: JobRepository,
    *,
    fail: bool = False,
    url_service: object | None = None,
    known_tsgs: set[str] | None = None,
) -> WebState:
    """Build a :class:`WebState` with a fake :class:`ServiceContainer`."""
    services = ServiceContainer(
        meeting=_FakeMeetingService(fail=fail),  # type: ignore[arg-type]
        tdoc=None,  # type: ignore[arg-type]
        tdoc_cr=url_service,  # type: ignore[arg-type]
        tdoc_sync=None,  # type: ignore[arg-type]
        tdoc_repo=None,  # type: ignore[arg-type]
        tsg=_FakeTsgService(known_tsgs),  # type: ignore[arg-type]
        wi=None,  # type: ignore[arg-type]
        spec=_FakeSpecService(fail=fail),  # type: ignore[arg-type]
        search=None,
        semantic_search=None,
        tdoc_file_repo=None,  # type: ignore[arg-type]
        job_repo=repo,
    )
    settings = Settings()
    return WebState(settings=settings, engine=None, services=services, jobs=_JobWorkerHandleFake())  # type: ignore[arg-type]


class _JobWorkerHandleFake:
    """Minimal stand-in for :class:`JobWorkerHandle` used by these tests."""

    def __init__(self) -> None:
        self.event_queues: dict[str, asyncio.Queue[dict]] = {}
        self.cancel_events: dict[str, asyncio.Event] = {}
        self._cancel_requests: set[str] = set()

    def register_queue(self, job_id: str, queue: asyncio.Queue[dict]) -> None:
        self.event_queues[job_id] = queue

    def unregister_queue(self, job_id: str) -> None:
        self.event_queues.pop(job_id, None)

    def cancel(self, job_id: str) -> bool:
        event = self.cancel_events.get(job_id)
        if event is not None:
            event.set()
            return True
        self._cancel_requests.add(job_id)
        return True

    def consume_cancel_request(self, job_id: str) -> bool:
        if job_id in self._cancel_requests:
            self._cancel_requests.discard(job_id)
            return True
        return False


def _make_repo() -> SQLAlchemyJobRepository:
    """Build a ``JobRepository`` backed by an in-memory sqlite engine."""
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    repo = SQLAlchemyJobRepository()
    repo._session_factory = session_factory  # type: ignore[attr-defined]
    return repo


def _run_worker_once(worker: JobWorker, repo: JobRepository) -> None:
    """Run the claim-and-run path for the first queued job, synchronously.

    The real :meth:`JobWorker.run` loops forever; this helper drives a
    single ``_claim_and_run`` under an event loop so tests can assert
    the terminal state without starting the infinite loop.
    """

    async def _claim() -> None:
        queued = repo.list(status=JobStatus.QUEUED, limit=1)
        assert queued, "expected a queued job"
        sem = asyncio.Semaphore(1)
        await worker._claim_and_run(queued[0], sem)  # type: ignore[attr-defined]

    asyncio.run(_claim())


def _drain(queue: asyncio.Queue) -> list[dict]:
    """Drain a queue non-destructively for assertions."""
    out: list[dict] = []
    while True:
        try:
            out.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return out


def test_worker_runs_queued_job() -> None:
    """A ``SYNC_MEETINGS`` job is claimed, logs, and succeeds."""
    repo = _make_repo()
    state = _make_state(repo)
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"})
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.SUCCEEDED
    assert done.result_summary == {
        "status": "synced",
        "reason": "meeting sync ok",
        "synced_count": 3,
    }
    assert len(done.log_lines) >= 1


def test_worker_rejects_unknown_tsg_for_meeting_sync() -> None:
    """A ``SYNC_MEETINGS`` job for a TSG not in the ``tsgs`` table fails fast.

    A legacy / unknown TSG (e.g. ``R6``) must be rejected before any
    network work or DB writes, instead of running a sweep that fails
    every meeting insert with an FK constraint error.
    """
    repo = _make_repo()
    state = _make_state(repo, known_tsgs={"R5", "SA2"})
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R6"})
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.FAILED
    assert "R6" in (done.error or "")


def test_worker_runs_spec_sync_job() -> None:
    """A ``SYNC_SPECS`` job is claimed, logs, and succeeds."""
    repo = _make_repo()
    state = _make_state(repo)
    job = repo.create(JobKind.SYNC_SPECS, {"tsg": "R5", "force": True})
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.SUCCEEDED
    assert done.result_summary == {
        "status": "synced",
        "reason": "spec sync ok",
        "synced_count": 5,
        "version_count": 12,
    }
    assert len(done.log_lines) >= 1


def test_worker_runs_spec_sync_by_spec_id() -> None:
    """A ``SYNC_SPECS`` job with ``spec_id`` dispatches to ``sync_spec``."""
    repo = _make_repo()
    state = _make_state(repo)
    job = repo.create(JobKind.SYNC_SPECS, {"spec_id": "36.579-5", "force": True})
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.SUCCEEDED
    assert done.result_summary == {
        "status": "synced",
        "reason": "spec sync ok",
        "synced_count": 1,
        "version_count": 2,
    }
    fsvc = state.services.spec
    assert len(fsvc.calls) == 1
    name, kwargs = fsvc.calls[0]
    assert name == "sync_spec"
    assert kwargs.get("force") is True
    assert kwargs.get("per_version_details") is False


def test_worker_runs_spec_sync_with_per_version_details() -> None:
    """The handler forwards ``per_version_details=True`` to the service."""
    repo = _make_repo()
    state = _make_state(repo)
    job = repo.create(
        JobKind.SYNC_SPECS,
        {"tsg": "R5", "force": True, "per_version_details": True},
    )
    assert job.id
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    fsvc = state.services.spec
    assert len(fsvc.calls) == 1
    name, kwargs = fsvc.calls[0]
    assert name == "sync"
    assert kwargs.get("per_version_details") is True


def test_worker_rejects_unknown_tsg_for_spec_sync() -> None:
    """A ``SYNC_SPECS`` job for a TSG not in the ``tsgs`` table fails fast.

    A legacy / unknown TSG (e.g. ``R6``) must be rejected before any
    network work or DB writes, instead of running a full sweep that
    fails every spec with an FK constraint error.
    """
    repo = _make_repo()
    state = _make_state(repo, known_tsgs={"R5", "SA2"})
    job = repo.create(JobKind.SYNC_SPECS, {"tsg": "R6", "force": True})
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.FAILED
    assert "R6" in (done.error or "")
    fsvc = state.services.spec
    assert len(fsvc.calls) == 0


def test_worker_marks_failed_on_exception() -> None:
    """A handler that raises marks the job FAILED with ``str(exc)``."""
    repo = _make_repo()
    state = _make_state(repo, fail=True)
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"})
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.FAILED
    assert "boom" in (done.error or "")


def test_worker_cancels_on_event() -> None:
    """A pre-set cancellation event marks the job CANCELLED."""
    repo = _make_repo()
    state = _make_state(repo)
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"})

    cancel_event = asyncio.Event()
    cancel_event.set()
    state.jobs.cancel_events[job.id] = cancel_event

    worker = JobWorker(state, repo=repo)
    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.CANCELLED
    assert done.error is None


def test_worker_cancels_queued_job_on_pending_request() -> None:
    """A cancel requested while the job is QUEUED is honoured on claim."""
    repo = _make_repo()
    state = _make_state(repo)
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"})

    # Cancel before the worker claims the job: no running event exists,
    # so the request is recorded as pending on the handle.
    assert state.jobs.cancel(job.id) is True
    assert job.id in state.jobs._cancel_requests  # type: ignore[attr-defined]

    worker = JobWorker(state, repo=repo)
    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.CANCELLED
    assert done.error is None
    assert job.id not in state.jobs._cancel_requests  # type: ignore[attr-defined]


def test_worker_emits_named_sse_events() -> None:
    """The job's SSE queue receives log + terminal status events."""
    repo = _make_repo()
    state = _make_state(repo)
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"})

    # Attach the SSE queue before the worker picks the job up, mirroring
    # how the T8 route registers a queue before POSTing the job.
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=100)
    state.jobs.register_queue(job.id, queue)

    worker = JobWorker(state, repo=repo)
    _run_worker_once(worker, repo)

    events = _drain(queue)
    kinds = [ev["event"] for ev in events]
    assert "log" in kinds
    assert any(
        ev["event"] == "status" and ev["data"]["status"] == "succeeded"
        for ev in events
    )


def test_worker_drops_oldest_events() -> None:
    """Filling the queue past capacity discards the oldest event."""
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=2)
    queue.put_nowait({"event": "log", "data": {"line": "first"}})
    queue.put_nowait({"event": "log", "data": {"line": "second"}})
    JobWorker._enqueue(queue, {"event": "log", "data": {"line": "third"}})

    drained = _drain(queue)
    assert len(drained) == 2
    assert drained[-1]["data"]["line"] == "third"


def test_progress_throttle_drops_suppressed_messages() -> None:
    """Rapid progress calls under the throttle are dropped, not buffered.

    With a large ``progress_interval_seconds`` only the first call emits;
    subsequent throttled calls within the interval are dropped so the log
    does not fill up with per-item lines on a fast run.
    """
    repo = _make_repo()
    state = _make_state(repo)
    state.settings.server.progress_interval_seconds = 1000.0

    async def handler(job, services, settings, *, progress, cancel_event):
        progress("first")
        progress("second")
        progress("third")
        return {"ok": True}

    worker = JobWorker(
        state,
        repo=repo,
        handlers={JobKind.SYNC_MEETINGS: handler},  # type: ignore[dict-item]
    )
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"})
    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.SUCCEEDED
    assert any("first" in line for line in done.log_lines)
    assert not any("second" in line for line in done.log_lines)
    assert not any("third" in line for line in done.log_lines)


def test_progress_force_bypasses_throttle() -> None:
    """``progress(message, force=True)`` emits even within the throttle window.

    Terminal-summary calls use ``force=True`` so the "done: …" line always
    lands in ``log_lines`` regardless of how recently the previous line
    was written. The interval check is bypassed; ``last_emit`` is still
    updated so the next non-forced call still sees a fresh window.
    """
    repo = _make_repo()
    state = _make_state(repo)
    state.settings.server.progress_interval_seconds = 1000.0

    async def handler(job, services, settings, *, progress, cancel_event):
        progress("first")
        progress("done: 5 parsed, 0 failed", force=True)
        return {"ok": True}

    worker = JobWorker(
        state,
        repo=repo,
        handlers={JobKind.SYNC_MEETINGS: handler},  # type: ignore[dict-item]
    )
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"})
    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done is not None
    assert any("first" in line for line in done.log_lines)
    assert any("done: 5 parsed" in line for line in done.log_lines)


def test_progress_emitted_line_keeps_receive_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The emitted line carries the timestamp captured when ``progress()`` ran.

    Before the receive-time fix, every buffered line shared the (later)
    flush timestamp. Now the timestamp is captured at ``progress()`` call
    time, so a single emitted line shows when the work actually happened
    instead of when the buffer happened to flush.
    """
    import doc3gpp.web.workers.job_worker as jw

    stamps = iter(["01:02:03", "04:05:06", "07:08:09"])
    monkeypatch.setattr(jw, "_iso_now", lambda: next(stamps))

    repo = _make_repo()
    state = _make_state(repo)
    state.settings.server.progress_interval_seconds = 1000.0

    async def handler(job, services, settings, *, progress, cancel_event):
        progress("first")           # emits — ts "01:02:03"
        progress("second")          # throttled — dropped
        progress("done", force=True)  # bypasses — ts "07:08:09"
        return {"ok": True}

    worker = JobWorker(
        state,
        repo=repo,
        handlers={JobKind.SYNC_MEETINGS: handler},  # type: ignore[dict-item]
    )
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"})
    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done is not None
    assert "[01:02:03] first" in done.log_lines
    assert "[07:08:09] done" in done.log_lines
    assert not any("second" in line for line in done.log_lines)


def test_progress_from_thread_is_safe() -> None:
    """``progress()`` called from an ``asyncio.to_thread`` executor lands in ``log_lines``.

    ``asyncio.Queue`` is not thread-safe and the handler may emit from a
    worker thread (``_parse_tdoc_url`` runs ``extract_from_url_batch`` via
    ``asyncio.to_thread``). The ``progress`` closure must schedule onto the
    owning loop with ``call_soon_threadsafe`` instead of touching the queue
    directly, otherwise the line is lost (or the job errors).
    """
    repo = _make_repo()
    state = _make_state(repo)

    async def handler(job, services, settings, *, progress, cancel_event):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: progress("from-thread"))
        return {"ok": True}

    worker = JobWorker(
        state,
        repo=repo,
        handlers={JobKind.SYNC_MEETINGS: handler},  # type: ignore[dict-item]
    )
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"})
    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done is not None
    assert any("from-thread" in line for line in done.log_lines)


# ---------------------------------------------------------------------------
# Mark-running idempotency + orphan-recovery + worker poll cadence.
#
# These pin the bug fixes for the third worker-stuck regression: the worker
# was using ``cleanup_interval_seconds`` (default 300s) as its poll cadence,
# so freshly enqueued parse / sync / cache-purge jobs waited five minutes
# before pickup. The fix splits the two cadences into
# ``poll_interval_seconds`` (default 1s) and ``cleanup_interval_seconds``
# (default 300s), adds a WHERE-status guard to ``mark_running`` so two
# workers can't both write ``started_at`` for the same job, and adds an
# orphan-recovery sweep on startup that fails out ``RUNNING`` rows left
# behind by a crashed prior worker.
# ---------------------------------------------------------------------------


def test_mark_running_is_idempotent() -> None:
    """A second ``mark_running`` reports the claim lost and does not overwrite state."""
    repo = _make_repo()
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"})

    claimed, first = repo.mark_running(job.id, message="starting")
    assert claimed is True
    assert first.status is JobStatus.RUNNING
    assert first.started_at is not None
    first_started_at = first.started_at

    # Second call from another worker should not rewrite started_at and
    # should report the claim as lost (it was QUEUED -> RUNNING; second
    # call sees RUNNING and is a no-op).
    claimed_again, second = repo.mark_running(job.id, message="late")
    assert claimed_again is False
    assert second.id == first.id
    assert second.status is JobStatus.RUNNING
    # No new "late" log line on the no-op branch — the guard is meant to
    # make this safe even when two workers tick the same row.
    assert "late" not in second.log_lines
    # Round-trip back through the repo: started_at must survive.
    persisted = repo.get(job.id)
    assert persisted is not None
    assert persisted.started_at == first_started_at


def test_mark_running_no_op_on_terminal_rows() -> None:
    """``mark_running`` reports the claim lost on rows in a terminal status."""
    repo = _make_repo()
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"})
    repo.mark_running(job.id)
    repo.mark_succeeded(job.id, summary={"ok": True})

    claimed, after_success = repo.mark_running(job.id, message="oops")
    assert claimed is False
    assert after_success.status is JobStatus.SUCCEEDED
    assert after_success.error is None
    # The "oops" log line is dropped because the claim lost the race;
    # the row stays SUCCEEDED.
    assert "oops" not in after_success.log_lines


def test_worker_recovers_orphaned_running_jobs() -> None:
    """RUNNING rows left over from a crashed worker become FAILED on startup."""
    repo = _make_repo()
    state = _make_state(repo)
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"})
    repo.mark_running(job.id)

    worker = JobWorker(state, repo=repo)
    worker._recover_orphans()  # type: ignore[attr-defined]

    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.FAILED
    assert done.error == "orphaned_after_restart"


def test_recover_orphans_skips_queued_and_terminal_rows() -> None:
    """Only RUNNING rows are flipped; QUEUED and terminal rows are untouched."""
    repo = _make_repo()
    state = _make_state(repo)
    queued = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R1"})
    succeeded = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R2"})
    repo.mark_running(succeeded.id)
    repo.mark_succeeded(succeeded.id, summary={"ok": True})
    running = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R3"})
    repo.mark_running(running.id)

    worker = JobWorker(state, repo=repo)
    worker._recover_orphans()  # type: ignore[attr-defined]

    assert repo.get(queued.id).status is JobStatus.QUEUED
    assert repo.get(succeeded.id).status is JobStatus.SUCCEEDED
    assert repo.get(running.id).status is JobStatus.FAILED
    assert repo.get(running.id).error == "orphaned_after_restart"


def test_pickup_uses_short_poll_interval() -> None:
    """Freshly enqueued jobs are picked up within ~2s, not after 5 min.

    Drives the real :meth:`JobWorker.run` loop in a bounded task, then
    submits a job after the worker is already running and asserts the
    terminal state lands well before ``cleanup_interval_seconds`` (300s)
    would have ticked. Without the fix the worker sleeps 300s between
    pickups and this test would hang for that long.
    """
    repo = _make_repo()
    state = _make_state(repo)
    # Tight intervals so the test does not actually wait minutes.
    state.settings.server.poll_interval_seconds = 0.05
    state.settings.server.cleanup_interval_seconds = 60.0
    state.settings.server.max_concurrent_jobs = 1

    worker = JobWorker(state, repo=repo)

    async def _drive() -> str:
        task = asyncio.create_task(worker.run())
        try:
            # Let the worker hit its idle-sleep before we enqueue so
            # we are exercising the post-enqueue pickup path rather
            # than a coincidental first-tick grab.
            await asyncio.sleep(0.2)
            job_id = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"}).id
            # Poll for terminal state with a generous 2s budget. The
            # old loop would have needed ~300s, so this failing-fast
            # timeout is the actual regression assertion.
            for _ in range(40):
                row = repo.get(job_id)
                if row is not None and row.status is JobStatus.SUCCEEDED:
                    return job_id
                await asyncio.sleep(0.05)
            raise AssertionError(
                f"worker did not pick up job within 2s; row is {repo.get(job_id)}"
            )
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_drive())


def test_poll_interval_setting_default() -> None:
    """``poll_interval_seconds`` defaults to 1s and ``cleanup_interval_seconds`` stays 300s.

    Pins the contract that the two knobs are independent; flipping the
    cleanup interval must not move the pickup cadence, and vice-versa.
    """
    s = Settings()
    assert s.server.poll_interval_seconds == 1.0
    assert s.server.cleanup_interval_seconds == 300


def test_poll_interval_setting_validation() -> None:
    """Tiny / negative poll intervals are rejected by Pydantic."""
    from pydantic import ValidationError

    from doc3gpp.settings.schema import ServerSettings

    with pytest.raises(ValidationError):
        ServerSettings(poll_interval_seconds=0.01)  # below the ge=0.05 floor
    with pytest.raises(ValidationError):
        ServerSettings(poll_interval_seconds=-1.0)


# ---------------------------------------------------------------------------
# Claim-race skip + worker-task naming (shutdown cancel-event lookup).
# ---------------------------------------------------------------------------


def test_claim_lost_skips_handler() -> None:
    """A job already claimed by another worker is not executed twice.

    ``mark_running``'s WHERE-status guard makes the losing claim a
    no-op; ``_claim_and_run`` must check the returned status and skip
    the handler instead of running the job a second time.
    """
    repo = _make_repo()
    state = _make_state(repo)
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"})
    repo.mark_running(job.id)  # another worker already claimed it

    worker = JobWorker(state, repo=repo)

    async def _claim() -> None:
        sem = asyncio.Semaphore(1)
        await worker._claim_and_run(job, sem)  # type: ignore[attr-defined]

    asyncio.run(_claim())

    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.RUNNING  # untouched by the losing worker
    assert done.result_summary is None
    assert done.error is None


def test_worker_shutdown_signals_in_flight_handlers() -> None:
    """Cancelling the worker task sets the cancel event of in-flight jobs.

    Regression for the bug where the shutdown path looked up the cancel
    event via ``task.get_name()`` but the handler tasks were created
    without a ``name=``, so ``get_name()`` returned ``"Task-2"``-style
    names and the lookup always missed — in-flight handlers never saw
    the cooperative-cancel signal and the shutdown gather could hang
    until the handler finished on its own.
    """
    repo = _make_repo()
    state = _make_state(repo)
    state.settings.server.poll_interval_seconds = 0.05
    state.settings.server.max_concurrent_jobs = 1

    started = asyncio.Event()
    saw_cancel = asyncio.Event()

    async def _slow_handler(
        job, services, settings, *, progress, cancel_event
    ) -> dict:
        started.set()
        # Block until the worker's shutdown path sets the event.
        while not cancel_event.is_set():
            await asyncio.sleep(0.01)
        saw_cancel.set()
        raise asyncio.CancelledError()

    worker = JobWorker(
        state,
        repo=repo,
        handlers={JobKind.SYNC_MEETINGS: _slow_handler},  # type: ignore[dict-item]
    )

    async def _drive() -> None:
        task = asyncio.create_task(worker.run())
        try:
            job_id = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"}).id
            await asyncio.wait_for(started.wait(), timeout=2.0)
            # Cancel the worker task directly (bypassing
            # ``handle.shutdown``) so the ``run()`` CancelledError path
            # — the one that looks up cancel events by task name — is
            # the code under test.
            task.cancel()
            await asyncio.wait_for(saw_cancel.wait(), timeout=2.0)
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.CancelledError:
                pass  # the worker task re-raises after draining handlers
            row = repo.get(job_id)
            assert row is not None
            assert row.status is JobStatus.CANCELLED
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(_drive())


class _FakeTDocCrServiceForUrl:
    """Fake ``TDocCrService`` whose ``extract_from_url_batch`` is stubbed."""

    def __init__(
        self,
        *,
        results: list[DirectParseResult] | None = None,
        failures: dict[str, str] | None = None,
        skipped: dict[str, str] | None = None,
        file_urls: list[str] | None = None,
        raise_extract: Exception | None = None,
    ) -> None:
        self.results = results or []
        self.failures = failures or {}
        self.skipped = skipped or {}
        self.file_urls: list[str] = file_urls or []
        self.raise_extract = raise_extract
        self.extract_calls: list[dict] = []
        self.collect_calls: list[dict] = []

    def collect_3gpp_file_urls(self, url: str, *, max_depth: int) -> list[str]:
        self.collect_calls.append({"url": url, "max_depth": max_depth})
        return list(self.file_urls)

    def extract_from_url_batch(
        self,
        url: str,
        *,
        max_depth: int,
        force: bool,
        full: bool,
        max_tdoc_size_bytes: int | None,
        on_progress: object | None = None,
    ) -> DirectParseBatchResult:
        self.extract_calls.append({
            "url": url,
            "max_depth": max_depth,
            "force": force,
            "full": full,
            "max_tdoc_size_bytes": max_tdoc_size_bytes,
        })
        if self.raise_extract is not None:
            raise self.raise_extract
        return DirectParseBatchResult(
            results=self.results,
            failures=self.failures,
            skipped=self.skipped,
        )


def test_parse_tdoc_url_passes_on_progress() -> None:
    """The PARSE_TDOC_URL handler passes a callable on_progress to the service."""
    repo = _make_repo()
    captured: dict = {}

    class _FakeUrlService:
        def extract_from_url_batch(self, url, **kwargs):
            captured["on_progress"] = kwargs.get("on_progress")
            return DirectParseBatchResult(results=[], failures={}, skipped={})

    state = _make_state(repo, url_service=_FakeUrlService())
    repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/tsg_ran/WG2_RL2/TSGR2_135/"},
    )
    worker = JobWorker(state, repo=repo)
    _run_worker_once(worker, repo)
    assert callable(captured.get("on_progress"))


def test_parse_tdoc_url_handler_rejects_non_3gpp_url() -> None:
    """Defence-in-depth: a tampered Job with a non-3GPP url raises ValueError."""
    from doc3gpp.models.jobs import JobKind

    repo = _make_repo()
    job = repo.create(JobKind.PARSE_TDOC_URL, {"url": "https://example.com/bad.zip"})
    state = _make_state(repo)
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.FAILED
    assert "3GPP FTP" in (done.error or "")


def test_parse_tdoc_url_handler_happy_path() -> None:
    """Happy path: results map to ``files[]`` with the right status labels."""
    from doc3gpp.models.tdoc_cr import DirectParseResult
    from doc3gpp.models.jobs import JobKind

    repo = _make_repo()
    state = _make_state(
        repo,
        url_service=_FakeTDocCrServiceForUrl(
            results=[
                DirectParseResult(
                    source_kind="url-3gpp",
                    markdown="",
                    details=None,
                    extract_meta=None,
                    from_cache=False,
                    persisted=True,
                    tdoc_id="R5-260001",
                    tdoc_id_in_tdocs=True,
                    source_url="https://www.3gpp.org/ftp/R5s260001.zip",
                ),
                DirectParseResult(
                    source_kind="url-3gpp",
                    markdown="",
                    details=None,
                    extract_meta=None,
                    from_cache=False,
                    persisted=False,
                    tdoc_id="R5-260002",
                    tdoc_id_in_tdocs=False,
                    source_url="https://www.3gpp.org/ftp/R5s260002.zip",
                ),
            ],
            failures={"https://www.3gpp.org/ftp/R5s260003.zip": "ZipError: corrupt"},
            skipped={"https://www.3gpp.org/ftp/R5s260004.zip": "TDocTooLargeError: ..."},
        ),
    )
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/", "force": True, "max_depth": 2},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.SUCCEEDED
    assert done.result_summary == {
        "requested": 4,
        "successes": 2,
        "failures": 1,
        "skipped": 1,
        "files": [
            {
                "tdoc_id": "R5-260001",
                "ftp_url": "https://www.3gpp.org/ftp/R5s260001.zip",
                "status": "ok",
            },
            {
                "tdoc_id": "R5-260002",
                "ftp_url": "https://www.3gpp.org/ftp/R5s260002.zip",
                "status": "parsed-no-fk",
            },
        ],
    }


class _FakeTDocSyncCoordinator:
    """Fake coordinator that records ``sync_for_meeting_id`` calls."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def sync_for_meeting_id(
        self, meeting_id: int, *, force: bool = False, on_progress: object | None = None
    ) -> object:
        from doc3gpp.models.sync import SyncOutcome
        self.calls.append(meeting_id)
        return SyncOutcome(
            status="synced", reason="ok", synced_count=0, file_count=0
        )

    def sync_for_meeting_name(self, *a, **k):  # pragma: no cover - not exercised here
        raise NotImplementedError

    def sync_all_tracked_meetings(self, *, force: bool = False):  # pragma: no cover
        raise NotImplementedError


def _make_url_state(
    repo: JobRepository,
    *,
    url_service: object,
    auto_sync: bool = False,
) -> WebState:
    settings = Settings()
    settings.sync.auto_sync = auto_sync
    services = ServiceContainer(
        meeting=_FakeMeetingService(),  # type: ignore[arg-type]
        tdoc=None,  # type: ignore[arg-type]
        tdoc_cr=url_service,  # type: ignore[arg-type]
        tdoc_sync=_FakeTDocSyncCoordinator(),  # type: ignore[arg-type]
        tdoc_repo=None,  # type: ignore[arg-type]
        tsg=None,  # type: ignore[arg-type]
        wi=None,  # type: ignore[arg-type]
        spec=_FakeSpecService(),  # type: ignore[arg-type]
        search=None,
        semantic_search=None,
        tdoc_file_repo=None,  # type: ignore[arg-type]
        job_repo=repo,
    )
    return WebState(settings=settings, engine=None, services=services, jobs=_JobWorkerHandleFake())  # type: ignore[arg-type]


def test_parse_tdoc_url_handler_recursive_uses_settings_depth() -> None:
    """``recursive=True`` is forwarded as ``settings.tdoc_parse.max_ftp_depth``."""
    from doc3gpp.models.jobs import JobKind

    repo = _make_repo()
    fake = _FakeTDocCrServiceForUrl()
    state = _make_url_state(repo, url_service=fake)
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/", "recursive": True},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done.status is JobStatus.SUCCEEDED
    assert fake.extract_calls[0]["max_depth"] == state.settings.tdoc_parse.max_ftp_depth


def test_parse_tdoc_url_handler_explicit_max_depth_forwarded() -> None:
    """``max_depth=5`` is forwarded verbatim; default is 2 when omitted."""
    from doc3gpp.models.jobs import JobKind

    repo = _make_repo()
    fake = _FakeTDocCrServiceForUrl()
    state = _make_url_state(repo, url_service=fake)
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/", "max_depth": 5},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done.status is JobStatus.SUCCEEDED
    assert fake.extract_calls[0]["max_depth"] == 5


def test_parse_tdoc_url_handler_default_max_depth_is_two() -> None:
    """No ``max_depth`` in params → ``max_depth=2`` forwarded to service."""
    from doc3gpp.models.jobs import JobKind

    repo = _make_repo()
    fake = _FakeTDocCrServiceForUrl()
    state = _make_url_state(repo, url_service=fake)
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/"},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done.status is JobStatus.SUCCEEDED
    assert fake.extract_calls[0]["max_depth"] == 2


def test_parse_tdoc_url_handler_auto_sync_runs_when_enabled() -> None:
    """``settings.sync.auto_sync=True`` triggers ``trigger_auto_sync``."""
    from doc3gpp.models.jobs import JobKind

    class _CandidateTDocCrService(_FakeTDocCrServiceForUrl):
        def __init__(self) -> None:
            super().__init__(
                file_urls=["https://www.3gpp.org/ftp/R5s260001.zip"],
            )

        def collect_3gpp_file_urls(self, url: str, *, max_depth: int) -> list[str]:
            return ["https://www.3gpp.org/ftp/R5s260001.zip"]

    repo = _make_repo()
    fake = _CandidateTDocCrService()
    state = _make_url_state(repo, url_service=fake, auto_sync=True)
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/"},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done.status is JobStatus.SUCCEEDED
    # The "auto-sync" progress line is appended; the parse still ran.
    assert any("auto-sync" in line for line in done.log_lines)
    assert any("done:" in line for line in done.log_lines)


def test_parse_tdoc_url_handler_auto_sync_disabled_skips_step() -> None:
    """``settings.sync.auto_sync=False`` → no ``collect_3gpp_file_urls`` call."""
    from doc3gpp.models.jobs import JobKind

    repo = _make_repo()
    fake = _FakeTDocCrServiceForUrl()
    state = _make_url_state(repo, url_service=fake, auto_sync=False)
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/"},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done.status is JobStatus.SUCCEEDED
    assert fake.collect_calls == []
    assert not any("auto-sync" in line for line in done.log_lines)


def test_parse_tdoc_url_handler_auto_sync_empty_candidates_skips() -> None:
    """``auto_sync=True`` with an empty candidate set does NOT trigger auto-sync."""
    from doc3gpp.models.jobs import JobKind

    repo = _make_repo()
    fake = _FakeTDocCrServiceForUrl()  # empty file_urls -> no candidates
    state = _make_url_state(repo, url_service=fake, auto_sync=True)
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/"},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done.status is JobStatus.SUCCEEDED
    assert len(fake.collect_calls) == 1          # collection ran
    assert not any("auto-sync" in line for line in done.log_lines)  # no trigger


def test_parse_tdoc_url_handler_auto_sync_failure_does_not_abort() -> None:
    """An exception in ``trigger_auto_sync`` is logged; the parse still runs."""
    from doc3gpp.models.jobs import JobKind

    class _RaisingTDocCrService(_FakeTDocCrServiceForUrl):
        def collect_3gpp_file_urls(self, url: str, *, max_depth: int) -> tuple[str, ...]:
            raise RuntimeError("network down")

    repo = _make_repo()
    fake = _RaisingTDocCrService()
    state = _make_url_state(repo, url_service=fake, auto_sync=True)
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/"},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done.status is JobStatus.SUCCEEDED
    assert any("done:" in line for line in done.log_lines)


def test_parse_tdoc_url_handler_cancellation_raises_cancelled() -> None:
    """``cancel_event`` set before the service call → ``CANCELLED`` job state."""
    import asyncio as _asyncio
    from doc3gpp.models.jobs import JobKind
    from doc3gpp.web.workers.job_worker import JobWorker as _JW

    repo = _make_repo()
    fake = _FakeTDocCrServiceForUrl()
    state = _make_url_state(repo, url_service=fake)
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/"},
    )

    cancel_event = _asyncio.Event()
    cancel_event.set()
    # Wire the pre-set event into worker state so ``_claim_and_run`` picks it
    # up instead of creating a fresh unset one.
    state.jobs.cancel_events[job.id] = cancel_event
    worker = _JW(state, repo=repo)

    async def _claim() -> None:
        sem = _asyncio.Semaphore(1)
        await worker._claim_and_run(repo.get(job.id), sem)  # type: ignore[attr-defined]

    _asyncio.run(_claim())

    done = repo.get(job.id)
    assert done.status is JobStatus.CANCELLED
    assert fake.extract_calls == []  # never reached the service


def test_parse_tdoc_url_handler_size_cap_forwarded() -> None:
    """``settings.tdoc_parse.max_tdoc_size_kb`` is forwarded as ``kb * 1024``."""
    from doc3gpp.models.jobs import JobKind

    repo = _make_repo()
    fake = _FakeTDocCrServiceForUrl()
    state = _make_url_state(repo, url_service=fake)
    state.settings.tdoc_parse.max_tdoc_size_kb = 1000
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/"},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done.status is JobStatus.SUCCEEDED
    assert fake.extract_calls[0]["max_tdoc_size_bytes"] == 1000 * 1024


def test_parse_tdoc_url_handler_size_cap_zero_means_unlimited() -> None:
    """``max_tdoc_size_kb=0`` → ``max_tdoc_size_bytes=None`` forwarded."""
    from doc3gpp.models.jobs import JobKind

    repo = _make_repo()
    fake = _FakeTDocCrServiceForUrl()
    state = _make_url_state(repo, url_service=fake)
    state.settings.tdoc_parse.max_tdoc_size_kb = 0
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/"},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done.status is JobStatus.SUCCEEDED
    assert fake.extract_calls[0]["max_tdoc_size_bytes"] is None


# ---------------------------------------------------------------------------
# In-flight cancellation of sync_* / cache_purge handlers
#
# The cancel button on the web /jobs page calls JobWorkerHandle.cancel which
# sets cancel_events[job_id] mid-run. The handler MUST observe the cancel
# event — either before invoking the blocking service or after it returns —
# and raise :class:`asyncio.CancelledError` so the worker routes the job to
# CANCELLED instead of SUCCEEDED. Before the fix, ``sync_meetings`` /
# ``sync_tdocs`` / ``sync_tdocs_all`` / ``sync_specs`` / ``cache_purge``
# held the event loop with the synchronous service call and never polled
# the event, so the job ran to completion and was marked SUCCEEDED.
#
# The fix wraps each blocking service call in ``await asyncio.to_thread``
# (so the loop is freed) and polls ``cancel_event.is_set()`` both before
# and after the call (so a pre-set or mid-flight event short-circuits the
# work and lands the job in CANCELLED). The pre-set tests below pin the
# "before-call" half of the contract: the cancel check fires before the
# service is invoked so the job never reaches the blocking work.
# ---------------------------------------------------------------------------


def _run_worker_with_pre_set_cancel(
    worker: JobWorker, repo: JobRepository, job_id: str
):
    """Run a worker claim with the cancel event pre-set.

    The test pattern is: pre-set the event (simulating "cancel arrived
    while the handler was about to do its blocking work"), then run the
    worker, then assert CANCELLED. The handler's ``cancel_event.is_set()``
    check fires BEFORE the service call so the service is never invoked.
    """

    async def _claim() -> None:
        queued = repo.list(status=JobStatus.QUEUED, limit=1)
        assert queued, "expected a queued job"
        sem = asyncio.Semaphore(1)
        await worker._claim_and_run(queued[0], sem)  # type: ignore[attr-defined]

    asyncio.run(_claim())
    return repo.get(job_id)


def _run_worker_with_mid_flight_cancel(
    worker: JobWorker,
    repo: JobRepository,
    job_id: str,
    cancel_event: asyncio.Event,
    *,
    trigger_delay: float = 0.05,
    release: threading.Event | None = None,
) -> None:
    """Run a worker claim; set the cancel event from a side-task mid-flight.

    Schedules a sibling asyncio task that waits ``trigger_delay`` seconds
    and then sets ``cancel_event``. The side-task also unblocks
    ``release`` (a threading.Event blocking the fake service call) after a
    short grace period so the to_thread can return and the handler's
    post-call cancel check can fire. Used to exercise the in-flight
    cancellation contract: the handler's blocking service call MUST be
    wrapped in ``asyncio.to_thread`` (so the loop is free) and MUST poll
    ``cancel_event.is_set()`` after the call (so the cancel lands the
    job in CANCELLED, not SUCCEEDED).
    """

    async def _claim() -> None:
        queued = repo.list(status=JobStatus.QUEUED, limit=1)
        assert queued, "expected a queued job"
        sem = asyncio.Semaphore(1)

        async def _trigger() -> None:
            # Set cancel shortly after the to_thread starts so the
            # handler's post-call check fires.
            await asyncio.sleep(trigger_delay)
            cancel_event.set()
            # Give the to_thread time to return after the cancel lands;
            # if a release event is provided, unblock the service call
            # so the to_thread can finish.
            if release is not None:
                await asyncio.sleep(0.02)
                release.set()

        trigger_task = asyncio.create_task(_trigger())
        try:
            await worker._claim_and_run(queued[0], sem)  # type: ignore[attr-defined]
        finally:
            trigger_task.cancel()
            try:
                await trigger_task
            except asyncio.CancelledError:
                pass

    asyncio.run(_claim())


def _make_state_with_services(repo: JobRepository, **services_kwargs) -> WebState:
    """Build a :class:`WebState` with the named fake services, defaults for the rest."""
    from doc3gpp.settings.schema import Settings
    defaults = dict(
        meeting=None,
        tdoc=None,
        tdoc_cr=None,
        tdoc_sync=None,
        tdoc_repo=None,
        tsg=_FakeTsgService({"R5"}),
        wi=None,
        spec=_FakeSpecService(),
        search=None,
        semantic_search=None,
        tdoc_file_repo=None,
        job_repo=repo,
    )
    defaults.update(services_kwargs)
    return WebState(
        settings=Settings(),
        engine=None,
        services=ServiceContainer(**defaults),  # type: ignore[arg-type]
        jobs=_JobWorkerHandleFake(),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Mid-flight cancellation: the bug the user reported
#
# A cancel that lands WHILE the handler's blocking service call is in
# flight must reach the handler (which yields via ``asyncio.to_thread``)
# so the job ends in CANCELLED. Without ``to_thread`` + a post-call
# ``cancel_event.is_set()`` check, the loop is held and the cancel
# cannot land — the job reaches SUCCEEDED.
#
# The fakes below await a ``release`` event INSIDE the service call to
# simulate a long-running blocking request: they yield to the loop, the
# cancel-task sets the event, and the handler observes it after the
# service returns (the post-call cancel check fires).
# ---------------------------------------------------------------------------


class _BlockOnReleaseMeetingService:
    """``MeetingService.sync`` is a BLOCKING call that waits for ``release``.

    Mirrors the production service shape: ``def sync(...)`` is synchronous
    and returns a ``SyncOutcome``. The blocking wait simulates a long
    network request; ``asyncio.to_thread`` runs it in a worker thread so
    the loop is free to deliver the cancel event.
    """

    def __init__(self, release: threading.Event) -> None:
        from doc3gpp.models.sync import SyncOutcome
        self.release = release

        def sync(*_a, **_kw):
            release.wait()  # blocking
            return SyncOutcome(
                status="synced", reason="ok", synced_count=3
            )

        self.sync = sync


class _BlockOnReleaseTDocSync:
    """TDocSyncCoordinator whose methods are BLOCKING calls waiting on ``release``."""

    def __init__(self, release: threading.Event) -> None:
        from doc3gpp.models.sync import SyncOutcome
        self.release = release

        def _by_id(*_a, **_kw):
            release.wait()
            return SyncOutcome(
                status="synced", reason="ok", synced_count=2, file_count=10,
            )

        def _all(*_a, **_kw):
            release.wait()
            return SyncOutcome(
                status="synced", reason="ok", synced_count=2, file_count=10,
            )

        self.sync_for_meeting_id = _by_id
        self.sync_for_meeting_name = _by_id
        self.sync_all_tracked_meetings = _all


class _BlockOnReleaseSpecService:
    """``SpecService.sync`` / ``sync_spec`` are BLOCKING calls waiting on ``release``."""

    def __init__(self, release: threading.Event) -> None:
        from doc3gpp.models.sync import SyncOutcome
        self.release = release

        def _impl(*_a, **_kw):
            release.wait()
            return SyncOutcome(
                status="synced", reason="ok", synced_count=5, version_count=12
            )

        self.sync = _impl
        self.sync_spec = _impl


def test_sync_meetings_mid_flight_cancel_lands_cancelled() -> None:
    """Mid-flight cancel on ``sync_meetings`` → CANCELLED.

    Reproduces the user-reported bug: clicking Cancel on a running
    sync_meetings job must transition the job to CANCELLED, not let it
    run to completion. The fake service is a BLOCKING call that waits
    for ``release``; ``asyncio.to_thread`` runs it in a worker thread
    so the loop is free, the side-task sets the cancel event during
    the wait, the handler observes it after the service returns,
    raises CancelledError, and the worker marks the job CANCELLED.
    Before the fix, the handler ran the service synchronously and
    never yielded → the cancel couldn't land → SUCCEEDED.
    """
    repo = _make_repo()
    release = threading.Event()
    svc = _BlockOnReleaseMeetingService(release=release)
    state = _make_state_with_services(repo, meeting=svc)
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"})

    cancel_event = asyncio.Event()
    state.jobs.cancel_events[job.id] = cancel_event

    worker = JobWorker(state, repo=repo)
    _run_worker_with_mid_flight_cancel(
        worker, repo, job.id, cancel_event, release=release,
    )

    done = repo.get(job.id)
    assert done.status is JobStatus.CANCELLED, (
        f"expected CANCELLED after mid-flight cancel; got {done.status.value}"
    )


def test_sync_tdocs_mid_flight_cancel_lands_cancelled() -> None:
    """Mid-flight cancel on ``sync_tdocs`` → CANCELLED."""
    repo = _make_repo()
    release = threading.Event()
    coord = _BlockOnReleaseTDocSync(release=release)
    state = _make_state_with_services(repo, tdoc_sync=coord)
    job = repo.create(JobKind.SYNC_TDOCS, {"meeting_id": 42})

    cancel_event = asyncio.Event()
    state.jobs.cancel_events[job.id] = cancel_event

    worker = JobWorker(state, repo=repo)
    _run_worker_with_mid_flight_cancel(
        worker, repo, job.id, cancel_event, release=release,
    )

    done = repo.get(job.id)
    assert done.status is JobStatus.CANCELLED


def test_sync_tdocs_all_mid_flight_cancel_lands_cancelled() -> None:
    """Mid-flight cancel on ``sync_tdocs_all`` → CANCELLED."""
    repo = _make_repo()
    release = threading.Event()
    coord = _BlockOnReleaseTDocSync(release=release)
    state = _make_state_with_services(repo, tdoc_sync=coord)
    job = repo.create(JobKind.SYNC_TDOCS_ALL, {})

    cancel_event = asyncio.Event()
    state.jobs.cancel_events[job.id] = cancel_event

    worker = JobWorker(state, repo=repo)
    _run_worker_with_mid_flight_cancel(
        worker, repo, job.id, cancel_event, release=release,
    )

    done = repo.get(job.id)
    assert done.status is JobStatus.CANCELLED


def test_sync_specs_mid_flight_cancel_lands_cancelled() -> None:
    """Mid-flight cancel on ``sync_specs`` (by tsg) → CANCELLED."""
    repo = _make_repo()
    release = threading.Event()
    svc = _BlockOnReleaseSpecService(release=release)
    state = _make_state_with_services(repo, spec=svc)
    job = repo.create(JobKind.SYNC_SPECS, {"tsg": "R5"})

    cancel_event = asyncio.Event()
    state.jobs.cancel_events[job.id] = cancel_event

    worker = JobWorker(state, repo=repo)
    _run_worker_with_mid_flight_cancel(
        worker, repo, job.id, cancel_event, release=release,
    )

    done = repo.get(job.id)
    assert done.status is JobStatus.CANCELLED


def test_sync_specs_by_id_mid_flight_cancel_lands_cancelled() -> None:
    """Mid-flight cancel on ``sync_specs`` (by spec_id) → CANCELLED."""
    repo = _make_repo()
    release = threading.Event()
    svc = _BlockOnReleaseSpecService(release=release)
    state = _make_state_with_services(repo, spec=svc)
    job = repo.create(JobKind.SYNC_SPECS, {"spec_id": "36.579-5"})

    cancel_event = asyncio.Event()
    state.jobs.cancel_events[job.id] = cancel_event

    worker = JobWorker(state, repo=repo)
    _run_worker_with_mid_flight_cancel(
        worker, repo, job.id, cancel_event, release=release,
    )

    done = repo.get(job.id)
    assert done.status is JobStatus.CANCELLED


def test_cache_purge_mid_flight_cancel_lands_cancelled() -> None:
    """Mid-flight cancel on ``cache_purge`` → CANCELLED."""
    import tempfile
    from pathlib import Path

    repo = _make_repo()
    release = threading.Event()
    with tempfile.TemporaryDirectory() as td:

        class _TrackingCache:
            def __init__(self, *args, **kwargs):
                self.purged = False
                self.purge_subdir_called = False

            def purge(self):
                release.wait()
                self.purged = True
                return 0

            def purge_subdir(self, scope):
                release.wait()
                self.purge_subdir_called = True
                return 0

        cache = _TrackingCache()
        state = _make_state_with_services(repo)
        from doc3gpp.settings.schema import CacheSettings

        monkey = __import__("pytest").MonkeyPatch()
        monkey.setattr(
            "doc3gpp.scraping.cache.TDocCache",
            lambda *a, **kw: cache,
        )
        try:
            state.settings = type(state.settings)(
                cache=CacheSettings(dir=Path(td)),
            )
            job = repo.create(JobKind.CACHE_PURGE, {"scope": "all"})
            cancel_event = asyncio.Event()
            state.jobs.cancel_events[job.id] = cancel_event
            worker = JobWorker(state, repo=repo)
            _run_worker_with_mid_flight_cancel(
                worker, repo, job.id, cancel_event, release=release,
            )
            done = repo.get(job.id)
            # Job lands in CANCELLED because the handler's post-call
            # cancel check fires after the to_thread returns; the
            # purge may or may not have set the flag depending on
            # timing, so we don't assert on it — only on the terminal
            # job status.
            assert done.status is JobStatus.CANCELLED
        finally:
            monkey.undo()
