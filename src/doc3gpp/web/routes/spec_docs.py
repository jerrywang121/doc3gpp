"""HTTP routes for the spec-document corpus (read page + TOC + search + schema).

``GET /specs/{spec_id}/docs`` renders one version's stored source, TOC,
and chunks; ``GET /specs/{spec_id}/docs/toc`` renders the stored TOC for one
``(spec_id, version)`` pair; ``GET /spec-docs/search`` dispatches through
the unified search facade. ``GET /spec-docs/schema`` is a
static registry read (no DB access).

``?format=json`` returns the same payload shape as
``doc3gpp spec doc toc show --format json`` /
``doc3gpp spec doc search --format json``: the TOC envelope
(``spec_id / version / release / docx_count / entries / files``) and
bare arrays of chunk / semantic hit objects. Domain errors
(``SpecDocUnknownSpecError`` / ``SpecDocUnknownVersionError``) map to
HTTP 404 via :mod:`doc3gpp.web.errors`; oversize / no-docx failures
map to 422 with the handler message.
"""
# FastAPI dependency calls in route signatures are intentional.
# ruff: noqa: B008
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from doc3gpp.models.index import IndexStatus
from doc3gpp.models.schema_info import RESOURCE_SCHEMAS, schema_payload
from doc3gpp.models.search import SearchIndexCorruptError, SearchUnavailableError
from doc3gpp.models.semantic_search import (
    EmbedderUnavailableError,
    SemanticSearchUnavailableError,
    VectorIndexUnavailableError,
)
from doc3gpp.models.spec import Spec, SpecVersion
from doc3gpp.models.spec_doc import (
    SpecDocSearchFilters,
    SpecDocUnknownVersionError,
)
from doc3gpp.models.unified_search import spec_doc_search_result_to_dict
from doc3gpp.services.spec_doc_index_service import SpecDocIndexService
from doc3gpp.services.spec_doc_search_facade import SpecDocSearchFacade
from doc3gpp.services.spec_doc_service import SpecDocService
from doc3gpp.services.spec_service import SpecService
from doc3gpp.web.deps import (
    get_pending_jobs,
    get_spec_doc_index_service,
    get_spec_doc_search_facade,
    get_spec_doc_service,
    get_spec_service,
)
from doc3gpp.web.errors import (
    InvalidFilterError,
    SearchIndexCorruptWebError,
    SettingsDisabledError,
    SpecNotFoundError,
)
from doc3gpp.web.filters import is_htmx_request, parse_int_query, parse_text_query
from doc3gpp.web.render import spec_doc_toc_to_json
from doc3gpp.web.templates_setup import templates

router = APIRouter(tags=["spec-docs"])


_LIMIT_CAP = 200
_DOC_CHUNK_LIMIT_DEFAULT = 20
_DOC_CHUNK_LIMIT_CAP = 100


def _spec_doc_page_items(current_page: int, total_pages: int) -> list[int | None]:
    if total_pages <= 0:
        return []
    if total_pages <= 10:
        return list(range(1, total_pages + 1))

    if current_page <= 6:
        first_page = 1
    elif current_page >= total_pages - 4:
        first_page = total_pages - 9
    else:
        first_page = current_page - 4

    last_page = first_page + 9
    items: list[int | None] = []
    if first_page > 1:
        items.append(None)
    items.extend(range(first_page, last_page + 1))
    if last_page < total_pages:
        items.append(None)
    return items

# Mirrors ``settings.output.fields.spec_doc_toc`` — what
# ``doc3gpp spec doc toc show --format json`` projects its ``entries``
# through by default.
_SPEC_DOC_TOC_FIELDS = ["section_no", "title", "level", "source_file"]


@router.get("/specs/{spec_id}/docs", include_in_schema=False)
async def spec_doc_show(
    request: Request,
    spec_id: str,
    version: str | None = Query(default=None),
    release: str | None = Query(default=None),
    sections: str | None = Query(default=None),
    tables: str | None = Query(default=None),
    limit: str | None = Query(default="20"),
    offset: str | None = Query(default="0"),
    spec_service: SpecService = Depends(get_spec_service),
    doc_service: SpecDocService | None = Depends(get_spec_doc_service),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render one stored spec-document version and its readable chunks."""
    spec: Spec | None = spec_service.get(spec_id)
    if spec is None:
        raise SpecNotFoundError(f"Spec {spec_id!r} not found")
    if not version:
        raise InvalidFilterError("version query param is required")

    versions: list[SpecVersion] = spec_service.list_versions(
        spec_id, limit=500, version=version
    )
    version_row = next((row for row in versions if row.version == version), None)
    if version_row is None:
        available_rows = spec_service.list_versions(spec_id, limit=500)
        available = [row.version for row in available_rows]
        available_text = ", ".join(available) if available else "none"
        raise SpecDocUnknownVersionError(
            f"Unknown version {version!r} for spec {spec_id!r}; "
            f"available versions: {available_text}",
            available=available,
        )

    parsed_limit = parse_int_query(
        limit, min=1, max=_DOC_CHUNK_LIMIT_CAP
    ) or _DOC_CHUNK_LIMIT_DEFAULT
    parsed_offset = parse_int_query(offset, min=0) or 0
    parsed_release = parse_text_query(release)
    parsed_sections = parse_text_query(sections)
    parsed_tables = parse_text_query(tables)

    if doc_service is None:
        raise SettingsDisabledError("spec-doc parse is not available in this build")

    source = doc_service.get_source(spec_id, version)
    toc = None
    total_chunks = 0
    total_pages = 0
    effective_offset = 0
    chunks: list[Any] = []
    if source is not None and source.parsed_at is not None:
        try:
            toc = doc_service.get_toc(spec_id, version, release=parsed_release)
        except SpecDocUnknownVersionError:
            # A parsed source without a TOC is still useful: keep its chunks visible.
            toc = None
        total_chunks = doc_service.count_chunks(
            spec_id,
            version=version,
            release=parsed_release,
            sections=parsed_sections,
            tables=parsed_tables,
        )
        total_pages = (total_chunks + parsed_limit - 1) // parsed_limit
        if total_pages:
            last_page_offset = (total_pages - 1) * parsed_limit
            effective_offset = (
                min(parsed_offset, last_page_offset) // parsed_limit
            ) * parsed_limit
            chunks = doc_service.list_chunks(
                spec_id,
                version=version,
                release=parsed_release,
                sections=parsed_sections,
                tables=parsed_tables,
                limit=parsed_limit + 1,
                offset=effective_offset,
            )

    display_chunks = chunks[:parsed_limit]
    current_page = effective_offset // parsed_limit + 1 if total_pages else 0
    page_items = _spec_doc_page_items(current_page, total_pages)
    first_offset = 0 if current_page > 1 else None
    previous_offset = effective_offset - parsed_limit if current_page > 1 else None
    next_offset = (
        effective_offset + parsed_limit
        if len(chunks) > parsed_limit and current_page < total_pages
        else None
    )
    last_offset = (
        (total_pages - 1) * parsed_limit if current_page < total_pages else None
    )
    context = {
        "active_nav": "specs",
        "spec": spec,
        "version_row": version_row,
        "source": source,
        "toc": toc,
        "chunks": chunks,
        "display_chunks": display_chunks,
        "total_chunks": total_chunks,
        "current_page": current_page,
        "total_pages": total_pages,
        "page_items": page_items,
        "first_offset": first_offset,
        "previous_offset": previous_offset,
        "limit": parsed_limit,
        "offset": effective_offset,
        "next_offset": next_offset,
        "last_offset": last_offset,
        "sections": parsed_sections or "",
        "tables": parsed_tables or "",
        "release": parsed_release or "",
        "pending_jobs": pending_jobs,
    }
    template_name = (
        "partials/spec_doc_show_results.html"
        if is_htmx_request(request)
        else "spec_doc_show.html"
    )
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context=context,
    )


@router.get("/specs/{spec_id}/docs/toc", include_in_schema=False)
async def spec_doc_toc(
    request: Request,
    spec_id: str,
    version: str | None = Query(default=None),
    release: str | None = Query(default=None),
    format: str | None = Query(default=None, alias="format"),
    service: SpecDocService | None = Depends(get_spec_doc_service),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render the stored TOC for ``(spec_id, version)`` or its JSON envelope.

    ``?format=json`` returns the same payload as
    ``doc3gpp spec doc toc show --format json``: ``{"spec_id",
    "version", "release", "docx_count", "entries": [...],
    "files": [...]}`` with entries projected through the default TOC
    fields. A missing ``version`` query param raises 400; an
    unparsed / unknown pair raises 404 via
    ``SpecDocUnknownVersionError``.
    """
    if service is None:
        raise SettingsDisabledError(
            "spec-doc parse is not available in this build"
        )
    if not version:
        raise InvalidFilterError("version query param is required")
    toc = service.get_toc(spec_id, version, release=release)

    if format == "json":
        return JSONResponse(
            content=spec_doc_toc_to_json(toc, list(_SPEC_DOC_TOC_FIELDS))
        )

    if is_htmx_request(request):
        return templates.TemplateResponse(
            request=request,
            name="partials/spec_doc_toc_results.html",
            context={
                "toc": toc,
                "entries": toc.entries,
                "files": toc.files,
                "total": len(toc.entries),
                "spec_id": spec_id,
                "version": version,
            },
        )
    return templates.TemplateResponse(
        request=request,
        name="spec_doc_toc.html",
        context={
            "active_nav": "specs",
            "toc": toc,
            "entries": toc.entries,
            "files": toc.files,
            "total": len(toc.entries),
            "spec_id": spec_id,
            "version": version,
            "release": release or "",
            "pending_jobs": pending_jobs,
        },
    )


def _build_spec_doc_filters(
    *,
    spec: str | None,
    release: str | None,
    version: str | None,
    sections: str | None,
    tables: str | None,
    limit: int,
    offset: int,
) -> SpecDocSearchFilters:
    """Compose a :class:`SpecDocSearchFilters` from raw query params."""
    return SpecDocSearchFilters(
        spec_id=parse_text_query(spec),
        release=parse_text_query(release),
        version=parse_text_query(version),
        sections=parse_text_query(sections),
        tables=parse_text_query(tables),
        limit=limit,
        offset=offset,
    )


@router.get("/spec-docs/search", include_in_schema=False)
async def spec_doc_search_query(
    request: Request,
    text: str | None = Query(default=None),
    semantic: str | None = Query(default=None),
    spec: str | None = Query(default=None),
    release: str | None = Query(default=None),
    version: str | None = Query(default=None),
    sections: str | None = Query(default=None),
    tables: str | None = Query(default=None),
    limit: str | None = Query(default="20"),
    offset: str | None = Query(default="0"),
    format: str | None = Query(default=None, alias="format"),
    facade: SpecDocSearchFacade | None = Depends(get_spec_doc_search_facade),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    if facade is None:
        raise SettingsDisabledError("search is not available in this build")
    parsed_limit = parse_int_query(limit, min=1, max=_LIMIT_CAP) or 20
    parsed_offset = parse_int_query(offset, min=0) or 0
    filters = _build_spec_doc_filters(
        spec=spec,
        release=release,
        version=version,
        sections=sections,
        tables=tables,
        limit=parsed_limit,
        offset=parsed_offset,
    )
    error: str | None = None
    try:
        results = facade.search(text=text, semantic=semantic, filters=filters)
    except SearchIndexCorruptError as exc:
        if format == "json":
            raise SearchIndexCorruptWebError(str(exc), resource="spec_doc") from exc
        error = (
            "search index corrupt; "
            f"run `doc3gpp spec doc index --rebuild`: {exc}"
        )
        results = []
    except (
        SearchUnavailableError,
        SemanticSearchUnavailableError,
        EmbedderUnavailableError,
        VectorIndexUnavailableError,
    ) as exc:
        raise SettingsDisabledError(str(exc)) from exc
    if format == "json":
        return JSONResponse(
            content=[spec_doc_search_result_to_dict(result) for result in results]
        )

    if results:
        mode = results[0].search_mode.value
    elif text and text.strip() and semantic and semantic.strip():
        mode = "hybrid"
    elif text and text.strip():
        mode = "fts5"
    elif semantic and semantic.strip():
        mode = "semantic"
    else:
        mode = "filter"
    shared = {
        "mode": mode,
        "search_mode": mode,
        "search_resource": "spec_doc",
        "text": text or "",
        "semantic": semantic or "",
        "query": text or "",
        "results": results,
        "hits": results,
        "total": len(results),
        "limit": parsed_limit,
        "offset": parsed_offset,
        "error": error,
        "filters": {
            "spec": spec or "",
            "release": release or "",
            "version": version or "",
            "sections": sections or "",
            "tables": tables or "",
        },
    }
    if is_htmx_request(request):
        return templates.TemplateResponse(
            request=request,
            name="partials/spec_doc_search_results.html",
            context={**shared, "pending_jobs": pending_jobs},
        )
    return templates.TemplateResponse(
        request=request,
        name="spec_docs_search.html",
        context={**shared, "active_nav": "search", "pending_jobs": pending_jobs},
    )


@router.get("/spec-docs/index", include_in_schema=False)
async def spec_doc_index_status(
    request: Request,
    format: str | None = Query(default=None, alias="format"),
    service: SpecDocIndexService | None = Depends(get_spec_doc_index_service),
) -> Any:
    if service is None:
        raise SettingsDisabledError("index maintenance is not available in this build")
    status: IndexStatus = service.status()
    if format == "json":
        return JSONResponse(content=status.to_dict())
    payload = json.dumps(status.to_dict(), ensure_ascii=False, indent=2)
    if is_htmx_request(request):
        escaped_payload = payload.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return HTMLResponse(
            f'<div id="index-status"><h2>Spec-document index</h2><pre>{escaped_payload}</pre></div>'
        )
    escaped_payload = payload.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return HTMLResponse(
        "<!doctype html><html><head><title>doc3gpp index status</title></head>"
        f"<body><main><h1>Spec-document index</h1><pre>{escaped_payload}</pre></main></body></html>"
    )


@router.get("/spec-docs/schema", include_in_schema=False)
async def spec_doc_schema(
    request: Request,
    format: str | None = Query(default=None, alias="format"),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render ``schema.html`` or the CLI-identical JSON field descriptors.

    ``?format=json`` returns the same payload as
    ``doc3gpp spec doc schema --format json``: a bare array of
    ``{table, field, type, nullable, description, values}`` rows from
    :func:`doc3gpp.models.schema_info.schema_payload`. No DB access.
    """
    if format == "json":
        return JSONResponse(content=schema_payload("spec_doc"))
    tables = RESOURCE_SCHEMAS["spec_doc"]
    template_name = (
        "partials/schema_results.html" if is_htmx_request(request) else "schema.html"
    )
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "active_nav": "search",
            "resource": "spec_doc",
            "tables": tables,
            "total": sum(len(t.fields) for t in tables),
            "pending_jobs": pending_jobs,
        },
    )


__all__ = ["router"]
