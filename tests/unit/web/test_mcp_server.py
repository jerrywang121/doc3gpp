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
