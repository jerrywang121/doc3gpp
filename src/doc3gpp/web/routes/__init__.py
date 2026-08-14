"""HTTP read routers exposed by the ``doc3gpp server`` surface.

Every module in this package exports a single ``APIRouter`` with the
appropriate prefix + tags. :func:`build_app` (in
:mod:`doc3gpp.web.app`) mounts them in :func:`build_app`'s body.

Job HTTP routes (T8) and the MCP sub-app (T9) live in their own
modules so T6 only depends on the read-side services and
repositories.
"""
from __future__ import annotations

from fastapi import APIRouter

from doc3gpp.web.routes.jobs import router as jobs_router
from doc3gpp.web.routes.landing import router as landing_router
from doc3gpp.web.routes.meetings import router as meetings_router
from doc3gpp.web.routes.search import router as search_router
from doc3gpp.web.routes.specs import router as specs_router
from doc3gpp.web.routes.sync import router as sync_router
from doc3gpp.web.routes.tdocs import router as tdocs_router
from doc3gpp.web.routes.tsgs import router as tsgs_router
from doc3gpp.web.routes.wis import router as wis_router


def all_routers() -> list[APIRouter]:
    """Return every router in the order they should be mounted."""
    return [
        landing_router,
        meetings_router,
        tdocs_router,
        tsgs_router,
        wis_router,
        specs_router,
        search_router,
        jobs_router,
        sync_router,
    ]


__all__ = ["all_routers"]