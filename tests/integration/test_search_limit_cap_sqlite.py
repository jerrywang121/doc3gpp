"""Search ``limit`` maximum is 150 on every search surface; list limits keep 200+.

The distinct-TDoc widening window holds at least 170 TDocs, so a search
limit above 150 cannot always be satisfied. Search surfaces reject 151;
list surfaces keep their own maximum.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from mcp.shared.exceptions import MCPError
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import CacheSettings, MCPSettings, ServerSettings, Settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.web.app import build_app, build_state
from doc3gpp.web.errors import MCP_CODE_INVALID_PARAMS
from doc3gpp.web.mcp_server import build_mcp_server
from doc3gpp.web.workers.job_worker import JobWorker


@pytest.fixture()
def empty_db(sqlite_env, monkeypatch):
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "false")
    get_settings.cache_clear()
    create_schema()
    yield
    get_settings.cache_clear()


def test_tdoc_search_limit_150_is_accepted(empty_db) -> None:
    result = CliRunner().invoke(
        app, ["tdoc", "search", "--tsg", "RAN1", "--limit", "150", "--format", "json"]
    )
    assert result.exit_code == 0, result.output


def test_tdoc_search_limit_151_is_rejected(empty_db) -> None:
    result = CliRunner().invoke(
        app, ["tdoc", "search", "--tsg", "RAN1", "--limit", "151", "--format", "json"]
    )
    assert result.exit_code == 2
    assert "150" in result.output


def test_spec_doc_search_limit_150_is_accepted(empty_db) -> None:
    result = CliRunner().invoke(
        app, ["spec", "doc", "search", "--spec", "38.331", "--limit", "150", "--format", "json"]
    )
    assert result.exit_code == 0, result.output


def test_spec_doc_search_limit_151_is_rejected(empty_db) -> None:
    result = CliRunner().invoke(
        app, ["spec", "doc", "search", "--spec", "38.331", "--limit", "151", "--format", "json"]
    )
    assert result.exit_code == 2
    assert "150" in result.output


def test_tdoc_and_spec_list_limit_200_is_still_accepted(empty_db) -> None:
    runner = CliRunner()
    tdoc_list = runner.invoke(app, ["tdoc", "list", "--limit", "200"])
    spec_list = runner.invoke(app, ["spec", "list", "--limit", "200"])
    assert tdoc_list.exit_code == 0, tdoc_list.output
    assert spec_list.exit_code == 0, spec_list.output


@pytest.fixture()
def web_client(empty_db, monkeypatch, tmp_path):
    async def _noop_run(self) -> None:  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(JobWorker, "run", _noop_run)
    settings = Settings(
        server=ServerSettings(enabled=True),
        mcp=MCPSettings(enabled=True),
        cache=CacheSettings(dir=tmp_path / "cache"),
    )
    settings.sync.auto_sync = False
    with TestClient(build_app(settings)) as client:
        yield client


def test_http_tdoc_search_limit_150_is_accepted(web_client) -> None:
    response = web_client.get("/tdocs/search", params={"format": "json", "limit": "150"})
    assert response.status_code == 200


def test_http_tdoc_search_limit_151_is_rejected(web_client) -> None:
    response = web_client.get("/tdocs/search", params={"format": "json", "limit": "151"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_filter"


def test_http_spec_doc_search_limit_150_is_accepted(web_client) -> None:
    response = web_client.get(
        "/spec-docs/search", params={"format": "json", "limit": "150"}
    )
    assert response.status_code == 200


def test_http_spec_doc_search_limit_151_is_rejected(web_client) -> None:
    response = web_client.get(
        "/spec-docs/search", params={"format": "json", "limit": "151"}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_filter"


def test_http_tdoc_list_limit_200_is_still_accepted(web_client) -> None:
    response = web_client.get("/tdocs", params={"format": "json", "limit": "200"})
    assert response.status_code == 200


def _mcp_server():
    state = build_state(get_settings())
    return build_mcp_server(state)


def test_mcp_search_tdoc_limit_150_is_accepted(empty_db) -> None:
    server = _mcp_server()
    asyncio.run(server.call_tool("search_tdoc", {"limit": 150}))


def test_mcp_search_tdoc_limit_151_is_rejected(empty_db) -> None:
    server = _mcp_server()
    with pytest.raises(MCPError) as exc_info:
        asyncio.run(server.call_tool("search_tdoc", {"limit": 151}))
    assert exc_info.value.code == MCP_CODE_INVALID_PARAMS


def test_mcp_search_spec_docs_limit_150_is_accepted(empty_db) -> None:
    server = _mcp_server()
    asyncio.run(server.call_tool("search_spec_docs", {"limit": 150}))


def test_mcp_search_spec_docs_limit_151_is_rejected(empty_db) -> None:
    server = _mcp_server()
    with pytest.raises(MCPError) as exc_info:
        asyncio.run(server.call_tool("search_spec_docs", {"limit": 151}))
    assert exc_info.value.code == MCP_CODE_INVALID_PARAMS


def test_mcp_list_tdocs_limit_200_is_still_accepted(empty_db) -> None:
    server = _mcp_server()
    asyncio.run(server.call_tool("list_tdocs", {"limit": 200}))
