"""HTTP routes for TDoc read surfaces.

* ``GET /tdocs`` — list page with HTMX-driven filters (the same
  filter grammar as ``tdoc list --format json`` / ``tdoc parse``).
* ``GET /tdocs/{tdoc_id}`` — detail page reusing the
  :class:`TDocShowRecord` composition (shared with the CLI so the
  JSON envelope is byte-identical).
* ``GET /tdocs/{tdoc_id}/content?format=markdown`` — returns the raw
  cached markdown bytes for ``tdoc.ftp_url``.
* ``GET /tdocs/{tdoc_id}/content?format=html`` — renders the cached
  markdown as HTML via ``markdown-it-py`` + Pygments.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi import Path as PathParam
from fastapi import Query, Request
from fastapi.responses import JSONResponse, Response
from markdown_it import MarkdownIt

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_show import TDocShowRecord, TDocShowRepos
from doc3gpp.scraping.cache_keys import derive_cache_file
from doc3gpp.services.tdoc_cr_service import TDocNotFoundError
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
from doc3gpp.web.deps import get_settings, get_tdoc_file_repo, get_tdoc_service
from doc3gpp.web.errors import InvalidFilterError
from doc3gpp.web.filters import parse_int_query, parse_text_query
from doc3gpp.web.render import to_jsonable
from doc3gpp.web.templates_setup import templates


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/tdocs", tags=["tdocs"])


_LIMIT_CAP = 200


_MD_RENDERER = MarkdownIt("commonmark", {"html": True, "linkify": True}).enable("table")


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


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def list_tdocs(
    request: Request,
    tdoc_id: str | None = Query(default=None),
    meeting_id: int | None = Query(default=None),
    agenda_item: str | None = Query(default=None),
    type: str | None = Query(default=None),
    source: str | None = Query(default=None),
    for_decision: str | None = Query(default=None),
    against_decision: str | None = Query(default=None),
    decision: str | None = Query(default=None),
    work_item: str | None = Query(default=None),
    version: str | None = Query(default=None),
    start_after: str | None = Query(default=None),
    start_before: str | None = Query(default=None),
    limit: int | None = Query(default=50),
    offset: int | None = Query(default=0),
    format: str | None = Query(default=None, alias="format"),
    service: TDocService = Depends(get_tdoc_service),
) -> Any:
    """Render ``tdoc_list.html`` or a JSON list of TDoc rows."""
    parsed_limit = parse_int_query(
        str(limit) if limit is not None else None, min=1, max=_LIMIT_CAP,
    ) or 50
    parsed_offset = parse_int_query(
        str(offset) if offset is not None else None, min=0,
    ) or 0

    rows = service.list_recent_with_meeting(
        limit=parsed_limit,
        offset=parsed_offset,
        tdoc_id=parse_text_query(tdoc_id),
        meeting_id=meeting_id,
        title=parse_text_query(agenda_item),
        tdoc_type=parse_text_query(type),
        source=parse_text_query(source),
        version=parse_text_query(version),
    )

    if format == "json":
        return JSONResponse(content={"tdocs": to_jsonable(rows)})

    next_offset = (
        parsed_offset + len(rows) if len(rows) == parsed_limit else None
    )
    return templates.TemplateResponse(
        request=request,
        name="tdoc_list.html",
        context={
            "active_nav": "tdocs",
            "tdocs": rows,
            "total": len(rows),
            "offset": parsed_offset,
            "next_offset": next_offset,
            "filters": {
                "tdoc_id": tdoc_id or "",
                "meeting_id": meeting_id,
                "agenda_item": agenda_item or "",
                "type": type or "",
                "source": source or "",
                "for_decision": for_decision or "",
                "against_decision": against_decision or "",
                "decision": decision or "",
                "work_item": work_item or "",
                "version": version or "",
                "start_after": start_after or "",
                "start_before": start_before or "",
                "limit": parsed_limit,
            },
        },
    )


@router.get("/{tdoc_id}", include_in_schema=False)
async def show_tdoc(
    request: Request,
    tdoc_id: str = PathParam(...),
    format: str | None = Query(default=None, alias="format"),
    service: TDocService = Depends(get_tdoc_service),
    file_repo: Any = Depends(get_tdoc_file_repo),
) -> Any:
    """Render ``tdoc_show.html`` or a :class:`TDocShowRecord` JSON payload."""
    repos = _build_show_repos(request, file_repo)
    # The composition is delegated to the models layer so HTTP and CLI
    # JSON stay byte-identical. ``service`` is currently unused by the
    # composition but is injected so future fields (e.g. an ETag-like
    # last-sync hint) can pull from the service without changing the
    # route signature.
    _ = service
    record = TDocShowRecord.from_tdoc_id(tdoc_id, repos)

    if format == "json":
        return JSONResponse(content=to_jsonable(record))

    return templates.TemplateResponse(
        request=request,
        name="tdoc_show.html",
        context={"active_nav": "tdocs", "record": record},
    )


@router.get("/{tdoc_id}/content", include_in_schema=False)
async def tdoc_content(
    request: Request,
    tdoc_id: str = PathParam(...),
    format: str = Query(default="html"),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Return cached markdown bytes or render them as HTML."""
    tdoc_repo = SQLAlchemyTDocRepository()
    tdoc: TDoc | None = tdoc_repo.get_by_id(tdoc_id)
    if tdoc is None or not tdoc.ftp_url:
        raise TDocNotFoundError(
            f"TDoc '{tdoc_id}' is not stored or has no ftp_url"
        )

    cache_file = derive_cache_file(tdoc.ftp_url)
    markdown_path = Path(settings.cache.dir) / "markdown" / cache_file
    if not markdown_path.exists():
        raise TDocNotFoundError(
            f"No cached markdown for TDoc {tdoc_id}. Run "
            f"doc3gpp tdoc parse --tdoc {tdoc_id} first."
        )
    markdown_text = markdown_path.read_text(encoding="utf-8")

    if format == "markdown":
        return Response(
            content=markdown_text,
            media_type="text/markdown; charset=utf-8",
        )
    if format == "html":
        html_content = _MD_RENDERER.render(markdown_text)
        return templates.TemplateResponse(
            request=request,
            name="tdoc_content.html",
            context={
                "active_nav": "tdocs",
                "tdoc_id": tdoc_id,
                "html_content": html_content,
            },
        )
    raise InvalidFilterError(
        f"format must be 'markdown' or 'html', got: {format!r}"
    )


__all__ = ["router"]