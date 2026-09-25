from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.spec import Spec, SpecVersion
from doc3gpp.settings.schema import CacheSettings, MCPSettings, ServerSettings, Settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.spec_sql import SQLAlchemySpecRepository
from doc3gpp.web.app import build_app
from doc3gpp.web.app import build_state
from doc3gpp.web.mcp_server import build_mcp_server


def _seed_main_spec() -> None:
    repo = SQLAlchemySpecRepository()
    repo.upsert(Spec(spec_id="36.579-5", type="TS", title="NR conformance"))
    repo.upsert_versions(
        [
            SpecVersion(
                spec_id="36.579-5",
                version="19.2.0",
                ftp_url="https://example.test/19.2.0.zip",
            )
        ]
    )


def test_spec_reads_treat_missing_specdata_ledger_as_empty(sqlite_env) -> None:
    create_schema("main")
    _seed_main_spec()

    cli_runner = CliRunner()
    list_result = cli_runner.invoke(app, ["spec", "list", "--format", "json"])
    assert list_result.exit_code == 0, list_result.stdout
    assert json.loads(list_result.stdout)[0]["parsed"] is None

    show_result = cli_runner.invoke(
        app, ["spec", "show", "36.579-5", "--format", "json"]
    )
    assert show_result.exit_code == 0, show_result.stdout
    assert json.loads(show_result.stdout)["versions"][0]["parsed"] is False

    parsed_result = cli_runner.invoke(
        app, ["spec", "list", "--parsed", "true", "--format", "json"]
    )
    assert parsed_result.exit_code == 0, parsed_result.stdout
    assert json.loads(parsed_result.stdout) == []

    settings = Settings(
        server=ServerSettings(enabled=True, port=8765),
        mcp=MCPSettings(enabled=False),
        cache=CacheSettings(dir=sqlite_env.parent / "cache"),
    )
    with TestClient(build_app(settings)) as client:
        response = client.get("/specs?format=json")
        assert response.status_code == 200, response.text
        assert response.json()[0]["parsed"] is None

        show_response = client.get("/specs/36.579-5?format=json")
        assert show_response.status_code == 200, show_response.text
        assert show_response.json()["versions"][0]["parsed"] is False

        parsed_response = client.get("/specs?format=json&parsed=true")
        assert parsed_response.status_code == 200, parsed_response.text
        assert parsed_response.json() == []

    mcp_state = build_state(
        Settings(
            server=ServerSettings(enabled=True, port=8765),
            mcp=MCPSettings(enabled=True),
            cache=CacheSettings(dir=sqlite_env.parent / "cache"),
        )
    )
    mcp_server = build_mcp_server(mcp_state)

    async def call_mcp(name: str, arguments: dict) -> dict | list:
        result = await mcp_server.call_tool(name, arguments)
        assert result.is_error is False
        return json.loads(result.content[0].text)

    mcp_list = asyncio.run(call_mcp("list_specs", {}))
    assert mcp_list[0]["parsed"] is None
    mcp_show = asyncio.run(
        call_mcp("get_spec", {"spec_id": "36.579-5"})
    )
    assert mcp_show["versions"][0]["parsed"] is False
    assert asyncio.run(call_mcp("list_specs", {"parsed": True})) == []


def test_spec_show_version_key_order_matches_cli_http_and_mcp(sqlite_env) -> None:
    create_schema("all")
    _seed_main_spec()

    cli_result = CliRunner().invoke(
        app, ["spec", "show", "36.579-5", "--format", "json"]
    )
    assert cli_result.exit_code == 0, cli_result.stdout
    cli_keys = list(json.loads(cli_result.stdout)["versions"][0])

    settings = Settings(
        server=ServerSettings(enabled=True, port=8765),
        mcp=MCPSettings(enabled=True),
        cache=CacheSettings(dir=sqlite_env.parent / "cache"),
    )
    state = build_state(settings)
    server = build_mcp_server(state)
    with TestClient(build_app(settings)) as client:
        http_response = client.get("/specs/36.579-5?format=json")
        assert http_response.status_code == 200, http_response.text
        http_keys = list(http_response.json()["versions"][0])

        async def call_mcp() -> str:
            result = await server.call_tool("get_spec", {"spec_id": "36.579-5"})
            assert result.is_error is False
            return result.content[0].text

        mcp_keys = list(json.loads(asyncio.run(call_mcp()))["versions"][0])

    assert cli_keys == http_keys == mcp_keys == [
        "version",
        "parsed",
        "release",
        "ftp_url",
        "meeting_id",
        "meeting_name",
        "upload_date",
        "pdf_url",
        "crs",
    ]
