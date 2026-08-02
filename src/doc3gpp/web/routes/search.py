"""HTTP routes for search.

``GET /search`` runs the FTS5 ``SearchService.search(query, filters)``
read path; ``GET /search/sem`` runs the hybrid FTS5 + vector read via
``SemanticSearchService.search``. The ``?fts5_query=`` opt-in FTS5
path on ``/search/sem`` honours the spec: without it the route is
pure-vector.

Both routes share ``search_results.html``. The semantic variant
swaps the search form partial to surface ``fts5_query``,
``fts5_weight``, and a RRF-aware column layout.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from doc3gpp.models.search import SearchFilters
from doc3gpp.services.search_service import SearchService
from doc3gpp.services.semantic_search_service import SemanticSearchService
from doc3gpp.web.deps import get_search_service, get_semantic_search_service
from doc3gpp.web.errors import InvalidFilterError, SettingsDisabledError
from doc3gpp.web.filters import parse_int_query, parse_text_query
from doc3gpp.web.render import to_jsonable
from doc3gpp.web.templates_setup import templates


router = APIRouter(prefix="/search", tags=["search"])


_LIMIT_CAP = 200


def _build_filters(
    *,
    tsg: str | None,
    meeting: str | None,
    release: str | None,
    spec: str | None,
    since: str | None,
    until: str | None,
    limit: int,
) -> SearchFilters:
    """Compose a :class:`SearchFilters` from raw query params."""
    return SearchFilters(
        tsg=parse_text_query(tsg),
        meeting=parse_text_query(meeting),
        release=parse_text_query(release),
        spec=parse_text_query(spec),
        since=parse_text_query(since),
        until=parse_text_query(until),
        limit=limit,
    )


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def search_query(
    request: Request,
    q: str | None = Query(default=None),
    tsg: str | None = Query(default=None),
    meeting: str | None = Query(default=None),
    release: str | None = Query(default=None),
    spec: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    limit: int | None = Query(default=20),
    format: str | None = Query(default=None, alias="format"),
    service: SearchService | None = Depends(get_search_service),
) -> Any:
    """Render ``search_results.html`` or a JSON list of FTS5 hits."""
    if service is None:
        raise SettingsDisabledError(
            "search is not enabled in settings (set [search].enabled = true)"
        )
    parsed_limit = parse_int_query(
        str(limit) if limit is not None else None, min=1, max=_LIMIT_CAP,
    ) or 20
    filters = _build_filters(
        tsg=tsg, meeting=meeting, release=release,
        spec=spec, since=since, until=until, limit=parsed_limit,
    )
    hits = []
    error: str | None = None
    if q:
        try:
            hits = service.search(q, filters)
        except Exception as exc:
            error = str(exc)

    if format == "json":
        return JSONResponse(
            content={"query": q, "hits": to_jsonable(hits), "error": error},
        )

    return templates.TemplateResponse(
        request=request,
        name="search_results.html",
        context={
            "active_nav": "search",
            "mode": "fts5",
            "query": q,
            "hits": hits,
            "total": len(hits),
            "limit": parsed_limit,
            "error": error,
            "filters": {
                "tsg": tsg or "",
                "meeting": meeting or "",
                "release": release or "",
                "spec": spec or "",
                "since": since or "",
                "until": until or "",
            },
        },
    )


@router.get("/sem", include_in_schema=False)
async def search_semantic(
    request: Request,
    q: str | None = Query(default=None),
    fts5_query: str | None = Query(default=None),
    fts5_weight: float | None = Query(default=0.5),
    limit: int | None = Query(default=20),
    format: str | None = Query(default=None, alias="format"),
    service: SemanticSearchService | None = Depends(get_semantic_search_service),
) -> Any:
    """Render ``search_results.html`` or a JSON list of semantic hits."""
    if service is None:
        raise SettingsDisabledError(
            "semantic search is not enabled (set [semantic_search].enabled = true)"
        )
    parsed_limit = parse_int_query(
        str(limit) if limit is not None else None, min=1, max=_LIMIT_CAP,
    ) or 20
    if fts5_weight is None or not (0.0 <= fts5_weight <= 1.0):
        raise InvalidFilterError(
            f"fts5_weight must be between 0.0 and 1.0, got {fts5_weight!r}"
        )

    hits = []
    error: str | None = None
    if q:
        try:
            hits = service.search(
                q,
                fts5_query=fts5_query,
                filters=SearchFilters(limit=parsed_limit),
                limit=parsed_limit,
                fts5_weight=fts5_weight,
            )
        except Exception as exc:
            error = str(exc)

    if format == "json":
        return JSONResponse(
            content={"query": q, "fts5_query": fts5_query, "hits": to_jsonable(hits)},
        )

    return templates.TemplateResponse(
        request=request,
        name="search_results.html",
        context={
            "active_nav": "search",
            "mode": "sem",
            "query": q,
            "fts5_query": fts5_query,
            "fts5_weight": fts5_weight,
            "limit": parsed_limit,
            "hits": hits,
            "error": error,
        },
    )


__all__ = ["router"]