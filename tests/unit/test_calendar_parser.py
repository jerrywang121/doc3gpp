from __future__ import annotations

from datetime import date
from pathlib import Path

from doc3gpp.parsers.calendar_parser import parse_3gpp_calendar


def test_parse_3gpp_calendar_sample() -> None:
    """Real-world RAN5 calendar page snapshot — covers multiple parser paths.

    The fixture is a hand-curated subset of the live 3GPP DynaReport page
    (``https://www.3gpp.org/dynareport?code=Meetings-R5.htm``) chosen to
    exercise the parser's edge cases in one place:

    * Future meetings inside and outside the default 2-year window
    * Empty / populated location cells
    * FTP path extraction from the docs cell, with and without a trailing
      ``docs`` segment
    * Real-world date formatting using ``&#8209;`` (U+2011) between digits
    * A CANCELLED row, which the parser must skip silently
    * A long-running "e-mail" meeting whose ``end_date`` is well in the past
    """
    fixture_path = Path("tests/fixtures/sample_pages/3GPP-meeting-R5.html")
    html = fixture_path.read_text(encoding="utf-8")

    meetings = parse_3gpp_calendar(html)

    # The CANCELLED row is filtered out by the parser; six rows remain.
    assert len(meetings) == 6

    by_id = {m.meeting_id: m for m in meetings}

    # TTCN Workshop#74 is preserved from the previous fixture and remains the
    # richest happy-path example (Online, FTP path, doc range).
    workshop = by_id[85434]
    assert workshop.name == "R5--TTCN Workshop#74"
    assert workshop.title == "TTCN Workshop#74"
    assert workshop.location == "Online"
    assert workshop.start_date == date(2026, 7, 2)
    assert workshop.end_date == date(2026, 7, 2)
    # Real-world 3GPP docs href ends with ``\docs`` (no trailing slash).
    assert workshop.ftp_url == "tsg_ran/WG5_Test_ex-T1/Workshop/TSGR5_Workshop_2026/docs"
    assert workshop.start_doc == "R5w260200"
    assert workshop.end_doc == "R5w260201"

    # R5-121 is a future meeting whose location cell is an empty anchor tag.
    r5_121 = by_id[85637]
    assert r5_121.name == "R5-121"
    assert r5_121.title == "3GPPRAN5#121"
    assert r5_121.location == ""
    assert r5_121.start_date == date(2028, 11, 13)
    assert r5_121.end_date == date(2028, 11, 17)
    assert r5_121.ftp_url is None
    assert r5_121.start_doc is None
    assert r5_121.end_doc is None

    # R5-116 has a populated location but no FTP/docs yet (upcoming).
    r5_116 = by_id[82711]
    assert r5_116.location == "Prague"
    assert r5_116.ftp_url is None

    # CANCELLED row (R5-96) must be filtered out of the result.
    assert 39947 not in by_id

    # R5-95-e is a historical electronic meeting with FTP and a doc range.
    r5_95e = by_id[60240]
    assert r5_95e.name == "R5-95-e"
    assert r5_95e.location == "Online"
    assert r5_95e.start_date == date(2022, 5, 9)
    assert r5_95e.end_date == date(2022, 5, 20)
    assert r5_95e.ftp_url == "TSG_RAN/WG5_Test_ex-T1/TSGR5_95_Electronic/docs"
    assert r5_95e.start_doc == "R5-222050"
    assert r5_95e.end_doc == "R5-223886"

    # R5-79 is a historical face-to-face meeting (Busan) with FTP + docs.
    r5_79 = by_id[18788]
    assert r5_79.location == "Busan"
    assert r5_79.start_date == date(2018, 5, 21)
    assert r5_79.ftp_url == "TSG_RAN/WG5_Test_ex-T1/TSGR5_79_Busan/docs"

    # The long-running e-mail meeting: FTP path extracted from the Files cell
    # (cell 8) because cell 5 contains only a ``-`` placeholder.
    email_meeting = by_id[11017]
    assert email_meeting.name == "R5-0-TTCN e-mail 200"
    assert email_meeting.location == "Electronic Meeting"
    assert email_meeting.start_date == date(2005, 3, 1)
    assert email_meeting.end_date == date(2005, 12, 31)
    assert email_meeting.ftp_url == "TSG_RAN/WG5_Test_ex-T1/TTCN_CRs"
    assert email_meeting.start_doc is None
    assert email_meeting.end_doc is None