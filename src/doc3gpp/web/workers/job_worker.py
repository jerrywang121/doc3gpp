"""Asyncio worker loop that runs background jobs for the HTTP / MCP server.

T7 introduces the single-process worker that claims ``QUEUED`` jobs
from the :class:`JobRepository`, resolves the per-kind handler from
:data:`JobHandlers.KIND_TO_HANDLER`, runs it while streaming log /
status events to the job's SSE queue, and transitions the row to a
terminal state (``SUCCEEDED`` / ``FAILED`` / ``CANCELLED``).

Design notes (spec'd v1 trade-offs):

* **Single worker task.** The worker is one asyncio task; jobs run
  sequentially (``max_concurrent_jobs`` bounds how many are picked up
  per tick). There is deliberately no multi-process pool and no
  ``asyncio.to_thread`` wrapping — the service methods are blocking
  (httpx + sqlalchemy) and v1 accepts that they occupy the event loop
  (spec line ~645).
* **Cooperative cancellation.** Each job gets an ``asyncio.Event``;
  handlers check :meth:`asyncio.Event.is_set` between chunks and raise
  :class:`asyncio.CancelledError` to unwind. :meth:`JobWorkerHandle.cancel`
  sets the event.
* **Retention cleanup.** On every tick the worker deletes terminal jobs
  older than the configured retention window
  (``Settings.server.log_retention``).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from humanfriendly import InvalidTimespan, parse_timespan

from doc3gpp.models.jobs import Job, JobKind, JobStatus
from doc3gpp.repository.protocols import JobRepository
from doc3gpp.web.state import WebState
from doc3gpp.web.workers.handlers import Handler, JobHandlers

logger = logging.getLogger(__name__)


def _iso_now() -> str:
    """Return the current UTC time as an ISO-8601 string (second precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_retention(retention: str) -> float:
    """Parse a ``log_retention`` string into a seconds float.

    Falls back to 7 days when the string is invalid so the worker never
    crashes on an unparseable config.
    """
    try:
        return float(parse_timespan(retention))
    except (InvalidTimespan, ValueError):
        logger.warning("unparseable log_retention %r; using 7d", retention)
        return float(parse_timespan("7d"))


class JobWorker:
    """Background job worker owned by the server lifespan.

    Runs a single loop that claims one ``QUEUED`` job per tick, streams
    its progress to the job's SSE queue, and writes the terminal state
    back through the repository.
    """

    def __init__(
        self,
        state: WebState,
        *,
        queue_size: int = 100,
        repo: JobRepository | None = None,
        handlers: dict[JobKind, Handler] | None = None,
    ) -> None:
        """Initialize the worker.

        Args:
            state: The per-app :class:`WebState` holding the repository,
                services, and settings.
            queue_size: Maximum number of SSE events buffered per job.
            repo: Optional explicit :class:`JobRepository` (tests inject
                a fake). Defaults to ``state.services.job_repo``.
            handlers: Optional explicit handler registry (tests inject a
                fake). Defaults to :data:`JobHandlers.KIND_TO_HANDLER`.
        """
        self._state = state
        self._repo = repo or state.services.job_repo
        self._handlers = handlers or JobHandlers.KIND_TO_HANDLER
        self._queue_size = queue_size

    async def run(self) -> None:
        """Run the worker loop until :meth:`shutdown` cancels the task.

        The loop runs cleanup on startup and then once per tick,
        sleeping ``Settings.server.cleanup_interval_seconds`` between
        ticks.
        """
        settings = self._state.settings
        retention_seconds = _parse_retention(settings.server.log_retention)
        cleanup_interval = settings.server.cleanup_interval_seconds
        max_concurrent = settings.server.max_concurrent_jobs

        self._cleanup(retention_seconds)
        logger.info("job worker started (max_concurrent_jobs=%s)", max_concurrent)

        semaphore = asyncio.Semaphore(max_concurrent)
        while True:
            try:
                queued = self._repo.list(status=JobStatus.QUEUED, limit=max_concurrent)
            except Exception:
                logger.exception("job worker failed to list queued jobs")
                queued = []

            pending: list[asyncio.Task] = []
            for job in queued:
                task = asyncio.create_task(self._claim_and_run(job, semaphore))
                pending.append(task)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            await asyncio.sleep(cleanup_interval)
            self._cleanup(retention_seconds)

    async def _claim_and_run(
        self,
        job: Job,
        semaphore: asyncio.Semaphore,
    ) -> None:
        """Claim, run, and terminalise a single queued job under ``semaphore``."""
        async with semaphore:
            handler = self._handlers.get(job.kind)
            if handler is None:
                logger.warning("no handler for job kind %s", job.kind)
                self._repo.mark_failed(job.id, error=f"unknown job kind: {job.kind}")
                return

            # Register cancellation state, preserving any event a route or
            # test already attached.
            cancel_events = self._state.jobs.cancel_events
            if job.id not in cancel_events:
                cancel_events[job.id] = asyncio.Event()
            # Honour a cancel requested while the job was still queued.
            if self._state.jobs.consume_cancel_request(job.id):
                cancel_events[job.id].set()
            # Reuse an already-registered SSE queue (the route may attach
            # one before the worker picks the job up), else create one.
            queue = self._state.jobs.event_queues.get(job.id) or asyncio.Queue(
                maxsize=self._queue_size
            )
            self._state.jobs.register_queue(job.id, queue)
            cancel_event = cancel_events[job.id]

            def progress(message: str) -> None:
                line = f"[{_iso_now()}] {message}"
                try:
                    self._repo.append_log(job.id, line=line)
                except Exception:
                    logger.exception("failed to append log for job %s", job.id)
                self._enqueue(queue, {"event": "log", "data": {"line": line}})

            try:
                self._repo.mark_running(job.id, message="starting")
                self._enqueue(
                    queue,
                    {"event": "status", "data": {"status": JobStatus.RUNNING.value}},
                )
                if cancel_event.is_set():
                    raise asyncio.CancelledError()
                summary = handler(job, self._state.services, self._state.settings, progress=progress, cancel_event=cancel_event)
                summary = await summary
            except asyncio.CancelledError:
                self._enqueue(
                    queue,
                    {"event": "status", "data": {"status": JobStatus.CANCELLED.value}},
                )
                self._repo.mark_cancelled(job.id)
                logger.info("job %s cancelled", job.id)
            except Exception as exc:  # noqa: BLE001 - terminalise on any error
                logger.exception("job %s failed", job.id)
                self._enqueue(
                    queue,
                    {"event": "status", "data": {"status": JobStatus.FAILED.value, "error": str(exc)}},
                )
                self._repo.mark_failed(job.id, error=str(exc))
            else:
                self._enqueue(
                    queue,
                    {
                        "event": "status",
                        "data": {
                            "status": JobStatus.SUCCEEDED.value,
                            "summary": dict(summary),
                        },
                    },
                )
                self._repo.mark_succeeded(job.id, summary=dict(summary))
                logger.info("job %s succeeded", job.id)
            finally:
                self._state.jobs.unregister_queue(job.id)
                self._state.jobs.cancel_events.pop(job.id, None)

    @staticmethod
    def _enqueue(queue: asyncio.Queue[dict], event: dict) -> None:
        """Put an event on ``queue``, dropping the oldest entry when full."""
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(event)

    def _cleanup(self, retention_seconds: float) -> None:
        """Delete terminal jobs older than the retention window."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=retention_seconds)
        try:
            deleted = self._repo.delete_older_than(cutoff)
        except Exception:
            logger.exception("job worker cleanup failed")
            return
        if deleted:
            logger.info("job worker cleanup deleted %s job(s)", deleted)


__all__ = ["JobWorker"]
