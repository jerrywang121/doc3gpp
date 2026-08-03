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
from doc3gpp.web.filters import is_htmx_request, parse_int_query, parse_text_query
from doc3gpp.web.render import wi_rows
from doc3gpp.web.templates_setup import templates


router = APIRouter(prefix="/wis", tags=["wis"])


_LIMIT_CAP = 200

# Mirrors ``settings.output.fields.wi`` — what
# ``doc3gpp wi list --format json`` emits by default.
_WI_DEFAULT_FIELDS = ["wi_id", "acronym", "release", "name"]


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def list_wis(
    request: Request,
    tsg: str | None = Query(default=None),
    name: str | None = Query(default=None),
    acronym: str | None = Query(default=None),
    release: str | None = Query(default=None),
    limit: str | None = Query(default="50"),
    format: str | None = Query(default=None, alias="format"),
    service: WiService = Depends(get_wi_service),
) -> Any:
    """Render ``wi_list.html`` or a JSON list of WIs.

    ``?format=json`` returns the same payload as
    ``doc3gpp wi list --format json``: a bare array of field-selected
    rows (``settings.output.fields.wi`` by default) with every cell
    string-coerced via :func:`doc3gpp.web.render.wi_rows`.

    ``limit`` is declared as ``str`` so an empty form value
    (``limit=``) doesn't trigger a 422 — :func:`parse_int_query`
    treats ``""`` as ``None`` and the route fills in the default. The
    CLI's typed path accepts ints only; the HTTP path is a best-effort
    form-binding layer.
    """
    parsed_limit = parse_int_query(limit, min=1, max=_LIMIT_CAP) or 50
    wis = service.list_recent(
        limit=parsed_limit,
        tsg=parse_text_query(tsg),
        name_like=parse_text_query(name),
        acronym_like=parse_text_query(acronym),
        release_like=parse_text_query(release),
    )

    if format == "json":
        return JSONResponse(content=wi_rows(wis, _WI_DEFAULT_FIELDS))

    template_name = (
        "partials/wi_results.html" if is_htmx_request(request) else "wi_list.html"
    )
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "active_nav": "wis",
            "wis": wis,
            "total": len(wis),
            "limit": parsed_limit,
            "offset": 0,
            "next_offset": None,
            "filters": {
                "tsg": tsg or "",
                "name": name or "",
                "acronym": acronym or "",
                "release": release or "",
                "limit": parsed_limit,
            },
        },
    )


__all__ = ["router"]