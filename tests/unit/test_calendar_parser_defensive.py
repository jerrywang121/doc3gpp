"""Tests for the defensive behaviour of the 3GPP calendar parser.

The production calendar page rarely exposes malformed rows, but layout
regressions on upstream and historically odd rows (missing dates,
swapped dates, etc.) must not abort the whole sync. These tests assert
that the parser logs and skips bad rows while still extracting the
healthy ones.
"""

from __future__ import annotations

from datetime import date

from doc3gpp.parsers.calendar_parser import parse_3gpp_calendar


def _row(
    *,
    meeting_id: int | None = 85434,
    title: str = "TTCN Workshop#74",
    location: str = "Online",
    start_date: str = "2026-07-02",
    end_date: str = "2026-07-02",
    docs_text: str = "R5w260200 - R5w260201",
    docs_href: str | None = "https://www.3gpp.org//ftp/tsg_ran/WG5_Test_ex-T1/docs/",
    file_href: str | None = "https://www.3gpp.org//ftp/tsg_ran/WG5_Test_ex-T1//",
    cell_count: int = 9,
    name: str = "R5--TTCN Workshop#74",
) -> str:
    """Build a minimal `<tr>` for the meetings table.

    ``name`` controls the rendered meeting name link; the link target is
    derived from ``meeting_id``. ``cell_count`` lets us simulate rows that
    have too few cells to even be considered.
    """
    href = "https://portal.3gpp.org/Home.aspx#/meeting"
    if meeting_id is not None:
        href = f"{href}?MtgId={meeting_id}"
    cells = [
        f'<td><a href="{href}" target="_blank">{name}</a></td>',
        f'<td>{title}</td>',
        f'<td>{location}</td>',
        f'<td>{start_date}</td>',
        f'<td>{end_date}</td>',
        f'<td><a href="{docs_href or ""}" target="_blank">{docs_text}</a></td>',
        "<td>Register</td>",
        "<td>Participants</td>",
        f'<td><a href="{file_href or ""}" target="_blank">Files</a></td>',
    ]
    cells = cells[:cell_count]
    return "<tr>" + "".join(cells) + "</tr>"


def _page(rows: list[str], *, body_content: str = "") -> str:
    body = body_content or "<h1>3GPP RAN5 Meetings</h1>" + "".join(rows)
    return (
        "<!DOCTYPE html><html><head><title>3GPP RAN5 Meetings</title></head>"
        f"<body><table class=\"meetings\"><tbody>{''.join(rows)}</tbody></table></body></html>"
        if rows
        else f"<!DOCTYPE html><html><head></head><body>{body}</body></html>"
    )


def test_parse_3gpp_calendar_skips_row_with_unparseable_dates(caplog) -> None:
    page = _page(
        [
            _row(),
            _row(meeting_id=99002, title="Bad date row", start_date="not-a-date"),
        ]
    )

    with caplog.at_level("WARNING", logger="doc3gpp.parsers.calendar_parser"):
        meetings = parse_3gpp_calendar(page)

    assert [m.meeting_id for m in meetings] == [85434]
    assert any("malformed meeting row" in record.message for record in caplog.records)


def test_parse_3gpp_calendar_skips_row_with_swapped_dates(caplog) -> None:
    page = _page(
        [
            _row(),
            _row(
                meeting_id=99003,
                title="Swapped dates",
                start_date="2026-07-10",
                end_date="2026-07-02",
            ),
        ]
    )

    with caplog.at_level("WARNING", logger="doc3gpp.parsers.calendar_parser"):
        meetings = parse_3gpp_calendar(page)

    assert [m.meeting_id for m in meetings] == [85434]
    assert any("after end_date" in record.message for record in caplog.records)


def test_parse_3gpp_calendar_skips_short_row(caplog) -> None:
    page = _page(
        [
            _row(),
            _row(meeting_id=99004, cell_count=4),
        ]
    )

    with caplog.at_level("WARNING", logger="doc3gpp.parsers.calendar_parser"):
        meetings = parse_3gpp_calendar(page)

    assert [m.meeting_id for m in meetings] == [85434]


def test_parse_3gpp_calendar_warns_when_table_missing_on_populated_page(caplog) -> None:
    page = (
        "<!DOCTYPE html><html><head><title>3GPP RAN5 Meetings</title></head>"
        "<body><h1>3GPP RAN5 Meetings</h1></body></html>"
    )

    with caplog.at_level("WARNING", logger="doc3gpp.parsers.calendar_parser"):
        meetings = parse_3gpp_calendar(page)

    assert meetings == []
    assert any("no <table class='meetings'>" in record.message for record in caplog.records)


def test_parse_3gpp_calendar_silent_when_page_truly_empty(caplog) -> None:
    """Empty pages should NOT warn - we only warn when content suggests a layout regression."""

    page = ""

    with caplog.at_level("WARNING", logger="doc3gpp.parsers.calendar_parser"):
        meetings = parse_3gpp_calendar(page)

    assert meetings == []
    assert not any("no <table class='meetings'>" in record.message for record in caplog.records)


def test_parse_3gpp_calendar_detects_cancelled_with_prefix() -> None:
    page = _page([_row(meeting_id=1, title="Cancelled: TTCN Workshop#99")])

    assert parse_3gpp_calendar(page) == []


def test_parse_3gpp_calendar_detects_cancelled_with_suffix_variants() -> None:
    cases = [
        "TTCN Workshop#99 - Cancelled",
        "TTCN Workshop#99 — Cancelled",
        "CANCELLED TTCN Workshop#99",
        "TTCN Workshop#99 CANCELLED",
    ]
    for title in cases:
        page = _page([_row(meeting_id=99, title=title)])
        assert parse_3gpp_calendar(page) == [], title


def test_parse_3gpp_calendar_keeps_healthy_row_dates_and_docs() -> None:
    page = _page([_row()])

    [meeting] = parse_3gpp_calendar(page)
    assert meeting.meeting_id == 85434
    assert meeting.start_date == date(2026, 7, 2)
    assert meeting.end_date == date(2026, 7, 2)
    assert meeting.start_doc == "R5w260200"
    assert meeting.end_doc == "R5w260201"


def test_parse_3gpp_calendar_skips_row_with_empty_doc_range() -> None:
    page = _page([_row(docs_text="", docs_href=None)])

    [meeting] = parse_3gpp_calendar(page)
    assert meeting.start_doc is None
    assert meeting.end_doc is None
