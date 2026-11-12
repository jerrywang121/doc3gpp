"""Unit coverage for LISTSERV MCP registration and argument contracts."""

import asyncio
import json
from datetime import date
from types import SimpleNamespace

import pytest

from doc3gpp.models.jobs import JobKind
from doc3gpp.models.listserv import date_matches
from doc3gpp.web.mcp_server import build_mcp_server


class _JobRepository:
    def __init__(self) -> None:
        self.created: list[tuple[JobKind, dict[str, object]]] = []

    def create(self, kind: JobKind, params: dict[str, object]) -> SimpleNamespace:
        self.created.append((kind, params))
        return SimpleNamespace(id=f"job-{len(self.created)}", status=SimpleNamespace(value="queued"))


def _state() -> tuple[SimpleNamespace, _JobRepository]:
    job_repo = _JobRepository()
    listserv = SimpleNamespace(list_ids=list)
    search_calls: list[dict[str, object]] = []

    def search(**kwargs: object) -> list[object]:
        search_calls.append(kwargs)
        return []

    listserv_search = SimpleNamespace(search=search, search_calls=search_calls)
    listserv_index = SimpleNamespace(status=lambda: SimpleNamespace(to_dict=lambda: {"fts5": {}}))
    services = SimpleNamespace(
        tdoc_content=object(),
        listserv=listserv,
        listserv_search=listserv_search,
        listserv_index=listserv_index,
        job_repo=job_repo,
    )
    return SimpleNamespace(settings=object(), services=services), job_repo


def _json_call(server: object, name: str, arguments: dict[str, object]) -> object:
    result = asyncio.run(server.call_tool(name, arguments))
    assert result.is_error is False
    return json.loads(result.content[0].text)


def test_listserv_tools_are_registered_with_required_exact_list_ids() -> None:
    state, _ = _state()
    server = build_mcp_server(state)

    registered = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {
        "listserv_sync",
        "listserv_parse",
        "listserv_list",
        "listserv_search",
        "listserv_show",
        "listserv_index_status",
        "listserv_index_rebuild",
    } <= registered
    for name in ("listserv_sync", "listserv_parse"):
        schema = server._tool_manager.get_tool(name).parameters
        assert "list_id" in schema["required"]
        assert schema["properties"]["list_id"]["type"] == "string"


def test_listserv_search_schema_and_call_accept_all_filters_modes_and_dates() -> None:
    state, _ = _state()
    server = build_mcp_server(state)
    schema = server._tool_manager.get_tool("listserv_search").parameters
    properties = schema["properties"]
    assert {"date", "list_id", "sender", "subject", "tdocs", "text", "semantic", "limit", "offset"} <= set(properties)
    date_schema = properties["date"]
    date_variants = date_schema.get("anyOf", [date_schema])
    assert any(
        variant.get("type") == "array"
        and variant.get("items", {}).get("type") == "string"
        for variant in date_variants
    )

    result = _json_call(
        server,
        "listserv_search",
        {
            "date": [">= 2026-10-01", "<= 2026-10-31"],
            "list_id": "RAN-WG1",
            "sender": "%Alice%",
            "subject": "%handover%",
            "tdocs": "R1-2610793",
            "text": "handover",
            "semantic": "radio access",
            "limit": 7,
            "offset": 4,
        },
    )
    assert result == []
    call = state.services.listserv_search.search_calls[0]
    filters = call["filters"]
    assert call["text"] == "handover"
    assert call["semantic"] == "radio access"
    assert filters.list_id == "RAN-WG1"
    assert filters.sender == "%Alice%"
    assert filters.subject == "%handover%"
    assert filters.tdocs == "R1-2610793"
    assert filters.limit == 7 and filters.offset == 4
    assert date_matches(filters.dates, date(2026, 10, 15))
    assert len(filters.dates.intervals) == 1


@pytest.mark.parametrize(
    ("name", "kind"),
    [("listserv_sync", JobKind.LISTSERV_SYNC), ("listserv_parse", JobKind.LISTSERV_PARSE)],
)
def test_listserv_batch_tools_enqueue_shared_job_kinds(name: str, kind: JobKind) -> None:
    state, job_repo = _state()
    server = build_mcp_server(state)

    result = _json_call(
        server,
        name,
        {"list_id": "ran-wg1", "date": [">= 2026-10-01", "<= 2026-10-31"], "force": True},
    )
    assert result["job_id"] == "job-1"
    assert result["status"] == "queued"
    assert job_repo.created == [
        (kind, {"list_id": "RAN-WG1", "dates": [">= 2026-10-01", "<= 2026-10-31"], "force": True})
    ]


def test_listserv_read_tools_serialize_empty_arrays_and_status_as_json() -> None:
    state, _ = _state()
    server = build_mcp_server(state)
    state.services.listserv.list_ids = list

    assert _json_call(server, "listserv_list", {}) == {"list_ids": []}
    assert _json_call(server, "listserv_search", {}) == []
    assert _json_call(server, "listserv_index_status", {}) == {"fts5": {}}


def test_listserv_index_rebuild_enqueues_through_shared_validation() -> None:
    state, job_repo = _state()
    server = build_mcp_server(state)

    result = _json_call(
        server,
        "listserv_index_rebuild",
        {"rebuild_all": True, "batch": 9, "resume": True, "stale_only": True},
    )
    assert result["job_id"] == "job-1"
    assert job_repo.created == [
        (
            JobKind.LISTSERV_INDEX,
            {
                "rebuild": False,
                "rebuild_embeddings": False,
                "rebuild_all": True,
                "resume": True,
                "stale_only": True,
                "batch": 9,
            },
        )
    ]


def test_listserv_sync_rejects_invalid_date_before_enqueuing() -> None:
    state, job_repo = _state()
    server = build_mcp_server(state)
    from mcp.shared.exceptions import MCPError

    with pytest.raises(MCPError, match="date"):
        asyncio.run(
            server.call_tool(
                "listserv_sync",
                {"list_id": "RAN-WG1", "date": ["not-a-date"]},
            )
        )
    assert job_repo.created == []



def test_listserv_search_corruption_maps_resource_and_rebuild_hint() -> None:
    from mcp.shared.exceptions import MCPError

    from doc3gpp.models.search import SearchIndexCorruptError

    state, _ = _state()

    def corrupt(**_kwargs: object) -> list[object]:
        raise SearchIndexCorruptError("broken LISTSERV FTS index")

    state.services.listserv_search.search = corrupt
    server = build_mcp_server(state)

    with pytest.raises(MCPError) as exc_info:
        asyncio.run(
            server.call_tool("listserv_search", {"text": "handover"})
        )

    error = exc_info.value
    assert error.code == -32603
    assert error.message == "broken LISTSERV FTS index"
    assert error.data == {
        "error": "search_index_corrupt",
        "detail": "broken LISTSERV FTS index",
        "resource": "listserv",
        "hint": "run: doc3gpp listserv index --rebuild",
        "id": "broken LISTSERV FTS index",
    }


def test_generic_search_corruption_keeps_generic_mcp_mapping() -> None:
    from doc3gpp.models.search import SearchIndexCorruptError
    from doc3gpp.web.errors import SearchIndexCorruptWebError, map_mcp_error

    mapped = map_mcp_error(SearchIndexCorruptError("broken generic index"))

    assert mapped is not None
    code, message, data = mapped
    assert code == -32603
    assert message == "broken generic index"
    assert data == {
        "error": "search_index_corrupt",
        "detail": "broken generic index",
        "resource": "search",
        "id": "broken generic index",
    }

    assert (
        map_mcp_error(
            SearchIndexCorruptWebError("broken spec-doc index", resource="spec_doc")
        )
        is None
    )
