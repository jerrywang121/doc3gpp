"""Integration parity tests for the shared LISTSERV MCP and HTTP surfaces."""

import asyncio
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from doc3gpp.models.jobs import JobKind
from doc3gpp.models.listserv import (
    ArchivePage,
    ListservDetail,
    ListservFilters,
    ListservMessage,
    parse_date_set,
)
from doc3gpp.models.listserv_payloads import listserv_record_payload, listserv_search_payload
from doc3gpp.scraping.listserv_source import ListservSource
from doc3gpp.services.factory import build_listserv_search_service, build_listserv_service
from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import MCPSettings, ServerSettings, Settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.listserv_sql import SQLAlchemyListservRepository
from doc3gpp.web.app import build_app
from doc3gpp.web.mcp_server import build_mcp_server

LIST_ID = "RAN-WG1"
MESSAGE_URL = "https://list.etsi.org/scripts/wa.exe?A2=RAN-WG1;2026-10-15;EXACT"
MISSING_URL = "https://list.etsi.org/scripts/wa.exe?A2=RAN-WG1;missing"


@pytest.fixture()
def surfaces(sqlite_env, disable_auto_sync, monkeypatch):
    def fail_network(*_args, **_kwargs):
        raise AssertionError("a LISTSERV read attempted remote access")

    monkeypatch.setattr(ListservSource, "fetch", fail_network)
    monkeypatch.setenv("DOC3GPP_SERVER__POLL_INTERVAL_SECONDS", "60")
    get_settings.cache_clear()
    create_schema("main")
    repo = SQLAlchemyListservRepository()
    page = ArchivePage(
        list_id=LIST_ID,
        url=f"https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L={LIST_ID}",
        period="2026-10-W2",
    )
    repo.commit_page(
        page,
        [
            ListservMessage(
                url=MESSAGE_URL,
                list_id=LIST_ID,
                subject="Stored R1-2610793",
                sender="Alice",
                date=datetime(2026, 10, 15, 12, 30, tzinfo=timezone.utc),
                date_precision="datetime",
                week="2026-10-W2",
                subject_tdocs=("R1-2610793",),
            )
        ],
        parse_date_set(["= 2026-10-15"]),
        (),
    )
    repo.save_detail(
        ListservDetail(
            url=MESSAGE_URL,
            attachments=(),
            tdocs=("R1-2610793",),
            content="x" * 250,
            content_truncated=True,
            parser_version="1",
            parsed_at=datetime(2026, 10, 16, tzinfo=timezone.utc),
        )
    )
    settings = Settings(
        server=ServerSettings(enabled=True, port=8765),
        mcp=MCPSettings(enabled=True),
    )
    app = build_app(settings)
    with TestClient(app) as http:
        yield http, build_mcp_server(http.app.state.web), http.app.state.web


def _call_json(server, name: str, arguments: dict[str, object]) -> object:
    result = asyncio.run(server.call_tool(name, arguments))
    assert result.is_error is False
    return json.loads(result.content[0].text)


def test_mcp_show_matches_http_at_the_exact_url(surfaces):
    http, server, _state = surfaces
    tool_payload = _call_json(server, "listserv_show", {"url": MESSAGE_URL})
    response = http.get("/api/listserv/show", params={"url": MESSAGE_URL})

    assert response.status_code == 200
    assert tool_payload == response.json()
    assert tool_payload == listserv_record_payload(build_listserv_service().show(MESSAGE_URL))
    assert tool_payload["url"] == MESSAGE_URL
    assert tool_payload["content"] == "x" * 250
    assert tool_payload["content_truncated"] is True


def test_mcp_search_matches_http_with_repeated_dates_and_both_truncation_flags(surfaces):
    http, server, _state = surfaces
    params = [
        ("date", ">= 2026-10-10"),
        ("date", "<= 2026-10-20"),
        ("list_id", LIST_ID),
        ("limit", "5"),
        ("offset", "0"),
    ]
    arguments = {
        "date": [">= 2026-10-10", "<= 2026-10-20"],
        "list_id": LIST_ID,
        "limit": 5,
        "offset": 0,
    }

    tool_payload = _call_json(server, "listserv_search", arguments)
    response = http.get("/api/listserv/search", params=params)
    expected = [
        listserv_search_payload(item)
        for item in build_listserv_search_service().search(
            filters=ListservFilters(
                list_id=LIST_ID,
                dates=parse_date_set([">= 2026-10-10", "<= 2026-10-20"]),
                limit=5,
            )
        )
    ]

    assert response.status_code == 200
    assert tool_payload == response.json() == expected
    assert tool_payload[0]["url"] == MESSAGE_URL
    assert tool_payload[0]["content_truncated"] is True
    assert tool_payload[0]["preview_truncated"] is True


def test_mcp_list_and_index_status_match_http_and_read_without_network(surfaces):
    http, server, _state = surfaces
    assert _call_json(server, "listserv_list", {}) == http.get("/api/listserv/lists").json()
    assert _call_json(server, "listserv_index_status", {}) == http.get(
        "/api/listserv/index/status"
    ).json()


def test_mcp_sync_parse_and_rebuild_enqueue_the_http_job_kinds(surfaces):
    _http, server, state = surfaces
    cases = (
        (
            "listserv_sync",
            {"list_id": "ran-wg1", "date": [">= 2026-10-01"], "force": True},
            JobKind.LISTSERV_SYNC,
            {"list_id": LIST_ID, "dates": [">= 2026-10-01"], "force": True},
        ),
        (
            "listserv_parse",
            {"list_id": LIST_ID, "date": ["= 2026-10-15"]},
            JobKind.LISTSERV_PARSE,
            {"list_id": LIST_ID, "dates": ["= 2026-10-15"], "force": False},
        ),
        (
            "listserv_index_rebuild",
            {"rebuild_all": True},
            JobKind.LISTSERV_INDEX,
            {
                "rebuild": False,
                "rebuild_embeddings": False,
                "rebuild_all": True,
                "resume": False,
                "stale_only": False,
            },
        ),
    )
    for name, arguments, kind, expected_params in cases:
        result = _call_json(server, name, arguments)
        assert result["job_id"]
        assert result["status"] == "queued"
        job = state.services.job_repo.get(result["job_id"])
        assert job.kind is kind
        assert dict(job.params) == expected_params


def test_mcp_show_not_found_is_actionable(surfaces):
    _http, server, _state = surfaces
    from mcp.shared.exceptions import MCPError

    from doc3gpp.web.errors import MCP_CODE_NOT_FOUND

    with pytest.raises(MCPError) as error:
        asyncio.run(server.call_tool("listserv_show", {"url": MISSING_URL}))
    assert error.value.code == MCP_CODE_NOT_FOUND
    assert "not found" in str(error.value).lower()
