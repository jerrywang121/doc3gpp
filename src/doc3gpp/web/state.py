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

from dataclasses import dataclass
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
    """Placeholder handle for the background job worker (replaced in T7).

    T4 attaches one of these to :attr:`WebState.jobs` so downstream
    code (route handlers, the MCP mount) can already type-check against
    the eventual real handle. T7 swaps the placeholder for the real
    implementation without changing the field type.
    """


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