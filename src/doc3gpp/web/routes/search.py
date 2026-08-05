"""HTTP routes for search.

``GET /search`` runs the FTS5 ``SearchService.search(query, filters)``
read path; ``GET /search/sem`` runs the hybrid FTS5 + vector read via
``SemanticSearchService.search``. The ``?fts5_query=`` opt-in FTS5
path on ``/search/sem`` honours the spec: without it the route is
pure-vector.

Both routes share ``search_results.html``. The semantic variant
swaps the search form partial to surface ``fts5_query``,
``fts5_weight``, and a RRF-aware column layout.

``?format=json`` returns the same payload shape as
``doc3gpp search query --format json`` / ``search sem --format json``:
a bare array of hit objects. FTS5 hits carry ``tdoc_id / score /
previews / title / meeting / tsg / uploaded_date / ftp_url / wis``;
semantic hits carry the RRF fields with the metadata sub-record nested
under ``hit``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from doc3gpp.models.search import SearchFilters, SearchHit
from doc3gpp.models.semantic_search import SemanticSearchHit
from doc3gpp.services.search_service import SearchService
from doc3gpp.services.semantic_search_service import SemanticSearchService
from doc3gpp.web.deps import get_pending_jobs, get_search_service, get_semantic_search_service
from doc3gpp.web.errors import InvalidFilterError, SettingsDisabledError
from doc3gpp.web.filters import is_htmx_request, parse_date_query, parse_int_query, parse_text_query
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
    tdoc_id: str | None,
    limit: int,
) -> SearchFilters:
    """Compose a :class:`SearchFilters` from raw query params.

    ``since`` / ``until`` are validated as date filters first so a
    malformed value surfaces as HTTP 400 (``invalid_filter``) rather
    than being swallowed by the query path.
    """
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


def _fts5_hit_to_json(hit: SearchHit) -> dict[str, Any]:
    """Shape one :class:`SearchHit` exactly like the CLI's JSON renderer.

    Mirrors ``cli.py::_render_search_hits`` (json branch): the same
    key order and the same values, including the raw ``previews``
    mapping with its ``<<...>>`` match markers.
    """
    return {
        "tdoc_id": hit.tdoc_id,
        "score": hit.score,
        "previews": hit.previews,
        "title": hit.title,
        "meeting": hit.meeting,
        "tsg": hit.tsg,
        "uploaded_date": hit.uploaded_date,
        "ftp_url": hit.ftp_url,
        "wis": hit.wis,
    }


def _semantic_hit_to_json(hit: SemanticSearchHit) -> dict[str, Any]:
    """Shape one :class:`SemanticSearchHit` exactly like the CLI's JSON renderer.

    Mirrors ``cli.py::_render_semantic_hits`` (json branch): RRF
    fields at the top level and the ``SearchHit`` metadata bag nested
    under ``hit``.
    """
    return {
        "tdoc_id": hit.tdoc_id,
        "rrf_score": hit.rrf_score,
        "rank_fts5": hit.rank_fts5,
        "rank_vec": hit.rank_vec,
        "min_chunk_distance": hit.min_chunk_distance,
        "best_chunk_id": hit.best_chunk_id,
        "hit": {
            "tdoc_id": hit.hit.tdoc_id,
            "title": hit.hit.title,
            "ftp_url": hit.hit.ftp_url,
            "wis": hit.hit.wis,
        },
    }


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
    sem: str | None = Query(default=None),
    tdoc_id: str | None = Query(default=None, alias="tdoc-id"),
    limit: str | None = Query(default="20"),
    format: str | None = Query(default=None, alias="format"),
    service: SearchService | None = Depends(get_search_service),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render ``search_results.html`` or a JSON list of FTS5 hits."""
    if service is None:
        raise SettingsDisabledError(
            "search is not enabled in settings (set [search].enabled = true)"
        )
    parsed_limit = parse_int_query(limit, min=1, max=_LIMIT_CAP) or 20
    filters = _build_filters(
        tsg=tsg, meeting=meeting, release=release,
        spec=spec, since=since, until=until, tdoc_id=tdoc_id,
        limit=parsed_limit,
    )
    hits: list[SearchHit] = []
    error: str | None = None
    if q:
        # Service exceptions propagate to the generic handler, which
        # emits the 500 envelope with a request_id correlation id.
        hits = service.search(q, filters, sem_query=sem or None)

    if format == "json":
        return JSONResponse(
            content=[_fts5_hit_to_json(h) for h in hits],
        )

    template_name = (
        "partials/search_results.html" if is_htmx_request(request) else "search_results.html"
    )
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "active_nav": "search",
            "mode": "fts5",
            "query": q,
            "hits": hits,
            "total": len(hits),
            "limit": parsed_limit,
            "error": error,
            "pending_jobs": pending_jobs,
            "filters": {
                "tsg": tsg or "",
                "meeting": meeting or "",
                "release": release or "",
                "spec": spec or "",
                "since": since or "",
                "until": until or "",
                "sem": sem or "",
                "tdoc_id": tdoc_id or "",
            },
        },
    )


@router.get("/sem", include_in_schema=False)
async def search_semantic(
    request: Request,
    q: str | None = Query(default=None),
    tsg: str | None = Query(default=None),
    meeting: str | None = Query(default=None),
    release: str | None = Query(default=None),
    spec: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    tdoc_id: str | None = Query(default=None, alias="tdoc-id"),
    fts5_query: str | None = Query(default=None),
    fts5_weight: float | None = Query(default=0.5),
    limit: str | None = Query(default="20"),
    format: str | None = Query(default=None, alias="format"),
    service: SemanticSearchService | None = Depends(get_semantic_search_service),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render ``search_results.html`` or a JSON list of semantic hits."""
    if service is None:
        raise SettingsDisabledError(
            "semantic search is not enabled (set [semantic_search].enabled = true)"
        )
    parsed_limit = parse_int_query(limit, min=1, max=_LIMIT_CAP) or 20
    if fts5_weight is None or not (0.0 <= fts5_weight <= 1.0):
        raise InvalidFilterError(
            f"fts5_weight must be between 0.0 and 1.0, got {fts5_weight!r}"
        )
    # The sem form always submits an ``fts5_query`` field; a blank value
    # arrives as ``""``. The service treats any non-``None`` value as an
    # opt-in FTS5 path, so an empty string would run FTS5 with an empty
    # query and return zero hits. Normalise blank to ``None`` so the
    # default is pure-vector, matching ``doc3gpp search sem``.
    if fts5_query is not None and not fts5_query.strip():
        fts5_query = None

    hits: list[SemanticSearchHit] = []
    error: str | None = None
    if q:
        # Service exceptions propagate to the generic handler, which
        # emits the 500 envelope with a request_id correlation id.
        hits = service.search(
            q,
            fts5_query=fts5_query,
            filters=_build_filters(
                tsg=tsg, meeting=meeting, release=release,
                spec=spec, since=since, until=until, tdoc_id=tdoc_id,
                limit=parsed_limit,
            ),
            limit=parsed_limit,
            fts5_weight=fts5_weight,
        )

    if format == "json":
        return JSONResponse(
            content=[_semantic_hit_to_json(h) for h in hits],
        )

    template_name = (
        "partials/search_results.html" if is_htmx_request(request) else "search_results.html"
    )
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "active_nav": "search",
            "mode": "sem",
            "query": q,
            "fts5_query": fts5_query,
            "fts5_weight": fts5_weight,
            "limit": parsed_limit,
            "hits": hits,
            "error": error,
            "pending_jobs": pending_jobs,
            "filters": {
                "tsg": tsg or "",
                "meeting": meeting or "",
                "release": release or "",
                "spec": spec or "",
                "since": since or "",
                "until": until or "",
                "tdoc_id": tdoc_id or "",
            },
        },
    )


__all__ = ["router"]