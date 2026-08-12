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

import functools
from typing import TYPE_CHECKING, Annotated, Any, Callable, TypeVar

from pydantic import Field

from doc3gpp.models.jobs import JobKind
from doc3gpp.web import render
from doc3gpp.web.errors import (
    CacheMissError,
    InvalidFilterError,
    JobAlreadyTerminalError,
    JobNotFoundError,
    MeetingNotFoundError,
    SettingsDisabledError,
    SpecNotFoundError,
    TDocNotFoundError,
    TSGNotFoundError,
    map_mcp_error,
)

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from doc3gpp.web.state import WebState

_MEETING_FIELDS = ["meeting_id", "name", "location", "start_date", "end_date", "ftp_url", "start_doc", "end_doc"]
_TDOC_FIELDS = ["tdoc_id", "meeting_name", "title", "source", "type", "status", "cr_cat", "spec", "version", "related_wis"]
_TSG_FIELDS = ["tsg_name", "short_name", "description"]
_WI_FIELDS = ["wi_id", "acronym", "release", "name"]
_SPEC_FIELDS = ["spec_id", "type", "title", "status", "radio_tech", "initial_release", "tsg", "rapporteurs"]
_VERSION_FIELDS = ["version", "release", "ftp_url", "meeting_id", "meeting_name", "upload_date", "pdf_url", "crs"]

_SEARCH_FILTER_KEYS = ("tsg", "meeting", "meeting_id", "tdoc_id", "release", "spec", "since", "until")

_F = TypeVar("_F", bound=Callable[..., Any])


def _package_version() -> str:
    """Return the installed ``doc3gpp`` distribution version.

    Prefers ``importlib.metadata`` (authoritative — matches the
    ``pyproject.toml`` version) and falls back to the package
    ``__version__`` constant when the distribution metadata is
    unavailable (e.g. running from a source checkout without an
    editable install). Feeds the MCP ``serverInfo.version`` block.
    """
    try:
        from importlib.metadata import version

        return version("doc3gpp")
    except Exception:  # noqa: BLE001 - best-effort version probe
        from doc3gpp import __version__

        return __version__


def _mcp_error_guard(fn: _F) -> _F:
    """Translate domain exceptions into MCP JSON-RPC protocol errors.

    The MCP SDK's ``Tool.run`` re-raises any :class:`MCPError` raised by
    a tool body as a JSON-RPC protocol error (so a client sees a proper
    ``code``), whereas any other exception becomes an ``isError``
    ``ToolError`` with no code. This decorator sits below
    ``@server.tool(...)`` so every mapped domain exception surfaces with
    the design spec's ``-320xx`` / ``-326xx`` codes instead of a bare
    tool error. Unmapped exceptions are re-raised unchanged (the SDK
    falls back to ``ToolError``).
    """
    from mcp.shared.exceptions import MCPError

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except MCPError:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberate transport boundary
            mapped = map_mcp_error(exc)
            if mapped is not None:
                code, message, data = mapped
                raise MCPError(code=code, message=message, data=data) from exc
            raise

    return wrapper  # type: ignore[return-value]


def _to_json(value: Any) -> str:
    """Serialize a value to a single compact JSON string.

    MCP v2's :meth:`MCPServer.call_tool` emits **one** ``TextContent``
    item per element of a list-returning tool and **zero** items for an
    empty list. Returning a raw list therefore both drops HTTP parity
    (a single array) and produces an empty content list for empty
    results. Wrapping list (and other) payloads in a JSON string keeps a
    single ``TextContent`` item for every call and matches the HTTP
    ``?format=json`` surface byte-for-byte.

    The compact separators and ``ensure_ascii=False`` deliberately mirror
    Starlette's :class:`~starlette.responses.JSONResponse` (which is what
    the HTTP ``?format=json`` routes render through) so the two surfaces
    produce identical bytes.
    """
    import json

    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


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


def _enqueue(state: "WebState", kind: JobKind, params: dict[str, Any], message: str) -> str:
    job = state.services.job_repo.create(kind, params)
    return _to_json(
        {
            "job_id": job.id,
            "status": job.status.value,
            "message": message,
            "links": {
                "self": _job_url(job.id),
                "events": f"{_job_url(job.id)}/events",
            },
        }
    )


def build_mcp_server(state: "WebState") -> "MCPServer":
    """Build and return an :class:`MCPServer` wired to ``state``.

    All tools are registered here so :mod:`doc3gpp.web.app` just needs
    to call ``build_mcp_server(state).streamable_http_app()`` and mount
    it. Raises ``ImportError`` when the ``mcp`` package is not
    installed so the caller can skip the mount gracefully.
    """
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        "doc3gpp",
        version=_package_version(),
        title="doc3gpp MCP",
        description=(
            "3GPP TDoc, meeting, WI and search tools over the doc3gpp "
            "database (scraped from 3gpp.org)."
        ),
        website_url="https://github.com/jerrywang121/doc3gpp",
    )
    services = state.services

    # ---- Meetings -------------------------------------------------
    @server.tool(name="list_meetings", description="List meetings, optionally filtered by TSG, name, location or year. The tsg, name and location filters support Rich filter patterns: SQL LIKE patterns: use % as a wildcard (e.g. name='%handover%' matches any name containing 'handover'); a leading ! flips to NOT LIKE; 'null'/'not-null' match column nullability. A plain value with no wildcard still matches exactly.")
    @_mcp_error_guard
    def list_meetings(
        tsg: Annotated[str | None, Field(description="Rich filter patterns on the TSG short name (e.g. 'R5', '%RAN%').")] = None,
        name: Annotated[str | None, Field(description="Rich filter patterns on the meeting name (e.g. '%RAN%').")] = None,
        location: Annotated[str | None, Field(description="Rich filter patterns on the meeting location (e.g. '%Edinburgh%').")] = None,
        year: Annotated[int | None, Field(description="Match meetings whose end date falls in this year.")] = None,
        limit: Annotated[int, Field(description="Maximum number of meetings to return.")] = 50,
        offset: Annotated[int, Field(description="Number of meetings to skip for pagination.")] = 0,
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
    @_mcp_error_guard
    def get_meeting(meeting_id: Annotated[int, Field(description="Numeric 3GPP meeting id (e.g. 260013).")]) -> str:
        meeting = services.meeting.get_by_id(meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(str(meeting_id))
        return _to_json({"meeting": render.to_jsonable(meeting)})

    # ---- TDocs ----------------------------------------------------
    @server.tool(name="list_tdocs", description="List TDocs, optionally filtered by any tdoc field. The tdoc_id, meeting, title, source, spec, wi, release, version, cr_num, cr_pack, status, cr_cat, tdoc_type and revision_of/revised_to filters support Rich filter patterns: SQL LIKE patterns: use % as a wildcard (e.g. title='%handover%' matches any title containing 'handover'); a leading ! flips to NOT LIKE; 'null'/'not-null' match column nullability. A plain value with no wildcard still matches exactly.")
    @_mcp_error_guard
    def list_tdocs(
        limit: Annotated[int, Field(description="Maximum number of tdocs to return.")] = 50,
        offset: Annotated[int, Field(description="Number of tdocs to skip for pagination.")] = 0,
        tdoc_id: Annotated[str | None, Field(description="Rich filter on the tdoc id (e.g. 'R5-26%').")] = None,
        meeting: Annotated[str | None, Field(description="Rich filter on the parent meeting name (e.g. '%RAN%')")] = None,
        meeting_id: Annotated[int | None, Field(description="Exact numeric meeting id filter.")] = None,
        status: Annotated[str | None, Field(description="Rich filter on status (e.g. 'Approved').")] = None,
        cr_cat: Annotated[str | None, Field(description="Rich filter on CR category (e.g. 'A').")] = None,
        spec: Annotated[str | None, Field(description="Rich filter on the spec (e.g. '38.300').")] = None,
        wi: Annotated[str | None, Field(description="Rich filter on the work item (e.g. '%MIMO%').")] = None,
        revision_of: Annotated[str | None, Field(description="Rich filter on the tdoc this revises (e.g. 'R5-260001').")] = None,
        revised_to: Annotated[str | None, Field(description="Rich filter on the tdoc that revises this one.")] = None,
        title: Annotated[str | None, Field(description="Rich filter on the title (e.g. '%handover%').")] = None,
        source: Annotated[str | None, Field(description="Rich filter on the source (e.g. '%Ericsson%').")] = None,
        tdoc_type: Annotated[str | None, Field(description="Rich filter on the tdoc type (e.g. 'CR').")] = None,
        release: Annotated[str | None, Field(description="Rich filter on the release (e.g. 'Rel-17').")] = None,
        version: Annotated[str | None, Field(description="Rich filter on the version (e.g. '17.1.0').")] = None,
        cr_num: Annotated[str | None, Field(description="Rich filter on the CR number.")] = None,
        cr_pack: Annotated[str | None, Field(description="Rich filter on the CR pack.")] = None,
    ) -> str:
        rows = services.tdoc.list_recent_with_meeting(
            limit=limit,
            offset=offset,
            tdoc_id=tdoc_id,
            meeting_like=meeting,
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
    @_mcp_error_guard
    def get_tdoc(tdoc_id: Annotated[str, Field(description="Canonical tdoc id (e.g. 'R5-260013').")]) -> str:
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
        return _to_json(render.to_jsonable(record))

    @server.tool(name="get_tdoc_content", description="Return the cached markdown body for a tdoc id.")
    @_mcp_error_guard
    def get_tdoc_content(
        tdoc_id: Annotated[str, Field(description="Canonical tdoc id (e.g. 'R5-260013').")],
        format: Annotated[str, Field(description="Output format: 'markdown' (default) or 'html'.")] = "markdown",
    ) -> str:
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
    @_mcp_error_guard
    def list_tsgs() -> str:
        return _to_json(render.tsg_rows(services.tsg.list_all(), _TSG_FIELDS))

    @server.tool(name="get_tsg", description="Get a single TSG by short name, with its recent meetings.")
    @_mcp_error_guard
    def get_tsg(short_name: Annotated[str, Field(description="TSG short name (e.g. 'R5', 'CT1').")]) -> str:
        tsg = services.tsg.get_by_short_name(short_name)
        if tsg is None:
            raise TSGNotFoundError(short_name)
        meetings = services.meeting.list_recent(tsg=tsg.short_name, limit=200)
        return _to_json({"tsg": render.to_jsonable(tsg), "meetings": render.to_jsonable(meetings)})

    # ---- WIs ------------------------------------------------------
    @server.tool(name="list_wis", description="List work items, optionally filtered by TSG, name, acronym or release. The name, acronym and release filters support Rich filter patterns: SQL LIKE patterns: use % as a wildcard (e.g. name='%handover%' matches any name containing 'handover'); a leading ! flips to NOT LIKE; 'null'/'not-null' match column nullability. A plain value with no wildcard still matches exactly.")
    @_mcp_error_guard
    def list_wis(
        tsg: Annotated[str | None, Field(description="TSG short name filter (e.g. 'R5').")] = None,
        name: Annotated[str | None, Field(description="Rich filter pattern on the WI name (e.g. '%MIMO%').")] = None,
        acronym: Annotated[str | None, Field(description="Rich filter pattern on the WI acronym (e.g. '%NR%').")] = None,
        release: Annotated[str | None, Field(description="Rich filter pattern on the release (e.g. 'Rel-17').")] = None,
        limit: Annotated[int, Field(description="Maximum number of WIs to return.")] = 50,
    ) -> str:
        wis = services.wi.list_recent(
            limit=limit,
            tsg=tsg,
            name_like=name,
            acronym_like=acronym,
            release_like=release,
        )
        return _to_json(render.wi_rows(wis, _WI_FIELDS))

    # ---- Specs ----------------------------------------------------
    @server.tool(name="list_specs", description="List 3GPP specifications, optionally filtered by TSG, type, spec id, title, status, radio technology, initial release, related WIs or rapporteurs. The spec_id, title, status, radio_tech, initial_release, wis and rapporteurs filters support Rich filter patterns: SQL LIKE patterns: use % as a wildcard (e.g. spec_id='36.579%' matches any spec id starting with '36.579'); a leading ! flips to NOT LIKE; 'null'/'not-null' match column nullability. A plain value with no wildcard still matches exactly.")
    @_mcp_error_guard
    def list_specs(
        tsg: Annotated[str | None, Field(description="TSG short name filter (e.g. 'R5').")] = None,
        type: Annotated[str | None, Field(description="Spec type filter: 'TS' or 'TR'.")] = None,
        spec_id: Annotated[str | None, Field(description="Rich filter pattern on the spec id (e.g. '36.579-5').")] = None,
        title: Annotated[str | None, Field(description="Rich filter pattern on the title.")] = None,
        status: Annotated[str | None, Field(description="Rich filter pattern on the status.")] = None,
        radio_tech: Annotated[str | None, Field(description="Rich filter pattern on radio technologies.")] = None,
        initial_release: Annotated[str | None, Field(description="Rich filter pattern on the initial release (e.g. 'Rel-20').")] = None,
        wis: Annotated[str | None, Field(description="Rich filter pattern on related WIs.")] = None,
        rapporteurs: Annotated[str | None, Field(description="Rich filter pattern on rapporteurs.")] = None,
        limit: Annotated[int, Field(description="Maximum number of specs to return.")] = 50,
        offset: Annotated[int, Field(description="Number of specs to skip for pagination.")] = 0,
    ) -> str:
        specs = services.spec.list_recent(
            limit=limit, offset=offset, tsg=tsg, type=type, spec_id=spec_id,
            title=title, status=status, radio_tech=radio_tech,
            initial_release=initial_release, wis=wis, rapporteurs=rapporteurs,
        )
        return _to_json(render.spec_rows(specs, _SPEC_FIELDS))

    @server.tool(name="get_spec", description="Get a single spec by its dotted id, including its version rows. Set no_wis_crs to drop the 'wis' header field and per-version 'crs' field; version is a rich filter pattern (e.g. '19.%').")
    @_mcp_error_guard
    def get_spec(
        spec_id: Annotated[str, Field(description="Dotted spec id (e.g. '36.579-5').")],
        limit: Annotated[int, Field(description="Maximum number of version rows to return.")] = 10,
        offset: Annotated[int, Field(description="Number of version rows to skip for pagination.")] = 0,
        version: Annotated[str | None, Field(description="Rich filter pattern on the version (e.g. '19.%').")] = None,
        no_wis_crs: Annotated[bool, Field(description="Drop the 'wis' header field and per-version 'crs' field.")] = False,
    ) -> str:
        spec = services.spec.get(spec_id)
        if spec is None:
            raise SpecNotFoundError(spec_id)
        versions = services.spec.list_versions(
            spec_id, limit=limit, offset=offset, version=version
        )
        spec_fields = [f for f in _SPEC_FIELDS if not (no_wis_crs and f == "wis")]
        version_fields = [f for f in _VERSION_FIELDS if not (no_wis_crs and f == "crs")]
        return _to_json({
            "spec": {f: getattr(spec, f, None) for f in spec_fields},
            "versions": render.spec_version_rows(versions, version_fields),
        })

    # ---- Search ---------------------------------------------------
    @server.tool(name="search_tdocs", description="Full-text (FTS5) search over tdoc text. Optional filters on tsg, meeting, release, spec support Rich filter patterns: SQL LIKE patterns: use % as a wildcard (e.g. name='%handover%' matches any name containing 'handover'); a leading ! flips to NOT LIKE; 'null'/'not-null' match column nullability. A plain value with no wildcard still matches exactly.")
    @_mcp_error_guard
    def search_tdocs(
        query: Annotated[str, Field(description='Full-text query with FTS5 MATCH expression over tdoc text, phrases shall be wrapped with double quotes, support AND, OR and NOT (e.g. \'handover AND beamforming NOT "CSI report"\').')],
        tsg: Annotated[str | None, Field(description="Exact TSG short name filter (e.g. 'R5').")] = None,
        meeting: Annotated[str | None, Field(description="Rich filter on the meeting name or title")] = None,
        release: Annotated[str | None, Field(description="Rich filter pattern on the release (e.g. 'Rel-17').")] = None,
        spec: Annotated[str | None, Field(description="Rich filter pattern on the spec (e.g. '38.300').")] = None,
        since: Annotated[str | None, Field(description="Earliest uploaded_date (ISO 'YYYY-MM-DD').")] = None,
        until: Annotated[str | None, Field(description="Latest uploaded_date (ISO 'YYYY-MM-DD').")] = None,
        limit: Annotated[int, Field(description="Maximum number of hits to return.")] = 20,
        sem_query: Annotated[str | None, Field(description="Optional semantic rerank query; when set, results are reranked by embedding similarity to this text.")] = None,
    ) -> str:
        if services.search is None:
            raise SettingsDisabledError("search is not available in this build")
        from doc3gpp.services.search_service import SearchFilters

        filters = SearchFilters(tsg=tsg, meeting=meeting, release=release, spec=spec, since=since, until=until, limit=limit)
        hits = services.search.search(query, filters, sem_query=sem_query)
        return _to_json([_fts5_hit_to_json(h) for h in hits])

    @server.tool(name="semantic_search_tdocs", description="Semantic (embedding) search over tdoc text with natural-language query, optionally blended with an FTS5 query via reciprocal-rank fusion (RRF). Optional filters on tsg, meeting, release, spec support Rich filter patterns: SQL LIKE patterns: use % as a wildcard (e.g. name='%handover%' matches any name containing 'handover'); a leading ! flips to NOT LIKE; 'null'/'not-null' match column nullability. A plain value with no wildcard still matches exactly.")
    @_mcp_error_guard
    def semantic_search_tdocs(
        query: Annotated[str, Field(description="Natural-language semantic query over tdoc text (e.g. 'handover signalling procedures').")],
        fts5_query: Annotated[str | None, Field(description="Optional FTS5 MATCH expression, support AND, OR and NOT (e.g. 'handover AND beamforming NOT \"CSI report\"'). When omitted, only embedding-KNN runs (no RRF). When supplied, results are merged with the vector ranking via RRF.")] = None,
        tsg: Annotated[str | None, Field(description="Exact TSG short name filter (e.g. 'R5').")] = None,
        meeting: Annotated[str | None, Field(description="Rich filter on the meeting name or title")] = None,
        meeting_id: Annotated[int | None, Field(description="Exact numeric meeting id filter.")] = None,
        tdoc_id: Annotated[str | None, Field(description="Exact tdoc id filter (e.g. 'R5-260013').")] = None,
        release: Annotated[str | None, Field(description="Rich filter pattern on the release (e.g. 'Rel-17').")] = None,
        spec: Annotated[str | None, Field(description="Rich filter pattern on the spec (e.g. '38.300').")] = None,
        since: Annotated[str | None, Field(description="Earliest uploaded_date (ISO 'YYYY-MM-DD').")] = None,
        until: Annotated[str | None, Field(description="Latest uploaded_date (ISO 'YYYY-MM-DD').")] = None,
        limit: Annotated[int, Field(description="Maximum number of hits to return.")] = 20,
        fts5_weight: Annotated[float, Field(description="Blend weight (0.0..1.0) for the FTS5 rank in RRF; the vector weight is 1 - fts5_weight. Ignored when fts5_query is omitted.")] = 0.5,
    ) -> str:
        if services.semantic_search is None:
            raise SettingsDisabledError("semantic search is not available in this build")
        if not 0.0 <= fts5_weight <= 1.0:
            raise InvalidFilterError("fts5_weight must be between 0.0 and 1.0")
        from doc3gpp.models.search import SearchFilters

        filters = SearchFilters(
            tsg=tsg, meeting=meeting, meeting_id=meeting_id, tdoc_id=tdoc_id,
            release=release, spec=spec, since=since, until=until, limit=limit,
        )
        hits = services.semantic_search.search(
            query, fts5_query=fts5_query, filters=filters,
            limit=limit, fts5_weight=fts5_weight,
        )
        return _to_json([_semantic_hit_to_json(h) for h in hits])

    # ---- Jobs -----------------------------------------------------
    @server.tool(name="sync_meetings", description="Enqueue a meeting-calendar sync for a TSG.")
    @_mcp_error_guard
    def sync_meetings(tsg: Annotated[str, Field(description="TSG short name to sync the meeting calendar for (e.g. 'R5').")]) -> str:
        if not tsg:
            raise InvalidFilterError("tsg is required")
        return _enqueue(state, JobKind.SYNC_MEETINGS, {"tsg": tsg}, f"queued sync_meetings for TSG {tsg}")

    @server.tool(name="sync_tdocs", description="Enqueue a tdoc-list sync for a meeting id.")
    @_mcp_error_guard
    def sync_tdocs(meeting_id: Annotated[int | None, Field(description="Numeric meeting id to sync the tdoc list for.")] = None) -> str:
        if meeting_id is None:
            raise InvalidFilterError("meeting_id is required")
        return _enqueue(state, JobKind.SYNC_TDOCS, {"meeting_id": meeting_id}, f"queued sync_tdocs for meeting {meeting_id}")

    @server.tool(name="sync_tdocs_by_meeting", description="Enqueue a tdoc-list sync for a meeting by name.")
    @_mcp_error_guard
    def sync_tdocs_by_meeting(meeting: Annotated[str | None, Field(description="Meeting name to sync the tdoc list for (e.g. 'RAN5#106').")] = None) -> str:
        if not meeting:
            raise InvalidFilterError("meeting is required")
        return _enqueue(state, JobKind.SYNC_TDOCS, {"meeting_name": meeting}, f"queued sync_tdocs for meeting {meeting}")

    @server.tool(name="sync_all_tdocs", description="Enqueue a bulk sync of every tracked meeting's tdocs.")
    @_mcp_error_guard
    def sync_all_tdocs() -> str:
        return _enqueue(state, JobKind.SYNC_TDOCS_ALL, {"force": False}, "queued sync_all_tdocs")

    @server.tool(name="parse_tdocs", description="Enqueue extraction of tdoc cover pages + change details.")
    @_mcp_error_guard
    def parse_tdocs(
        filter: Annotated[dict[str, Any] | None, Field(description="Filter dict selecting which tdocs to parse. Supported keys: tdoc_id, meeting, meeting_id, status, cr_cat, spec, wi, revision_of, revised_to, title, ftp_url, source, tdoc_type, uploaded_date, release, version, cr_num, cr_pack. Text fields (all except meeting_id) are SQL LIKE patterns: use % as a wildcard (e.g. {'tdoc_id': 'R5-26%', 'meeting': '%RAN%', 'title': '%handover%'}); a leading ! flips to NOT LIKE; 'null'/'not-null' match column nullability; uploaded_date accepts '<op> YYYY-MM-DD'. A plain value with no wildcard still matches exactly.")] = None,
        force: Annotated[bool, Field(description="Re-parse tdocs already present in the cover-page table.")] = False,
        full: Annotated[bool, Field(description="Parse full content (not just cover page + change details).")] = False,
        max_batch: Annotated[int | None, Field(description="Cap on the number of tdocs parsed in this job.")] = None,
    ) -> str:
        if not filter:
            raise InvalidFilterError("filter is required")
        params: dict[str, Any] = {"filter": filter, "force": force, "full": full}
        if max_batch is not None:
            params["max_batch"] = max_batch
        return _enqueue(state, JobKind.PARSE_TDOCS, params, "queued parse_tdocs")

    @server.tool(name="rebuild_search_index", description="Enqueue an FTS5 search-index rebuild.")
    @_mcp_error_guard
    def rebuild_search_index(
        stale_only: Annotated[bool, Field(description="Only re-index tdocs uploaded since the last index.")] = False,
        resume: Annotated[bool, Field(description="Resume from the last indexed tdoc instead of starting fresh.")] = False,
    ) -> str:
        return _enqueue(state, JobKind.REBUILD_SEARCH, {"stale_only": stale_only, "resume": resume}, "queued rebuild_search_index")

    @server.tool(name="purge_cache", description="Enqueue a cache purge (scope: markdown, zips or all).")
    @_mcp_error_guard
    def purge_cache(
        scope: Annotated[str, Field(description="Cache scope to purge: 'markdown', 'zips' or 'all'.")] = "markdown",
        yes: Annotated[bool, Field(description="Must be true to actually purge the cache.")] = False,
    ) -> str:
        if not yes:
            raise InvalidFilterError("yes must be true to purge the cache")
        if scope not in ("markdown", "zips", "all"):
            raise InvalidFilterError("scope must be one of 'markdown'|'zips'|'all'")
        return _enqueue(state, JobKind.CACHE_PURGE, {"scope": scope}, f"queued purge_cache ({scope})")

    @server.tool(name="get_job", description="Get a job's full detail by id.")
    @_mcp_error_guard
    def get_job(job_id: Annotated[str, Field(description="Job id (UUID4 hex string).")]) -> str:
        job = state.services.job_repo.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        from doc3gpp.web.routes.jobs import _envelope

        return _to_json(_envelope(job))

    @server.tool(name="cancel_job", description="Request cooperative cancellation of a queued or running job.")
    @_mcp_error_guard
    def cancel_job(job_id: Annotated[str, Field(description="Job id (UUID4 hex string) to cancel.")]) -> str:
        from doc3gpp.models.jobs import JobStatus

        job = state.services.job_repo.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED):
            raise JobAlreadyTerminalError(job_id)
        state.jobs.cancel(job_id)
        from doc3gpp.web.routes.jobs import _envelope

        return _to_json(_envelope(job))

    @server.tool(name="list_jobs", description="List recent jobs (newest first), optionally filtered by status.")
    @_mcp_error_guard
    def list_jobs(
        status: Annotated[str | None, Field(description="Filter by job status: 'queued', 'running', 'succeeded', 'failed' or 'cancelled'.")] = None,
        limit: Annotated[int, Field(description="Maximum number of jobs to return.")] = 50,
        offset: Annotated[int, Field(description="Number of jobs to skip for pagination.")] = 0,
    ) -> str:
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
        return _to_json(
            {
                "jobs": [_envelope(j) for j in jobs],
                "total": len(jobs),
                "limit": limit,
                "offset": offset,
            }
        )

    return server


__all__ = ["build_mcp_server"]
