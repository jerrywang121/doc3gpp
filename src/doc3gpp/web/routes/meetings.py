"""HTTP routes for the meeting list / detail pages.

* ``GET /meetings`` — list page with HTMX-driven filters.
* ``GET /meetings/{meeting_id}`` — detail page with a "Sync this
  meeting's TDocs" button that POSTs to ``/jobs/sync_tdocs`` (T8
  supplies the actual handler; the form is wired today so the UI is
  complete by the time T8 lands).

``?format=json`` returns the same payload as the underlying
``MeetingService.list_recent`` / ``MeetingService.get_by_id`` calls —
:func:`doc3gpp.web.render.to_jsonable` is the single source of truth
for serialisation.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi import Path as PathParam
from fastapi import Query, Request
from fastapi.responses import JSONResponse

from doc3gpp.models.meeting import Meeting
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.tdoc_sync_coordinator import MeetingNotFoundError
from doc3gpp.web.deps import get_meeting_service
from doc3gpp.web.filters import parse_date_query, parse_int_query, parse_text_query
from doc3gpp.web.render import to_jsonable
from doc3gpp.web.templates_setup import templates


router = APIRouter(prefix="/meetings", tags=["meetings"])


_LIMIT_CAP = 200


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def list_meetings(
    request: Request,
    tsg: str | None = Query(default=None),
    year: int | None = Query(default=None),
    start_after: str | None = Query(default=None),
    start_before: str | None = Query(default=None),
    location: str | None = Query(default=None),
    limit: int | None = Query(default=50),
    offset: int | None = Query(default=0),
    format: str | None = Query(default=None, alias="format"),
    service: MeetingService = Depends(get_meeting_service),
) -> Any:
    """Render ``meeting_list.html`` or a JSON list of meetings."""
    parsed_limit = parse_int_query(str(limit) if limit is not None else None,
                                    min=1, max=_LIMIT_CAP) or 50
    parsed_offset = parse_int_query(str(offset) if offset is not None else None,
                                    min=0) or 0
    parsed_start_after = parse_date_query(start_after)
    parsed_start_before = parse_date_query(start_before)
    parsed_tsg = parse_text_query(tsg)
    parsed_location = parse_text_query(location)

    meetings = service.list_recent(
        limit=parsed_limit,
        offset=parsed_offset,
        tsg=parsed_tsg,
        location_like=parsed_location,
        year=year,
    )

    if format == "json":
        return JSONResponse(content={"meetings": to_jsonable(meetings)})

    next_offset = parsed_offset + len(meetings) if len(meetings) == parsed_limit else None
    return templates.TemplateResponse(
        request=request,
        name="meeting_list.html",
        context={
            "active_nav": "meetings",
            "meetings": meetings,
            "total": len(meetings),
            "offset": parsed_offset,
            "next_offset": next_offset,
            "filters": {
                "tsg": parsed_tsg or "",
                "year": year,
                "start_after": parsed_start_after or "",
                "start_before": parsed_start_before or "",
                "location": parsed_location or "",
                "limit": parsed_limit,
            },
        },
    )


@router.get("/{meeting_id}", include_in_schema=False)
async def show_meeting(
    request: Request,
    meeting_id: int = PathParam(...),
    format: str | None = Query(default=None, alias="format"),
    service: MeetingService = Depends(get_meeting_service),
) -> Any:
    """Render ``meeting_show.html`` (default) or a JSON meeting record."""
    meeting: Meeting | None = service.get_by_id(meeting_id)
    if meeting is None:
        raise MeetingNotFoundError(f"Meeting {meeting_id} not found")

    if format == "json":
        return JSONResponse(content={"meeting": to_jsonable(meeting)})

    return templates.TemplateResponse(
        request=request,
        name="meeting_show.html",
        context={"active_nav": "meetings", "meeting": meeting},
    )


__all__ = ["router"]