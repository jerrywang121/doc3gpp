from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.spec import Spec, SpecVersion
from doc3gpp.models.spec_doc_zip_inventory import SpecDocZipInventory
from doc3gpp.services.factory import build_spec_service
from doc3gpp.services.spec_doc_service import SpecDocService
from doc3gpp.settings.schema import (
    CacheSettings,
    MCPSettings,
    ServerSettings,
    Settings,
    SpecDocSettings,
)
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.spec_doc_zip_inventory_sql import (
    SQLAlchemySpecDocZipInventoryRepository,
)
from doc3gpp.storage.repositories.spec_sql import SQLAlchemySpecRepository
from doc3gpp.web.app import build_app, build_state
from doc3gpp.web.mcp_server import build_mcp_server


def _seed_main_spec() -> None:
    repo = SQLAlchemySpecRepository()
    spec_id = "36.579-5"
    repo.upsert(Spec(spec_id=spec_id, type="TS", title="NR conformance"))
    repo.upsert_versions(
        [
            SpecVersion(
                spec_id=spec_id,
                version=version,
                ftp_url=f"https://example.test/{version}.zip",
                release=release,
            )
            for version, release in (
                ("19.0.0", "Rel-19"),
                ("18.5.0", "Rel-18"),
                ("17.0.0", "Rel-17"),
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
    show_payload = json.loads(show_result.stdout)
    assert show_payload["versions"][0]["parsed"] is False
    assert show_payload["versions"][0]["zip_files"] is None

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
        show_payload = show_response.json()
        assert show_payload["versions"][0]["parsed"] is False
        assert show_payload["versions"][0]["zip_files"] is None

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
    assert mcp_show["versions"][0]["zip_files"] is None
    assert asyncio.run(call_mcp("list_specs", {"parsed": True})) == []


def test_spec_show_version_key_order_matches_cli_http_and_mcp(
    sqlite_env, monkeypatch, tmp_path
) -> None:
    create_schema("all")
    _seed_main_spec()
    spec_id = "36.579-5"
    inventory_repo = SQLAlchemySpecDocZipInventoryRepository()
    inventory_repo.upsert(
        SpecDocZipInventory(
            spec_id=spec_id,
            version="19.0.0",
            release="Rel-19",
            zip_files=("a.docx", "a.docx"),
        )
    )
    inventory_repo.upsert(
        SpecDocZipInventory(
            spec_id=spec_id,
            version="18.5.0",
            release="Rel-18",
            zip_files=(),
        )
    )

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("spec detail reads must not download or parse ZIP files")

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_list", unexpected_call
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail", unexpected_call
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_dynareport_detail", unexpected_call
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.parse_spec_list", unexpected_call
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.parse_spec_detail", unexpected_call
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_doc_service.fetch_spec_doc_zip", unexpected_call
    )
    monkeypatch.setattr(SpecDocService, "_read_zip_cache", unexpected_call)
    monkeypatch.setattr(SpecDocService, "prepare_parse", unexpected_call)

    settings = Settings(
        server=ServerSettings(enabled=True, port=8765),
        mcp=MCPSettings(enabled=True),
        cache=CacheSettings(dir=sqlite_env.parent / "cache"),
        spec_doc=SpecDocSettings(cache_dir=tmp_path / "spec-doc-cache"),
    )
    cache_probe = SpecDocService(
        spec_repo=SQLAlchemySpecRepository(),
        doc_repo=object(),
        inventory_repo=inventory_repo,
        settings=settings,
    )
    zip_cache = cache_probe._zip_path(spec_id, "19.0.0")
    zip_cache.parent.mkdir(parents=True, exist_ok=True)
    zip_cache.write_bytes(b"stale cached zip")
    zip_cache.unlink()
    assert not zip_cache.exists()

    cli_result = CliRunner().invoke(
        app, ["spec", "show", spec_id, "--format", "json", "--limit", "3"]
    )
    assert cli_result.exit_code == 0, cli_result.stdout
    cli_payload = json.loads(cli_result.stdout)
    cli_keys = list(cli_payload["versions"][0])

    state = build_state(settings)
    server = build_mcp_server(state)
    with TestClient(build_app(settings)) as client:
        http_response = client.get(
            f"/specs/{spec_id}?format=json&limit=3"
        )
        assert http_response.status_code == 200, http_response.text
        http_payload = http_response.json()
        http_keys = list(http_payload["versions"][0])

        async def call_mcp(arguments: dict) -> str:
            result = await server.call_tool("get_spec", arguments)
            assert result.is_error is False
            return result.content[0].text

        mcp_text = asyncio.run(call_mcp({"spec_id": spec_id, "limit": 3}))
        mcp_payload = json.loads(mcp_text)
        mcp_keys = list(mcp_payload["versions"][0])

        assert http_response.content.decode("utf-8") == mcp_text

        html_response = client.get(f"/specs/{spec_id}")
        assert html_response.status_code == 200, html_response.text
        assert "a.docx" in html_response.text
        assert "No files in this ZIP archive." in html_response.text
        assert "ZIP inventory is unavailable for this version." in html_response.text
        assert (
            "doc3gpp spec doc parse --spec 36.579-5 --version 17.0.0 --force"
            in html_response.text
        )
        assert "doc3gpp db init --scope specdata" in html_response.text

        cli_filtered = CliRunner().invoke(
            app,
            [
                "spec", "show", spec_id, "--format", "json",
                "--limit", "1", "--offset", "0", "--version", "18.5.0",
            ],
        )
        assert cli_filtered.exit_code == 0, cli_filtered.stdout
        filtered_cli_payload = json.loads(cli_filtered.stdout)
        filtered_http_response = client.get(
            f"/specs/{spec_id}?format=json&limit=1&offset=0&version=18.5.0"
        )
        assert filtered_http_response.status_code == 200
        filtered_http_payload = filtered_http_response.json()
        filtered_mcp_text = asyncio.run(
            call_mcp(
                {"spec_id": spec_id, "limit": 1, "offset": 0, "version": "18.5.0"}
            )
        )
        filtered_mcp_payload = json.loads(filtered_mcp_text)
        assert filtered_http_response.content.decode("utf-8") == filtered_mcp_text

    assert [row["version"] for row in cli_payload["versions"]] == [
        row["version"] for row in http_payload["versions"]
    ]
    assert [row["version"] for row in http_payload["versions"]] == [
        row["version"] for row in mcp_payload["versions"]
    ]
    expected_zip_files = [["a.docx", "a.docx"], [], None]
    assert [row["zip_files"] for row in cli_payload["versions"]] == expected_zip_files
    assert [row["zip_files"] for row in mcp_payload["versions"]] == expected_zip_files
    assert [row["zip_files"] for row in http_payload["versions"]] == expected_zip_files
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
        "zip_files",
    ]
    for payload in (
        filtered_cli_payload,
        filtered_http_payload,
        filtered_mcp_payload,
    ):
        assert [row["version"] for row in payload["versions"]] == ["18.5.0"]
        assert [row["zip_files"] for row in payload["versions"]] == [[]]


def test_spec_detail_html_escapes_inventory_member_names(sqlite_env) -> None:
    create_schema("all")
    spec_id = "38.331"
    spec_repo = SQLAlchemySpecRepository()
    spec_repo.upsert(Spec(spec_id=spec_id, type="TS", title="NR RRC"))
    spec_repo.upsert_versions(
        [
            SpecVersion(
                spec_id=spec_id,
                version="19.0.0",
                ftp_url="https://example.test/19.0.0.zip",
            )
        ]
    )
    SQLAlchemySpecDocZipInventoryRepository().upsert(
        SpecDocZipInventory(
            spec_id=spec_id,
            version="19.0.0",
            release="Rel-19",
            zip_files=("Café.docx", "<script>alert(1)</script>"),
        )
    )

    with TestClient(build_app(Settings())) as client:
        response = client.get(f"/specs/{spec_id}")

    assert response.status_code == 200, response.text
    assert "Café.docx" in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text


def test_spec_detail_does_not_hide_unrelated_inventory_database_errors(
    sqlite_env, monkeypatch
) -> None:
    import sqlite3

    from sqlalchemy.exc import OperationalError

    create_schema("all")
    _seed_main_spec()

    def fail_inventory_lookup(*_args, **_kwargs):
        raise OperationalError(
            "SELECT from spec_doc_zip_inventory",
            {},
            sqlite3.OperationalError("database is locked"),
        )

    monkeypatch.setattr(
        SQLAlchemySpecDocZipInventoryRepository,
        "get_for_versions",
        fail_inventory_lookup,
    )
    settings = Settings(
        server=ServerSettings(enabled=True, port=8765),
        cache=CacheSettings(dir=sqlite_env.parent / "cache"),
    )
    with TestClient(build_app(settings), raise_server_exceptions=False) as client:
        response = client.get("/specs/36.579-5?format=json")

    assert response.status_code == 500



def test_sdk_versions_batch_inventory_without_network_cache_or_parse(
    sqlite_env, tmp_path, monkeypatch
) -> None:
    create_schema("all")
    spec_id = "38.331"
    spec_repo = SQLAlchemySpecRepository()
    spec_repo.upsert(Spec(spec_id=spec_id, type="TS", title="NR RRC"))
    spec_repo.upsert_versions(
        [
            SpecVersion(
                spec_id=spec_id,
                version=version,
                ftp_url=f"https://example.test/{version}.zip",
                release=release,
            )
            for version, release in (
                ("19.0.0", "Rel-19"),
                ("18.5.0", "Rel-18"),
                ("17.0.0", "Rel-17"),
            )
        ]
    )
    inventory_repo = SQLAlchemySpecDocZipInventoryRepository()
    inventory_repo.upsert(
        SpecDocZipInventory(
            spec_id=spec_id,
            version="19.0.0",
            release="Rel-19",
            zip_files=("a.docx", "a.docx"),
        )
    )
    inventory_repo.upsert(
        SpecDocZipInventory(
            spec_id=spec_id,
            version="18.5.0",
            release="Rel-18",
            zip_files=(),
        )
    )

    cache_service = SpecDocService(
        spec_repo=spec_repo,
        doc_repo=object(),
        inventory_repo=inventory_repo,
        settings=Settings(
            spec_doc=SpecDocSettings(cache_dir=tmp_path / "spec-doc-cache")
        ),
    )
    zip_cache = cache_service._zip_path(spec_id, "19.0.0")
    zip_cache.parent.mkdir(parents=True, exist_ok=True)
    zip_cache.write_bytes(b"stale cached zip")
    zip_cache.unlink()
    assert not zip_cache.exists()

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError(
            "SDK version listing must not network, read cache, or parse"
        )

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_list", unexpected_call
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail", unexpected_call
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_dynareport_detail", unexpected_call
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.parse_spec_list", unexpected_call
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.parse_spec_detail", unexpected_call
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_doc_service.fetch_spec_doc_zip", unexpected_call
    )
    monkeypatch.setattr(SpecDocService, "_read_zip_cache", unexpected_call)
    monkeypatch.setattr(SpecDocService, "prepare_parse", unexpected_call)

    rows = build_spec_service().list_versions(spec_id)

    assert [row.version for row in rows] == ["19.0.0", "18.5.0", "17.0.0"]
    assert [row.release for row in rows] == ["Rel-19", "Rel-18", "Rel-17"]
    assert [row.zip_files for row in rows] == [("a.docx", "a.docx"), (), None]
