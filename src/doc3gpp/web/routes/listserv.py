"""ETSI LISTSERV archive HTTP endpoints: SQL-only reads and job-enqueuing mutations.

Read endpoints never fetch from ETSI. ``sync``/``parse``/``index`` POSTs validate
the exact request and enqueue a background job; the ``enqueue_listserv_*``
helpers are shared with the MCP surface.
"""
# FastAPI dependency calls in route signatures are intentional.
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from doc3gpp.models.index import IndexRequest
from doc3gpp.models.jobs import Job, JobKind, JSONValue
from doc3gpp.models.listserv import ListservFilters, parse_date_set
from doc3gpp.models.listserv_payloads import (
    listserv_record_payload,
    listserv_search_payload,
)
from doc3gpp.models.search import SearchError, SearchIndexCorruptError, SearchQueryError
from doc3gpp.repository.protocols import JobRepository
from doc3gpp.services.listserv_index import ListservIndexService
from doc3gpp.services.listserv_search import ListservSearchService
from doc3gpp.services.listserv_service import ListservService
from doc3gpp.web.deps import (
    get_job_repo,
    get_listserv_index_service,
    get_listserv_search_service,
    get_listserv_service,
    get_pending_jobs,
)
from doc3gpp.web.errors import (
    InvalidFilterError,
    SettingsDisabledError,
    map_search_error,
)
from doc3gpp.web.templates_setup import templates
from doc3gpp.web.workers.listserv_jobs import validate_batch_request

router = APIRouter(prefix="/api/listserv", tags=["listserv"])


def _optional_search_filter(value: str | None) -> str | None:
    """Treat blank HTTP query values as absent, preserving rich-filter syntax."""
    return value if value and value.strip() else None


def enqueue_listserv_batch(
    job_repo: JobRepository,
    kind: JobKind,
    list_id: object,
    dates: object = None,
    force: bool = False,
) -> Job:
    """Validate the exact list ID/dates and enqueue a LISTSERV sync or parse job."""
    if kind not in (JobKind.LISTSERV_SYNC, JobKind.LISTSERV_PARSE):
        raise ValueError(f"not a LISTSERV batch job kind: {kind}")
    try:
        normalized, date_list = validate_batch_request(list_id, dates)
    except ValueError as exc:
        raise InvalidFilterError(str(exc)) from exc
    params: dict[str, JSONValue] = {
        "list_id": normalized,
        "dates": list(date_list),
        "force": bool(force),
    }
    return job_repo.create(kind, params)


def enqueue_listserv_sync(
    job_repo: JobRepository,
    list_id: object,
    dates: object = None,
    force: bool = False,
) -> Job:
    return enqueue_listserv_batch(job_repo, JobKind.LISTSERV_SYNC, list_id, dates, force)


def enqueue_listserv_parse(
    job_repo: JobRepository,
    list_id: object,
    dates: object = None,
    force: bool = False,
) -> Job:
    return enqueue_listserv_batch(job_repo, JobKind.LISTSERV_PARSE, list_id, dates, force)


def enqueue_listserv_index(
    job_repo: JobRepository,
    *,
    rebuild: bool = False,
    rebuild_embeddings: bool = False,
    rebuild_all: bool = False,
    batch: int | None = None,
    resume: bool = False,
    stale_only: bool = False,
) -> Job:
    """Validate index flags and enqueue a LISTSERV index maintenance job."""
    if batch is not None and batch < 1:
        raise InvalidFilterError("batch must be at least 1")
    if rebuild_all and (rebuild or rebuild_embeddings):
        raise InvalidFilterError(
            "rebuild_all is mutually exclusive with rebuild and rebuild_embeddings"
        )
    params: dict[str, JSONValue] = {
        "rebuild": rebuild,
        "rebuild_embeddings": rebuild_embeddings,
        "rebuild_all": rebuild_all,
        "resume": resume,
        "stale_only": stale_only,
    }
    if batch is not None:
        params["batch"] = batch
    # Constructing the request keeps the params shape tied to the service contract.
    IndexRequest(**params)  # type: ignore[arg-type]
    return job_repo.create(JobKind.LISTSERV_INDEX, params)


def _queued(job: Job) -> JSONResponse:
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job.id,
            "status": job.status.value,
            "url": f"/jobs/{job.id}",
        },
    )


class _BatchBody(BaseModel):
    list_id: str
    dates: list[str] = []
    force: bool = False


class _IndexBody(BaseModel):
    rebuild: bool = False
    rebuild_embeddings: bool = False
    rebuild_all: bool = False
    batch: int | None = None
    resume: bool = False
    stale_only: bool = False


@router.get("/lists")
def list_listserv_ids(
    service: ListservService = Depends(get_listserv_service),
) -> dict[str, list[str]]:
    return {"list_ids": service.list_ids()}


@router.get("/search")
def search_listserv(
    date: list[str] | None = Query(default=None),
    list_id: str | None = Query(default=None),
    sender: str | None = Query(default=None),
    subject: str | None = Query(default=None),
    tdocs: str | None = Query(default=None),
    text: str | None = Query(default=None),
    semantic: str | None = Query(default=None),
    limit: int = Query(default=20),
    offset: int = Query(default=0),
    service: ListservSearchService = Depends(get_listserv_search_service),
) -> list[dict[str, Any]]:
    date_values = [value for value in (date or []) if value.strip()]
    try:
        filters = ListservFilters(
            list_id=_optional_search_filter(list_id),
            sender=_optional_search_filter(sender),
            subject=_optional_search_filter(subject),
            tdocs=_optional_search_filter(tdocs),
            dates=parse_date_set(date_values) if date_values else None,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise InvalidFilterError(str(exc)) from exc
    try:
        results = service.search(text=text, semantic=semantic, filters=filters)
    except SearchQueryError:
        raise
    except SearchIndexCorruptError as exc:
        raise map_search_error(exc, resource="listserv") from exc
    except SearchError as exc:
        raise SettingsDisabledError(str(exc)) from exc
    return [listserv_search_payload(r) for r in results]


@router.get("/show")
def show_listserv_message(
    url: str = Query(...),
    service: ListservService = Depends(get_listserv_service),
) -> dict[str, Any]:
    return listserv_record_payload(service.show(url))


@router.get("/index/status")
def listserv_index_status(
    service: ListservIndexService = Depends(get_listserv_index_service),
) -> dict[str, Any]:
    return service.status().to_dict()


@router.post("/sync", status_code=202)
def post_listserv_sync(
    body: _BatchBody, job_repo: JobRepository = Depends(get_job_repo)
) -> JSONResponse:
    return _queued(enqueue_listserv_sync(job_repo, body.list_id, body.dates, body.force))


@router.post("/parse", status_code=202)
def post_listserv_parse(
    body: _BatchBody, job_repo: JobRepository = Depends(get_job_repo)
) -> JSONResponse:
    return _queued(enqueue_listserv_parse(job_repo, body.list_id, body.dates, body.force))


@router.post("/index", status_code=202)
def post_listserv_index(
    body: _IndexBody, job_repo: JobRepository = Depends(get_job_repo)
) -> JSONResponse:
    return _queued(enqueue_listserv_index(job_repo, **body.model_dump()))


ui_router = APIRouter(tags=["listserv-ui"])


@ui_router.get("/listserv", include_in_schema=False)
def listserv_page(
    request: Request,
    date: list[str] | None = Query(default=None),
    list_id: str | None = Query(default=None),
    sender: str | None = Query(default=None),
    subject: str | None = Query(default=None),
    tdocs: str | None = Query(default=None),
    text: str | None = Query(default=None),
    semantic: str | None = Query(default=None),
    limit: int = Query(default=20),
    offset: int = Query(default=0),
    service: ListservService = Depends(get_listserv_service),
    search_service: ListservSearchService = Depends(get_listserv_search_service),
    index_service: ListservIndexService = Depends(get_listserv_index_service),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render stored LISTSERV search data without syncing or parsing."""
    date_values = [
        part.strip()
        for value in (date or [])
        for part in value.split(",")
        if part.strip()
    ]
    page_limit = max(1, min(limit, 150))
    page_offset = max(offset, 0)
    normalized_list_id = _optional_search_filter(list_id)
    normalized_sender = _optional_search_filter(sender)
    normalized_subject = _optional_search_filter(subject)
    normalized_tdocs = _optional_search_filter(tdocs)
    list_ids = list_listserv_ids(service)["list_ids"]
    index_status = listserv_index_status(index_service)
    hits = search_listserv(
        date=date_values or None,
        list_id=normalized_list_id,
        sender=normalized_sender,
        subject=normalized_subject,
        tdocs=normalized_tdocs,
        text=text,
        semantic=semantic,
        limit=page_limit,
        offset=page_offset,
        service=search_service,
    )
    next_offset = (
        page_offset + len(hits) if len(hits) == page_limit else None
    )
    return templates.TemplateResponse(
        request=request,
        name="listserv.html",
        context={
            "active_nav": "listserv",
            "pending_jobs": pending_jobs,
            "list_ids": list_ids,
            "index_status": index_status,
            "hits": hits,
            "filters": {
                "list_id": normalized_list_id,
                "sender": normalized_sender,
                "subject": normalized_subject,
                "tdocs": normalized_tdocs,
            },
            "date_filters": date_values,
            "text": text,
            "semantic": semantic,
            "limit": page_limit,
            "offset": page_offset,
            "next_offset": next_offset,
        },
    )


@ui_router.get("/listserv/show", include_in_schema=False)

def listserv_show_page(
    request: Request,
    url: str = Query(...),
    service: ListservService = Depends(get_listserv_service),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render the stored payload for one exact archive URL."""
    record = show_listserv_message(url=url, service=service)
    try:
        parsed_url = urlsplit(record["url"])
        port = parsed_url.port
    except ValueError:
        upstream_url = None
    else:
        upstream_url = (
            record["url"]
            if parsed_url.scheme == "https"
            and parsed_url.hostname == "list.etsi.org"
            and port in (None, 443)
            and parsed_url.username is None
            and parsed_url.password is None
            else None
        )
    return templates.TemplateResponse(
        request=request,
        name="listserv_show.html",
        context={
            "active_nav": "listserv",
            "pending_jobs": pending_jobs,
            "record": record,
            "upstream_url": upstream_url,
        },
    )


__all__ = [
    "enqueue_listserv_batch",
    "enqueue_listserv_index",
    "enqueue_listserv_parse",
    "enqueue_listserv_sync",
    "router",
    "ui_router",
]
