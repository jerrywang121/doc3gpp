"""Unit tests for the MCP server tool surface.

These are offline unit tests: no live 3GPP network, no FTS5 setup.
The ``build_mcp_server`` factory is invoked against a freshly built
``WebState`` per test (via ``build_state``), and tool calls are driven
through the in-process ``MCPServer.call_tool`` API.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from mcp.shared.exceptions import MCPError


def _server():
    from doc3gpp.settings.loader import get_settings
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.web.app import build_state
    from doc3gpp.web.mcp_server import build_mcp_server

    create_schema()
    state = build_state(get_settings())
    server = build_mcp_server(state)
    return state, server


async def _call(server, name: str, args: dict):
    return await server.call_tool(name, args)


def test_parse_tdoc_url_rejects_non_3gpp_url(sqlite_env) -> None:
    """Non-3GPP URLs raise ``InvalidFilterError`` (clean MCP error, no job)."""
    _, server = _server()

    async def run():
        return await _call(
            server,
            "parse_tdoc_url",
            {"url": "https://example.com/bad.zip"},
        )

    with pytest.raises(MCPError) as exc_info:
        asyncio.run(run())
    assert "3GPP FTP" in exc_info.value.message


def test_parse_tdoc_url_rejects_empty_url(sqlite_env) -> None:
    """Empty URL is treated as a non-3GPP URL → rejected."""
    _, server = _server()

    async def run():
        return await _call(server, "parse_tdoc_url", {"url": ""})

    with pytest.raises(MCPError):
        asyncio.run(run())


def test_parse_tdoc_url_rejects_recursive_with_explicit_max_depth(sqlite_env) -> None:
    """``recursive=True, max_depth=5`` → mutex violation, no job enqueued."""
    _, server = _server()

    async def run():
        return await _call(
            server,
            "parse_tdoc_url",
            {
                "url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/",
                "recursive": True,
                "max_depth": 5,
            },
        )

    with pytest.raises(MCPError) as exc_info:
        asyncio.run(run())
    assert "mutually exclusive" in exc_info.value.message


def test_parse_tdoc_url_enqueues_with_all_defaults(sqlite_env) -> None:
    """Default flags → params carry ``max_depth=2``, ``recursive=False``."""
    state, server = _server()
    captured: list[tuple[object, dict]] = []
    real_create = state.services.job_repo.create

    def capturing_create(kind, params):
        job = real_create(kind, params)
        captured.append((kind, dict(params)))
        return job

    state.services.job_repo.create = capturing_create  # type: ignore[assignment]

    async def run():
        return await _call(
            server,
            "parse_tdoc_url",
            {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/"},
        )

    result = asyncio.run(run())
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "queued"
    kind, params = captured[0]
    from doc3gpp.models.jobs import JobKind

    assert kind is JobKind.PARSE_TDOC_URL
    assert params == {
        "url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/",
        "force": False,
        "full": False,
        "recursive": False,
        "max_depth": 2,
    }


def test_parse_tdoc_url_enqueues_recursive_without_max_depth(sqlite_env) -> None:
    """``recursive=True`` (with default ``max_depth=2``) drops ``max_depth`` from params."""
    state, server = _server()
    captured: list[tuple[object, dict]] = []
    real_create = state.services.job_repo.create

    def capturing_create(kind, params):
        job = real_create(kind, params)
        captured.append((kind, dict(params)))
        return job

    state.services.job_repo.create = capturing_create  # type: ignore[assignment]

    async def run():
        return await _call(
            server,
            "parse_tdoc_url",
            {
                "url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/",
                "recursive": True,
                "force": True,
                "full": True,
            },
        )

    result = asyncio.run(run())
    assert result.is_error is False
    kind, params = captured[0]
    from doc3gpp.models.jobs import JobKind

    assert kind is JobKind.PARSE_TDOC_URL
    assert "max_depth" not in params
    assert params == {
        "url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/",
        "force": True,
        "full": True,
        "recursive": True,
    }


def test_parse_tdoc_url_enqueues_explicit_max_depth(sqlite_env) -> None:
    """``max_depth=3`` is forwarded verbatim."""
    state, server = _server()
    captured: list[tuple[object, dict]] = []
    real_create = state.services.job_repo.create

    def capturing_create(kind, params):
        job = real_create(kind, params)
        captured.append((kind, dict(params)))
        return job

    state.services.job_repo.create = capturing_create  # type: ignore[assignment]

    async def run():
        return await _call(
            server,
            "parse_tdoc_url",
            {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/", "max_depth": 3},
        )

    result = asyncio.run(run())
    assert result.is_error is False
    _, params = captured[0]
    assert params["max_depth"] == 3


def test_mcp_get_tdoc_prefers_url_when_both_supplied(sqlite_env) -> None:
    """Both ``tdoc_id`` and ``ftp_url`` → URL wins, tdoc_id ignored (human ruling)."""
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.models.tdoc import TDoc
    import json

    _, server = _server()
    url = "R5/26.001/R5s260001.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260001", ftp_url=url)
    )

    async def run():
        return await _call(
            server,
            "get_tdoc",
            {"tdoc_id": "other_tdoc", "ftp_url": url},
        )

    payload = json.loads(asyncio.run(run()).content[0].text)
    assert payload["ftp_url"] == url
    assert payload["tdoc"]["tdoc_id"] == "R5s260001"


def test_mcp_get_tdoc_rejects_neither(sqlite_env) -> None:
    """Neither ``tdoc_id`` nor ``ftp_url`` → invalid-params error."""
    _, server = _server()

    async def run():
        return await _call(server, "get_tdoc", {})

    with pytest.raises(MCPError) as exc_info:
        asyncio.run(run())
    assert "exactly one of tdoc_id or ftp_url" in exc_info.value.message


def test_mcp_get_tdoc_by_url_404_on_no_rows(sqlite_env) -> None:
    """Empty DB → ``TDocUrlNotFoundError`` surfaces as an MCP error."""
    _, server = _server()

    async def run():
        return await _call(
            server,
            "get_tdoc",
            {"ftp_url": "TSG_RAN/missing.zip"},
        )

    with pytest.raises(MCPError) as exc_info:
        asyncio.run(run())
    assert "no stored rows match ftp_url" in exc_info.value.message.lower()


def test_mcp_get_tdoc_url_normalisation(sqlite_env) -> None:
    """A full https URL and a bare relative path resolve the same record."""
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.models.tdoc import TDoc
    import json

    _, server = _server()
    bare = "R5/26.001/R5s260001.zip"
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260001", ftp_url=bare))

    async def run(url):
        return await _call(server, "get_tdoc", {"ftp_url": url})

    full_payload = json.loads(asyncio.run(run(f"https://www.3gpp.org/ftp/{bare}")).content[0].text)
    bare_payload = json.loads(asyncio.run(run(bare)).content[0].text)
    assert full_payload == bare_payload


def test_mcp_get_tdoc_by_url_returns_json_envelope(sqlite_env) -> None:
    """The URL-mode JSON envelope mirrors the CLI ``--format json`` shape."""
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.storage.repositories.tdoc_file_sql import SQLAlchemyTDocFileRepository
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails
    from doc3gpp.models.tdoc_file import TDocFile
    import json

    _, server = _server()
    url = "R5/26.001/R5s260001.zip"
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260001", ftp_url=url))
    SQLAlchemyTDocCrRepository().upsert(
        TDocCRDetails(tdoc_id="R5s260001", ftp_url=url, cr_num="0001")
    )
    SQLAlchemyTDocFileRepository().upsert_many(
        [
            TDocFile(
                ftp_url="R5/26.001/R5s260001.zip",
                tdoc_id="R5s260001",
                type="revision",
                file="R5s260001.zip",
            )
        ]
    )

    async def run():
        return await _call(server, "get_tdoc", {"ftp_url": url})

    payload = json.loads(asyncio.run(run()).content[0].text)
    assert payload["ftp_url"] == url
    assert payload["tdoc"]["tdoc_id"] == "R5s260001"
    assert payload["cover"]["cr_num"] == "0001"
    assert len(payload["files"]) == 1


def test_mcp_get_tdoc_existing_tdoc_id_path_unchanged(sqlite_env) -> None:
    """Regression: the ``tdoc_id`` path still works (no behavioural change)."""
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.models.tdoc import TDoc
    import json

    _, server = _server()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260001", ftp_url="x.zip"))

    async def run():
        return await _call(server, "get_tdoc", {"tdoc_id": "R5s260001"})

    payload = json.loads(asyncio.run(run()).content[0].text)
    assert payload["tdoc"]["tdoc_id"] == "R5s260001"
