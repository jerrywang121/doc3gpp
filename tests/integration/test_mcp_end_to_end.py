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

import json

import pytest

from doc3gpp.services.spec_service import (
    SpecUnknownOnUpstreamError,
)
from doc3gpp.web.app import build_state
from doc3gpp.web.errors import map_domain_error, map_mcp_error
from doc3gpp.web.mcp_server import build_mcp_server


def _state_and_server():
    from doc3gpp.settings.loader import get_settings
    from doc3gpp.storage.db.migrate import create_schema

    create_schema()
    state = build_state(get_settings())
    server = build_mcp_server(state)
    return state, server


def test_mcp_server_info_identity(sqlite_env) -> None:
    """The MCP ``serverInfo`` block carries the package identity.

    The SDK derives ``serverInfo`` from the ``MCPServer`` constructor's
    identity fields (name / version / title / description / website_url),
    which feed the ``initialize`` response on both the streamable_http
    and sse transports.
    """
    from doc3gpp.web.mcp_server import _package_version

    _, server = _state_and_server()
    assert server.name == "doc3gpp"
    assert server.version == _package_version()
    assert server.version  # non-empty (e.g. "0.1.1")


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
        "list_specs",
        "get_spec",
        "list_testcases",
        "get_testcase",
        "search_tdoc",
        "semantic_search_tdoc",
        "sync_meetings",
        "sync_tdocs",
        "sync_tdocs_by_meeting",
        "sync_all_tdocs",
        "sync_specs",
        "sync_testcases",
        "parse_tdocs",
        "parse_tdoc_url",
        "parse_spec_docs",
        "rebuild_tdoc_search_index",
        "purge_cache",
        "get_job",
        "cancel_job",
        "list_jobs",
    }
    assert expected <= names
    assert "search_tdocs" not in names
    assert "semantic_search_tdocs" not in names
    assert "rebuild_search_index" not in names
    assert "parse_spec_docs" in names
    assert not any(name.startswith("fetch_spec") for name in names)


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


def test_rebuild_tdoc_search_index_enqueues(sqlite_env) -> None:
    """The TDoc search-index MCP tool preserves its job payload and kind."""
    import asyncio
    import json

    state, server = _state_and_server()

    async def run():
        created = await server.call_tool(
            "rebuild_tdoc_search_index",
            {"stale_only": True, "resume": True},
        )
        envelope = json.loads(created.content[0].text)
        detail = await server.call_tool(
            "get_job", {"job_id": envelope["job_id"]}
        )
        return envelope, detail

    envelope, detail = asyncio.run(run())
    assert envelope["status"] == "queued"
    assert envelope["message"] == "queued rebuild_tdoc_search_index"
    assert detail.is_error is False
    detail_payload = json.loads(detail.content[0].text)
    assert detail_payload["kind"] == "rebuild_search"
    assert detail_payload["params"] == {"stale_only": True, "resume": True}
    del state.engine


def test_sync_specs_tool_enqueues(sqlite_env) -> None:
    """``sync_specs`` MCP tool returns the queued envelope."""
    import asyncio
    import json

    state, server = _state_and_server()

    async def run():
        created = await server.call_tool("sync_specs", {"tsg": "R5", "force": True})
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
    assert detail_payload["kind"] == "sync_specs"
    assert detail_payload["params"] == {"tsg": "R5", "force": True, "per_version_details": False}
    del state.engine


def test_sync_specs_tool_by_spec_id_enqueues(sqlite_env) -> None:
    import asyncio
    import json

    state, server = _state_and_server()

    async def run():
        created = await server.call_tool("sync_specs", {"spec_id": "36.579-5", "force": False})
        envelope = json.loads(created.content[0].text)
        assert envelope["status"] == "queued"
        job_id = envelope["job_id"]
        detail = await server.call_tool("get_job", {"job_id": job_id})
        return detail

    detail = asyncio.run(run())
    assert detail.is_error is False
    detail_payload = json.loads(detail.content[0].text)
    assert detail_payload["kind"] == "sync_specs"
    assert detail_payload["params"] == {"spec_id": "36.579-5", "force": False, "per_version_details": False}
    del state.engine


def test_sync_specs_tool_per_version_details_enqueues(sqlite_env) -> None:
    """The MCP tool's ``per_version_details=True`` reaches ``job.params``."""
    import asyncio
    import json

    state, server = _state_and_server()

    async def run():
        created = await server.call_tool(
            "sync_specs", {"tsg": "R5", "force": False, "per_version_details": True}
        )
        envelope = json.loads(created.content[0].text)
        return envelope["job_id"]

    job_id = asyncio.run(run())
    detail = asyncio.run(server.call_tool("get_job", {"job_id": job_id}))
    detail_payload = json.loads(detail.content[0].text)
    assert detail_payload["params"] == {
        "tsg": "R5",
        "force": False,
        "per_version_details": True,
    }
    del state.engine


def test_get_meeting_not_found_raises(sqlite_env) -> None:
    """Unknown meeting id propagates MeetingNotFoundError as an MCP -32004 protocol error."""
    import asyncio

    from mcp.shared.exceptions import MCPError

    from doc3gpp.web.errors import MCP_CODE_NOT_FOUND

    _, server = _state_and_server()

    async def run():
        return await server.call_tool("get_meeting", {"meeting_id": 999999})

    with pytest.raises(MCPError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code == MCP_CODE_NOT_FOUND


def test_sse_transport_mounts_two_endpoints(sqlite_env) -> None:
    """transport='sse' mounts GET /mcp/sse and POST /mcp/messages/."""
    from fastapi.testclient import TestClient

    from doc3gpp.settings.schema import CacheSettings, MCPSettings, ServerSettings, Settings
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.web.app import build_app

    state, _ = _state_and_server()
    app = build_app(
        Settings(
            server=ServerSettings(enabled=True, port=8765),
            mcp=MCPSettings(enabled=True, transport="sse"),
            cache=CacheSettings(dir=state.settings.cache.dir),
        )
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        # GET /mcp/sse opens the SSE stream. A bare GET without the SSE
        # handshake headers fails SDK validation (500), but the route is
        # mounted — assert it is not a 404.
        resp = client.get("/mcp/sse")
        assert resp.status_code != 404, "sse endpoint not mounted"
        # POST /mcp/messages/ is the message endpoint.
        resp2 = client.post("/mcp/messages/", json={})
        assert resp2.status_code != 404, "messages endpoint not mounted"

    get_engine.cache_clear()
    del state.engine


def test_mcp_allows_configured_browser_origin(sqlite_env) -> None:
    """A cross-origin browser request is accepted when the origin is allowed.

    Regression for the 403 "Invalid Origin header" that blocked browser
    MCP clients: the SDK's transport-security layer rejects cross-origin
    requests unless the origin is in ``allowed_origins``.
    """
    from fastapi.testclient import TestClient

    from doc3gpp.settings.schema import CacheSettings, MCPSettings, ServerSettings, Settings
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.web.app import build_app

    state, _ = _state_and_server()
    app = build_app(
        Settings(
            server=ServerSettings(enabled=True, port=8765),
            mcp=MCPSettings(enabled=True, allowed_origins=["http://127.0.0.1"]),
            cache=CacheSettings(dir=state.settings.cache.dir),
        )
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/mcp/",
            headers={
                "Host": "127.0.0.1:8765",
                "Origin": "http://127.0.0.1",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("application/json"), resp.headers

    get_engine.cache_clear()
    del state.engine


def test_mcp_default_allows_localhost_browser_origin(sqlite_env) -> None:
    """The default ``allowed_origins`` lets a localhost browser client connect."""
    from fastapi.testclient import TestClient

    from doc3gpp.settings.schema import CacheSettings, MCPSettings, ServerSettings, Settings
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.web.app import build_app

    state, _ = _state_and_server()
    app = build_app(
        Settings(
            server=ServerSettings(enabled=True, port=8765),
            mcp=MCPSettings(enabled=True),
            cache=CacheSettings(dir=state.settings.cache.dir),
        )
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/mcp/",
            headers={
                "Host": "127.0.0.1:8765",
                "Origin": "http://127.0.0.1",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
        )
        assert resp.status_code == 200, resp.text

    get_engine.cache_clear()
    del state.engine


def _seed_spec_corpus() -> None:
    """Seed one spec + one version so the spec tools return rows."""
    from doc3gpp.models.spec import Spec, SpecVersion
    from doc3gpp.models.tsg import Tsg
    from doc3gpp.storage.repositories.spec_sql import SQLAlchemySpecRepository
    from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository

    SQLAlchemyTsgRepository().upsert_many(
        [Tsg(tsg_name="RAN", short_name="R5", description="Radio Access Network")],
    )
    repo = SQLAlchemySpecRepository()
    repo.upsert(
        Spec(
            spec_id="36.579-5",
            type="TS",
            title="NR conformance",
            tsg="R5",
            status="Under change control",
            radio_tech="LTE,5G",
            initial_release="Rel-15",
            wis="FS_NR_TEST",
        )
    )
    repo.upsert_versions(
        [
            SpecVersion(
                spec_id="36.579-5",
                version="18.3.0",
                ftp_url="https://www.3gpp.org/ftp/Specs/archive/36_series/36.579-5/36579-5-i30.zip",
                release="Rel-18",
                meeting_id=108,
                meeting_name="RAN#108",
            )
        ]
    )


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


def test_list_specs_tool(sqlite_env) -> None:
    """``list_specs`` MCP tool returns seeded spec rows."""
    import asyncio

    _state_and_server()  # runs create_schema()
    _seed_spec_corpus()
    _, server = _state_and_server()

    async def run():
        return await server.call_tool("list_specs", {"tsg": "R5"})

    result = asyncio.run(run())
    assert result.is_error is False
    import json

    payload = json.loads(result.content[0].text)
    assert "spec_id" in payload[0]
    assert payload[0]["spec_id"] == "36.579-5"


def test_list_specs_rapporteurs_filter(sqlite_env) -> None:
    """``list_specs`` accepts and applies the rapporteurs filter."""
    import asyncio

    _state_and_server()  # runs create_schema()
    _seed_spec_corpus()
    _, server = _state_and_server()

    async def run():
        return await server.call_tool("list_specs", {"rapporteurs": "not-null"})

    result = asyncio.run(run())
    assert result.is_error is False
    import json

    payload = json.loads(result.content[0].text)
    assert payload == []


def test_get_spec_tool(sqlite_env) -> None:
    """``get_spec`` MCP tool returns spec + version rows for a seeded spec."""
    import asyncio

    _state_and_server()  # runs create_schema()
    _seed_spec_corpus()
    _, server = _state_and_server()

    async def run():
        return await server.call_tool("get_spec", {"spec_id": "36.579-5"})

    result = asyncio.run(run())
    assert result.is_error is False
    import json

    payload = json.loads(result.content[0].text)
    assert payload["spec"]["spec_id"] == "36.579-5"
    assert payload["versions"][0]["version"] == "18.3.0"


def test_get_spec_tool_not_found(sqlite_env) -> None:
    """Unknown spec id surfaces as a JSON-RPC -32004 protocol error."""
    import asyncio

    from mcp.shared.exceptions import MCPError

    from doc3gpp.web.errors import MCP_CODE_NOT_FOUND

    _, server = _state_and_server()

    async def run():
        return await server.call_tool("get_spec", {"spec_id": "99.999"})

    with pytest.raises(MCPError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code == MCP_CODE_NOT_FOUND


def test_get_spec_tool_version_and_no_wis_crs(sqlite_env) -> None:
    """``get_spec`` accepts ``version`` and ``no_wis_crs``; JSON matches HTTP."""
    import asyncio
    import json

    from fastapi.testclient import TestClient

    from doc3gpp.settings.schema import CacheSettings, MCPSettings, ServerSettings, Settings
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.web.app import build_app

    _state_and_server()  # runs create_schema()
    _seed_spec_corpus()
    state, server = _state_and_server()
    app = build_app(
        Settings(
            server=ServerSettings(enabled=True, port=8765),
            mcp=MCPSettings(enabled=True),
            cache=CacheSettings(dir=state.settings.cache.dir),
        )
    )
    with TestClient(app) as client:

        async def call(name: str, args: dict) -> str:
            result = await server.call_tool(name, args)
            assert result.is_error is False, result
            return result.content[0].text

        mcp_bytes = asyncio.run(call(
            "get_spec",
            {"spec_id": "36.579-5", "no_wis_crs": True},
        ))
        http_resp = client.get("/specs/36.579-5?format=json&no_wis_crs=true")
        assert http_resp.status_code == 200, http_resp.text
        http_bytes = http_resp.content.decode("utf-8")
        assert json.loads(mcp_bytes) == json.loads(http_bytes)
        assert "wis" not in json.loads(mcp_bytes)["spec"]

        asyncio.run(call(
            "get_spec",
            {"spec_id": "36.579-5", "version": "19.%"},
        ))

    get_engine.cache_clear()
    del state.engine


def test_spec_tools_parity_with_http_json(sqlite_env) -> None:
    """Spec MCP tools' JSON bytes match the matching HTTP ``?format=json`` route."""
    import asyncio
    import json

    from fastapi.testclient import TestClient

    from doc3gpp.settings.schema import CacheSettings, MCPSettings, ServerSettings, Settings
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.web.app import build_app

    _state_and_server()  # runs create_schema()
    _seed_spec_corpus()
    state, server = _state_and_server()
    app = build_app(
        Settings(
            server=ServerSettings(enabled=True, port=8765),
            mcp=MCPSettings(enabled=True),
            cache=CacheSettings(dir=state.settings.cache.dir),
        )
    )
    with TestClient(app) as client:

        async def call(name: str, args: dict) -> str:
            result = await server.call_tool(name, args)
            assert result.is_error is False, result
            return result.content[0].text

        mcp_bytes = asyncio.run(call("list_specs", {"tsg": "R5"}))
        http_resp = client.get("/specs/?tsg=R5&format=json")
        assert http_resp.status_code == 200, http_resp.text
        http_bytes = http_resp.content.decode("utf-8")
        assert mcp_bytes == http_bytes, (
            f"list_specs parity broke: MCP={mcp_bytes!r} HTTP={http_bytes!r}"
        )

        mcp_spec = asyncio.run(call("get_spec", {"spec_id": "36.579-5"}))
        http_spec = client.get("/specs/36.579-5?format=json").content.decode("utf-8")
        assert json.loads(mcp_spec) == json.loads(http_spec)

    get_engine.cache_clear()
    del state.engine


def test_spec_doc_mcp_exposes_plural_metadata_filters(sqlite_env) -> None:
    import asyncio

    from doc3gpp.models.spec_doc import SpecDocHit, SpecDocSemanticHit

    state, server = _state_and_server()

    class FakeSearch:
        def __init__(self):
            self.filters = None
            self.hit = SpecDocHit(
                chunk_id="38.331@19.0.0#0",
                spec_id="38.331",
                version="19.0.0",
                release="Rel-19",
                sections="5 Scope",
                tables="Table 1 Values",
                chunk_index=0,
                text="handover",
                score=0.1,
                previews={},
            )

        def search(self, _query, _filters):
            self.filters = _filters
            return [self.hit]

    search_service = FakeSearch()

    class FakeSemantic:
        def __init__(self):
            self.kwargs = None

        def search(self, _query, **_kwargs):
            self.kwargs = _kwargs
            return [
                SpecDocSemanticHit(
                    chunk_id="38.331@19.0.0#0",
                    rrf_score=0.1,
                    hit=search_service.hit,
                )
            ]

    semantic_service = FakeSemantic()
    state.services.spec_doc_search = search_service
    state.services.spec_doc_semantic = semantic_service

    async def run():
        tools = await server.list_tools()
        by_name = {tool.name: tool for tool in tools}
        for name in ("search_spec_docs", "semantic_search_spec_docs"):
            properties = by_name[name].input_schema["properties"]
            assert "sections" in properties
            assert "tables" in properties
            assert "section" not in properties

        search_result = await server.call_tool(
            "search_spec_docs",
            {"query": "handover", "sections": "%5%", "tables": "%UE%"},
        )
        semantic_result = await server.call_tool(
            "semantic_search_spec_docs",
            {"query": "handover", "sections": "%5%", "tables": "%UE%"},
        )
        return search_result, semantic_result

    search_result, semantic_result = asyncio.run(run())
    assert search_result.is_error is False
    assert semantic_result.is_error is False
    assert json.loads(search_result.content[0].text)[0]["sections"] == "5 Scope"
    nested = json.loads(semantic_result.content[0].text)[0]["hit"]
    assert nested["sections"] == "5 Scope"
    assert nested["tables"] == "Table 1 Values"
    assert "section_no" not in nested
    assert search_service.filters.sections == "%5%"
    assert search_service.filters.tables == "%UE%"
    assert semantic_service.kwargs["filters"].sections == "%5%"
    assert semantic_service.kwargs["filters"].tables == "%UE%"

    from doc3gpp.storage.db.session import get_engine

    get_engine.cache_clear()
    del state.engine


def _state_and_search_server(search_corpus):
    """Build state + MCP server with a real passthrough search service.

    The default ``build_state`` composes a semantic-capable search
    service whose reranker would lazy-load an embedding model; for
    FTS5-focused tests we swap in a :class:`SearchService` wired to a
    :class:`PassthroughReranker` so the FTS5 query path is real and no
    model is touched.
    """
    from doc3gpp.services.search_service import PassthroughReranker, SearchService
    from doc3gpp.storage.repositories.search_sql import SQLAlchemySearchIndexRepository

    state, server = _state_and_server()
    state.services.search = SearchService(
        repo=SQLAlchemySearchIndexRepository(),
        reranker=PassthroughReranker(),
    )
    return state, server


def test_search_tdoc_normalises_jargon_queries(search_corpus) -> None:
    """``nb-iot`` in an operator query must not crash FTS5.

    Regression for the ``no such column: iot`` error that previously
    produced a tool failure: the raw query was passed to FTS5 MATCH,
    which parses ``nb-iot`` as ``nb - iot``.
    """
    import asyncio
    import json

    from doc3gpp.storage.db.session import get_engine

    state, server = _state_and_search_server(search_corpus)

    async def run():
        return await server.call_tool(
            "search_tdoc", {"query": "nb-iot AND scheduling", "limit": 20}
        )

    result = asyncio.run(run())
    assert result.is_error is False
    hits = json.loads(result.content[0].text)
    assert hits
    assert hits[0]["tdoc_id"] == "RP-2200456"
    get_engine.cache_clear()
    del state.engine


def test_search_tdoc_stopwords_only_raises_invalid_params(search_corpus) -> None:
    """A stopwords-only query is a client error (invalid params), not a 500."""
    import asyncio

    from mcp.shared.exceptions import MCPError

    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.web.errors import MCP_CODE_INVALID_PARAMS

    state, server = _state_and_search_server(search_corpus)

    async def run():
        return await server.call_tool("search_tdoc", {"query": "the"})

    with pytest.raises(MCPError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code == MCP_CODE_INVALID_PARAMS
    get_engine.cache_clear()
    del state.engine


def test_search_tdoc_accepts_sem_query(sqlite_env, search_corpus) -> None:
    """search_tdoc forwards sem_query to the search service."""
    import asyncio

    import numpy as np

    from doc3gpp.services.semantic_reranker import SemanticReranker
    from doc3gpp.services.search_service import SearchService
    from doc3gpp.storage.repositories.search_sql import SQLAlchemySearchIndexRepository
    from doc3gpp.storage.repositories.vector_sql import SQLAlchemyVectorIndexRepository
    from doc3gpp.web.mcp_server import build_mcp_server
    from doc3gpp.web.state import JobWorkerHandle, ServiceContainer, WebState
    from doc3gpp.settings.schema import Settings
    from doc3gpp.storage.db.session import get_engine, get_testcase_engine
    from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository
    from doc3gpp.services import factory

    recorded: list[str] = []

    class RecordingEmbedder:
        def encode(self, texts: list[str]) -> np.ndarray:
            recorded.extend(texts)
            return np.zeros((len(texts), 384), dtype=np.float32)

    settings = Settings()
    services = ServiceContainer(
        meeting=factory.build_meeting_service(),
        tdoc=factory.build_tdoc_service(),
        tdoc_cr=factory.build_tdoc_cr_service(embedder=RecordingEmbedder()),
        tdoc_sync=factory.build_tdoc_sync_coordinator(),
        tdoc_repo=factory.build_tdoc_repository(),
        tsg=factory.build_tsg_service(),
        wi=factory.build_wi_service(),
        spec=factory.build_spec_service(),
        testcase=factory.build_testcase_service(),
        search=SearchService(
            repo=SQLAlchemySearchIndexRepository(),
            reranker=SemanticReranker(
                embedder=RecordingEmbedder(),
                vector_repo=SQLAlchemyVectorIndexRepository(),
                settings=settings,
            ),
        ),
        semantic_search=factory.build_semantic_search_service(embedder=RecordingEmbedder()),
        tdoc_file_repo=factory.build_tdoc_file_repository(),
        job_repo=SQLAlchemyJobRepository(),
    )
    state = WebState(
        settings=settings,
        engine=get_engine(),
        testcase_engine=get_testcase_engine(),
        services=services,
        jobs=JobWorkerHandle(),
    )
    server = build_mcp_server(state)

    async def run():
        return await server.call_tool(
            "search_tdoc",
            {"query": "scheduling", "limit": 5, "sem_query": "scheduling"},
        )

    result = asyncio.run(run())
    assert result is not None
    text = result.content[0].text
    assert text.startswith("[")
    assert recorded == ["scheduling"]
    get_engine.cache_clear()
    get_testcase_engine.cache_clear()
    del state.engine
    del state.testcase_engine


def test_semantic_search_tdoc_forwards_args_and_preserves_contract(sqlite_env) -> None:
    """The renamed MCP semantic tool preserves forwarding, output, and errors."""
    import asyncio

    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.shared.exceptions import MCPError

    from doc3gpp.models.search import SearchFilters, SearchHit
    from doc3gpp.models.semantic_search import SemanticSearchHit
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.web.errors import MCP_CODE_INVALID_PARAMS

    class RecordingSemanticSearchService:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def search(
            self,
            query: str,
            *,
            fts5_query: str | None,
            filters: SearchFilters,
            limit: int,
            fts5_weight: float,
        ) -> list[SemanticSearchHit]:
            self.calls.append(
                {
                    "query": query,
                    "fts5_query": fts5_query,
                    "filters": filters,
                    "limit": limit,
                    "fts5_weight": fts5_weight,
                }
            )
            return [
                SemanticSearchHit(
                    tdoc_id="R5-260001",
                    rrf_score=0.42,
                    hit=SearchHit(
                        tdoc_id="R5-260001",
                        score=-1.2,
                        previews={},
                        title="Handover signalling",
                        meeting="RAN5#108",
                        tsg="R5",
                        uploaded_date="2026-01-02",
                        ftp_url="r5/26.001/r5-260001.zip",
                        wis="FS_HANDOVER",
                    ),
                    rank_fts5=1,
                    rank_vec=0,
                    min_chunk_distance=0.125,
                    best_chunk_id="R5-260001#0",
                ),
            ]

    state, server = _state_and_server()
    fake = RecordingSemanticSearchService()
    state.services.semantic_search = fake

    async def run():
        return await server.call_tool(
            "semantic_search_tdoc",
            {
                "query": "handover signalling",
                "fts5_query": '"handover" AND signalling',
                "tsg": "R5",
                "meeting": "%RAN%",
                "meeting_id": 108,
                "tdoc_id": "R5-260001",
                "release": "Rel-18",
                "spec": "38.300",
                "since": "2026-01-01",
                "until": "2026-12-31",
                "limit": 7,
                "fts5_weight": 0.75,
            },
        )

    result = asyncio.run(run())
    assert result.is_error is False
    assert fake.calls == [
        {
            "query": "handover signalling",
            "fts5_query": '"handover" AND signalling',
            "filters": SearchFilters(
                tsg="R5",
                meeting="%RAN%",
                meeting_id=108,
                tdoc_id="R5-260001",
                release="Rel-18",
                spec="38.300",
                since="2026-01-01",
                until="2026-12-31",
                limit=7,
            ),
            "limit": 7,
            "fts5_weight": 0.75,
        },
    ]
    assert json.loads(result.content[0].text) == [
        {
            "tdoc_id": "R5-260001",
            "rrf_score": 0.42,
            "rank_fts5": 1,
            "rank_vec": 0,
            "min_chunk_distance": 0.125,
            "best_chunk_id": "R5-260001#0",
            "hit": {
                "tdoc_id": "R5-260001",
                "title": "Handover signalling",
                "ftp_url": "r5/26.001/r5-260001.zip",
                "wis": "FS_HANDOVER",
            },
        },
    ]

    async def run_invalid_weight():
        return await server.call_tool(
            "semantic_search_tdoc",
            {"query": "handover", "fts5_weight": 1.1},
        )

    with pytest.raises(MCPError) as invalid_exc:
        asyncio.run(run_invalid_weight())
    assert invalid_exc.value.code == MCP_CODE_INVALID_PARAMS
    assert invalid_exc.value.data["error"] == "invalid_filter"

    async def run_removed_name():
        return await server.call_tool(
            "semantic_search_tdocs", {"query": "handover"}
        )

    with pytest.raises(ToolError, match="Unknown tool: semantic_search_tdocs") as old_exc:
        asyncio.run(run_removed_name())
    assert str(old_exc.value) == "Unknown tool: semantic_search_tdocs"

    get_engine.cache_clear()
    del state.engine


def test_web_errors_maps_spec_unknown_on_upstream() -> None:
    """``map_domain_error`` / ``map_mcp_error`` cover ``SpecUnknownOnUpstreamError``."""
    resp_unknown = map_domain_error(
        SpecUnknownOnUpstreamError("38.523-1", "missing fields: title, type")
    )
    assert resp_unknown.status_code == 404
    body_unknown = json.loads(resp_unknown.body)
    assert body_unknown["error"] == "spec_unknown_on_upstream"
    assert "38.523-1" in body_unknown["detail"]

    mcp1 = map_mcp_error(SpecUnknownOnUpstreamError("38.523-1", "missing"))
    assert mcp1 is not None
    code, _msg, data = mcp1
    assert code == -32004
    assert data["error"] == "spec_unknown_on_upstream"
    assert data["resource"] == "spec"


def test_call_list_meetings_name_filter(sqlite_env) -> None:
    """``list_meetings`` accepts a ``name`` filter and applies it to the seeded row."""
    import asyncio
    import json

    _state_and_server()  # runs create_schema()
    _seed_corpus()
    _, server = _state_and_server()

    async def run():
        return await server.call_tool("list_meetings", {"name": "%SA2%"})

    result = asyncio.run(run())
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert len(payload) == 1
    assert payload[0]["name"] == "SA2#156"


def test_call_list_meetings_name_no_match(sqlite_env) -> None:
    """``list_meetings`` with a no-match ``name`` pattern returns ``[]``."""
    import asyncio

    _state_and_server()  # runs create_schema()
    _seed_corpus()
    _, server = _state_and_server()

    async def run():
        return await server.call_tool("list_meetings", {"name": "no-match-%"})

    result = asyncio.run(run())
    assert result.is_error is False
    assert result.content[0].text == "[]"


def test_cancel_succeeded_job_returns_envelope(sqlite_env) -> None:
    """cancel_job on a SUCCEEDED job returns the envelope (idempotent)."""
    import asyncio

    from doc3gpp.models.jobs import JobKind

    state, server = _state_and_server()
    repo = state.services.job_repo
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    repo.mark_succeeded(job.id, summary={"ok": True})

    async def run():
        return await server.call_tool("cancel_job", {"job_id": job.id})

    result = asyncio.run(run())
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["job_id"] == job.id
    assert payload["status"] == "succeeded"
    assert payload["summary"] == {"ok": True}
    del state.engine


def test_cancel_failed_job_returns_envelope(sqlite_env) -> None:
    """cancel_job on a FAILED job returns the envelope + error field."""
    import asyncio

    from doc3gpp.models.jobs import JobKind

    state, server = _state_and_server()
    repo = state.services.job_repo
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    repo.mark_failed(job.id, error="boom")

    async def run():
        return await server.call_tool("cancel_job", {"job_id": job.id})

    result = asyncio.run(run())
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "failed"
    assert payload["error"] == "boom"
    del state.engine


def test_cancel_cancelled_job_returns_envelope(sqlite_env) -> None:
    """cancel_job on an already-CANCELLED job returns the envelope."""
    import asyncio

    from doc3gpp.models.jobs import JobKind

    state, server = _state_and_server()
    repo = state.services.job_repo
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    repo.mark_cancelled(job.id)

    async def run():
        return await server.call_tool("cancel_job", {"job_id": job.id})

    result = asyncio.run(run())
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "cancelled"
    del state.engine


def test_cancel_unknown_job_raises_job_not_found(sqlite_env) -> None:
    """cancel_job on an unknown id still raises MCPError(code=-32004)."""
    import asyncio

    from mcp.shared.exceptions import MCPError

    _, server = _state_and_server()

    async def run():
        return await server.call_tool("cancel_job", {"job_id": "deadbeef"})

    with pytest.raises(MCPError) as exc_info:
        asyncio.run(run())
    assert "deadbeef" in str(exc_info.value)


def test_mcp_get_tdoc_includes_cover_summary_of_change(sqlite_env) -> None:
    """The ``get_tdoc`` MCP tool surfaces ``cover.summary_of_change``."""
    import asyncio

    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_sql import (
        SQLAlchemyTDocCrRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import (
        SQLAlchemyTDocRepository,
    )
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails

    create_schema()
    url = "R5/26.001/R5s260001.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260001", ftp_url=url),
    )
    SQLAlchemyTDocCrRepository().upsert(
        TDocCRDetails(
            tdoc_id="R5s260001",
            ftp_url=url,
            summary_of_change="Add USIM config setter.",
        ),
    )

    _, server = _state_and_server()

    async def run():
        return await server.call_tool("get_tdoc", {"tdoc_id": "R5s260001"})

    result = asyncio.run(run())
    payload = json.loads(result.content[0].text)
    assert payload["cover"]["summary_of_change"] == "Add USIM config setter."


def test_mcp_get_tdoc_by_url_matches_http_route(sqlite_env) -> None:
    """MCP ``get_tdoc(ftp_url=...)`` output equals HTTP ``/tdocs/by-url?ftp_url=...&format=json``."""
    import asyncio
    import json

    from fastapi.testclient import TestClient

    from doc3gpp.settings.schema import CacheSettings, MCPSettings, ServerSettings, Settings
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails
    from doc3gpp.web.app import build_app

    state, server = _state_and_server()  # runs create_schema()
    url = "R5/26.001/R5s260001.zip"
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260001", ftp_url=url))
    SQLAlchemyTDocCrRepository().upsert(
        TDocCRDetails(tdoc_id="R5s260001", ftp_url=url, cr_num="0001")
    )

    app = build_app(
        Settings(
            server=ServerSettings(enabled=True, port=8765),
            mcp=MCPSettings(enabled=True),
            cache=CacheSettings(dir=state.settings.cache.dir),
        )
    )
    with TestClient(app) as client:
        http_response = client.get(
            "/tdocs/by-url", params={"ftp_url": url, "format": "json"}
        )
        assert http_response.status_code == 200
        http_payload = http_response.json()

        async def call(name: str, args: dict):
            result = await server.call_tool(name, args)
            assert result.is_error is False, result
            return result.content[0].text

        mcp_payload = json.loads(asyncio.run(call("get_tdoc", {"ftp_url": url})))

    assert mcp_payload == http_payload
    get_engine.cache_clear()
    del state.engine


def _seed_testcase_corpus() -> None:
    """Seed one testcase + one status row so the testcase tools return rows."""
    from doc3gpp.models.testcase import TestCase, TestCaseStatus
    from doc3gpp.storage.repositories.testcase_sql import (
        SQLAlchemyTestCaseRepository,
    )

    repo = SQLAlchemyTestCaseRepository()
    repo.upsert_many(
        [
            TestCase(
                testcase_id="TC_1",
                title="5G FR1 test",
                spec="38.523-1",
                group="5G",
                release="Rel-17",
            ),
        ]
    )
    repo.replace_statuses(
        "TC_1",
        "5G",
        [
            TestCaseStatus(
                testcase_id="TC_1",
                group="5G",
                path="FR1",
                gcf_ptcrb="Approved",
                ttcn_status="Approved",
            ),
        ],
    )


def test_mcp_list_testcases_parity(sqlite_env) -> None:
    """``list_testcases`` MCP tool returns seeded rows (compact byte parity)."""
    import asyncio

    state, server = _state_and_server()
    from doc3gpp.models.testcase import TestCase, TestCaseStatus, TestCaseWithStatuses

    rows = [
        TestCaseWithStatuses(
            testcase=TestCase(
                testcase_id="TC_1",
                title="T",
                spec="38.523-1",
                group="5G",
            ),
            statuses=[
                TestCaseStatus(
                    testcase_id="TC_1",
                    group="5G",
                    path="FR1",
                    gcf_ptcrb="Approved",
                    ttcn_status="Approved",
                )
            ],
        )
    ]
    state.services.testcase.list_recent = lambda **k: rows  # noqa: ARG005

    async def run():
        return await server.call_tool("list_testcases", {})

    result = asyncio.run(run())
    assert result.is_error is False
    assert '"FR1","gcf_ptcrb":"Approved"' in result.content[0].text
    del state.engine


def test_list_testcases_tool(sqlite_env) -> None:
    """``list_testcases`` MCP tool returns seeded testcase rows over sqlite."""
    import asyncio
    import json

    _state_and_server()  # runs create_schema()
    _seed_testcase_corpus()
    _, server = _state_and_server()

    async def run():
        return await server.call_tool("list_testcases", {})

    result = asyncio.run(run())
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload[0]["testcase_id"] == "TC_1"
    assert payload[0]["statuses"] == [{"path": "FR1", "gcf_ptcrb": "Approved", "ttcn_status": "Approved"}]


def test_list_testcases_tool_rejects_unknown_group(sqlite_env) -> None:
    """``list_testcases`` with an unknown group is an invalid-params error."""
    import asyncio

    from mcp.shared.exceptions import MCPError

    from doc3gpp.web.errors import MCP_CODE_INVALID_PARAMS

    _state_and_server()  # runs create_schema()
    _, server = _state_and_server()

    async def run():
        return await server.call_tool("list_testcases", {"group": "NOPE"})

    with pytest.raises(MCPError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code == MCP_CODE_INVALID_PARAMS


def test_get_testcase_tool_rejects_unknown_group(sqlite_env) -> None:
    """``get_testcase`` with an unknown group is an invalid-params error."""
    import asyncio

    from mcp.shared.exceptions import MCPError

    from doc3gpp.web.errors import MCP_CODE_INVALID_PARAMS

    _state_and_server()  # runs create_schema()
    _seed_testcase_corpus()
    _, server = _state_and_server()

    async def run():
        return await server.call_tool(
            "get_testcase", {"testcase_id": "TC_1", "group": "NOPE"}
        )

    with pytest.raises(MCPError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code == MCP_CODE_INVALID_PARAMS


def test_get_testcase_tool(sqlite_env) -> None:
    """``get_testcase`` MCP tool returns header + status rows for a seed."""
    import asyncio
    import json

    _state_and_server()  # runs create_schema()
    _seed_testcase_corpus()
    _, server = _state_and_server()

    async def run():
        return await server.call_tool("get_testcase", {"testcase_id": "TC_1"})

    result = asyncio.run(run())
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert isinstance(payload, list) and payload
    assert payload[0]["testcase_id"] == "TC_1"
    assert payload[0]["statuses"][0]["path"] == "FR1"
    assert "group" not in payload[0]["statuses"][0]


def test_get_testcase_tool_not_found(sqlite_env) -> None:
    """Unknown testcase id surfaces as a JSON-RPC -32004 protocol error."""
    import asyncio

    from mcp.shared.exceptions import MCPError

    from doc3gpp.web.errors import MCP_CODE_NOT_FOUND

    _, server = _state_and_server()

    async def run():
        return await server.call_tool("get_testcase", {"testcase_id": "NOPE"})

    with pytest.raises(MCPError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code == MCP_CODE_NOT_FOUND


def test_sync_testcases_tool_enqueues(sqlite_env) -> None:
    """``sync_testcases`` MCP tool returns the queued envelope."""
    import asyncio
    import json

    state, server = _state_and_server()

    async def run():
        created = await server.call_tool("sync_testcases", {"force": True})
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
    assert detail_payload["kind"] == "sync_testcases"
    assert detail_payload["params"] == {"force": True}
    del state.engine


def test_testcase_tools_parity_with_http_json(sqlite_env) -> None:
    """Testcase MCP tools' JSON bytes match the HTTP ``?format=json`` routes."""
    import asyncio
    import json

    from fastapi.testclient import TestClient

    from doc3gpp.settings.schema import (
        CacheSettings,
        MCPSettings,
        ServerSettings,
        Settings,
    )
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.web.app import build_app

    _state_and_server()  # runs create_schema()
    _seed_testcase_corpus()
    state, server = _state_and_server()
    app = build_app(
        Settings(
            server=ServerSettings(enabled=True, port=8765),
            mcp=MCPSettings(enabled=True),
            cache=CacheSettings(dir=state.settings.cache.dir),
        )
    )
    with TestClient(app) as client:

        async def call(name: str, args: dict) -> str:
            result = await server.call_tool(name, args)
            assert result.is_error is False, result
            return result.content[0].text

        mcp_bytes = asyncio.run(call("list_testcases", {}))
        http_resp = client.get("/testcases?format=json")
        assert http_resp.status_code == 200, http_resp.text
        http_bytes = http_resp.content.decode("utf-8")
        assert mcp_bytes == http_bytes, (
            f"list_testcases parity broke: MCP={mcp_bytes!r} HTTP={http_bytes!r}"
        )

        mcp_case = asyncio.run(call("get_testcase", {"testcase_id": "TC_1"}))
        http_case = client.get("/testcases/TC_1?format=json").content.decode("utf-8")
        assert json.loads(mcp_case) == json.loads(http_case)

    get_engine.cache_clear()
    del state.engine
