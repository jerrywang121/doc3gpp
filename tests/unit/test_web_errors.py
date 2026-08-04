"""Tests for the ``doc3gpp.web.errors`` module.

Locks in the mapping table that the FastAPI exception handlers rely on:
every domain exception raised by the service / repo layers must
produce a deterministic HTTP status + JSON body so route tests (and
downstream operator tooling) can assert on a stable shape.
"""
from __future__ import annotations

import httpx
import pytest

from doc3gpp.models.search import SearchQueryError
from doc3gpp.services.tdoc_cr_service import TDocNotFoundError
from doc3gpp.services.tdoc_sync_coordinator import MeetingNotFoundError
from doc3gpp.web.errors import (
    CacheMissError,
    InvalidFilterError,
    JobAlreadyTerminalError,
    JobNotFoundError,
    SettingsDisabledError,
    TSGNotFoundError,
    WINotFoundError,
    MCP_CODE_CACHE_MISS,
    MCP_CODE_INTERNAL_ERROR,
    MCP_CODE_INVALID_PARAMS,
    MCP_CODE_NOT_FOUND,
    map_domain_error,
    map_mcp_error,
    register_error_handlers,
)


_MAPPING_CASES = [
    pytest.param(TSGNotFoundError, "r5", 404, "tsg_not_found", id="tsg_not_found"),
    pytest.param(WINotFoundError, "wi-7", 404, "wi_not_found", id="wi_not_found"),
    pytest.param(InvalidFilterError, "bad filter", 400, "invalid_filter", id="invalid_filter"),
    pytest.param(JobNotFoundError, "abc", 404, "job_not_found", id="job_not_found"),
    pytest.param(JobAlreadyTerminalError, "succeeded", 409, "job_already_terminal", id="job_already_terminal"),
    pytest.param(SettingsDisabledError, "feature off", 503, "settings_disabled", id="settings_disabled"),
    pytest.param(CacheMissError, "no cached markdown", 404, "cache_miss", id="cache_miss"),
    pytest.param(SearchQueryError, "query has only stopwords", 400, "invalid_query", id="search_query_error"),
]


_LEGACY_DETAIL_CASES = [
    pytest.param(
        TDocNotFoundError, "foo", 404, "tdoc_not_found",
        "TDoc 'foo'", id="tdoc_not_found",
    ),
    pytest.param(
        MeetingNotFoundError, "bar", 404, "meeting_not_found",
        '"detail":"bar"', id="meeting_not_found",
    ),
]


@pytest.mark.parametrize("exc_type, message, status, slug", _MAPPING_CASES)
def test_map_domain_error_named_exceptions(exc_type, message, status, slug) -> None:
    """Every new exception passes ``str(exc)`` straight through as the detail."""
    response = map_domain_error(exc_type(message))
    assert response.status_code == status
    body = response.body.decode("utf-8")
    assert f'"error":"{slug}"' in body, body
    assert f'"detail":"{message}"' in body, body


@pytest.mark.parametrize("exc_type, message, status, slug, expected", _LEGACY_DETAIL_CASES)
def test_map_domain_error_legacy_wrapped_exceptions(
    exc_type, message, status, slug, expected,
) -> None:
    """Wrapped exceptions (``TDocNotFoundError`` / ``MeetingNotFoundError``) keep the input."""
    response = map_domain_error(exc_type(message))
    assert response.status_code == status
    body = response.body.decode("utf-8")
    assert f'"error":"{slug}"' in body, body
    assert expected in body, body


def test_map_domain_error_httpx_http_error_returns_502() -> None:
    """``httpx.HTTPError`` (and subclasses) maps to 502 with the expected body."""
    response = map_domain_error(httpx.ConnectError("upstream down"))
    assert response.status_code == 502
    body = response.body.decode("utf-8")
    assert '"error":"upstream_unavailable"' in body
    assert '"detail":"upstream down"' in body


def test_map_domain_error_cache_miss_surfaces_hint() -> None:
    """``CacheMissError`` carries the spec'd ``hint`` envelope field."""
    response = map_domain_error(
        CacheMissError(
            "No cached markdown for TDoc R5-260001.",
            hint="run: doc3gpp tdoc parse --tdoc R5-260001",
        )
    )
    assert response.status_code == 404
    body = response.body.decode("utf-8")
    assert '"error":"cache_miss"' in body
    assert '"hint":"run: doc3gpp tdoc parse --tdoc R5-260001"' in body


def test_map_domain_error_generic_exception_returns_500() -> None:
    """A bare ``Exception`` maps to 500 + ``request_id`` correlation id."""
    response = map_domain_error(RuntimeError("boom"))
    assert response.status_code == 500
    body = response.body.decode("utf-8")
    assert '"error":"internal_error"' in body
    assert '"detail":"internal server error"' in body
    assert '"request_id":"' in body
    assert body.count('"request_id":"') == 1


def test_register_error_handlers_attaches_handlers() -> None:
    """``register_error_handlers`` registers every mapped exception class."""
    from fastapi import FastAPI

    app = FastAPI()
    register_error_handlers(app)
    handlers = app.exception_handlers
    for exc_type in (
        TDocNotFoundError,
        MeetingNotFoundError,
        TSGNotFoundError,
        WINotFoundError,
        InvalidFilterError,
        JobNotFoundError,
        JobAlreadyTerminalError,
        SettingsDisabledError,
        CacheMissError,
        SearchQueryError,
        httpx.HTTPError,
        Exception,
    ):
        assert exc_type in handlers, f"missing handler for {exc_type.__name__}"


_MCP_MAPPING_CASES = [
    pytest.param(TSGNotFoundError, "r5", MCP_CODE_NOT_FOUND, "tsg", id="tsg_not_found"),
    pytest.param(WINotFoundError, "wi-7", MCP_CODE_NOT_FOUND, "wi", id="wi_not_found"),
    pytest.param(InvalidFilterError, "bad filter", MCP_CODE_INVALID_PARAMS, "filter", id="invalid_filter"),
    pytest.param(JobNotFoundError, "abc", MCP_CODE_NOT_FOUND, "job", id="job_not_found"),
    pytest.param(MeetingNotFoundError, "bar", MCP_CODE_NOT_FOUND, "meeting", id="meeting_not_found"),
    pytest.param(
        SearchQueryError, "query has only stopwords",
        MCP_CODE_INVALID_PARAMS, "query", id="search_query_error",
    ),
]


@pytest.mark.parametrize("exc_type, message, code, resource", _MCP_MAPPING_CASES)
def test_map_mcp_error_mapped_exceptions(exc_type, message, code, resource) -> None:
    """Mapped domain exceptions yield ``(code, message, data)`` with the canonical slug + resource."""
    mapped = map_mcp_error(exc_type(message))
    assert mapped is not None
    mcode, mmsg, data = mapped
    assert mcode == code
    assert mmsg == message
    assert data["resource"] == resource
    assert "error" in data and "detail" in data


def test_map_mcp_error_cache_miss_carries_hint() -> None:
    """``CacheMissError`` maps to -32005 and surfaces the parse hint."""
    mapped = map_mcp_error(
        CacheMissError(
            "No cached markdown for TDoc R5-260001.",
            hint="run: doc3gpp tdoc parse --tdoc R5-260001",
        )
    )
    assert mapped is not None
    mcode, _mmsg, data = mapped
    assert mcode == MCP_CODE_CACHE_MISS
    assert data["resource"] == "tdoc_content"
    assert data["hint"] == "run: doc3gpp tdoc parse --tdoc R5-260001"


def test_map_mcp_error_terminal_and_disabled_map_to_internal() -> None:
    """Terminal-cancel and disabled-feature map to the generic -32603 internal code."""
    assert map_mcp_error(JobAlreadyTerminalError("done"))[0] == MCP_CODE_INTERNAL_ERROR
    assert map_mcp_error(SettingsDisabledError("off"))[0] == MCP_CODE_INTERNAL_ERROR


def test_map_mcp_error_unmapped_returns_none() -> None:
    """A bare ``Exception`` has no MCP mapping and returns ``None``."""
    assert map_mcp_error(RuntimeError("boom")) is None
