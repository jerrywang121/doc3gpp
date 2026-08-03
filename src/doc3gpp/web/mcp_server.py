"""MCP server exposing the doc3gpp domain as tools over Streamable HTTP.

T9 supplies the real :class:`MCPServer` built from a live
:class:`WebState` (previously a placeholder with no services). Every
tool is a thin wrapper over an existing service method and returns the
same JSON shape as the matching HTTP route (``?format=json``), so the
MCP and HTTP surfaces are byte-for-byte parity (a ``-k mcp_end_to_end``
parity test guards this).

The installed ``mcp`` package is v2, which replaces ``FastMCP`` with
:class:`mcp.server.mcpserver.MCPServer`. We build a server and register
tools with :meth:`MCPServer.tool`; the server is stateless by default
(no ``stateless_http`` kwarg exists in v2), so the database is the
single source of state and a tool call observes whatever the worker or
CLI most recently wrote.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from doc3gpp.models.jobs import JobKind
from doc3gpp.web import render
from doc3gpp.web.errors import (
    CacheMissError,
    InvalidFilterError,
    JobAlreadyTerminalError,
    JobNotFoundError,
    MeetingNotFoundError,
    SettingsDisabledError,
    TDocNotFoundError,
    TSGNotFoundError,
)

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from doc3gpp.web.state import WebState

_MEETING_FIELDS = ["meeting_id", "name", "location", "start_date", "end_date", "ftp_url", "start_doc", "end_doc"]
_TDOC_FIELDS = ["tdoc_id", "meeting_name", "title", "source", "type", "status", "cr_cat", "spec", "version", "related_wis"]
_TSG_FIELDS = ["tsg_name", "short_name", "description"]
_WI_FIELDS = ["wi_id", "acronym", "release", "name"]

_SEARCH_FILTER_KEYS = ("tsg", "meeting", "meeting_id", "tdoc_id", "release", "spec", "since", "until")


def _to_json(value: Any) -> str:
    """Serialize a value to a single compact JSON string.

    MCP v2's :meth:`MCPServer.call_tool` emits **one** ``TextContent``
    item per element of a list-returning tool and **zero** items for an
    empty list. Returning a raw list therefore both drops HTTP parity
    (a single array) and produces an empty content list for empty
    results. Wrapping list (and other) payloads in a JSON string keeps a
    single ``TextContent`` item for every call and matches the HTTP
    ``?format=json`` surface byte-for-byte.
    """
    import json

    return json.dumps(value)


def _fts5_hit_to_json(hit: Any) -> dict[str, Any]:
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


def _semantic_hit_to_json(hit: Any) -> dict[str, Any]:
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


def _job_url(job_id: str) -> str:
    return f"/jobs/{job_id}"


def _enqueue(state: "WebState", kind: JobKind, params: dict[str, Any], message: str) -> dict[str, Any]:
    job = state.services.job_repo.create(kind, params)
    return {
        "job_id": job.id,
        "status": job.status.value,
        "message": message,
        "links": {
            "self": _job_url(job.id),
            "events": f"{_job_url(job.id)}/events",
        },
    }


def build_mcp_server(state: "WebState") -> "MCPServer":
    """Build and return an :class:`MCPServer` wired to ``state``.

    All tools are registered here so :mod:`doc3gpp.web.app` just needs
    to call ``build_mcp_server(state).streamable_http_app()`` and mount
    it. Raises ``ImportError`` when the ``mcp`` package is not
    installed so the caller can skip the mount gracefully.
    """
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("doc3gpp")
    services = state.services

    # ---- Meetings -------------------------------------------------
    @server.tool(name="list_meetings", description="List meetings, optionally filtered by TSG, name, location or year.")
    def list_meetings(
        tsg: str | None = None,
        name: str | None = None,
        location: str | None = None,
        year: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        meetings = services.meeting.list_recent(
            limit=limit,
            offset=offset,
            tsg=tsg,
            name_like=name,
            location_like=location,
            year=year,
        )
        return _to_json(render.meeting_rows(meetings, _MEETING_FIELDS))

    @server.tool(name="get_meeting", description="Get a single meeting by its numeric meeting id.")
    def get_meeting(meeting_id: int) -> dict[str, Any]:
        meeting = services.meeting.get_by_id(meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(str(meeting_id))
        return {"meeting": render.to_jsonable(meeting)}

    # ---- TDocs ----------------------------------------------------
    @server.tool(name="list_tdocs", description="List TDocs, optionally filtered by any tdoc field.")
    def list_tdocs(
        limit: int = 50,
        offset: int = 0,
        tdoc_id: str | None = None,
        meeting_like: str | None = None,
        meeting_id: int | None = None,
        status: str | None = None,
        cr_cat: str | None = None,
        spec: str | None = None,
        wi: str | None = None,
        revision_of: str | None = None,
        revised_to: str | None = None,
        title: str | None = None,
        source: str | None = None,
        tdoc_type: str | None = None,
        release: str | None = None,
        version: str | None = None,
        cr_num: str | None = None,
        cr_pack: str | None = None,
    ) -> str:
        rows = services.tdoc.list_recent_with_meeting(
            limit=limit,
            offset=offset,
            tdoc_id=tdoc_id,
            meeting_like=meeting_like,
            meeting_id=meeting_id,
            status=status,
            cr_cat=cr_cat,
            spec=spec,
            wi=wi,
            revision_of=revision_of,
            revised_to=revised_to,
            title=title,
            source=source,
            tdoc_type=tdoc_type,
            release=release,
            version=version,
            cr_num=cr_num,
            cr_pack=cr_pack,
        )
        return _to_json(render.tdoc_rows(rows, _TDOC_FIELDS))

    @server.tool(name="get_tdoc", description="Get a single tdoc by id, including its cover-page and extract details.")
    def get_tdoc(tdoc_id: str) -> dict[str, Any]:
        from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import SQLAlchemyTDocCrTtcnRepository
        from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import SQLAlchemyTDocCrChangeDetailsRepository
        from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
        from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
        from doc3gpp.web.routes.tdocs import TDocShowRepos, TDocShowRecord

        repos = TDocShowRepos(
            tdoc=SQLAlchemyTDocRepository(),
            cr=SQLAlchemyTDocCrRepository(),
            cr_ttcn=SQLAlchemyTDocCrTtcnRepository(),
            cr_change_details=SQLAlchemyTDocCrChangeDetailsRepository(),
            file=services.tdoc_file_repo,
        )
        record = TDocShowRecord.from_tdoc_id(tdoc_id, repos)
        return render.to_jsonable(record)

    @server.tool(name="get_tdoc_content", description="Return the cached markdown body for a tdoc id.")
    def get_tdoc_content(tdoc_id: str, format: str = "markdown") -> str:
        if format not in ("markdown", "html"):
            raise InvalidFilterError("format must be one of 'markdown'|'html'")
        from pathlib import Path

        from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
        from doc3gpp.web.routes.tdocs import _MD_RENDERER, derive_cache_file

        tdoc = SQLAlchemyTDocRepository().get_by_id(tdoc_id)
        if tdoc is None or not tdoc.ftp_url:
            raise TDocNotFoundError(tdoc_id)
        cache_file = derive_cache_file(tdoc.ftp_url)
        markdown_path = Path(state.settings.cache.dir) / "markdown" / cache_file
        if not markdown_path.exists():
            raise CacheMissError(
                f"No cached markdown for TDoc {tdoc_id}.",
                hint=f"run: doc3gpp tdoc parse --tdoc {tdoc_id}",
            )
        text = markdown_path.read_text(encoding="utf-8")
        if format == "html":
            return _MD_RENDERER.render(text)
        return text

    # ---- TSGs -----------------------------------------------------
    @server.tool(name="list_tsgs", description="List all standardisation groups (TSGs).")
    def list_tsgs() -> str:
        return _to_json(render.tsg_rows(services.tsg.list_all(), _TSG_FIELDS))

    @server.tool(name="get_tsg", description="Get a single TSG by short name, with its recent meetings.")
    def get_tsg(short_name: str) -> dict[str, Any]:
        tsg = services.tsg.get_by_short_name(short_name)
        if tsg is None:
            raise TSGNotFoundError(short_name)
        meetings = services.meeting.list_recent(tsg=tsg.short_name, limit=200)
        return {"tsg": render.to_jsonable(tsg), "meetings": render.to_jsonable(meetings)}

    # ---- WIs ------------------------------------------------------
    @server.tool(name="list_wis", description="List work items, optionally filtered by TSG, name, acronym or release.")
    def list_wis(
        tsg: str | None = None,
        name: str | None = None,
        acronym: str | None = None,
        release: str | None = None,
        limit: int = 50,
    ) -> str:
        wis = services.wi.list_recent(
            limit=limit,
            tsg=tsg,
            name_like=name,
            acronym_like=acronym,
            release_like=release,
        )
        return _to_json(render.wi_rows(wis, _WI_FIELDS))

    # ---- Search ---------------------------------------------------
    @server.tool(name="search_tdocs", description="Full-text (FTS5) search over tdoc text.")
    def search_tdocs(
        query: str,
        tsg: str | None = None,
        meeting: str | None = None,
        release: str | None = None,
        spec: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 20,
    ) -> str:
        if services.search is None:
            raise SettingsDisabledError("search is not available in this build")
        from doc3gpp.services.search_service import SearchFilters

        filters = SearchFilters(tsg=tsg, meeting=meeting, release=release, spec=spec, since=since, until=until, limit=limit)
        hits = services.search.search(query, filters)
        return _to_json([_fts5_hit_to_json(h) for h in hits])

    @server.tool(name="semantic_search_tdocs", description="Semantic (embedding) search over tdoc text.")
    def semantic_search_tdocs(
        query: str,
        limit: int = 20,
        fts5_weight: float = 0.5,
    ) -> str:
        if services.semantic_search is None:
            raise SettingsDisabledError("semantic search is not available in this build")
        if not 0.0 <= fts5_weight <= 1.0:
            raise InvalidFilterError("fts5_weight must be between 0.0 and 1.0")
        from doc3gpp.services.search_service import SearchFilters

        filters = SearchFilters(limit=limit)
        hits = services.semantic_search.search(query, None, filters=filters, limit=limit, fts5_weight=fts5_weight)
        return _to_json([_semantic_hit_to_json(h) for h in hits])

    # ---- Jobs -----------------------------------------------------
    @server.tool(name="sync_meetings", description="Enqueue a meeting-calendar sync for a TSG.")
    def sync_meetings(tsg: str) -> dict[str, Any]:
        if not tsg:
            raise InvalidFilterError("tsg is required")
        return _enqueue(state, JobKind.SYNC_MEETINGS, {"tsg": tsg}, f"queued sync_meetings for TSG {tsg}")

    @server.tool(name="sync_tdocs", description="Enqueue a tdoc-list sync for a meeting id.")
    def sync_tdocs(meeting_id: int | None = None) -> dict[str, Any]:
        if meeting_id is None:
            raise InvalidFilterError("meeting_id is required")
        return _enqueue(state, JobKind.SYNC_TDOCS, {"meeting_id": meeting_id}, f"queued sync_tdocs for meeting {meeting_id}")

    @server.tool(name="sync_tdocs_by_meeting", description="Enqueue a tdoc-list sync for a meeting by name.")
    def sync_tdocs_by_meeting(meeting: str | None = None) -> dict[str, Any]:
        if not meeting:
            raise InvalidFilterError("meeting is required")
        return _enqueue(state, JobKind.SYNC_TDOCS, {"meeting_name": meeting}, f"queued sync_tdocs for meeting {meeting}")

    @server.tool(name="sync_all_tdocs", description="Enqueue a bulk sync of every tracked meeting's tdocs.")
    def sync_all_tdocs() -> dict[str, Any]:
        return _enqueue(state, JobKind.SYNC_TDOCS_ALL, {"force": False}, "queued sync_all_tdocs")

    @server.tool(name="parse_tdocs", description="Enqueue extraction of tdoc cover pages + change details.")
    def parse_tdocs(
        filter: dict[str, Any] | None = None,
        force: bool = False,
        full: bool = False,
        max_batch: int | None = None,
    ) -> dict[str, Any]:
        if not filter:
            raise InvalidFilterError("filter is required")
        params: dict[str, Any] = {"filter": filter, "force": force, "full": full}
        if max_batch is not None:
            params["max_batch"] = max_batch
        return _enqueue(state, JobKind.PARSE_TDOCS, params, "queued parse_tdocs")

    @server.tool(name="rebuild_search_index", description="Enqueue an FTS5 search-index rebuild.")
    def rebuild_search_index(stale_only: bool = False, resume: bool = False) -> dict[str, Any]:
        return _enqueue(state, JobKind.REBUILD_SEARCH, {"stale_only": stale_only, "resume": resume}, "queued rebuild_search_index")

    @server.tool(name="purge_cache", description="Enqueue a cache purge (scope: markdown, zips or all).")
    def purge_cache(scope: str = "markdown", yes: bool = False) -> dict[str, Any]:
        if not yes:
            raise InvalidFilterError("yes must be true to purge the cache")
        if scope not in ("markdown", "zips", "all"):
            raise InvalidFilterError("scope must be one of 'markdown'|'zips'|'all'")
        return _enqueue(state, JobKind.CACHE_PURGE, {"scope": scope}, f"queued purge_cache ({scope})")

    @server.tool(name="get_job", description="Get a job's full detail by id.")
    def get_job(job_id: str) -> dict[str, Any]:
        job = state.services.job_repo.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        from doc3gpp.web.routes.jobs import _envelope

        return _envelope(job)

    @server.tool(name="cancel_job", description="Request cooperative cancellation of a queued or running job.")
    def cancel_job(job_id: str) -> dict[str, Any]:
        from doc3gpp.models.jobs import JobStatus

        job = state.services.job_repo.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED):
            raise JobAlreadyTerminalError(job_id)
        state.jobs.cancel(job_id)
        from doc3gpp.web.routes.jobs import _envelope

        return _envelope(job)

    @server.tool(name="list_jobs", description="List recent jobs (newest first), optionally filtered by status.")
    def list_jobs(status: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        from doc3gpp.models.jobs import JobStatus
        from doc3gpp.web.routes.jobs import _envelope

        parsed = None
        if status is not None:
            try:
                parsed = JobStatus(status)
            except ValueError:
                raise InvalidFilterError(f"unknown job status: {status!r}")
        fetched = state.services.job_repo.list(limit=limit + offset, status=parsed)
        jobs = fetched[offset : offset + limit]
        return {
            "jobs": [_envelope(j) for j in jobs],
            "total": len(jobs),
            "limit": limit,
            "offset": offset,
        }

    return server


__all__ = ["build_mcp_server"]
