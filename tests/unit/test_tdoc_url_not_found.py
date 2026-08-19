"""Unit tests for the new TDocUrlNotFoundError exception class."""
from __future__ import annotations

from doc3gpp.services.tdoc_cr_service import TDocUrlNotFoundError
from doc3gpp.web.errors import _ERROR_SLUGS, _MCP_RESOURCE_BY_EXC, _STATUS_BY_EXC


def test_tdoc_url_not_found_is_lookup_error() -> None:
    err = TDocUrlNotFoundError("TSG_RAN/WG5/foo.zip")
    assert isinstance(err, LookupError)
    assert err.ftp_url == "TSG_RAN/WG5/foo.zip"


def test_tdoc_url_not_found_message_mentions_sync_hint() -> None:
    err = TDocUrlNotFoundError("TSG_RAN/WG5/foo.zip")
    msg = str(err)
    assert "TSG_RAN/WG5/foo.zip" in msg
    assert "doc3gpp tdoc sync" in msg
    assert "doc3gpp tdoc parse --from-url" in msg


def test_tdoc_url_not_found_is_registered_in_error_slugs() -> None:
    assert _ERROR_SLUGS[TDocUrlNotFoundError] == "tdoc_url_not_found"


def test_tdoc_url_not_found_is_registered_in_mcp_table() -> None:
    resource, code = _MCP_RESOURCE_BY_EXC[TDocUrlNotFoundError]
    assert resource == "tdoc"
    # MCP_CODE_NOT_FOUND is -32004
    assert code == -32004


def test_tdoc_url_not_found_is_registered_in_status_table() -> None:
    assert _STATUS_BY_EXC[TDocUrlNotFoundError] == 404
