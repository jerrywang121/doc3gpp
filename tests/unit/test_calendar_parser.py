from __future__ import annotations

from datetime import date
from pathlib import Path

from doc3gpp.parsers.calendar_parser import parse_3gpp_calendar


def test_parse_3gpp_calendar_sample() -> None:
    fixture_path = Path("tests/fixtures/sample_pages/3GPP-meeting-R5.html")
    html = fixture_path.read_text(encoding="utf-8")

    meetings = parse_3gpp_calendar(html)

    assert len(meetings) == 1
    meeting = meetings[0]
    assert meeting.meeting_id == 85434
    assert meeting.name == "R5--TTCN Workshop#74"
    assert meeting.title == "3GPPRAN5-TTCN Workshop#74"
    assert meeting.location == "Online"
    assert meeting.start_date == date(2026, 7, 2)
    assert meeting.end_date == date(2026, 7, 2)
    assert meeting.ftp_url == "tsg_ran/WG5_Test_ex-T1/Workshop/TSGR5_Workshop_2026/docs/"
    assert meeting.start_doc == "R5w260200"
    assert meeting.end_doc == "R5w260201"
