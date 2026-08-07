"""FastAPI app factory + lifespan wiring for the ``doc3gpp server`` surface.

T4 + T6 surface:

* :class:`WebState` / :class:`ServiceContainer` /
  :class:`JobWorkerHandle` — see :mod:`doc3gpp.web.state` (extracted
  so :mod:`doc3gpp.web.deps` can type-check against the state shape
  without an ``app``-level circular import).
* :func:`build_app` — constructs the FastAPI app, registers the health
  endpoint, mounts the T6 routers, mounts the vendored static assets
  (``/static/htmx.min.js`` + ``/static/style.css``), and conditionally
  mounts the MCP sub-app at ``/mcp`` when both ``[server].enabled``
  and ``[mcp].enabled`` are true.
* :func:`build_state` — synchronous helper used by the lifespan to
  wire :class:`WebState` once on startup.

This module deliberately stays free of any business logic — every
service is composed by delegating to ``services.factory.build_*``.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from doc3gpp.config import get_settings
from doc3gpp.services import factory
from doc3gpp.settings.schema import Settings
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository
from doc3gpp.web.errors import register_error_handlers
from doc3gpp.web.routes import all_routers
from doc3gpp.web.state import JobWorkerHandle, ServiceContainer, WebState
from doc3gpp.web.templates_setup import mount_static
from doc3gpp.web.workers.job_worker import JobWorker

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def build_state(settings: Settings) -> WebState:
    """Compose every collaborator wired into the web app.

    Pure composition: no I/O. The lifespan is responsible for any
    engine-level cleanup (``engine.dispose()``) on shutdown.
    """
    engine = get_engine()
    embedder = factory.build_embedder(settings)
    services = ServiceContainer(
        meeting=factory.build_meeting_service(),
        tdoc=factory.build_tdoc_service(),
        tdoc_cr=factory.build_tdoc_cr_service(embedder=embedder),
        tdoc_sync=factory.build_tdoc_sync_coordinator(),
        tdoc_repo=factory.build_tdoc_repository(),
        tsg=factory.build_tsg_service(),
        wi=factory.build_wi_service(),
        search=factory.build_search_service(embedder=embedder),
        semantic_search=factory.build_semantic_search_service(embedder=embedder),
        tdoc_file_repo=factory.build_tdoc_file_repository(),
        job_repo=SQLAlchemyJobRepository(),
    )
    return WebState(
        settings=settings,
        engine=engine,
        services=services,
        jobs=JobWorkerHandle(),
    )


@asynccontextmanager
async def _mount_mcp_in_lifespan(app: FastAPI):
    """Build the real MCP server from ``app.state.web`` and mount at ``/mcp``.

    Only mounts when both ``[server].enabled`` and ``[mcp].enabled`` are
    true and the ``mcp`` package is importable; otherwise silently no-ops.
    Called from the lifespan after ``app.state.web`` is set so the tools
    can reach the wired services.

    The ``streamable_http_app()`` returned by the MCP SDK is a Starlette
    app whose own lifespan runs the session manager's ``run()`` (which
    initialises the per-connection task group). When that app is mounted
    into FastAPI its lifespan is never executed, so we must enter
    ``server.session_manager.run()`` ourselves here and yield while it is
    active; otherwise every request fails with "Task group is not
    initialized".
    """
    settings = app.state.web.settings
    if not (settings.mcp.enabled and settings.server.enabled):
        yield
        return
    try:
        from doc3gpp.web.mcp_server import build_mcp_server
        from mcp.server.transport_security import TransportSecuritySettings

        server = build_mcp_server(app.state.web)
        security = TransportSecuritySettings(
            allowed_hosts=["127.0.0.1:*", "localhost:*"],
            allowed_origins=settings.mcp.allowed_origins,
        )
        if settings.mcp.transport == "sse":
            app.mount(
                "/mcp",
                server.sse_app(
                    sse_path="/sse",
                    message_path="/messages/",
                    transport_security=security,
                ),
            )
        else:
            app.mount(
                "/mcp",
                server.streamable_http_app(
                    streamable_http_path="/",
                    transport_security=security,
                ),
            )
        async with server.session_manager.run():
            yield
    except ImportError:
        logger.warning(
            "doc3gpp.web.app: mcp package is not installed; skipping /mcp mount",
        )
        yield
    except Exception:
        logger.warning(
            "doc3gpp.web.app: failed to construct MCP server; skipping /mcp mount",
            exc_info=True,
        )
        yield


def build_app(settings: Settings | None = None) -> FastAPI:
    """Construct the FastAPI app, wire lifespan, register handlers."""
    if settings is None:
        settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = build_state(settings)
        app.state.web = state
        worker = JobWorker(state)
        handle = JobWorkerHandle(max_concurrent_jobs=settings.server.max_concurrent_jobs)
        handle.task = asyncio.create_task(worker.run())
        state.jobs = handle
        try:
            async with _mount_mcp_in_lifespan(app):
                yield
        finally:
            await handle.shutdown()
            state.engine.dispose()

    app = FastAPI(title="doc3gpp", lifespan=lifespan)
    register_error_handlers(app)
    mount_static(app)

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    for router in all_routers():
        app.include_router(router)

    return app


__all__ = [
    "JobWorkerHandle",
    "ServiceContainer",
    "WebState",
    "build_app",
    "build_state",
]