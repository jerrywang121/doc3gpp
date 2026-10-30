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



def test_cli_tdoc_url_not_found_lists_all_lookup_tables(monkeypatch) -> None:
    from types import SimpleNamespace

    import pytest
    import typer

    from doc3gpp import cli

    monkeypatch.setattr(cli, "_resolve_compact", lambda compact: compact)
    monkeypatch.setattr(
        cli,
        "build_tdoc_repository",
        lambda: SimpleNamespace(get_by_ftp_url=lambda _url: None),
    )
    monkeypatch.setattr(
        cli,
        "build_tdoc_cr_repository",
        lambda: SimpleNamespace(
            get_by_url=lambda _url: None,
            get_extract_meta_by_url=lambda _url: None,
        ),
    )
    monkeypatch.setattr(
        cli,
        "build_tdoc_cr_ttcn_repository",
        lambda: SimpleNamespace(get_by_url=lambda _url: None),
    )
    monkeypatch.setattr(
        cli,
        "build_tdoc_cr_change_details_repository",
        lambda: SimpleNamespace(get_by_url=lambda _url: None),
    )
    monkeypatch.setattr(
        cli,
        "build_tdoc_file_repository",
        lambda: SimpleNamespace(get_by_ftp_url=lambda _url: ()),
    )
    monkeypatch.setattr(
        cli,
        "build_tdoc_zip_inventory_repository",
        lambda: SimpleNamespace(get_by_url=lambda _url: None),
    )

    with pytest.raises(typer.BadParameter) as caught:
        cli._tdoc_show_by_ftp_url(
            "https://www.3gpp.org/ftp/TSG_RAN/WG5/missing.zip",
            "table",
            None,
        )

    assert (
        "No row in tdocs, tdoc_cr_cover_page, tdoc_cr_ttcn_details, "
        "tdoc_extracts, tdoc_cr_change_details, tdoc_files, or "
        "tdoc_zip_inventory matches ftp_url"
    ) in str(caught.value)
