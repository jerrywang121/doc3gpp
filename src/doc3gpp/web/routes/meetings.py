"""HTTP routes for the meeting list / detail pages.

* ``GET /meetings`` — list page with HTMX-driven filters.
* ``GET /meetings/{meeting_id}`` — detail page with a "Sync this
  meeting's TDocs" button that POSTs to ``/jobs/sync_tdocs`` (T8
  supplies the actual handler; the form is wired today so the UI is
  complete by the time T8 lands).

``?format=json`` on the list route returns the same payload as
``doc3gpp meeting list --format json`` (a bare array of field-selected,
string-coerced rows via :func:`doc3gpp.web.render.meeting_rows`); the
detail route returns the :func:`doc3gpp.web.render.to_jsonable`
envelope of the meeting record.
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
from doc3gpp.web.filters import is_htmx_request, parse_int_query, parse_text_query
from doc3gpp.web.render import meeting_rows, to_jsonable
from doc3gpp.web.templates_setup import templates


router = APIRouter(prefix="/meetings", tags=["meetings"])


_LIMIT_CAP = 200

# Mirrors ``settings.output.fields.meeting`` — what
# ``doc3gpp meeting list --format json`` emits by default.
_MEETING_DEFAULT_FIELDS = [
    "meeting_id",
    "name",
    "location",
    "start_date",
    "end_date",
    "ftp_url",
    "start_doc",
    "end_doc",
]


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def list_meetings(
    request: Request,
    tsg: str | None = Query(default=None),
    year: str | None = Query(default=None),
    location: str | None = Query(default=None),
    limit: str | None = Query(default="50"),
    offset: str | None = Query(default="0"),
    format: str | None = Query(default=None, alias="format"),
    service: MeetingService = Depends(get_meeting_service),
) -> Any:
    """Render ``meeting_list.html`` or a JSON list of meetings.

    ``?format=json`` returns the same payload as
    ``doc3gpp meeting list --format json``: a bare array of
    field-selected rows (``settings.output.fields.meeting`` by
    default) with every cell string-coerced via
    :func:`doc3gpp.web.render.meeting_rows`.

    The numeric query params (``year``, ``limit``, ``offset``) are
    declared as ``str`` so an empty form value (``year=``) doesn't
    trigger a 422 — :func:`parse_int_query` treats ``""`` as ``None``
    and the route fills in the default. The CLI's typed path accepts
    ints only; the HTTP path is a best-effort form-binding layer.
    """
    parsed_limit = parse_int_query(limit, min=1, max=_LIMIT_CAP) or 50
    parsed_offset = parse_int_query(offset, min=0) or 0
    parsed_tsg = parse_text_query(tsg)
    parsed_location = parse_text_query(location)
    parsed_year = parse_int_query(year, min=1970, max=2100)

    meetings = service.list_recent(
        limit=parsed_limit,
        offset=parsed_offset,
        tsg=parsed_tsg,
        location_like=parsed_location,
        year=parsed_year,
    )

    if format == "json":
        return JSONResponse(content=meeting_rows(meetings, _MEETING_DEFAULT_FIELDS))

    next_offset = parsed_offset + len(meetings) if len(meetings) == parsed_limit else None
    template_name = (
        "partials/meeting_results.html" if is_htmx_request(request) else "meeting_list.html"
    )
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "active_nav": "meetings",
            "meetings": meetings,
            "total": len(meetings),
            "limit": parsed_limit,
            "offset": parsed_offset,
            "next_offset": next_offset,
            "filters": {
                "tsg": parsed_tsg or "",
                "year": year,
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