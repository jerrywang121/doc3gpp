"""Landing route: ``GET /``.

Returns the navigation hub. The list of sections comes from a
module-level constant — a static table that drives both the menu and
the rendered sections list. ``?format=json`` returns the same list as
JSON for machine clients (handy for ``curl`` / smoke tests).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from doc3gpp.web.deps import get_pending_jobs
from doc3gpp.web.render import to_jsonable
from doc3gpp.web.templates_setup import templates


router = APIRouter(tags=["landing"])


# Static table for the landing page. Each section maps a label +
# description to a router URL. The router is intentionally minimal —
# new sections appear here first and grow into their own module.
_SECTIONS: list[dict[str, str]] = [
    {
        "label": "TSGs",
        "href": "/tsgs",
        "description": "Reference list of 3GPP TSGs (Working Groups + Plenaries).",
    },
    {
        "label": "Meetings",
        "href": "/meetings",
        "description": "Browse stored meeting records, optionally filtered by TSG / year.",
    },
    {
        "label": "TDocs",
        "href": "/tdocs",
        "description": "Browse stored TDoc metadata with the same filter grammar as the CLI.",
    },
    {
        "label": "Specs",
        "href": "/specs",
        "description": "3GPP specifications (TSs / TRs) with their versions.",
    },
    {
        "label": "WIs",
        "href": "/wis",
        "description": "Work Items indexed per TSG.",
    },
    {
        "label": "Search",
        "href": "/search",
        "description": "Full-text search across stored TDocs (FTS5).",
    },
    {
        "label": "Jobs",
        "href": "/jobs",
        "description": "Background job queue status (read-only view).",
    },
]


@router.get("/", include_in_schema=False)
async def landing(
    request: Request,
    format: str | None = Query(default=None, alias="format"),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render ``landing.html`` (default) or a JSON sections list."""
    if format == "json":
        return JSONResponse(content={"sections": to_jsonable(_SECTIONS)})
    return templates.TemplateResponse(
        request=request,
        name="landing.html",
        context={"sections": _SECTIONS, "active_nav": "home", "pending_jobs": pending_jobs},
    )


__all__ = ["router"]