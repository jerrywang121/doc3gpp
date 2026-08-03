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
    from doc3gpp.repository.protocols import JobRepository
    from doc3gpp.services.meetings_service import MeetingService
    from doc3gpp.services.search_service import SearchService
    from doc3gpp.services.semantic_search_service import SemanticSearchService
    from doc3gpp.services.tdoc_cr_service import TDocCrService
    from doc3gpp.services.tdoc_service import TDocService
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
    max_concurrent_jobs: int = 1

    def register_queue(self, job_id: str, queue: asyncio.Queue[dict]) -> None:
        """Attach an SSE event queue for ``job_id``."""
        self.event_queues[job_id] = queue

    def unregister_queue(self, job_id: str) -> None:
        """Remove the SSE event queue for ``job_id`` (terminal state)."""
        self.event_queues.pop(job_id, None)

    def cancel(self, job_id: str) -> bool:
        """Request cooperative cancellation for ``job_id``.

        Returns ``True`` when a cancellation event was found and set,
        ``False`` when the job has no registered event (already
        finished, or never started).
        """
        event = self.cancel_events.get(job_id)
        if event is None:
            return False
        event.set()
        return True

    async def shutdown(self) -> None:
        """Request cancellation for every job and await the worker task.

        Sets every registered cancellation event, then awaits the
        worker's ``task`` with a bounded timeout so a stuck blocking
        handler can't hang server shutdown forever.
        """
        for event in self.cancel_events.values():
            event.set()
        task = self.task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()


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
    tsg: "TsgService"
    wi: "WiService"
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