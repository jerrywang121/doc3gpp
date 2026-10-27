"""HTTP routes for TDoc read surfaces.

* ``GET /tdocs`` — list page with HTMX-driven filters (the same
  filter grammar as ``tdoc list --format json`` / ``tdoc parse``).
* ``GET /tdocs/{tdoc_id}`` — detail page reusing the
  :class:`TDocShowRecord` composition (shared with the CLI so the
  JSON envelope is byte-identical).
* ``GET /tdocs/{tdoc_id}/content?format=markdown`` — returns the stored
  Markdown for the TDoc's ZIP, served from SQL through
  :class:`TDocContentService`.
* ``GET /tdocs/{tdoc_id}/content?format=html`` — renders the same
  Markdown as HTML via ``markdown-it-py`` + Pygments.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, JSONResponse, Response
from markdown_it import MarkdownIt

from doc3gpp.models.schema_info import RESOURCE_SCHEMAS, schema_payload
from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_content import TDocContent
from doc3gpp.models.tdoc_show import (
    TDocShowRecord,
    TDocShowRecordByUrl,
    TDocShowRepos,
)
from doc3gpp.parsers.normalizers import normalize_ftp_path
from doc3gpp.scraping.cache_keys import derive_cache_file
from doc3gpp.services.tdoc_content_service import (
    TDocContentService,
    render_tdoc_documents_markdown,
    tdoc_content_to_dict,
)
from doc3gpp.services.tdoc_cr_service import (
    TDocNotFoundError,
    TDocUrlNotFoundError,
)
from doc3gpp.services.tdoc_service import TDocService
from doc3gpp.settings.schema import Settings
from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
    SQLAlchemyTDocCrChangeDetailsRepository,
)
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
    SQLAlchemyTDocCrTtcnRepository,
)
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
from doc3gpp.web.deps import (
    get_pending_jobs,
    get_settings,
    get_tdoc_content_service,
    get_tdoc_file_repo,
    get_tdoc_service,
)
from doc3gpp.web.errors import (
    CacheMissError,
    InvalidFilterError,
    map_tdoc_content_error,
)
from doc3gpp.web.filters import (
    is_htmx_request,
    parse_bool_query,
    parse_date_query,
    parse_int_query,
    parse_text_query,
)
from doc3gpp.web.render import (
    TDOC_COLUMN_LABELS,
    TDOC_HTML_DEFAULT_FIELDS,
    tdoc_rows,
    tdoc_table_rows,
    to_jsonable,
)
from doc3gpp.web.templates_setup import templates

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/tdocs", tags=["tdocs"])


_LIMIT_CAP = 200

# The default field list mirrors ``settings.output.fields.tdoc``, which
# is what ``doc3gpp tdoc list --format json`` emits by default.
_TDOC_DEFAULT_FIELDS = [
    "tdoc_id",
    "meeting_name",
    "title",
    "source",
    "type",
    "status",
    "cr_cat",
    "spec",
    "version",
    "related_wis",
    "parsed",
]

_TDOC_ALLOWED_FIELDS = frozenset(TDOC_COLUMN_LABELS)

_MD_RENDERER = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable("table")
_REVISION_TAG_RE = re.compile(r"</?(?:ins|del)>", re.IGNORECASE)


def _revision_tag_rule(state, silent: bool) -> bool:
    match = _REVISION_TAG_RE.match(state.src, state.pos)
    if match is None:
        return False
    if not silent:
        state.push("html_inline", "", 0).content = match.group(0)
    state.pos = match.end()
    return True


_MD_RENDERER.inline.ruler.before("escape", "revision_tag", _revision_tag_rule)


def _build_show_repos(
    request: Request,
    file_repo: Any,
) -> TDocShowRepos:
    """Build a :class:`TDocShowRepos` from the per-app state collaborators.

    The repository Protocol-typed collaborators live on the per-app
    state container; we re-build concrete instances because the
    show-composition is an inner-layer read, not a service-layer
    operation. The same five repos the CLI's ``tdoc show`` uses.
    The file repo is injected via :func:`get_tdoc_file_repo` so tests
    can override it without poking at ``app.state``.
    """
    return TDocShowRepos(
        tdoc=SQLAlchemyTDocRepository(),
        cr=SQLAlchemyTDocCrRepository(),
        cr_ttcn=SQLAlchemyTDocCrTtcnRepository(),
        cr_change_details=SQLAlchemyTDocCrChangeDetailsRepository(),
        file=file_repo,
    )


def _resolve_cache_file(tdoc: TDoc) -> str:
    """Return the cache key for a TDoc's artefacts.

    Prefers the authoritative ``cache_file`` persisted on
    ``tdoc_extracts`` (keyed by ``tdoc.ftp_url``) — that is the name
    the parse actually wrote to the ``zips/`` / ``markdown/`` subtrees,
    and it can differ from :func:`derive_cache_file` depending on which
    parse path populated it (DB-mode keys on the relative ``ftp_url``,
    while the direct-URL path keys on the absolute ``https://...`` URL).
    Falls back to re-deriving when no extract row exists yet.
    """
    meta = SQLAlchemyTDocCrRepository().get_extract_meta_by_url(tdoc.ftp_url)
    if meta is not None and meta.cache_file:
        return meta.cache_file
    return derive_cache_file(tdoc.ftp_url)


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def list_tdocs(
    request: Request,
    tdoc_id: str | None = Query(default=None),
    meeting: str | None = Query(default=None),
    meeting_id: str | None = Query(default=None),
    source: str | None = Query(default=None),
    spec: str | None = Query(default=None),
    wi: str | None = Query(default=None),
    title: str | None = Query(default=None),
    cr_cat: str | None = Query(default=None, alias="cr-cat"),
    status: str | None = Query(default=None),
    type: str | None = Query(default=None),
    revision_of: str | None = Query(default=None, alias="revision-of"),
    revised_to: str | None = Query(default=None, alias="revised-to"),
    ftp_url: str | None = Query(default=None, alias="ftp-url"),
    release: str | None = Query(default=None),
    version: str | None = Query(default=None),
    cr_num: str | None = Query(default=None, alias="cr-num"),
    cr_pack: str | None = Query(default=None, alias="cr-pack"),
    ls_to: str | None = Query(default=None, alias="ls-to"),
    ls_cc: str | None = Query(default=None, alias="ls-cc"),
    original_ls: str | None = Query(default=None, alias="original-ls"),
    tdoc_for: str | None = Query(default=None, alias="for"),
    abstract: str | None = Query(default=None, alias="abstract"),
    secretary_remarks: str | None = Query(default=None, alias="secretary-remarks"),
    uploaded_date: str | None = Query(default=None, alias="uploaded-date"),
    parsed: str | None = Query(default=None),
    limit: str | None = Query(default="50"),
    offset: str | None = Query(default="0"),
    fields: list[str] | None = Query(default=None),
    format: str | None = Query(default=None, alias="format"),
    service: TDocService = Depends(get_tdoc_service),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render ``tdoc_list.html`` or a JSON list of TDoc rows.

    ``?format=json`` returns the same payload as
    ``doc3gpp tdoc list --format json``: a bare array of field-selected
    rows (``settings.output.fields.tdoc`` by default) with every cell
    string-coerced via :func:`doc3gpp.web.render.tdoc_rows`.

    The numeric query params (``meeting_id``, ``limit``, ``offset``)
    are declared as ``str`` so an empty form value (``meeting_id=``)
    doesn't trigger a 422 — :func:`parse_int_query` treats ``""`` as
    ``None`` and the route fills in the default. The CLI's typed path
    accepts ints only; the HTTP path is a best-effort form-binding layer.
    """
    parsed_limit = parse_int_query(limit, min=1, max=_LIMIT_CAP) or 50
    parsed_offset = parse_int_query(offset, min=0) or 0
    parsed_meeting_id = parse_int_query(meeting_id, min=1)
    parsed_uploaded_date = parse_date_query(uploaded_date)
    parsed_state = parse_bool_query(parsed)

    if fields:
        unknown = [f for f in fields if f not in _TDOC_ALLOWED_FIELDS]
        if unknown:
            raise InvalidFilterError(
                "unknown fields: "
                + ", ".join(sorted(unknown))
                + f"; valid: {', '.join(sorted(_TDOC_ALLOWED_FIELDS))}"
            )
        selected_fields = [f for f in fields if f]
    else:
        selected_fields = None
    html_fields = selected_fields or list(TDOC_HTML_DEFAULT_FIELDS)
    json_fields = selected_fields or list(_TDOC_DEFAULT_FIELDS)

    rows = service.list_recent_with_meeting(
        limit=parsed_limit,
        offset=parsed_offset,
        tdoc_id=parse_text_query(tdoc_id),
        meeting_like=parse_text_query(meeting),
        meeting_id=parsed_meeting_id,
        title=parse_text_query(title),
        tdoc_type=parse_text_query(type),
        source=parse_text_query(source),
        spec=parse_text_query(spec),
        wi=parse_text_query(wi),
        cr_cat=parse_text_query(cr_cat),
        status=parse_text_query(status),
        revision_of=parse_text_query(revision_of),
        revised_to=parse_text_query(revised_to),
        ftp_url=parse_text_query(ftp_url),
        release=parse_text_query(release),
        version=parse_text_query(version),
        cr_num=parse_text_query(cr_num),
        cr_pack=parse_text_query(cr_pack),
        ls_to=parse_text_query(ls_to),
        ls_cc=parse_text_query(ls_cc),
        original_ls=parse_text_query(original_ls),
        tdoc_for=parse_text_query(tdoc_for),
        abstract=parse_text_query(abstract),
        secretary_remarks=parse_text_query(secretary_remarks),
        uploaded_date=parsed_uploaded_date,
        parsed=parsed_state,
    )

    if format == "json":
        return JSONResponse(content=tdoc_rows(rows, json_fields))

    next_offset = (
        parsed_offset + len(rows) if len(rows) == parsed_limit else None
    )
    table_rows = tdoc_table_rows(rows, html_fields)
    for r, item in zip(table_rows, rows):
        r.setdefault("tdoc_id", item.tdoc.tdoc_id)
        r.setdefault("status", item.tdoc.status or "")
    template_name = (
        "partials/tdoc_results.html" if is_htmx_request(request) else "tdoc_list.html"
    )
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "active_nav": "tdocs",
            "tdocs": table_rows,
            "total": len(rows),
            "limit": parsed_limit,
            "offset": parsed_offset,
            "next_offset": next_offset,
            "pending_jobs": pending_jobs,
            "fields": html_fields,
            "column_labels": TDOC_COLUMN_LABELS,
            "filters": {
                "tdoc_id": tdoc_id or "",
                "meeting": meeting or "",
                "meeting_id": parsed_meeting_id,
                "title": title or "",
                "type": type or "",
                "source": source or "",
                "spec": spec or "",
                "wi": wi or "",
                "cr_cat": cr_cat or "",
                "status": status or "",
                "revision_of": revision_of or "",
                "revised_to": revised_to or "",
                "ftp_url": ftp_url or "",
                "release": release or "",
                "version": version or "",
                "cr_num": cr_num or "",
                "cr_pack": cr_pack or "",
                "ls_to": ls_to or "",
                "ls_cc": ls_cc or "",
                "original_ls": original_ls or "",
                "tdoc_for": tdoc_for or "",
                "abstract": abstract or "",
                "secretary_remarks": secretary_remarks or "",
                "uploaded_date": uploaded_date or "",
                "limit": parsed_limit,
            },
        },
    )


@router.get("/schema", include_in_schema=False)
async def tdoc_schema(
    request: Request,
    format: str | None = Query(default=None, alias="format"),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render ``schema.html`` or the CLI-identical JSON field descriptors.

    ``?format=json`` returns the same payload as
    ``doc3gpp tdoc schema --format json``: a bare array of
    ``{table, field, type, nullable, description, values}`` rows from
    :func:`doc3gpp.models.schema_info.schema_payload`. No DB access.
    """
    if format == "json":
        return JSONResponse(content=schema_payload("tdoc"))
    tables = RESOURCE_SCHEMAS["tdoc"]
    template_name = (
        "partials/schema_results.html" if is_htmx_request(request) else "schema.html"
    )
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "active_nav": "tdocs",
            "resource": "tdoc",
            "tables": tables,
            "total": sum(len(t.fields) for t in tables),
            "pending_jobs": pending_jobs,
        },
    )


@router.get("/by-url", include_in_schema=False)
async def show_tdoc_by_url(
    request: Request,
    ftp_url: str | None = Query(default=None),
    format: str | None = Query(default=None, alias="format"),
    file_repo: Any = Depends(get_tdoc_file_repo),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Render ``tdoc_show.html`` polymorphed on URL mode, or return JSON.

    The URL-anchored read; mirrors ``doc3gpp tdoc show --ftp-url``.
    Auto-sync is never triggered because there is no parent TDoc /
    meeting to anchor a sync on (CLI parity).
    """
    if not ftp_url:
        raise InvalidFilterError("ftp_url query param is required")
    normalised = normalize_ftp_path(ftp_url)
    if not normalised:
        raise InvalidFilterError(
            f"ftp_url {ftp_url!r} normalised to an empty path"
        )

    repos = _build_show_repos(request, file_repo)
    record = TDocShowRecordByUrl.from_ftp_url(normalised, repos)

    if (
        record.tdoc is None
        and record.cover is None
        and record.extracted_at is None
        and record.ttcn is None
        and record.changes is None
        and not record.files
    ):
        raise TDocUrlNotFoundError(normalised)

    if format == "json":
        return JSONResponse(content=to_jsonable(record))

    has_cached_zip = False
    if record.ftp_url:
        cache_file = derive_cache_file(record.ftp_url)
        has_cached_zip = (Path(settings.cache.dir) / "zips" / cache_file).exists()

    return templates.TemplateResponse(
        request=request,
        name="tdoc_show.html",
        context={
            "active_nav": "tdocs",
            "record": record,
            "has_cached_zip": has_cached_zip,
        },
    )


@router.get("/{tdoc_id}", include_in_schema=False)
async def show_tdoc(
    request: Request,
    tdoc_id: str = PathParam(...),
    format: str | None = Query(default=None, alias="format"),
    file_repo: Any = Depends(get_tdoc_file_repo),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Render ``tdoc_show.html`` or a :class:`TDocShowRecord` JSON payload."""
    repos = _build_show_repos(request, file_repo)
    # The composition is delegated to the models layer so HTTP and CLI
    # JSON stay byte-identical.
    record = TDocShowRecord.from_tdoc_id(tdoc_id, repos)

    if format == "json":
        return JSONResponse(content=to_jsonable(record))

    has_cached_zip = False
    if record.tdoc.ftp_url:
        cache_file = derive_cache_file(record.tdoc.ftp_url)
        has_cached_zip = (Path(settings.cache.dir) / "zips" / cache_file).exists()

    return templates.TemplateResponse(
        request=request,
        name="tdoc_show.html",
        context={
            "active_nav": "tdocs",
            "record": record,
            "has_cached_zip": has_cached_zip,
        },
    )


def _validate_tdoc_content_format(format: str) -> None:
    if format not in ("html", "markdown", "json"):
        raise InvalidFilterError(
            f"format must be one of 'html'|'markdown'|'json', got: {format!r}"
        )


def _render_tdoc_content_response(
    request: Request,
    content: TDocContent,
    *,
    format: str,
    pending_jobs: int,
) -> Response:
    _validate_tdoc_content_format(format)
    completion_headers = (
        {}
        if content.complete
        else {"X-Doc3gpp-Extraction-Complete": "false"}
    )

    if format == "json":
        return Response(
            content=json.dumps(
                tdoc_content_to_dict(content),
                separators=(",", ":"),
                ensure_ascii=False,
            ),
            media_type="application/json; charset=utf-8",
        )

    if format == "markdown":
        return Response(
            content=render_tdoc_documents_markdown(
                content.documents, include_source_headers=True
            ),
            media_type="text/markdown; charset=utf-8",
            headers=completion_headers,
        )

    documents = [
        {
            "source_file": document.source_file,
            "is_main": document.is_main,
            "source_label": (
                "Main document" if document.is_main else "Supporting document"
            ),
            "html_content": _MD_RENDERER.render(document.markdown),
        }
        for document in content.documents
    ]
    response = templates.TemplateResponse(
        request=request,
        name="tdoc_content.html",
        context={
            "active_nav": "tdocs",
            "tdoc_id": content.tdoc_id,
            "ftp_url": content.ftp_url,
            "documents": documents,
            "pending_jobs": pending_jobs,
        },
    )
    for name, value in completion_headers.items():
        response.headers[name] = value
    return response


@router.get("/by-url/content", include_in_schema=False)
async def tdoc_content_by_url(
    request: Request,
    ftp_url: str | None = Query(default=None),
    source_file: str | None = Query(default=None),
    format: str = Query(default="html"),
    content_service: TDocContentService = Depends(get_tdoc_content_service),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Response:
    """Return stored Markdown for an exact ZIP URL without extraction or sync."""
    if not ftp_url or not ftp_url.strip():
        raise InvalidFilterError("ftp_url query param is required")
    _validate_tdoc_content_format(format)

    try:
        content = content_service.get(ftp_url=ftp_url, source_file=source_file)
    except ValueError as exc:
        raise map_tdoc_content_error(exc, ftp_url=ftp_url) from exc

    return _render_tdoc_content_response(
        request, content, format=format, pending_jobs=pending_jobs
    )


@router.get("/{tdoc_id}/content", include_in_schema=False)
async def tdoc_content(
    request: Request,
    tdoc_id: str = PathParam(...),
    source_file: str | None = Query(default=None),
    format: str = Query(default="html"),
    content_service: TDocContentService = Depends(get_tdoc_content_service),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Response:
    """Return stored TDoc Markdown as HTML, Markdown, or JSON."""
    _validate_tdoc_content_format(format)
    try:
        content = content_service.get(
            tdoc_id=tdoc_id, source_file=source_file
        )
    except ValueError as exc:
        raise map_tdoc_content_error(exc, tdoc_id=tdoc_id) from exc

    return _render_tdoc_content_response(
        request, content, format=format, pending_jobs=pending_jobs
    )


@router.get("/{tdoc_id}/download", include_in_schema=False)
async def tdoc_download(
    tdoc_id: str = PathParam(...),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Serve the cached 3GPP zip bytes for ``tdoc_id`` when available.

    The ``zips/`` cache subtree holds the raw bytes 3GPP served for the
    TDoc's ``ftp_url`` (see ``derive_cache_file``). A cache miss maps to
    the canonical ``cache_miss`` envelope with a hint pointing at
    ``doc3gpp tdoc parse --tdoc <id>``.
    """
    tdoc_repo = SQLAlchemyTDocRepository()
    tdoc: TDoc | None = tdoc_repo.get_by_id(tdoc_id)
    if tdoc is None or not tdoc.ftp_url:
        raise TDocNotFoundError(
            f"TDoc '{tdoc_id}' is not stored or has no ftp_url"
        )

    cache_file = _resolve_cache_file(tdoc)
    zip_path = Path(settings.cache.dir) / "zips" / cache_file
    if not zip_path.exists():
        raise CacheMissError(
            f"No cached zip for TDoc {tdoc_id}.",
            hint=f"run: doc3gpp tdoc parse --tdoc {tdoc_id}",
        )

    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=cache_file,
    )


__all__ = ["router"]