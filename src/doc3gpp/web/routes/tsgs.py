"""HTTP routes for the TSG reference pages.

TSGs are a small lookup table (~18 records) so the list endpoint
returns every record in a single SQL query. The detail endpoint
embeds a meeting-list partial scoped by ``tsg``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi import Path as PathParam
from fastapi import Query, Request
from fastapi.responses import JSONResponse

from doc3gpp.models.schema_info import RESOURCE_SCHEMAS, schema_payload
from doc3gpp.models.tsg import Tsg
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.tsg_service import TsgService
from doc3gpp.web.deps import get_meeting_service, get_pending_jobs, get_tsg_service
from doc3gpp.web.errors import TSGNotFoundError
from doc3gpp.web.filters import is_htmx_request
from doc3gpp.web.render import to_jsonable, tsg_rows
from doc3gpp.web.templates_setup import templates


router = APIRouter(prefix="/tsgs", tags=["tsgs"])


# Mirrors ``settings.output.fields.tsg`` — what
# ``doc3gpp tsg list --format json`` emits by default.
_TSG_DEFAULT_FIELDS = ["tsg_name", "short_name", "description"]


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def list_tsgs(
    request: Request,
    format: str | None = Query(default=None, alias="format"),
    service: TsgService = Depends(get_tsg_service),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render ``tsg_list.html`` or a JSON list of TSGs.

    ``?format=json`` returns the same payload as
    ``doc3gpp tsg list --format json``: a bare array of field-selected
    rows (``settings.output.fields.tsg`` by default) with every cell
    string-coerced via :func:`doc3gpp.web.render.tsg_rows`.
    """
    tsgs = service.list_all()
    if format == "json":
        return JSONResponse(content=tsg_rows(tsgs, _TSG_DEFAULT_FIELDS))
    return templates.TemplateResponse(
        request=request,
        name="tsg_list.html",
        context={"active_nav": "tsgs", "tsgs": tsgs, "pending_jobs": pending_jobs},
    )


@router.get("/schema", include_in_schema=False)
async def tsg_schema(
    request: Request,
    format: str | None = Query(default=None, alias="format"),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render ``schema.html`` or the CLI-identical JSON field descriptors.

    ``?format=json`` returns the same payload as
    ``doc3gpp tsg schema --format json``: a bare array of
    ``{table, field, type, nullable, description, values}`` rows from
    :func:`doc3gpp.models.schema_info.schema_payload`. No DB access.
    """
    if format == "json":
        return JSONResponse(content=schema_payload("tsg"))
    tables = RESOURCE_SCHEMAS["tsg"]
    template_name = (
        "partials/schema_results.html" if is_htmx_request(request) else "schema.html"
    )
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "active_nav": "tsgs",
            "resource": "tsg",
            "tables": tables,
            "total": sum(len(t.fields) for t in tables),
            "pending_jobs": pending_jobs,
        },
    )


@router.get("/{short_name}", include_in_schema=False)
async def show_tsg(
    request: Request,
    short_name: str = PathParam(...),
    format: str | None = Query(default=None, alias="format"),
    tsg_service: TsgService = Depends(get_tsg_service),
    meeting_service: MeetingService = Depends(get_meeting_service),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render ``tsg_show.html`` or a JSON payload with TSG + meetings."""
    tsg: Tsg | None = tsg_service.get_by_short_name(short_name)
    if tsg is None:
        raise TSGNotFoundError(f"TSG {short_name!r} not found")
    meetings = meeting_service.list_recent(tsg=tsg.short_name, limit=200)

    if format == "json":
        return JSONResponse(
            content={"tsg": to_jsonable(tsg), "meetings": to_jsonable(meetings)},
        )

    return templates.TemplateResponse(
        request=request,
        name="tsg_show.html",
        context={
            "active_nav": "tsgs",
            "tsg": tsg,
            "meetings": meetings,
            "pending_jobs": pending_jobs,
        },
    )


__all__ = ["router"]