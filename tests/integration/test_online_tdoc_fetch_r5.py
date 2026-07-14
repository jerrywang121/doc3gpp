from __future__ import annotations

import httpx
import pytest

from doc3gpp.scraping.calendar_source import fetch_calendar
from doc3gpp.scraping.portal_source import fetch_tdocs_from_portal

pytestmark = pytest.mark.online

PORTAL_TEMPLATE = "https://portal.3gpp.org/ngppapp/GenerateDocumentList.aspx?meetingId={meeting_id}"


def _fetch_live_or_skip() -> list:
    try:
        return fetch_calendar("https://www.3gpp.org/dynareport?code=Meetings-R5.htm")
    except httpx.HTTPError as exc:
        pytest.skip(f"online calendar endpoint not reachable in this environment: {exc}")


def _find_meeting(meetings, year: int, has_ttcns: bool = True, workshop: bool | None = None):
    for m in meetings:
        if not getattr(m, "meeting_id", None):
            continue
        if not getattr(m, "start_date", None) or m.start_date.year != year:
            continue
        title = (m.title or m.name or "").lower()
        if has_ttcns and "ttcn" not in title:
            continue
        if workshop is True and "workshop" not in title:
            continue
        if workshop is False and "workshop" in title:
            continue
        return m
    return None


def test_online_fetch_tdoc_list_r5_ttcn_workshop_2025() -> None:
    meetings = _fetch_live_or_skip()
    m = _find_meeting(meetings, 2025, has_ttcns=True, workshop=True)
    if m is None:
        pytest.skip("No R5 TTCN workshop meeting in 2025 found")

    tdocs = fetch_tdocs_from_portal(
        meeting_id=m.meeting_id, url_template=PORTAL_TEMPLATE
    )
    if not tdocs:
        pytest.skip(f"No accessible TDoc list found for meeting {m.name} ({m.meeting_id})")
    assert any(td.tdoc_id.lower().startswith("r5w") or td.tdoc_id.lower().startswith("r5-") for td in tdocs)


def test_online_fetch_tdoc_list_r5_ttcn_email_2025() -> None:
    meetings = _fetch_live_or_skip()
    m = _find_meeting(meetings, 2025, has_ttcns=True, workshop=False)
    if m is None:
        pytest.skip("No R5 TTCN email meeting in 2025 found")

    tdocs = fetch_tdocs_from_portal(
        meeting_id=m.meeting_id, url_template=PORTAL_TEMPLATE
    )
    if not tdocs:
        pytest.skip(f"No accessible TDoc list found for meeting {m.name} ({m.meeting_id})")
    assert any(td.tdoc_id.lower().startswith("r5s") or td.tdoc_id.lower().startswith("r5-") for td in tdocs)
