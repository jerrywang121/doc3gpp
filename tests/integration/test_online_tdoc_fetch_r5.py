from __future__ import annotations

import httpx
import pytest

from doc3gpp.scraping.calendar_source import fetch_calendar
from doc3gpp.scraping.ftp_source import fetch_tdocs_from_meeting_ftp

pytestmark = pytest.mark.online


def _fetch_live_or_skip() -> list:
    try:
        return fetch_calendar("https://www.3gpp.org/dynareport?code=Meetings-R5.htm")
    except httpx.HTTPError as exc:
        pytest.skip(f"online calendar endpoint not reachable in this environment: {exc}")


def _find_meeting(meetings, year: int, has_ttcns: bool = True, workshop: bool | None = None):
    for m in meetings:
        if not getattr(m, "ftp_url", None):
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


def test_online_fetch_tdoc_list_r5_ttcn_workshop_2026() -> None:
    meetings = _fetch_live_or_skip()
    m = _find_meeting(meetings, 2026, has_ttcns=True, workshop=True)
    if m is None:
        pytest.skip("No R5 TTCN workshop meeting in 2026 with FTP url found")

    tdocs = fetch_tdocs_from_meeting_ftp(m.ftp_url, meeting_id=m.meeting_id)
    if not tdocs:
        pytest.skip(f"No accessible TDoc XLSX list found for meeting {m.name} ({m.meeting_id})")
    assert any(td.tdoc_id.lower().startswith("r5w") or td.tdoc_id.lower().startswith("r5-") for td in tdocs)


def test_online_fetch_tdoc_list_r5_ttcn_email_2026() -> None:
    meetings = _fetch_live_or_skip()
    m = _find_meeting(meetings, 2026, has_ttcns=True, workshop=False)
    if m is None:
        pytest.skip("No R5 TTCN email meeting in 2026 with FTP url found")

    tdocs = fetch_tdocs_from_meeting_ftp(m.ftp_url, meeting_id=m.meeting_id)
    if not tdocs:
        pytest.skip(f"No accessible TDoc XLSX list found for meeting {m.name} ({m.meeting_id})")
    assert any(td.tdoc_id.lower().startswith("r5s") or td.tdoc_id.lower().startswith("r5-") for td in tdocs)
