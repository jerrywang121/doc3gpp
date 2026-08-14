"""HTTP route for the ``/sync`` hub page.

Exposes two GETs:

* ``GET /sync`` — full HTML page with nine enqueue panels + the
  bottom ``#recent-jobs`` div.
* ``GET /sync?format=fragment`` — partial HTML containing only the
  recent-jobs table fragment (wrapped in ``<div id="recent-jobs">`` so
  HTMX ``outerHTML`` swap preserves the swap-target id on both ends).

The full page and the fragment both pull from the same underlying
``recent_jobs`` query so a refresh is byte-consistent with the initial
render.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from doc3gpp.repository.protocols import JobRepository
from doc3gpp.web.deps import get_job_repo, get_pending_jobs
from doc3gpp.web.templates_setup import templates


router = APIRouter(prefix="/sync", tags=["sync"])


_RECENT_LIMIT = 10


@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def sync_hub(
    request: Request,
    format: str | None = Query(default=None, alias="format"),
    job_repo: JobRepository = Depends(get_job_repo),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render the full ``sync.html`` page or just the recent-jobs fragment."""
    jobs = job_repo.list(limit=_RECENT_LIMIT) or []
    if format == "fragment":
        return templates.TemplateResponse(
            request=request,
            name="partials/sync_recent_jobs.html",
            context={"jobs": jobs},
        )
    return templates.TemplateResponse(
        request=request,
        name="sync.html",
        context={
            "active_nav": "sync",
            "recent_jobs": jobs,
            "pending_jobs": pending_jobs,
        },
    )


__all__ = ["router"]
