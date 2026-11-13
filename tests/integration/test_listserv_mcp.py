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


def test_catalog_mcp_filters_match_http_and_tool_schema(surfaces):
    from doc3gpp.storage.db.models import ListservListORM
    from doc3gpp.storage.db.session import get_session_factory

    http, server, _state = surfaces
    with get_session_factory()() as session:
        session.add(ListservListORM(list_id="LEGACY_NULL_LIST"))
        session.commit()
    for filters, expected in (
        ({"list_id": "3GPP_TSG_RAN_WG1", "tsg": "R1"}, ["3GPP_TSG_RAN_WG1"]),
        ({"list_id": "3GPP_TSG_RAN_WG1", "tsg": "R2"}, []),
        ({"list_id": "LEGACY%", "tsg": "null"}, ["LEGACY_NULL_LIST"]),
    ):
        payload = _call_json(server, "listserv_list", filters)
        assert payload == http.get("/api/listserv/lists", params=filters).json()
        assert payload["list_ids"] == expected
        assert [row["list_id"] for row in payload["lists"]] == expected
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    props = tools["listserv_list"].input_schema["properties"]
    assert "LIKE" in props["list_id"]["description"]
    assert "LIKE" in props["tsg"]["description"]


def test_mcp_list_and_index_status_match_http_and_read_without_network(surfaces):
    from doc3gpp.storage.db.models import ListservListORM
    from doc3gpp.storage.db.session import get_session_factory

    http, server, _state = surfaces
    with get_session_factory()() as session:
        session.add(ListservListORM(list_id="LEGACY_NULL_LIST"))
        session.commit()

    http_lists = http.get("/api/listserv/lists").json()
    mcp_lists = _call_json(server, "listserv_list", {})
    assert mcp_lists == http_lists
    assert isinstance(mcp_lists, dict)
    assert set(mcp_lists) == {"list_ids", "lists"}
    assert {"list_id": "LEGACY_NULL_LIST", "tsg": None, "description": None} in mcp_lists["lists"]
    assert {"list_id": "3GPP_TSG_CT", "tsg": "CP", "description": "3GPP_TSG_CT - Core Network and Terminals"} in mcp_lists["lists"]

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
            "queued listserv_sync",
        ),
        (
            "listserv_parse",
            {"list_id": LIST_ID, "date": ["= 2026-10-15"]},
            JobKind.LISTSERV_PARSE,
            {"list_id": LIST_ID, "dates": ["= 2026-10-15"], "force": False},
            "queued listserv_parse",
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
            "queued listserv_index",
        ),
    )
    for name, arguments, kind, expected_params, expected_message in cases:
        result = _call_json(server, name, arguments)
        assert set(result) == {"job_id", "status", "message", "links"}
        job_id = result["job_id"]
        assert job_id
        assert result["status"] == "queued"
        assert result["message"] == expected_message
        assert set(result["links"]) == {"self", "events"}
        assert result["links"] == {
            "self": f"/jobs/{job_id}",
            "events": f"/jobs/{job_id}/events",
        }
        job = state.services.job_repo.get(job_id)
        assert job.kind is kind
        assert dict(job.params) == expected_params


def test_parse_item_mcp_queues_canonical_and_documents_contract(surfaces):
    from mcp.shared.exceptions import MCPError

    _http, server, state = surfaces
    base = "https://list.etsi.org/scripts/wa.exe?A2=team_x;abc.2611B"
    canonical = "https://list.etsi.org/scripts/wa.exe?A2=TEAM_X;abc.2611B&S="
    for url in (base, base + "&S="):
        result = _call_json(server, "listserv_parse_item", {"url": url, "force": True})
        assert result["status"] == "queued"
        job = state.services.job_repo.get(result["job_id"])
        assert job.kind.value == "listserv_parse_item"
        assert dict(job.params) == {"url": canonical, "force": True}
    before = len(state.services.job_repo.list(limit=100))
    with pytest.raises(MCPError):
        asyncio.run(server.call_tool("listserv_parse_item", {"url": "https://evil.example/x"}))
    assert len(state.services.job_repo.list(limit=100)) == before
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    props = tools["listserv_parse_item"].input_schema["properties"]
    assert "url" in props and "force" in props and props["url"]["description"]


def test_packaged_skill_listserv_examples_call_registered_tools(surfaces):
    from pathlib import Path

    _http, server, state = surfaces
    skill = Path("src/doc3gpp/data/skills/doc3gpp/SKILL.md").read_text(encoding="utf-8")
    examples = {}
    for line in skill.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[0] in {"listserv_list", "listserv_parse_item"}:
            examples[parts[0]] = json.loads(parts[1])
    assert set(examples) == {"listserv_list", "listserv_parse_item"}
    catalog = _call_json(server, "listserv_list", examples["listserv_list"])
    assert "3GPP_TSG_SA_WG2" in catalog["list_ids"]
    assert len(catalog["list_ids"]) > 1
    assert all(list_id.startswith("3GPP_TSG_SA") for list_id in catalog["list_ids"])
    queued = _call_json(server, "listserv_parse_item", examples["listserv_parse_item"])
    job = state.services.job_repo.get(queued["job_id"])
    assert job.kind is JobKind.LISTSERV_PARSE_ITEM
    assert job.params["force"] is True
    assert job.params["url"].endswith(";5d45a78f.2611B&S=")


def test_mcp_standard_enqueue_response_shape_is_unchanged(surfaces):
    _http, server, _state = surfaces
    result = _call_json(server, "sync_meetings", {"tsg": "R5"})

    assert set(result) == {"job_id", "status", "message", "links"}
    job_id = result["job_id"]
    assert job_id
    assert result["status"] == "queued"
    assert result["message"] == "queued sync_meetings for TSG R5"
    assert set(result["links"]) == {"self", "events"}
    assert result["links"] == {
        "self": f"/jobs/{job_id}",
        "events": f"/jobs/{job_id}/events",
    }


def test_mcp_show_not_found_is_actionable(surfaces):
    _http, server, _state = surfaces
    from mcp.shared.exceptions import MCPError

    from doc3gpp.web.errors import MCP_CODE_NOT_FOUND

    with pytest.raises(MCPError) as error:
        asyncio.run(server.call_tool("listserv_show", {"url": MISSING_URL}))
    assert error.value.code == MCP_CODE_NOT_FOUND
    assert "not found" in str(error.value).lower()
