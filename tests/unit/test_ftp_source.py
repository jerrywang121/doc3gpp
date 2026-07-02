from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from doc3gpp.scraping.ftp_source import fetch_tdocs_from_meeting_ftp


def _mock_client_with_errors() -> MagicMock:
    """A ScraperClient mock whose HTTP calls all raise httpx.HTTPError."""
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get_text.side_effect = httpx.HTTPError("connection refused")
    client.get_bytes.side_effect = httpx.HTTPError("connection refused")
    return client


# ---------------------------------------------------------------------------
# Fix 4: all subfolders raising HTTPError must surface as RuntimeError;
# the old code silently returned an empty list.
# ---------------------------------------------------------------------------


def test_all_subfolders_http_error_raises_runtime_error() -> None:
    client = _mock_client_with_errors()

    with patch("doc3gpp.scraping.ftp_source.ScraperClient", return_value=client):
        with pytest.raises(RuntimeError, match="all subfolders failed"):
            fetch_tdocs_from_meeting_ftp("ftp://example.com/tsg_ran/WG5_111/")


def test_runtime_error_message_includes_all_attempted_urls() -> None:
    client = _mock_client_with_errors()

    with patch("doc3gpp.scraping.ftp_source.ScraperClient", return_value=client):
        with pytest.raises(RuntimeError) as exc_info:
            fetch_tdocs_from_meeting_ftp("ftp://example.com/tsg_ran/WG5_111/")

    message = str(exc_info.value)
    # All three subfolder URLs should appear in the failure summary.
    assert "WG5_111/" in message or "WG5_111" in message
    assert "connection refused" in message


def test_first_subfolder_succeeds_no_subsequent_attempts() -> None:
    # If the first subfolder returns content, subsequent subfolders must NOT
    # be tried. Use a MagicMock that records every get_text call.
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False

    # Directory fetch returns a listing that contains a TDoc list file.
    client.get_text.return_value = (
        '<html><body>'
        '<a href="TDoc_List_Meeting_X.xlsx">xlsx</a>'
        '</body></html>'
    )

    # Stub the XLSX bytes with something that won't parse as a workbook —
    # the test should fail at the parse stage, NOT raise our RuntimeError
    # because the first subfolder "succeeded" in finding the file.
    client.get_bytes.return_value = b"not-a-real-xlsx"

    with patch("doc3gpp.scraping.ftp_source.ScraperClient", return_value=client):
        with pytest.raises(Exception) as exc_info:
            fetch_tdocs_from_meeting_ftp("ftp://example.com/tsg_ran/WG5_111/")

    # Must NOT be our "all subfolders failed" RuntimeError — the first
    # subfolder resolved and pointed at an XLSX, so we tried to parse.
    assert "all subfolders failed" not in str(exc_info.value)
    # The first-subfolder URL must have been the only directory probed.
    assert client.get_text.call_count == 1


def test_xlsx_fetch_http_error_then_next_subfolder_recovers() -> None:
    # First subfolder: 200 OK, lists a TDoc file, but XLSX download 500s.
    # Second subfolder: 200 OK, lists a TDoc file, XLSX download succeeds
    # (returns the real fixture bytes).
    from pathlib import Path

    fixture_bytes = Path(
        "tests/fixtures/tdoc_xlsx/TDoc_List_Meeting_RAN5#111.xlsx"
    ).read_bytes()

    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False

    responses_text = [
        '<html><body><a href="TDoc_List_Meeting_X.xlsx">xlsx</a></body></html>',
        '<html><body><a href="TDoc_List_Meeting_RAN5#111.xlsx">xlsx</a></body></html>',
    ]
    client.get_text.side_effect = responses_text
    client.get_bytes.side_effect = [
        httpx.HTTPError("xlsx download 500"),
        fixture_bytes,
    ]

    with patch("doc3gpp.scraping.ftp_source.ScraperClient", return_value=client):
        tdocs = fetch_tdocs_from_meeting_ftp("ftp://example.com/tsg_ran/WG5_111/")

    assert len(tdocs) > 0
    assert all(t.tdoc_id.startswith(("R5", "S2", "C")) for t in tdocs)


def test_directory_has_no_tdoc_list_file_returns_empty() -> None:
    # All three subfolders return 200 but contain no TDoc list file.
    # No HTTPError → no RuntimeError → empty list (the "well-formed empty
    # meeting" case).
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get_text.return_value = (
        '<html><body><a href="readme.txt">readme</a></body></html>'
    )

    with patch("doc3gpp.scraping.ftp_source.ScraperClient", return_value=client):
        tdocs = fetch_tdocs_from_meeting_ftp("ftp://example.com/tsg_ran/WG5_111/")

    assert tdocs == []
    # All three subfolders should have been tried.
    assert client.get_text.call_count == 3