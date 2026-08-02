"""FastAPI app factory + lifespan wiring for the ``doc3gpp server`` surface.

T4 implements only the skeleton:

* :class:`WebState` — the per-app state container (settings, engine,
  composed services, placeholder job worker).
* :class:`ServiceContainer` — the bag of wired services composed from
  the existing :mod:`doc3gpp.services.factory` builders. T4 builds it
  inline (in :func:`build_state`); T7 / T8 / T9 reuse it.
* :class:`JobWorkerHandle` — minimal sentinel so the lifespan can
  attach a placeholder to ``app.state.web`` today. T7 replaces it with
  the real implementation (background task + per-job queues).
* :func:`build_app` — constructs the FastAPI app, registers the health
  endpoint, mounts T6 routers (placeholder), conditionally mounts the
  MCP sub-app at ``/mcp`` when both ``[server].enabled`` and
  ``[mcp].enabled`` are true (T9 fills in the real MCP server).
* :func:`build_state` — synchronous helper used by the lifespan to
  wire :class:`WebState` once on startup.

This module deliberately stays free of any business logic — every
service is composed by delegating to ``services.factory.build_*``.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import FastAPI
from sqlalchemy.engine import Engine

from doc3gpp.config import get_settings
from doc3gpp.services import factory
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.search_service import SearchService
from doc3gpp.services.semantic_search_service import SemanticSearchService
from doc3gpp.services.tdoc_cr_service import TDocCrService
from doc3gpp.services.tdoc_service import TDocService
from doc3gpp.services.wi_service import WiService
from doc3gpp.settings.schema import Settings
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository
from doc3gpp.storage.repositories.tdoc_file_sql import SQLAlchemyTDocFileRepository
from doc3gpp.web.errors import register_error_handlers

if TYPE_CHECKING:
    from doc3gpp.repository.protocols import JobRepository

logger = logging.getLogger(__name__)


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

    meeting: MeetingService
    tdoc: TDocService
    tdoc_cr: TDocCrService
    wi: WiService
    search: SearchService | None
    semantic_search: SemanticSearchService | None
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


def build_state(settings: Settings) -> WebState:
    """Compose every collaborator wired into the web app.

    Pure composition: no I/O. The lifespan is responsible for any
    engine-level cleanup (``engine.dispose()``) on shutdown.
    """
    engine = get_engine()
    services = ServiceContainer(
        meeting=factory.build_meeting_service(),
        tdoc=factory.build_tdoc_service(),
        tdoc_cr=factory.build_tdoc_cr_service(),
        wi=factory.build_wi_service(),
        search=factory.build_search_service(),
        semantic_search=factory.build_semantic_search_service(),
        tdoc_file_repo=SQLAlchemyTDocFileRepository(),
        job_repo=SQLAlchemyJobRepository(),
    )
    return WebState(
        settings=settings,
        engine=engine,
        services=services,
        jobs=JobWorkerHandle(),
    )


def _maybe_mount_mcp(app: FastAPI, settings: Settings) -> None:
    """Mount the MCP sub-app at ``/mcp`` when both server + MCP are enabled.

    T9 supplies the real :class:`MCPServer`; T4 wires the conditional
    so flipping the two TOML flags later is a no-op here. When the
    MCP package is not installed we log a warning and skip rather than
    failing the import — the HTTP surface must stay bootable in any
    environment. T9 also owns the constructor kwargs (``stateless_http``
    etc.) so T4 keeps the placeholder minimal.
    """
    if not (settings.mcp.enabled and settings.server.enabled):
        return
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        logger.warning(
            "doc3gpp.web.app: mcp package is not installed; skipping /mcp mount",
        )
        return
    try:
        placeholder = MCPServer("doc3gpp")
        app.mount("/mcp", placeholder.streamable_http_app())
    except Exception:
        logger.warning(
            "doc3gpp.web.app: failed to construct MCP placeholder; "
            "skipping /mcp mount",
            exc_info=True,
        )


def build_app(settings: Settings | None = None) -> FastAPI:
    """Construct the FastAPI app, wire lifespan, register handlers."""
    if settings is None:
        settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = build_state(settings)
        app.state.web = state
        try:
            yield
        finally:
            state.engine.dispose()

    app = FastAPI(title="doc3gpp", lifespan=lifespan)
    register_error_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    _maybe_mount_mcp(app, settings)
    return app


__all__ = [
    "JobWorkerHandle",
    "ServiceContainer",
    "WebState",
    "build_app",
    "build_state",
]
