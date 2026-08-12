"""Job HTTP routes (T8): POST / poll / SSE / cancel.

Background job queue surface:

* ``POST /jobs/sync/meetings``, ``POST /jobs/sync/tdocs``,
  ``POST /jobs/sync/tdocs/all``, ``POST /jobs/parse/tdocs``,
  ``POST /jobs/search/rebuild``, ``POST /jobs/cache/purge`` — enqueue a
  job and return ``202`` with the spec's job envelope.
* ``POST /jobs/sync_tdocs`` — flat alias required by the
  ``meeting_show.html`` form, which POSTs ``meeting_id`` as
  ``application/x-www-form-urlencoded``.
* ``GET /jobs`` — list recent jobs (JSON + HTML).
* ``GET /jobs/{job_id}`` — job detail JSON envelope.
* ``GET /jobs/{job_id}/events`` — ``text/event-stream`` of named
  ``status`` / ``log`` events.
* ``POST /jobs/{job_id}/cancel`` — cooperative cancellation (``200``
  or ``409`` when the job is already terminal).

Errors map through :func:`doc3gpp.web.errors.map_domain_error`:
``JobNotFoundError`` -> 404, ``JobAlreadyTerminalError`` -> 409.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from doc3gpp.models.jobs import JSONValue, Job, JobKind, JobStatus
from doc3gpp.repository.protocols import JobRepository
from doc3gpp.web.deps import get_job_repo, get_job_worker
from doc3gpp.web.errors import (
    InvalidFilterError,
    JobAlreadyTerminalError,
    JobNotFoundError,
)
from doc3gpp.web.render import to_jsonable
from doc3gpp.web.state import JobWorkerHandle
from doc3gpp.web.templates_setup import templates


router = APIRouter(prefix="/jobs", tags=["jobs"])


_LIMIT_CAP = 200
_TERMINAL_STATUSES = (
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
)


def _job_url(job_id: str) -> str:
    return f"/jobs/{job_id}"


def _envelope(job: Job, *, queued: bool = False) -> dict[str, Any]:
    """Build the spec's job detail / submission envelope.

    ``queued`` selects the slim POST response shape (no kind / params /
    timestamps) vs the full ``GET /jobs/{id}`` shape.
    """
    if queued:
        return {
            "job_id": job.id,
            "status": job.status.value,
            "links": {
                "self": _job_url(job.id),
                "events": f"{_job_url(job.id)}/events",
            },
        }
    return {
        "job_id": job.id,
        "kind": job.kind.value,
        "status": job.status.value,
        "params": to_jsonable(job.params),
        "result": to_jsonable(job.result_summary) if job.result_summary is not None else None,
        "error": job.error,
        "summary": to_jsonable(job.result_summary) if job.result_summary is not None else None,
        "log_tail": list(job.log_lines),
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.finished_at.isoformat() if job.finished_at else None,
        "links": {
            "self": _job_url(job.id),
            "events": f"{_job_url(job.id)}/events",
        },
    }


def _load_job(job_repo: JobRepository, job_id: str) -> Job:
    job = job_repo.get(job_id)
    if job is None:
        raise JobNotFoundError(f"Job {job_id} not found")
    return job


# ---------------------------------------------------------------------------
# Pydantic body models (one per JobKind)
# ---------------------------------------------------------------------------


class _SyncMeetingsBody(BaseModel):
    tsg: str


class _SyncTDocsBody(BaseModel):
    meeting_id: int | None = None
    meeting: str | None = None
    force: bool = False


class _SyncSpecsBody(BaseModel):
    tsg: str
    force: bool = False


class _ParseTDocsBody(BaseModel):
    filter: dict[str, Any] = {}
    force: bool = False
    full: bool = False
    max_batch: int | None = None


class _SearchRebuildBody(BaseModel):
    stale_only: bool = False
    resume: bool = False


class _CachePurgeBody(BaseModel):
    scope: str = "markdown"
    yes: bool = False


# ---------------------------------------------------------------------------
# POST enqueue endpoints
# ---------------------------------------------------------------------------


@router.post("/sync/meetings", status_code=202)
async def post_sync_meetings(
    body: _SyncMeetingsBody,
    job_repo: JobRepository = Depends(get_job_repo),
) -> JSONResponse:
    job = job_repo.create(
        JobKind.SYNC_MEETINGS,
        {"tsg": body.tsg},
    )
    return JSONResponse(status_code=202, content=_envelope(job, queued=True))


@router.post("/sync/tdocs", status_code=202)
async def post_sync_tdocs(
    body: _SyncTDocsBody,
    job_repo: JobRepository = Depends(get_job_repo),
) -> JSONResponse:
    params: dict[str, JSONValue] = {"force": body.force}
    if body.meeting_id is not None:
        params["meeting_id"] = body.meeting_id
    if body.meeting is not None:
        params["meeting_name"] = body.meeting
    if "meeting_id" not in params and "meeting_name" not in params:
        raise InvalidFilterError(
            "sync/tdocs requires 'meeting_id' or 'meeting' in the body"
        )
    job = job_repo.create(JobKind.SYNC_TDOCS, params)
    return JSONResponse(status_code=202, content=_envelope(job, queued=True))


@router.post("/sync/tdocs/all", status_code=202)
async def post_sync_tdocs_all(
    body: _SyncTDocsBody,
    job_repo: JobRepository = Depends(get_job_repo),
) -> JSONResponse:
    job = job_repo.create(JobKind.SYNC_TDOCS_ALL, {"force": body.force})
    return JSONResponse(status_code=202, content=_envelope(job, queued=True))


@router.post("/sync/specs", status_code=202)
async def post_sync_specs(
    body: _SyncSpecsBody,
    job_repo: JobRepository = Depends(get_job_repo),
) -> JSONResponse:
    job = job_repo.create(
        JobKind.SYNC_SPECS,
        {"tsg": body.tsg, "force": body.force},
    )
    return JSONResponse(status_code=202, content=_envelope(job, queued=True))


@router.post("/parse/tdocs", status_code=202)
async def post_parse_tdocs(
    body: _ParseTDocsBody,
    job_repo: JobRepository = Depends(get_job_repo),
) -> JSONResponse:
    params: dict[str, JSONValue] = {
        "filter": body.filter,
        "force": body.force,
        "full": body.full,
    }
    if body.max_batch is not None:
        params["max_batch"] = body.max_batch
    job = job_repo.create(JobKind.PARSE_TDOCS, params)
    return JSONResponse(status_code=202, content=_envelope(job, queued=True))


@router.post("/search/rebuild", status_code=202)
async def post_search_rebuild(
    body: _SearchRebuildBody,
    job_repo: JobRepository = Depends(get_job_repo),
) -> JSONResponse:
    job = job_repo.create(
        JobKind.REBUILD_SEARCH,
        {"stale_only": body.stale_only, "resume": body.resume},
    )
    return JSONResponse(status_code=202, content=_envelope(job, queued=True))


@router.post("/cache/purge", status_code=202)
async def post_cache_purge(
    body: _CachePurgeBody,
    job_repo: JobRepository = Depends(get_job_repo),
) -> JSONResponse:
    if not body.yes:
        raise InvalidFilterError("cache/purge requires 'yes': true to proceed")
    if body.scope not in ("markdown", "zips", "all"):
        raise InvalidFilterError(
            "cache/purge scope must be one of 'markdown'|'zips'|'all'"
        )
    job = job_repo.create(JobKind.CACHE_PURGE, {"scope": body.scope})
    return JSONResponse(status_code=202, content=_envelope(job, queued=True))


# Flat alias required by the meeting_show.html form: it POSTs
# ``meeting_id`` as form-encoded data to ``/jobs/sync_tdocs``.
@router.post("/sync_tdocs", status_code=202)
async def post_sync_tdocs_flat(
    request: Request,
    job_repo: JobRepository = Depends(get_job_repo),
) -> JSONResponse:
    meeting_id: str | None = None
    meeting: str | None = None
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        meeting_id = form.get("meeting_id")
        meeting = form.get("meeting")
    else:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        meeting_id = payload.get("meeting_id")
        meeting = payload.get("meeting")
    params: dict[str, JSONValue] = {}
    if meeting_id is not None and meeting_id != "":
        try:
            params["meeting_id"] = int(meeting_id)
        except (ValueError, TypeError):
            raise InvalidFilterError(
                f"sync_tdocs 'meeting_id' must be an integer, got {meeting_id!r}"
            )
    elif meeting is not None and meeting != "":
        params["meeting_name"] = meeting
    else:
        raise InvalidFilterError(
            "sync_tdocs requires 'meeting_id' or 'meeting' in the body"
        )
    job = job_repo.create(JobKind.SYNC_TDOCS, params)
    return JSONResponse(status_code=202, content=_envelope(job, queued=True))


# ---------------------------------------------------------------------------
# GET list + detail
# ---------------------------------------------------------------------------


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def list_jobs(
    request: Request,
    status: str | None = Query(default=None),
    limit: int | None = Query(default=50),
    offset: int | None = Query(default=0),
    format: str | None = Query(default=None, alias="format"),
    job_repo: JobRepository = Depends(get_job_repo),
) -> Any:
    """Render ``job_status.html`` (default) or a JSON list of jobs."""
    parsed_limit = max(1, min(limit or 50, _LIMIT_CAP))
    parsed_offset = max(0, offset or 0)
    parsed_status: JobStatus | None = None
    if status:
        try:
            parsed_status = JobStatus(status)
        except ValueError:
            raise InvalidFilterError(f"invalid job status: {status!r}")

    # The JobRepository.list protocol (T3-locked) exposes only
    # ``limit`` + ``status``; paginate by over-fetching + slicing so the
    # protocol stays untouched.
    fetched = job_repo.list(
        limit=parsed_limit + parsed_offset,
        status=parsed_status,
    )
    jobs = fetched[parsed_offset : parsed_offset + parsed_limit]

    if format == "json":
        return JSONResponse(
            content={
                "jobs": [_envelope(j) for j in jobs],
                "total": len(jobs),
                "limit": parsed_limit,
                "offset": parsed_offset,
            }
        )

    next_offset = (
        parsed_offset + len(jobs)
        if len(jobs) == parsed_limit
        else None
    )
    # The nav badge shows the count of in-flight jobs (QUEUED + RUNNING).
    # See ``get_pending_jobs`` in ``web/deps.py`` for the rationale.
    pending_jobs = (
        len(job_repo.list(status=JobStatus.QUEUED, limit=1000))
        + len(job_repo.list(status=JobStatus.RUNNING, limit=1000))
    )
    return templates.TemplateResponse(
        request=request,
        name="job_status.html",
        context={
            "active_nav": "jobs",
            "jobs": jobs,
            "total": len(jobs),
            "offset": parsed_offset,
            "next_offset": next_offset,
            "filter_status": parsed_status.value if parsed_status else "",
            "limit": parsed_limit,
            "pending_jobs": pending_jobs,
        },
    )


@router.get("/{job_id}", include_in_schema=False)
async def get_job(
    request: Request,
    job_id: str,
    format: str | None = Query(default=None, alias="format"),
    job_repo: JobRepository = Depends(get_job_repo),
) -> Any:
    """Return the job detail envelope (JSON), or ``404`` when unknown."""
    job = _load_job(job_repo, job_id)
    if format == "html":
        return templates.TemplateResponse(
            request=request,
            name="partials/job_status.html",
            context={"job": job},
        )
    return JSONResponse(content=_envelope(job))


# ---------------------------------------------------------------------------
# SSE events stream
# ---------------------------------------------------------------------------


def _sse_frame(event: dict[str, Any]) -> str:
    """Serialize an event dict ``{"event", "data"}`` to an SSE frame."""
    event_name = event.get("event", "message")
    data = event.get("data", {})
    return f"event: {event_name}\ndata: {json.dumps(data)}\n\n"


@router.get("/{job_id}/events", include_in_schema=False)
async def job_events(
    job_id: str,
    job_repo: JobRepository = Depends(get_job_repo),
    handle: JobWorkerHandle = Depends(get_job_worker),
) -> StreamingResponse:
    """Stream named SSE ``status`` / ``log`` events for ``job_id``.

    Replays the current state first (so a late-joining client still sees
    a ``running``/terminal status + buffered log lines), then drains the
    live per-job queue until the job reaches a terminal state.
    """
    _load_job(job_repo, job_id)  # 404 when unknown

    async def event_generator():
        # Reuse an already-registered queue when one exists so a live
        # (RUNNING) job's worker keeps pushing to the SAME queue this
        # stream drains. Registering a brand-new queue would clobber the
        # worker's captured reference and the stream would never see the
        # terminal event. Only create one when the job isn't claimed yet.
        queue = handle.event_queues.get(job_id)
        if queue is None:
            queue = asyncio.Queue()
            handle.register_queue(job_id, queue)
        try:
            # Replay current snapshot so the stream is useful to a
            # client that connects mid-flight. The stream always opens
            # with a ``running`` status frame (matching the spec's SSE
            # contract) even when the job has since reached a terminal
            # state.
            yield _sse_frame({"event": "status", "data": {"status": "running"}})
            job = job_repo.get(job_id)
            if job is not None:
                for line in job.log_lines:
                    yield _sse_frame({"event": "log", "data": {"line": line}})
                if job.status in _TERMINAL_STATUSES:
                    data: dict[str, Any] = {"status": job.status.value}
                    if job.status is JobStatus.SUCCEEDED and job.result_summary:
                        data["summary"] = dict(job.result_summary)
                    if job.status is JobStatus.FAILED and job.error:
                        data["error"] = job.error
                    yield _sse_frame({"event": "status", "data": data})
                    return
            # Drain live events until terminal.
            while True:
                event = await queue.get()
                yield _sse_frame(event)
                if event.get("event") == "status":
                    status = event["data"].get("status")
                    if status in (
                        JobStatus.SUCCEEDED.value,
                        JobStatus.FAILED.value,
                        JobStatus.CANCELLED.value,
                    ):
                        return
        finally:
            handle.unregister_queue(job_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


@router.post("/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    job_repo: JobRepository = Depends(get_job_repo),
    handle: JobWorkerHandle = Depends(get_job_worker),
) -> JSONResponse:
    """Request cooperative cancellation; ``200`` or ``409`` when terminal."""
    job = _load_job(job_repo, job_id)
    if job.status in _TERMINAL_STATUSES:
        raise JobAlreadyTerminalError(
            f"Job {job_id} is already {job.status.value}"
        )
    handle.cancel(job_id)
    return JSONResponse(content=_envelope(job))


__all__ = ["router"]
