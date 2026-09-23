"""HTTP routes for the spec-document corpus (TOC + search + schema).

``GET /specs/{spec_id}/docs/toc`` renders the stored TOC for one
``(spec_id, version)`` pair; ``GET /spec-docs/search`` runs the FTS5
``SpecDocSearchService.search(query, filters)`` read path and
``GET /spec-docs/search/sem`` runs the hybrid FTS5 + vector read via
``SpecDocSemanticService.search``. ``GET /spec-docs/schema`` is a
static registry read (no DB access).

``?format=json`` returns the same payload shape as
``doc3gpp spec doc toc show --format json`` /
``doc3gpp spec doc search query --format json`` /
``doc3gpp spec doc search sem --format json``: the TOC envelope
(``spec_id / version / release / docx_count / entries / files``) and
bare arrays of chunk / semantic hit objects. Domain errors
(``SpecDocUnknownSpecError`` / ``SpecDocUnknownVersionError``) map to
HTTP 404 via :mod:`doc3gpp.web.errors`; oversize / no-docx failures
map to 422 with the handler message.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from doc3gpp.models.schema_info import RESOURCE_SCHEMAS, schema_payload
from doc3gpp.models.search import SearchIndexCorruptError, SearchQueryError
from doc3gpp.models.semantic_search import (
    EmbedderUnavailableError,
    SemanticSearchQueryError,
    SemanticSearchUnavailableError,
    VectorIndexUnavailableError,
)
from doc3gpp.models.spec_doc import (
    SpecDocSearchFilters,
    SpecDocTooLargeError,
)
from doc3gpp.services.spec_doc_search_service import SpecDocSearchService
from doc3gpp.services.spec_doc_semantic_service import SpecDocSemanticService
from doc3gpp.services.spec_doc_service import SpecDocService
from doc3gpp.web.deps import (
    get_pending_jobs,
    get_spec_doc_search_service,
    get_spec_doc_semantic_service,
    get_spec_doc_service,
)
from doc3gpp.web.errors import InvalidFilterError, SettingsDisabledError
from doc3gpp.web.filters import is_htmx_request, parse_int_query, parse_text_query
from doc3gpp.web.render import (
    spec_doc_hit_to_json,
    spec_doc_semantic_hit_to_json,
    spec_doc_toc_to_json,
)
from doc3gpp.web.templates_setup import templates


router = APIRouter(tags=["spec-docs"])


_LIMIT_CAP = 200

# Mirrors ``settings.output.fields.spec_doc_toc`` — what
# ``doc3gpp spec doc toc show --format json`` projects its ``entries``
# through by default.
_SPEC_DOC_TOC_FIELDS = ["section_no", "title", "level", "source_file"]


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
    section: str | None,
    limit: int,
    offset: int,
) -> SpecDocSearchFilters:
    """Compose a :class:`SpecDocSearchFilters` from raw query params."""
    return SpecDocSearchFilters(
        spec_id=parse_text_query(spec),
        release=parse_text_query(release),
        version=parse_text_query(version),
        section=parse_text_query(section),
        limit=limit,
        offset=offset,
    )


@router.get("/spec-docs/search", include_in_schema=False)
async def spec_doc_search_query(
    request: Request,
    q: str | None = Query(default=None),
    spec: str | None = Query(default=None),
    release: str | None = Query(default=None),
    version: str | None = Query(default=None),
    section: str | None = Query(default=None),
    limit: str | None = Query(default="20"),
    offset: str | None = Query(default="0"),
    format: str | None = Query(default=None, alias="format"),
    service: SpecDocSearchService | None = Depends(get_spec_doc_search_service),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render ``spec_docs_search.html`` or a JSON list of spec-doc FTS5 hits.

    ``?format=json`` returns the same payload as
    ``doc3gpp spec doc search query --format json``: a bare array of
    hit dicts via :func:`doc3gpp.web.render.spec_doc_hit_to_json`.
    A disabled search service raises 503; ``SearchQueryError`` maps to
    400 via the error catalogue and corruption surfaces as 500 with a
    rebuild hint (mirroring the CLI's exit-code-3 path).
    """
    if service is None:
        raise SettingsDisabledError("search is not available in this build")
    parsed_limit = parse_int_query(limit, min=1, max=_LIMIT_CAP) or 20
    parsed_offset = parse_int_query(offset, min=0) or 0
    filters = _build_spec_doc_filters(
        spec=spec, release=release, version=version, section=section,
        limit=parsed_limit, offset=parsed_offset,
    )
    hits: list[Any] = []
    error: str | None = None
    if q:
        try:
            hits = service.search(q, filters)
        except SearchIndexCorruptError as exc:
            error = (
                f"search index corrupt; "
                f"run `doc3gpp spec doc search index --rebuild`: {exc}"
            )
            if format == "json":
                raise
            hits = []

    if format == "json":
        return JSONResponse(content=[spec_doc_hit_to_json(h) for h in hits])

    shared = {
        "mode": "fts5",
        "query": q,
        "hits": hits,
        "total": len(hits),
        "limit": parsed_limit,
        "offset": parsed_offset,
        "error": error,
        "filters": {
            "spec": spec or "",
            "release": release or "",
            "version": version or "",
            "section": section or "",
        },
    }
    if is_htmx_request(request):
        return templates.TemplateResponse(
            request=request,
            name="partials/spec_doc_search_results.html",
            context=dict(shared),
        )
    return templates.TemplateResponse(
        request=request,
        name="spec_docs_search.html",
        context={
            **shared,
            "active_nav": "search",
            "pending_jobs": pending_jobs,
        },
    )


@router.get("/spec-docs/search/sem", include_in_schema=False)
async def spec_doc_search_semantic(
    request: Request,
    q: str | None = Query(default=None),
    fts5_query: str | None = Query(default=None),
    fts5_weight: float | None = Query(default=0.5),
    spec: str | None = Query(default=None),
    release: str | None = Query(default=None),
    version: str | None = Query(default=None),
    section: str | None = Query(default=None),
    limit: str | None = Query(default="20"),
    offset: str | None = Query(default=None),
    format: str | None = Query(default=None, alias="format"),
    service: SpecDocSemanticService | None = Depends(
        get_spec_doc_semantic_service
    ),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render ``spec_docs_search.html`` or a JSON list of semantic hits.

    ``?format=json`` returns the same payload as
    ``doc3gpp spec doc search sem --format json``: a bare array of
    ``{chunk_id, rrf_score, rank_fts5, rank_vec, min_chunk_distance,
    hit}`` dicts. Without ``fts5_query`` only vector KNN runs
    (``hit`` is ``None`` for vector-only chunks); with it the FTS5 +
    vector sides fuse via chunk-level RRF. An unavailable service
    raises 503; a bad ``fts5_weight`` raises 400.
    """
    if service is None:
        raise SettingsDisabledError(
            "semantic search is not available in this build"
        )
    parsed_limit = parse_int_query(limit, min=1, max=_LIMIT_CAP) or 20
    parsed_offset = parse_int_query(offset, min=0) or 0
    if fts5_weight is None or not (0.0 <= fts5_weight <= 1.0):
        raise InvalidFilterError(
            f"fts5_weight must be between 0.0 and 1.0, got {fts5_weight!r}"
        )
    # The sem form always submits an ``fts5_query`` field; a blank value
    # arrives as ``""``. The service treats any non-``None`` value as an
    # opt-in FTS5 path, so an empty string would run FTS5 with an empty
    # query and return zero hits. Normalise blank to ``None`` so the
    # default is pure-vector, matching ``doc3gpp spec doc search sem``.
    if fts5_query is not None and not fts5_query.strip():
        fts5_query = None

    hits: list[Any] = []
    error: str | None = None
    if q:
        try:
            hits = service.search(
                q,
                fts5_query=fts5_query,
                filters=_build_spec_doc_filters(
                    spec=spec, release=release, version=version, section=section,
                    limit=parsed_limit, offset=parsed_offset,
                ),
                limit=parsed_limit,
                fts5_weight=fts5_weight,
            )
        except (SemanticSearchQueryError, SearchQueryError) as exc:
            raise InvalidFilterError(f"bad query: {exc}") from exc
        except (
            EmbedderUnavailableError,
            VectorIndexUnavailableError,
            SemanticSearchUnavailableError,
        ) as exc:
            raise SettingsDisabledError(str(exc)) from exc
        except SpecDocTooLargeError as exc:
            raise InvalidFilterError(str(exc)) from exc

    if format == "json":
        return JSONResponse(
            content=[spec_doc_semantic_hit_to_json(h) for h in hits]
        )

    shared = {
        "mode": "sem",
        "query": q,
        "fts5_query": fts5_query,
        "fts5_weight": fts5_weight,
        "hits": hits,
        "total": len(hits),
        "limit": parsed_limit,
        "offset": parsed_offset,
        "error": error,
        "filters": {
            "spec": spec or "",
            "release": release or "",
            "version": version or "",
            "section": section or "",
        },
    }
    if is_htmx_request(request):
        return templates.TemplateResponse(
            request=request,
            name="partials/spec_doc_search_results.html",
            context=dict(shared),
        )
    return templates.TemplateResponse(
        request=request,
        name="spec_docs_search.html",
        context={
            **shared,
            "active_nav": "search",
            "pending_jobs": pending_jobs,
        },
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
