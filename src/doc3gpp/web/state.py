"""Per-app state dataclasses for the ``doc3gpp server`` surface.

Extracted out of :mod:`doc3gpp.web.app` so :mod:`doc3gpp.web.deps` (and
the route modules that import from ``deps``) can type-check against
the state shape without importing ``app`` itself — which would create
a circular import (``app`` mounts the routers; routers import
``deps``; ``deps`` imports ``app`` for the dataclasses).

The shape mirrors the T4 contract:

* :class:`WebState` — per-app state container.
* :class:`ServiceContainer` — bag of wired services.
* :class:`JobWorkerHandle` — placeholder for the eventual real
  worker; T7 replaces it.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy.engine import Engine

from doc3gpp.settings.schema import Settings
from doc3gpp.storage.repositories.tdoc_file_sql import SQLAlchemyTDocFileRepository

if TYPE_CHECKING:
    from doc3gpp.repository.protocols import JobRepository, TDocRepository
    from doc3gpp.services.meetings_service import MeetingService
    from doc3gpp.services.search_service import SearchService
    from doc3gpp.services.semantic_search_service import SemanticSearchService
    from doc3gpp.services.spec_service import SpecService
    from doc3gpp.services.tdoc_cr_service import TDocCrService
    from doc3gpp.services.tdoc_service import TDocService
    from doc3gpp.services.tdoc_sync_coordinator import TDocSyncCoordinator
    from doc3gpp.services.tsg_service import TsgService
    from doc3gpp.services.wi_service import WiService


@dataclass(slots=True)
class JobWorkerHandle:
    """Handle for the background job worker (T7).

    Attached to :attr:`WebState.jobs` so route handlers (T8) and the
    MCP mount (T9) can drive the worker from request context without
    reaching into the running :class:`JobWorker` task directly.

    Attributes:
        task: The asyncio task running the worker's ``run()`` loop, or
            ``None`` before the lifespan starts it.
        event_queues: Per-job ``asyncio.Queue`` of SSE event dicts,
            keyed by ``job.id``. T8's ``/jobs/{id}/events`` stream
            drains the queue for the requested job; the queue is
            removed via :meth:`unregister_queue` when the job reaches
            a terminal state.
        cancel_events: Per-job ``asyncio.Event``, keyed by ``job.id``.
            :meth:`cancel` sets the event to ask the running handler to
            stop cooperatively.
        max_concurrent_jobs: How many jobs may run concurrently (from
            ``Settings.server.max_concurrent_jobs``).
    """

    task: asyncio.Task | None = None
    event_queues: dict[str, asyncio.Queue[dict]] = field(default_factory=dict)
    cancel_events: dict[str, asyncio.Event] = field(default_factory=dict)
    #: Job ids whose cancellation was requested while still ``QUEUED`` (no
    #: running handler event exists yet). Consumed by the worker when it
    #: claims the job, so a cancel-request on a queued job is not lost.
    _cancel_requests: set[str] = field(default_factory=set)
    max_concurrent_jobs: int = 1

    def register_queue(self, job_id: str, queue: asyncio.Queue[dict]) -> None:
        """Attach an SSE event queue for ``job_id``."""
        self.event_queues[job_id] = queue

    def unregister_queue(self, job_id: str) -> None:
        """Remove the SSE event queue for ``job_id`` (terminal state)."""
        self.event_queues.pop(job_id, None)

    def cancel(self, job_id: str) -> bool:
        """Request cooperative cancellation for ``job_id``.

        When a running handler already registered a cancellation event it
        is set immediately. Otherwise the request is recorded so the
        worker honours it once it claims the ``QUEUED`` job. Returns
        ``True`` whenever a request was recorded.
        """
        event = self.cancel_events.get(job_id)
        if event is not None:
            event.set()
            return True
        self._cancel_requests.add(job_id)
        return True

    def consume_cancel_request(self, job_id: str) -> bool:
        """Return whether ``job_id`` has a pending queued cancel request.

        Used by the worker when it claims a job: it mints a fresh
        cancellation event, and if a cancel was requested while the job
        was still queued, sets that event so the handler aborts
        immediately. The pending request is consumed (removed) so a
        later duplicate cancel does not re-fire.
        """
        if job_id in self._cancel_requests:
            self._cancel_requests.discard(job_id)
            return True
        return False

    async def shutdown(self) -> None:
        """Request cancellation for every job and await the worker task.

        Sets every registered cancellation event, then cancels the
        worker's ``task`` immediately so an idle loop (sleeping between
        cleanup ticks) stops promptly. The bounded wait below only guards
        against a stuck blocking handler taking longer to abort.
        """
        for event in self.cancel_events.values():
            event.set()
        task = self.task
        if task is None or task.done():
            return
        task.cancel()
        try:
            # return_exceptions swallows the CancelledError raised when the
            # cancelled task finishes, so shutdown never propagates it up to
            # the server lifespan / TestClient teardown.
            await asyncio.wait_for(
                asyncio.shield(asyncio.gather(task, return_exceptions=True)),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            # A stuck handler ignored cancellation; nothing more to do.
            pass


@dataclass(slots=True)
class ServiceContainer:
    """Bag of wired services composed at lifespan startup.

    Each field holds the live instance the lifespan built via
    :mod:`doc3gpp.services.factory`. Routes depend on individual fields
    via the helpers in :mod:`doc3gpp.web.deps`.
    """

    meeting: "MeetingService"
    tdoc: "TDocService"
    tdoc_cr: "TDocCrService"
    tdoc_sync: "TDocSyncCoordinator"
    tdoc_repo: "TDocRepository"
    tsg: "TsgService"
    wi: "WiService"
    spec: "SpecService"
    search: "SearchService | None"
    semantic_search: "SemanticSearchService | None"
    tdoc_file_repo: SQLAlchemyTDocFileRepository
    job_repo: "JobRepository"


@dataclass(slots=True)
class WebState:
    """Per-app state container attached to ``app.state.web``.

    Holds the resolved :class:`Settings`, the singleton SQLAlchemy
    :class:`Engine`, the :class:`ServiceContainer` of wired services,
    and a placeholder :class:`JobWorkerHandle` (replaced by T7).
    """

    settings: Settings
    engine: Engine
    services: ServiceContainer
    jobs: JobWorkerHandle


__all__ = ["JobWorkerHandle", "ServiceContainer", "WebState"]