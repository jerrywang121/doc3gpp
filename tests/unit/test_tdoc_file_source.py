"""Unit tests for the auxiliary-TDoc-file scraping path in
``doc3gpp.scraping.ftp_source``."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from doc3gpp.models.tdoc_file import (
    TDocFileTypeRevision,
    TDocFileTypeReview,
    TDocFileTypeSupport,
)
from doc3gpp.scraping.ftp_source import fetch_tdoc_files_from_meeting_ftp


def _client(*, directory_html: dict[str, str]) -> MagicMock:
    """Build a ScraperClient mock whose ``get_text`` returns the configured
    HTML for each subfolder. Missing entries raise ``httpx.HTTPError``
    (simulating a 404)."""

    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False

    def get_text(url: str) -> str:
        for subdir, html in directory_html.items():
            if url.rstrip("/").endswith(subdir.rstrip("/")):
                return html
        raise httpx.HTTPError(f"unexpected URL {url}")

    client.get_text.side_effect = get_text
    return client


def _listing_html(base: str, *filenames: str) -> str:
    body = "".join(
        f'<a class="file" href="{base}{fn}">{fn}</a>' for fn in filenames
    )
    return f"<html><body>{body}</body></html>"


def test_scans_each_known_subfolder_and_collects_unique_files() -> None:
    docs_base = "https://www.3gpp.org/ftp/meeting/Docs/"
    review_base = "https://www.3gpp.org/ftp/meeting/Review/"
    intermediate_crs_base = (
        "https://www.3gpp.org/ftp/meeting/Inbox/Intermediate_CRs/"
    )
    client = _client(
        directory_html={
            "inbox/": _listing_html(
                "https://www.3gpp.org/ftp/meeting/Inbox/", "R5s260001r1.zip"
            ),
            "inbox/intermediate_crs/": _listing_html(
                intermediate_crs_base, "R5-261719r1.zip"
            ),
            "docs/": _listing_html(docs_base, "R5s260001.zip"),
            "review/": _listing_html(
                review_base,
                "R5s260001_MCC160Comments.zip",
                "R5s260001_draft.zip",
            ),
        }
    )

    with patch("doc3gpp.scraping.ftp_source.ScraperClient", return_value=client):
        files = fetch_tdoc_files_from_meeting_ftp(
            "ftp://example.com/meeting/",
            tdoc_ids=["R5s260001", "R5-261719"],
        )

    by_file = {f.file: f for f in files}
    assert by_file["R5s260001r1.zip"].type == TDocFileTypeRevision
    assert by_file["R5s260001_MCC160Comments.zip"].type == TDocFileTypeReview
    assert by_file["R5s260001_draft.zip"].type == TDocFileTypeSupport
    assert by_file["R5-261719r1.zip"].type == TDocFileTypeRevision
    assert by_file["R5-261719r1.zip"].url == (
        f"{intermediate_crs_base}R5-261719r1.zip"
    )
    # Base TDoc ZIP is still skipped even though it appears in the listing.
    assert "R5s260001.zip" not in by_file
    # All five subfolders were attempted; tdocs/ 404'd because the
    # ``directory_html`` map has no entry for it.
    assert client.get_text.call_count == 5


def test_intermediate_crs_subfolder_is_scanned() -> None:
    intermediate_crs_base = (
        "https://www.3gpp.org/ftp/meeting/Inbox/Intermediate_CRs/"
    )
    client = _client(
        directory_html={
            "inbox/intermediate_crs/": _listing_html(
                intermediate_crs_base,
                "R5-261719r1.zip",
                "R5-261719_draft.zip",
                "R5-261719.zip",
            ),
        }
    )

    with patch("doc3gpp.scraping.ftp_source.ScraperClient", return_value=client):
        files = fetch_tdoc_files_from_meeting_ftp(
            "ftp://example.com/meeting/", tdoc_ids=["R5-261719"]
        )

    by_file = {f.file: f for f in files}
    assert by_file["R5-261719r1.zip"].type == TDocFileTypeRevision
    assert by_file["R5-261719_draft.zip"].type == TDocFileTypeSupport
    assert "R5-261719.zip" not in by_file


def test_missing_subfolder_is_silently_skipped() -> None:
    # Only ``docs/`` is reachable; the other four return HTTPError.
    docs_base = "https://www.3gpp.org/ftp/meeting/Docs/"
    client = _client(
        directory_html={"docs/": _listing_html(docs_base, "R5s260001r1.zip")}
    )

    with patch("doc3gpp.scraping.ftp_source.ScraperClient", return_value=client):
        files = fetch_tdoc_files_from_meeting_ftp(
            "tsg_ran/WG5/meeting/", tdoc_ids=["R5s260001"]
        )

    assert len(files) == 1
    assert files[0].type == TDocFileTypeRevision


def test_empty_tdoc_ids_short_circuits() -> None:
    client = _client(directory_html={})
    with patch("doc3gpp.scraping.ftp_source.ScraperClient", return_value=client):
        files = fetch_tdoc_files_from_meeting_ftp(
            "tsg_ran/WG5/meeting/", tdoc_ids=[]
        )
    assert files == []
    assert client.get_text.call_count == 0


def test_duplicate_url_across_subfolders_is_kept_once() -> None:
    base = "https://www.3gpp.org/ftp/meeting/"
    # The same file URL appears under both inbox/ and docs/.
    client = _client(
        directory_html={
            "inbox/": _listing_html(base, "R5s260001r1.zip"),
            "docs/": _listing_html(base, "R5s260001r1.zip"),
        }
    )

    with patch("doc3gpp.scraping.ftp_source.ScraperClient", return_value=client):
        files = fetch_tdoc_files_from_meeting_ftp(
            "tsg_ran/WG5/meeting/", tdoc_ids=["R5s260001"]
        )

    assert len(files) == 1
    assert files[0].file == "R5s260001r1.zip"


def test_terminal_subdir_in_ftp_url_is_stripped_to_avoid_double_scan() -> None:
    base = "https://www.3gpp.org/ftp/meeting/"
    client = _client(
        directory_html={
            "inbox/": _listing_html(base + "Inbox/", "R5s260001r1.zip"),
            "docs/": _listing_html(base + "Docs/"),
        }
    )

    with patch("doc3gpp.scraping.ftp_source.ScraperClient", return_value=client):
        files = fetch_tdoc_files_from_meeting_ftp(
            "tsg_ran/WG5/meeting/docs/", tdoc_ids=["R5s260001"]
        )

    assert len(files) == 1
    assert files[0].file == "R5s260001r1.zip"
