"""Parity + HTML tests for the resource `schema` surfaces."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from tests.integration.test_web_end_to_end import app_with_deps  # noqa: F401,F811


def test_rest_schema_json_matches_cli_compact(app_with_deps, sqlite_env) -> None:  # noqa: F811
    """``GET /<r>/schema?format=json`` bytes equal CLI ``schema --format json --compact``."""
    from doc3gpp.cli import app as cli_app

    app, _ = app_with_deps
    routes = {
        "tsg": "/tsgs/schema",
        "meeting": "/meetings/schema",
        "tdoc": "/tdocs/schema",
        "wi": "/wis/schema",
        "spec": "/specs/schema",
        "testcase": "/testcases/schema",
    }
    runner = CliRunner()
    with TestClient(app) as client:
        for resource, route in routes.items():
            http_resp = client.get(route, params={"format": "json"})
            assert http_resp.status_code == 200, (resource, http_resp.text)
            cli_result = runner.invoke(cli_app, [resource, "schema", "--format", "json", "--compact"])
            assert cli_result.exit_code == 0, (resource, cli_result.output)
            assert http_resp.content.decode("utf-8") == cli_result.output, resource
            assert isinstance(json.loads(http_resp.content), list)


def test_schema_html_and_partial(app_with_deps, sqlite_env) -> None:  # noqa: F811
    app, _ = app_with_deps
    with TestClient(app) as client:
        full = client.get("/tdocs/schema")
        assert full.status_code == 200
        assert "<!DOCTYPE" in full.text
        assert "tdoc_cr_cover_page" in full.text
        assert "changed_functions" in full.text
        partial = client.get("/tdocs/schema", headers={"HX-Request": "true"})
        assert partial.status_code == 200
        assert 'id="results"' in partial.text
        assert "<!DOCTYPE" not in partial.text


def test_mcp_schema_parity_with_http_json(sqlite_env) -> None:
    """MCP ``get_*_schema`` payloads match the HTTP ``?format=json`` bytes."""
    import asyncio

    from fastapi.testclient import TestClient

    from doc3gpp.settings.schema import CacheSettings, MCPSettings, ServerSettings, Settings
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.web.app import build_app

    from tests.integration.test_mcp_end_to_end import _seed_corpus, _state_and_server

    _state_and_server()
    _seed_corpus()
    state, server = _state_and_server()
    app = build_app(
        Settings(
            server=ServerSettings(enabled=True, port=8765),
            mcp=MCPSettings(enabled=True),
            cache=CacheSettings(dir=state.settings.cache.dir),
        )
    )
    cases = [
        ("get_tsg_schema", "/tsgs/schema?format=json"),
        ("get_meeting_schema", "/meetings/schema?format=json"),
        ("get_tdoc_schema", "/tdocs/schema?format=json"),
        ("get_wi_schema", "/wis/schema?format=json"),
        ("get_spec_schema", "/specs/schema?format=json"),
        ("get_testcase_schema", "/testcases/schema?format=json"),
    ]

    async def call(name: str):
        result = await server.call_tool(name, {})
        assert result.is_error is False, result
        return result.content[0].text

    with TestClient(app) as client:
        for tool_name, route in cases:
            mcp_bytes = asyncio.run(call(tool_name))
            http_resp = client.get(route)
            assert http_resp.status_code == 200, (tool_name, http_resp.text)
            assert mcp_bytes == http_resp.content.decode("utf-8"), tool_name

    get_engine.cache_clear()
    del state.engine
