"""Tests for the background job worker (:mod:`doc3gpp.web.workers`).

These are unit tests: they exercise the worker loop, the per-kind
handler registry, and the terminal-state transitions against an
in-memory sqlite ``jobs`` table plus a fake :class:`ServiceContainer`
whose service methods are stubs. No network calls are made.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from doc3gpp.models.jobs import JobKind, JobStatus
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


def _make_state(repo: JobRepository, *, fail: bool = False) -> WebState:
    """Build a :class:`WebState` with a fake :class:`ServiceContainer`."""
    services = ServiceContainer(
        meeting=_FakeMeetingService(fail=fail),  # type: ignore[arg-type]
        tdoc=None,  # type: ignore[arg-type]
        tdoc_cr=None,  # type: ignore[arg-type]
        tdoc_sync=None,  # type: ignore[arg-type]
        tdoc_repo=None,  # type: ignore[arg-type]
        tsg=None,  # type: ignore[arg-type]
        wi=None,  # type: ignore[arg-type]
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

    def register_queue(self, job_id: str, queue: asyncio.Queue[dict]) -> None:
        self.event_queues[job_id] = queue

    def unregister_queue(self, job_id: str) -> None:
        self.event_queues.pop(job_id, None)


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
