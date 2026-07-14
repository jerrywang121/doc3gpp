from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from doc3gpp.scraping.client import ScraperClient
from doc3gpp.scraping.portal_source import fetch_tdocs_from_portal


PORTAL_TEMPLATE = "https://portal.3gpp.org/ngppapp/GenerateDocumentList.aspx?meetingId={meeting_id}"
FIXTURE_PATH = Path("tests/fixtures/tdoc_xlsx/TDoc_List_Meeting_85434_portal.xlsx")


def _fixture_bytes() -> bytes:
    return FIXTURE_PATH.read_bytes()


def test_fetch_tdocs_from_portal_returns_parsed_rows() -> None:
    client = MagicMock(spec=ScraperClient)
    client.get_bytes.return_value = _fixture_bytes()

    tdocs = fetch_tdocs_from_portal(
        meeting_id=85434,
        url_template=PORTAL_TEMPLATE,
        client=client,
    )

    assert len(tdocs) > 0
    assert all(t.meeting_id == 85434 for t in tdocs)
    assert any(t.tdoc_id for t in tdocs)
    client.get_bytes.assert_called_once_with(
        "https://portal.3gpp.org/ngppapp/GenerateDocumentList.aspx?meetingId=85434"
    )


def test_fetch_tdocs_from_portal_url_template_must_contain_meeting_id() -> None:
    client = MagicMock(spec=ScraperClient)

    with pytest.raises(ValueError, match="meeting_id"):
        fetch_tdocs_from_portal(
            meeting_id=85434,
            url_template="https://portal.3gpp.org/ngppapp/GenerateDocumentList.aspx",
            client=client,
        )

    client.get_bytes.assert_not_called()


def test_fetch_tdocs_from_portal_propagates_http_error() -> None:
    client = MagicMock(spec=ScraperClient)
    client.get_bytes.side_effect = httpx.HTTPError("portal down")

    with pytest.raises(httpx.HTTPError, match="portal down"):
        fetch_tdocs_from_portal(
            meeting_id=85434,
            url_template=PORTAL_TEMPLATE,
            client=client,
        )


def test_fetch_tdocs_from_portal_uses_injected_client() -> None:
    client = MagicMock(spec=ScraperClient)
    client.get_bytes.return_value = _fixture_bytes()

    fetch_tdocs_from_portal(
        meeting_id=12345,
        url_template=PORTAL_TEMPLATE,
        client=client,
    )

    client.get_bytes.assert_called_once_with(
        "https://portal.3gpp.org/ngppapp/GenerateDocumentList.aspx?meetingId=12345"
    )
