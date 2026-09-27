"""Unified TDoc search and index-status routes."""
# FastAPI dependency calls in route signatures are intentional.
# ruff: noqa: B008
from __future__ import annotations

import html
import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from doc3gpp.models.index import IndexStatus
from doc3gpp.models.search import (
    SearchFilters,
    SearchIndexCorruptError,
    SearchUnavailableError,
)
from doc3gpp.models.semantic_search import (
    EmbedderUnavailableError,
    SemanticSearchUnavailableError,
    VectorIndexUnavailableError,
)
from doc3gpp.models.unified_search import (
    SearchMode,
    TDocSearchResult,
    tdoc_search_result_to_dict,
)
from doc3gpp.services.tdoc_index_service import TDocIndexService
from doc3gpp.services.tdoc_search_facade import TDocSearchFacade
from doc3gpp.web.deps import (
    get_pending_jobs,
    get_tdoc_index_service,
    get_tdoc_search_facade,
)
from doc3gpp.web.errors import SearchIndexCorruptWebError, SettingsDisabledError
from doc3gpp.web.filters import (
    is_htmx_request,
    parse_date_query,
    parse_int_query,
    parse_text_query,
)
from doc3gpp.web.templates_setup import templates

router = APIRouter(tags=["search"])

_LIMIT_CAP = 200


def _build_filters(
    *,
    tsg: str | None,
    meeting: str | None,
    release: str | None,
    spec: str | None,
    since: str | None,
    until: str | None,
    tdoc_id: str | None,
    limit: int,
) -> SearchFilters:
    return SearchFilters(
        tsg=parse_text_query(tsg),
        meeting=parse_text_query(meeting),
        release=parse_text_query(release),
        spec=parse_text_query(spec),
        since=parse_date_query(since),
        until=parse_date_query(until),
        tdoc_id=parse_text_query(tdoc_id),
        limit=limit,
    )


def _resolved_mode(
    results: list[TDocSearchResult],
    text: str | None,
    semantic: str | None,
) -> SearchMode:
    if results:
        return results[0].search_mode
    has_text = bool(text and text.strip())
    has_semantic = bool(semantic and semantic.strip())
    if has_text and has_semantic:
        return SearchMode.HYBRID
    if has_text:
        return SearchMode.FTS5
    if has_semantic:
        return SearchMode.SEMANTIC
    return SearchMode.FILTER


def _tdoc_context(
    *,
    text: str | None,
    semantic: str | None,
    results: list[TDocSearchResult],
    filters: dict[str, str],
    limit: int,
    error: str | None,
    pending_jobs: int,
) -> dict[str, Any]:
    mode = _resolved_mode(results, text, semantic)
    return {
        "active_nav": "search",
        "search_resource": "tdoc",
        "search_mode": mode.value,
        "text": text or "",
        "semantic": semantic or "",
        "query": text or "",
        "results": results,
        "hits": results,
        "total": len(results),
        "limit": limit,
        "offset": 0,
        "error": error,
        "pending_jobs": pending_jobs,
        "filters": filters,
    }


@router.get("/tdocs/search", include_in_schema=False)
@router.get("/tdocs/search/", include_in_schema=False)
async def search_query(
    request: Request,
    text: str | None = Query(default=None),
    semantic: str | None = Query(default=None),
    tsg: str | None = Query(default=None),
    meeting: str | None = Query(default=None),
    release: str | None = Query(default=None),
    spec: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    tdoc_id: str | None = Query(default=None, alias="tdoc-id"),
    limit: str | None = Query(default="20"),
    format: str | None = Query(default=None, alias="format"),
    facade: TDocSearchFacade | None = Depends(get_tdoc_search_facade),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    if facade is None:
        raise SettingsDisabledError("search is not available in this build")
    parsed_limit = parse_int_query(limit, min=1, max=_LIMIT_CAP) or 20
    parsed_filters = _build_filters(
        tsg=tsg,
        meeting=meeting,
        release=release,
        spec=spec,
        since=since,
        until=until,
        tdoc_id=tdoc_id,
        limit=parsed_limit,
    )
    try:
        results = facade.search(text=text, semantic=semantic, filters=parsed_filters)
    except SearchIndexCorruptError as exc:
        raise SearchIndexCorruptWebError(str(exc), resource="tdoc") from exc
    except (
        SearchUnavailableError,
        SemanticSearchUnavailableError,
        EmbedderUnavailableError,
        VectorIndexUnavailableError,
    ) as exc:
        raise SettingsDisabledError(str(exc)) from exc
    if format == "json":
        return JSONResponse(content=[tdoc_search_result_to_dict(result) for result in results])

    context = _tdoc_context(
        text=text,
        semantic=semantic,
        results=results,
        filters={
            "tsg": tsg or "",
            "meeting": meeting or "",
            "release": release or "",
            "spec": spec or "",
            "since": since or "",
            "until": until or "",
            "tdoc_id": tdoc_id or "",
        },
        limit=parsed_limit,
        error=None,
        pending_jobs=pending_jobs,
    )
    template_name = (
        "partials/search_results.html"
        if is_htmx_request(request)
        else "search_results.html"
    )
    return templates.TemplateResponse(request=request, name=template_name, context=context)


def _index_status_html(
    resource: str,
    status: IndexStatus,
    *,
    htmx: bool,
) -> HTMLResponse:
    payload = json.dumps(status.to_dict(), ensure_ascii=False, indent=2)
    title = html.escape(resource)
    if htmx:
        body = f'<div id="index-status"><h2>{title} index</h2><pre>{html.escape(payload)}</pre></div>'
    else:
        body = (
            "<!doctype html><html><head><title>doc3gpp index status</title></head>"
            f"<body><main><h1>{title} index</h1><pre>{html.escape(payload)}</pre></main></body></html>"
        )
    return HTMLResponse(body)


async def _get_index_status(
    request: Request,
    service: TDocIndexService | None,
    format: str | None,
) -> Any:
    if service is None:
        raise SettingsDisabledError("index maintenance is not available in this build")
    status = service.status()
    if format == "json":
        return JSONResponse(content=status.to_dict())
    return _index_status_html("TDoc", status, htmx=is_htmx_request(request))


@router.get("/tdocs/index", include_in_schema=False)
async def tdoc_index_status(
    request: Request,
    format: str | None = Query(default=None, alias="format"),
    service: TDocIndexService | None = Depends(get_tdoc_index_service),
) -> Any:
    return await _get_index_status(request, service, format)


__all__ = ["router"]
