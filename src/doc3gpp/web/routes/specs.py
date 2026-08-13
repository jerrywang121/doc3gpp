"""HTTP routes for the spec list + detail pages."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from doc3gpp.services.spec_service import SpecService
from doc3gpp.web.deps import get_pending_jobs, get_spec_service
from doc3gpp.web.errors import SpecNotFoundError
from doc3gpp.web.filters import is_htmx_request, parse_bool_query, parse_int_query, parse_text_query
from doc3gpp.web.render import spec_rows, spec_version_rows
from doc3gpp.web.templates_setup import templates


router = APIRouter(prefix="/specs", tags=["specs"])


_LIMIT_CAP = 200

# Mirrors ``settings.output.fields.spec`` — what
# ``doc3gpp spec list --format json`` emits by default.
_SPEC_DEFAULT_FIELDS = ["spec_id", "type", "title", "status", "radio_tech", "initial_release", "tsg", "rapporteurs"]
# Spec-show header fields: keeps ``wis`` by default (dropped only under
# ``no_wis_crs``), matching ``doc3gpp spec show``. Distinct from the
# ``spec list`` default fields which omit ``wis``.
_SPEC_SHOW_FIELDS = ["spec_id", "type", "title", "status", "radio_tech", "initial_release", "tsg", "wis", "rapporteurs"]
_VERSION_FIELDS = ["version", "release", "ftp_url", "meeting_id", "meeting_name", "upload_date", "pdf_url", "crs"]


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def list_specs(
    request: Request,
    tsg: str | None = Query(default=None),
    type: str | None = Query(default=None),
    spec_id: str | None = Query(default=None),
    title: str | None = Query(default=None),
    status: str | None = Query(default=None),
    radio_tech: str | None = Query(default=None),
    initial_release: str | None = Query(default=None),
    wis: str | None = Query(default=None),
    rapporteurs: str | None = Query(default=None),
    limit: str | None = Query(default="50"),
    offset: str | None = Query(default="0"),
    format: str | None = Query(default=None, alias="format"),
    service: SpecService = Depends(get_spec_service),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render ``spec_list.html`` or a JSON list of specs.

    ``?format=json`` returns the same payload as
    ``doc3gpp spec list --format json``: a bare array of field-selected
    rows (``settings.output.fields.spec`` by default) with every cell
    string-coerced via :func:`doc3gpp.web.render.spec_rows`.

    The numeric query params (``limit``, ``offset``) are declared as
    ``str`` so an empty form value (``limit=``) doesn't trigger a 422 —
    :func:`parse_int_query` treats ``""`` as ``None`` and the route
    fills in the default. The CLI's typed path accepts ints only; the
    HTTP path is a best-effort form-binding layer.
    """
    parsed_limit = parse_int_query(limit, min=1, max=_LIMIT_CAP) or 50
    parsed_offset = parse_int_query(offset, min=0) or 0
    specs = service.list_recent(
        limit=parsed_limit,
        offset=parsed_offset,
        tsg=parse_text_query(tsg),
        type=parse_text_query(type),
        spec_id=parse_text_query(spec_id),
        title=parse_text_query(title),
        status=parse_text_query(status),
        radio_tech=parse_text_query(radio_tech),
        initial_release=parse_text_query(initial_release),
        wis=parse_text_query(wis),
        rapporteurs=parse_text_query(rapporteurs),
    )

    if format == "json":
        return JSONResponse(content=spec_rows(specs, _SPEC_DEFAULT_FIELDS))

    next_offset = (
        parsed_offset + len(specs) if len(specs) == parsed_limit else None
    )
    template_name = (
        "partials/spec_results.html" if is_htmx_request(request) else "spec_list.html"
    )
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "active_nav": "specs",
            "specs": specs,
            "total": len(specs),
            "limit": parsed_limit,
            "offset": parsed_offset,
            "next_offset": next_offset,
            "pending_jobs": pending_jobs,
            "filters": {
                "tsg": tsg or "",
                "type": type or "",
                "spec_id": spec_id or "",
                "title": title or "",
                "status": status or "",
                "radio_tech": radio_tech or "",
                "initial_release": initial_release or "",
                "wis": wis or "",
                "rapporteurs": rapporteurs or "",
                "limit": parsed_limit,
            },
        },
    )


@router.get("/{spec_id}", include_in_schema=False)
async def show_spec(
    request: Request,
    spec_id: str,
    format: str | None = Query(default=None, alias="format"),
    limit: str | None = Query(default="10"),
    offset: str | None = Query(default="0"),
    version: str | None = Query(default=None),
    no_wis_crs: str | None = Query(default=None),
    service: SpecService = Depends(get_spec_service),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render ``spec_show.html`` or a JSON payload with the spec + versions."""
    parsed_limit = parse_int_query(limit, min=1, max=_LIMIT_CAP) or 10
    parsed_offset = parse_int_query(offset, min=0) or 0
    spec = service.get(spec_id)
    if spec is None:
        raise SpecNotFoundError(f"Spec {spec_id!r} not found")
    versions = service.list_versions(
        spec_id, limit=parsed_limit, offset=parsed_offset,
        version=parse_text_query(version),
    )

    slim = parse_bool_query(no_wis_crs) is True
    header_fields = [f for f in _SPEC_SHOW_FIELDS if not (slim and f == "wis")]
    version_fields = [f for f in _VERSION_FIELDS if not (slim and f == "crs")]

    if format == "json":
        return JSONResponse(
            content={
                "spec": {f: getattr(spec, f, None) for f in header_fields},
                "versions": spec_version_rows(versions, version_fields),
            },
        )

    next_offset = parsed_offset + len(versions) if len(versions) == parsed_limit else None
    return templates.TemplateResponse(
        request=request,
        name="spec_show.html",
        context={
            "active_nav": "specs",
            "spec": spec,
            "versions": versions,
            "limit": parsed_limit,
            "offset": parsed_offset,
            "next_offset": next_offset,
            "total": len(versions),
            "version": version or "",
            "no_wis_crs": slim,
            "pending_jobs": pending_jobs,
        },
    )


__all__ = ["router"]
