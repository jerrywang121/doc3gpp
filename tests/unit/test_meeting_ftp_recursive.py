"""Meeting FTP auxiliary files are discovered from bounded folder listings."""

from datetime import date
from unittest.mock import MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError

from doc3gpp.models.meeting import Meeting
from doc3gpp.scraping.ftp_source import fetch_tdoc_files_from_meeting_ftp
from doc3gpp.services.tdoc_sync_coordinator import TDocSyncCoordinator
from doc3gpp.settings.schema import SyncSettings


def _client(pages: dict[str, str]) -> MagicMock:
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False

    def get_text(url: str) -> str:
        try:
            return pages[url.lower()]
        except KeyError:
            raise httpx.HTTPError(f"missing listing: {url}") from None

    client.get_text.side_effect = get_text
    return client


def test_nested_folder_at_meeting_depth_four_is_scanned() -> None:
    root = "https://www.3gpp.org/ftp/meeting/"
    pages = {
        root + "inbox/": '<a href="Drafts/">Drafts</a>',
        root + "inbox/drafts/": (
            '<tr><td><img class="icon" src="/ftp/geticon.axd?file="></td>'
            '<td><a href="%5B101-e%5D%5B108%5D%20NR_Baskets">basket</a></td></tr>'
        ),
        root + "inbox/drafts/%5b101-e%5d%5b108%5d%20nr_baskets/": '<a href="Round%201/">Round 1</a>',
        root + "inbox/drafts/%5b101-e%5d%5b108%5d%20nr_baskets/round%201/": (
            '<a class="file" href="REV%20R4-2117581%20TP.docx">REV R4-2117581 TP.docx</a>'
            '<a href="too-deep/">too deep</a>'
        ),
        root + "inbox/drafts/%5b101-e%5d%5b108%5d%20nr_baskets/round%201/too-deep/": (
            '<a class="file" href="R4-2117581-too-deep.zip">R4-2117581-too-deep.zip</a>'
        ),
    }
    client = _client(pages)
    with patch("doc3gpp.scraping.ftp_source.ScraperClient", return_value=client):
        files = fetch_tdoc_files_from_meeting_ftp("meeting/", ["R4-2117581"])

    assert [(item.tdoc_id, item.type, item.file) for item in files] == [
        ("R4-2117581", "revision", "REV R4-2117581 TP.docx")
    ]
    assert not any("too-deep" in call.args[0] for call in client.get_text.call_args_list)


def test_folder_classification_and_first_known_id() -> None:
    root = "https://www.3gpp.org/ftp/meeting/"
    pages = {
        root + "inbox/": (
            '<a class="file" href="Draft%20R5-123456.docx">Draft R5-123456.docx</a>'
            '<a class="file" href="notes_R5-123457_R5-123456.pptx">notes_R5-123457_R5-123456.pptx</a>'
            '<a class="file" href="R5-123456.zip">R5-123456.zip</a>'
            '<a class="file" href="R5-123456.txt">R5-123456.txt</a>'
        ),
        root + "review/": (
            '<a class="file" href="x_R5-123456_MCC160COMMENTS.doc">x_R5-123456_MCC160COMMENTS.doc</a>'
            '<a class="file" href="draft_R5-123456.ppt">draft_R5-123456.ppt</a>'
        ),
        root + "tdoc/": '<a class="file" href="R5-123456r1.ZIP">R5-123456r1.ZIP</a>',
    }
    client = _client(pages)
    with patch("doc3gpp.scraping.ftp_source.ScraperClient", return_value=client):
        files = fetch_tdoc_files_from_meeting_ftp(
            "meeting/", ["R5-123456", "R5-123457"]
        )

    by_name = {item.file: (item.tdoc_id, item.type) for item in files}
    assert by_name == {
        "Draft R5-123456.docx": ("R5-123456", "draft"),
        "notes_R5-123457_R5-123456.pptx": ("R5-123457", "revision"),
        "x_R5-123456_MCC160COMMENTS.doc": ("R5-123456", "review"),
        "draft_R5-123456.ppt": ("R5-123456", "support"),
        "R5-123456r1.ZIP": ("R5-123456", "revision"),
    }


def test_group_scoping_prefers_first_known_id_for_meeting_group() -> None:
    root = "https://www.3gpp.org/ftp/meeting/"
    pages = {
        root + "docs/": (
            '<a class="file" href="R4-000001_R5-999999_R5-123456.pptx">'
            'R4-000001_R5-999999_R5-123456.pptx</a>'
            '<a class="file" href="R4-000001.doc">R4-000001.doc</a>'
        )
    }
    client = _client(pages)
    with patch("doc3gpp.scraping.ftp_source.ScraperClient", return_value=client):
        files = fetch_tdoc_files_from_meeting_ftp(
            "meeting/", ["R4-000001", "R5-123456"], tsg="R5"
        )
    assert [(item.tdoc_id, item.type) for item in files] == [("R5-123456", "revision")]


def test_ambiguous_missing_group_does_not_scan() -> None:
    client = _client({})
    with patch("doc3gpp.scraping.ftp_source.ScraperClient", return_value=client):
        assert fetch_tdoc_files_from_meeting_ftp(
            "meeting/", ["R4-000001", "R5-123456"]
        ) == []
    client.get_text.assert_not_called()


def test_only_direct_children_of_current_root_are_followed_and_deduplicated() -> None:
    root = "https://www.3gpp.org/ftp/meeting/"
    pages = {
        root + "inbox/": (
            '<a href="sub/">sub</a><a href="sub">duplicate</a><a href="%73ub/">alias</a>'
            '<a href="%2573ub/">nested alias</a>'
            '<a href="../review/">other root</a><a href="%2e%2e/other/">escape</a>'
            '<a href="https://evil.example/ftp/meeting/inbox/evil/">off host</a>'
            '<a href="?sort=name">sort</a>'
        ),
        root + "inbox/sub/": (
            '<a href="../">parent</a>'
            '<a class="file" href="R5-123456r1.zip">R5-123456r1.zip</a>'
            '<a class="file" href="R5-123456r1.zip">R5-123456r1.zip</a>'
            '<a class="file" href="%25525-123456r1.zip">R5-123456r1.zip</a>'
            '<a class="file" href="https://evil.example/R5-123456r2.zip">R5-123456r2.zip</a>'
        ),
    }
    client = _client(pages)
    with patch("doc3gpp.scraping.ftp_source.ScraperClient", return_value=client):
        files = fetch_tdoc_files_from_meeting_ftp("meeting/", ["R5-123456"])
    assert len(files) == 1
    requested = [call.args[0] for call in client.get_text.call_args_list]
    assert sum("/sub/" in url or "%73ub/" in url or "%2573ub/" in url for url in requested) == 1
    assert not any("evil" in url or "%2e%2e" in url or "?" in url for url in requested)


def test_nested_url_encoding_cannot_hide_a_directory_escape() -> None:
    root = "https://www.3gpp.org/ftp/meeting/"
    client = _client({
        root + "inbox/": '<a href="%252e%252e%252foutside/">escape</a>',
    })
    with patch("doc3gpp.scraping.ftp_source.ScraperClient", return_value=client):
        fetch_tdoc_files_from_meeting_ftp("meeting/", ["R5-123456"])
    assert not any("%252e" in call.args[0].lower() for call in client.get_text.call_args_list)


def test_max_depth_can_exclude_nested_listings() -> None:
    root = "https://www.3gpp.org/ftp/meeting/"
    pages = {root + "inbox/": '<a href="sub/">sub</a>'}
    client = _client(pages)
    with patch("doc3gpp.scraping.ftp_source.ScraperClient", return_value=client):
        files = fetch_tdoc_files_from_meeting_ftp(
            "meeting/", ["R5-123456"], max_depth=1
        )
    assert files == []
    assert all("/sub/" not in call.args[0] for call in client.get_text.call_args_list)


def test_sync_settings_have_validated_ftp_depth() -> None:
    assert getattr(SyncSettings(), "tdoc_ftp_max_depth", None) == 4
    assert SyncSettings(tdoc_ftp_max_depth=2).tdoc_ftp_max_depth == 2
    with pytest.raises(ValidationError):
        SyncSettings(tdoc_ftp_max_depth=-1)


def test_meeting_group_and_configured_depth_reach_file_scan() -> None:
    meeting_repo = MagicMock()
    meeting_repo.get_by_id.return_value = Meeting(
        meeting_id=1, name="meeting", title="", location="",
        start_date=date(2026, 1, 1), end_date=date(2026, 1, 2),
        ftp_url="meeting/", tsg="R5",
    )
    tdoc_repo = MagicMock()
    tdoc_repo.list_tdoc_ids_for_meeting.return_value = ["R5-123456", "R4-000001"]
    coordinator = TDocSyncCoordinator(
        meeting_repo, tdoc_repo, MagicMock(), tdoc_ftp_max_depth=2
    )
    with (
        patch.object(coordinator._tdocs, "sync_tdoc_list", return_value=2),
        patch("doc3gpp.services.tdoc_file_service.fetch_tdoc_files_from_meeting_ftp") as fetch,
    ):
        fetch.return_value = []
        coordinator.sync_for_meeting_id(1)
    fetch.assert_called_once_with(
        "meeting/", ["R5-123456", "R4-000001"], tsg="R5", max_depth=2
    )
