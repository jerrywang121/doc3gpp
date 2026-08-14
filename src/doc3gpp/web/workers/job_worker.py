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

        The loop polls the ``jobs`` table for ``QUEUED`` rows every
        ``Settings.server.poll_interval_seconds`` (default 1s) and
        spawns at most ``Settings.server.max_concurrent_jobs`` handler
        tasks. The ``await`` on each in-flight task is yielded back to
        the event loop on every poll tick — a slow handler no longer
        delays pickup of newly enqueued jobs by ``cleanup_interval_seconds``
        the way it did when the loop's only idle-wait was a single
        ``asyncio.sleep(cleanup_interval_seconds)``.

        Retention cleanup runs on its own longer cadence (every
        ``Settings.server.cleanup_interval_seconds``, default 5m)
        so the DB doesn't grow unbounded. The two cadences are now
        separate: ``poll_interval_seconds`` for pickup latency,
        ``cleanup_interval_seconds`` for retention — the previous
        design conflated them and a 5-minute cleanup interval became
        a 5-minute pickup delay for every freshly enqueued parse /
        sync / cache-purge request.
        """
        settings = self._state.settings
        retention_seconds = _parse_retention(settings.server.log_retention)
        cleanup_interval = float(settings.server.cleanup_interval_seconds)
        poll_interval = float(settings.server.poll_interval_seconds)
        max_concurrent = settings.server.max_concurrent_jobs

        self._cleanup(retention_seconds)
        self._recover_orphans()
        logger.info(
            "job worker started (max_concurrent_jobs=%s, poll=%.2fs, cleanup=%.0fs)",
            max_concurrent, poll_interval, cleanup_interval,
        )

        semaphore = asyncio.Semaphore(max_concurrent)
        in_flight: set[asyncio.Task] = set()
        last_cleanup = asyncio.get_running_loop().time()
        try:
            while True:
                # Spawn handlers for every fresh QUEUED row up to the
                # concurrency cap. ``_claim_and_run`` itself is the
                # claim guard — see ``mark_running``'s WHERE-status
                # guard for the idempotency check.
                try:
                    queued = self._repo.list(
                        status=JobStatus.QUEUED, limit=max_concurrent,
                    )
                except Exception:
                    logger.exception("job worker failed to list queued jobs")
                    queued = []

                for job in queued:
                    if len(in_flight) >= max_concurrent:
                        break
                    task = asyncio.create_task(
                        self._claim_and_run(job, semaphore),
                        name=job.id,
                    )
                    in_flight.add(task)
                    task.add_done_callback(in_flight.discard)

                # Sleep until the next tick OR until a handler finishes,
                # whichever comes first. Yields to the event loop so
                # uvicorn / MCP requests stay responsive on a slow
                # parse / sync.
                if in_flight:
                    done, _pending = await asyncio.wait(
                        in_flight,
                        timeout=poll_interval,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    # Drain exceptions so they don't go silent.
                    for task in done:
                        if exc := task.exception():
                            logger.error(
                                "job handler raised: %r", exc,
                                exc_info=exc,
                            )
                else:
                    await asyncio.sleep(poll_interval)

                # Retention cleanup on its own cadence. Runs even when
                # handlers are active so the table doesn't accumulate
                # rows indefinitely on a busy install.
                now = asyncio.get_running_loop().time()
                if now - last_cleanup >= cleanup_interval:
                    self._cleanup(retention_seconds)
                    last_cleanup = now
        except asyncio.CancelledError:
            logger.info("job worker loop cancelled; awaiting %d handlers", len(in_flight))
            if in_flight:
                # Give in-flight handlers a chance to react to the
                # cancellation event before tearing down. Each handler
                # checks ``cancel_event`` between chunks and raises
                # ``asyncio.CancelledError`` cooperatively; the wait
                # is bounded so we don't hang on a wedged handler.
                for task in in_flight:
                    job_id = task.get_name()
                    event = self._state.jobs.cancel_events.get(job_id)
                    if event is not None:
                        event.set()
                await asyncio.gather(*in_flight, return_exceptions=True)
            raise

    def _recover_orphans(self) -> None:
        """Reset ``RUNNING`` rows left behind by a crashed prior worker.

        Without this pass a process restart while a handler was mid-flight
        leaves the row pinned at ``RUNNING`` forever — the worker only
        re-claims ``QUEUED`` rows. Recovery marks each orphan as
        ``FAILED`` with a synthetic ``orphaned_after_restart`` error so
        operators can see what happened instead of watching the badge
        count an unkillable row indefinitely.
        """
        try:
            orphans = self._repo.list(status=JobStatus.RUNNING, limit=1000)
        except Exception:
            logger.exception("orphan recovery failed to list RUNNING jobs")
            return
        for orphan in orphans:
            logger.warning(
                "recovering orphaned RUNNING job %s (started_at=%s)",
                orphan.id, orphan.started_at,
            )
            try:
                self._repo.mark_failed(
                    orphan.id,
                    error="orphaned_after_restart",
                )
            except Exception:
                logger.exception(
                    "failed to mark orphaned job %s as failed", orphan.id,
                )

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

            loop = asyncio.get_running_loop()
            interval = float(self._state.settings.server.progress_interval_seconds)
            state = {"last_emit": -interval, "pending": []}

            def _emit(message: str, ts: str) -> None:
                state["last_emit"] = loop.time()
                line = f"[{ts}] {message}"
                try:
                    self._repo.append_log(job.id, line=line)
                except Exception:
                    logger.exception("failed to append log for job %s", job.id)
                self._enqueue(queue, {"event": "log", "data": {"line": line}})

            def _do_progress(message: str, ts: str) -> None:
                now = loop.time()
                if now - state["last_emit"] >= interval:
                    _emit(message, ts)
                else:
                    state["pending"].append((ts, message))

            def progress(message: str) -> None:
                ts = _iso_now()
                try:
                    current = asyncio.get_running_loop()
                except RuntimeError:
                    current = None
                if current is loop:
                    _do_progress(message, ts)
                else:
                    loop.call_soon_threadsafe(_do_progress, message, ts)

            try:
                claimed, _ = self._repo.mark_running(job.id, message="starting")
                if not claimed:
                    # The claim lost a race: another worker already
                    # picked the row up (or it went terminal while we
                    # were queued). ``mark_running``'s WHERE-status
                    # guard made the write a no-op; skip the handler so
                    # the job is not executed twice.
                    logger.info(
                        "job %s claim lost; skipping",
                        job.id,
                    )
                    return
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
                for ts, msg in state["pending"]:
                    _emit(msg, ts)
                state["pending"].clear()
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
