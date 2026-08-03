"""MCP end-to-end integration tests (offline).

These exercise :func:`doc3gpp.web.mcp_server.build_mcp_server` against a
real SQLite engine seeded via ``create_schema`` (the same fixtures the
HTTP routes and the CLI share). Each test builds a ``WebState`` through
:func:`doc3gpp.web.app.build_state` — the exact composition the lifespan
uses — and asserts the MCP tool's JSON output matches the corresponding
HTTP ``?format=json`` surface, byte-for-byte where practical.

The ``mcp`` package is v2 (``MCPServer``), so we drive the server
directly with ``await server.list_tools()`` / ``await server.call_tool()``
rather than over the wire.
"""
from __future__ import annotations

from doc3gpp.web.app import build_state
from doc3gpp.web.mcp_server import build_mcp_server


def _state_and_server():
    from doc3gpp.settings.loader import get_settings
    from doc3gpp.storage.db.migrate import create_schema

    create_schema()
    state = build_state(get_settings())
    server = build_mcp_server(state)
    return state, server


def test_list_tools_exposes_read_and_job_tools(sqlite_env) -> None:
    import asyncio

    _, server = _state_and_server()

    async def run():
        return await server.list_tools()

    tools = asyncio.run(run())
    names = {t.name for t in tools}
    expected = {
        "list_meetings",
        "get_meeting",
        "list_tdocs",
        "get_tdoc",
        "get_tdoc_content",
        "list_tsgs",
        "get_tsg",
        "list_wis",
        "search_tdocs",
        "semantic_search_tdocs",
        "sync_meetings",
        "sync_tdocs",
        "sync_tdocs_by_meeting",
        "sync_all_tdocs",
        "parse_tdocs",
        "rebuild_search_index",
        "purge_cache",
        "get_job",
        "cancel_job",
        "list_jobs",
    }
    assert expected <= names


def test_call_list_meetings_empty(sqlite_env) -> None:
    """Empty result is a single ``[]`` JSON string (not zero content items)."""
    import asyncio

    _, server = _state_and_server()

    async def run():
        return await server.call_tool("list_meetings", {"limit": 5})

    result = asyncio.run(run())
    assert result.is_error is False
    assert result.content[0].text == "[]"


def test_call_list_tsgs_returns_rows(sqlite_env) -> None:
    """Parity: GET /tsgs?format=json shape (tsg_rows with default fields)."""
    import asyncio

    from doc3gpp.models.tsg import Tsg
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository

    state, server = _state_and_server()
    repo = SQLAlchemyTsgRepository()
    repo.upsert_many(
        [
            Tsg(tsg_name="SA", short_name="SA", description="Services"),
            Tsg(tsg_name="RAN", short_name="RAN", description="Radio"),
        ]
    )

    async def run():
        return await server.call_tool("list_tsgs", {})

    result = asyncio.run(run())
    assert result.is_error is False
    import json

    payload = json.loads(result.content[0].text)
    assert sorted(payload, key=lambda t: t["short_name"]) == [
        {"tsg_name": "RAN", "short_name": "RAN", "description": "Radio"},
        {"tsg_name": "SA", "short_name": "SA", "description": "Services"},
    ]
    get_engine.cache_clear()
    del state.engine


def test_job_tools_enqueue_and_poll(sqlite_env) -> None:
    """Job tools return the queued envelope and are observable via get_job."""
    import asyncio
    import json

    state, server = _state_and_server()

    async def run():
        created = await server.call_tool("sync_meetings", {"tsg": "SA2"})
        envelope = json.loads(created.content[0].text)
        assert envelope["status"] == "queued"
        assert "links" in envelope and envelope["links"]["self"].startswith("/jobs/")
        job_id = envelope["job_id"]
        detail = await server.call_tool("get_job", {"job_id": job_id})
        return created, detail

    created, detail = asyncio.run(run())
    assert created.is_error is False
    assert detail.is_error is False
    detail_payload = json.loads(detail.content[0].text)
    assert detail_payload["kind"] == "sync_meetings"
    assert detail_payload["params"] == {"tsg": "SA2"}
    del state.engine


def test_get_meeting_not_found_raises(sqlite_env) -> None:
    """Unknown meeting id propagates MeetingNotFoundError as a ToolError."""
    import asyncio

    import pytest
    from mcp.server.mcpserver.exceptions import ToolError

    _, server = _state_and_server()

    async def run():
        return await server.call_tool("get_meeting", {"meeting_id": 999999})

    with pytest.raises(ToolError):
        asyncio.run(run())


def _seed_corpus() -> None:
    """Seed one meeting + one tdoc + one wi so read tools return rows."""
    from doc3gpp.models.meeting import Meeting
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tsg import Tsg
    from doc3gpp.models.wi import Wi
    from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository
    from doc3gpp.storage.repositories.wi_sql import SQLAlchemyWiRepository

    SQLAlchemyTsgRepository().upsert_many(
        [Tsg(tsg_name="SA", short_name="SA", description="Services")],
    )

    SQLAlchemyMeetingRepository().upsert_many(
        [
            Meeting(
                meeting_id=156,
                name="SA2#156",
                title="SA2 meeting 156",
                location="online",
                start_date=None,
                end_date=None,
            ),
        ]
    )
    SQLAlchemyTDocRepository().upsert_many(
        [
            TDoc(
                tdoc_id="S2-260001",
                title="A test tdoc",
                meeting_id=156,
                ftp_url="TSG_SA/WG2_Arch/S2-260001.zip",
                source="Ericsson",
            ),
        ]
    )
    SQLAlchemyWiRepository().upsert_many(
        [Wi(wi_id=101, acronym="FS_NET", release="Rel-18", name="Network", tsg_short="SA")],
    )


def test_read_tools_parity_with_http_json(sqlite_env) -> None:
    """Read tools' JSON bytes match the matching HTTP ``?format=json`` route.

    This locks the AC9 byte-for-byte parity contract: the MCP tool result
    and the corresponding HTTP route must serialize identically
    (compact separators + ``ensure_ascii=False``).
    """
    import asyncio
    import json

    from fastapi.testclient import TestClient

    from doc3gpp.settings.schema import CacheSettings, MCPSettings, ServerSettings, Settings
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.web.app import build_app

    _state_and_server()  # runs create_schema()
    _seed_corpus()
    state, server = _state_and_server()
    app = build_app(
        Settings(
            server=ServerSettings(enabled=True, port=8765),
            mcp=MCPSettings(enabled=True),
            cache=CacheSettings(dir=state.settings.cache.dir),
        )
    )
    with TestClient(app) as client:
        cases = [
            ("list_meetings", {}, "/meetings?format=json"),
            ("list_tsgs", {}, "/tsgs?format=json"),
            ("list_wis", {}, "/wis?format=json"),
            ("list_tdocs", {}, "/tdocs?format=json"),
        ]

        async def call(name: str, args: dict):
            result = await server.call_tool(name, args)
            assert result.is_error is False, result
            return result.content[0].text

        for tool_name, args, route in cases:
            mcp_bytes = asyncio.run(call(tool_name, args))
            http_resp = client.get(route)
            assert http_resp.status_code == 200, http_resp.text
            http_bytes = http_resp.content.decode("utf-8")
            assert mcp_bytes == http_bytes, (
                f"{tool_name} parity broke: MCP={mcp_bytes!r} HTTP={http_bytes!r}"
            )

        # get_meeting wraps in {"meeting": ...}
        mcp_meeting = asyncio.run(call("get_meeting", {"meeting_id": 156}))
        http_meeting = client.get("/meetings/156?format=json").content.decode("utf-8")
        assert json.loads(mcp_meeting) == json.loads(http_meeting)

    get_engine.cache_clear()
    del state.engine
