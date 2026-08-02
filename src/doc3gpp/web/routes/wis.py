"""HTTP routes for the WI (Work Item) list page.

WIs do not currently have a detail page — the spec table is the
canonical view. ``GET /wis`` returns the filterable list page.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from doc3gpp.services.wi_service import WiService
from doc3gpp.web.deps import get_wi_service
from doc3gpp.web.filters import parse_int_query, parse_text_query
from doc3gpp.web.render import to_jsonable
from doc3gpp.web.templates_setup import templates


router = APIRouter(prefix="/wis", tags=["wis"])


_LIMIT_CAP = 200


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def list_wis(
    request: Request,
    tsg: str | None = Query(default=None),
    name: str | None = Query(default=None),
    id: str | None = Query(default=None),
    limit: int | None = Query(default=50),
    format: str | None = Query(default=None, alias="format"),
    service: WiService = Depends(get_wi_service),
) -> Any:
    """Render ``wi_list.html`` or a JSON list of WIs."""
    parsed_limit = parse_int_query(
        str(limit) if limit is not None else None, min=1, max=_LIMIT_CAP,
    ) or 50
    wis = service.list_recent(
        limit=parsed_limit,
        tsg=parse_text_query(tsg),
        name_like=parse_text_query(name),
        acronym_like=parse_text_query(id),
    )

    if format == "json":
        return JSONResponse(content={"wis": to_jsonable(wis)})

    return templates.TemplateResponse(
        request=request,
        name="wi_list.html",
        context={
            "active_nav": "wis",
            "wis": wis,
            "total": len(wis),
            "offset": 0,
            "next_offset": None,
            "filters": {
                "tsg": tsg or "",
                "name": name or "",
                "id": id or "",
                "limit": parsed_limit,
            },
        },
    )


__all__ = ["router"]