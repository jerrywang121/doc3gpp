"""Offline unit tests for ETSI LISTSERV archive index and listing parsers."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from doc3gpp.models.listserv import ArchivePage
from doc3gpp.parsers.listserv_archive import (
    ListservArchiveParseError,
    parse_archive_index,
    parse_archive_page,
)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "listserv"
BASE_INDEX_URL = "https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE"


@pytest.fixture
def index_html() -> str:
    return (FIXTURES_DIR / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def split_index_html() -> str:
    return (FIXTURES_DIR / "split_week.html").read_text(encoding="utf-8")


@pytest.fixture
def month_only_html() -> str:
    return (FIXTURES_DIR / "month_only.html").read_text(encoding="utf-8")


@pytest.fixture
def empty_html() -> str:
    return (FIXTURES_DIR / "empty.html").read_text(encoding="utf-8")


@pytest.fixture
def pagination_html() -> str:
    return (FIXTURES_DIR / "pagination.html").read_text(encoding="utf-8")


@pytest.fixture
def malformed_html() -> str:
    return (FIXTURES_DIR / "malformed.html").read_text(encoding="utf-8")


def test_archive_index_keeps_both_split_week_urls(split_index_html: str) -> None:
    pages = parse_archive_index(
        split_index_html, "https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE", "EXAMPLE"
    )
    assert {parse_qs(urlsplit(page.url).query)["A1"][0] for page in pages} >= {
        "ind2304D",
        "ind2304D_1",
    }
    split_pages = [p for p in pages if p.period == "2023-04-W4"]
    assert len(split_pages) == 2
    assert [p.url for p in split_pages] == [
        "https://list.etsi.org/scripts/wa.exe?A1=ind2304D_1&L=EXAMPLE",
        "https://list.etsi.org/scripts/wa.exe?A1=ind2304D&L=EXAMPLE",
    ]


def test_archive_index_parses_labels_resolves_urls_and_deduplicates_by_canonical_url(
    index_html: str,
) -> None:
    pages = parse_archive_index(index_html, BASE_INDEX_URL, "example")
    assert all(p.list_id == "EXAMPLE" for p in pages)
    assert [(p.period, p.url) for p in pages] == [
        (
            "2026-10-W2",
            "https://list.etsi.org/scripts/wa.exe?A1=ind2610B&L=EXAMPLE",
        ),
        (
            "2026-10-W1",
            "https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE",
        ),
        (
            "2023-04-W4",
            "https://list.etsi.org/scripts/wa.exe?A1=ind2304D_1&L=EXAMPLE",
        ),
        (
            "2023-04-W4",
            "https://list.etsi.org/scripts/wa.exe?A1=ind2304D&L=EXAMPLE",
        ),
        (
            "2023-04",
            "https://list.etsi.org/scripts/wa.exe?A1=ind2304&L=EXAMPLE",
        ),
    ]


def test_archive_page_parses_messages_timezones_precision_tdocs_and_pagination(
    pagination_html: str,
) -> None:
    page = ArchivePage(
        list_id="example",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE",
        period="2026-10-W1",
    )
    messages, next_urls = parse_archive_page(pagination_html, page)

    assert len(messages) == 3

    first = messages[0]
    assert first.url == "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=1"
    assert first.list_id == "EXAMPLE"
    assert (
        first.subject
        == "[RAN1#126] Résumé MIMO — R1-2610793 r1-2610793 RP-2601234 and R5s260009"
    )
    assert first.sender == "[log in to unmask]"
    assert first.date_precision == "datetime"
    assert first.date.tzinfo is not None
    assert first.date.date() == date(2026, 10, 1)
    assert first.date.hour == 23
    assert first.date.minute == 30
    assert first.date.utcoffset() == timedelta(hours=-7)
    assert first.week == "2026-10-W1"
    assert first.subject_tdocs == ("R1-2610793", "RP-2601234", "R5S260009")

    second = messages[1]
    assert second.url == "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=2"
    assert second.subject == "Re: 会議 agenda update R4-2607922"
    assert second.sender == "WG Chair <[log in to unmask]>"
    assert second.date == datetime(2026, 10, 2, 14, 5, 9)  # noqa: DTZ001
    assert second.date.tzinfo is None
    assert second.date_precision == "datetime"
    assert second.week == "2026-10-W1"
    assert second.subject_tdocs == ("R4-2607922",)

    third = messages[2]
    assert third.url == "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=3"
    assert third.subject == "Draft schedule notice"
    assert third.sender == "[log in to unmask]"
    assert third.date == datetime(2026, 10, 3, 0, 0, 0)  # noqa: DTZ001
    assert third.date.tzinfo is None
    assert third.date_precision == "date"
    assert third.week == "2026-10-W1"
    assert third.subject_tdocs == ()

    assert next_urls == [
        "https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE&O=D&S=51",
        "https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE&O=D&S=101",
    ]


def test_archive_page_month_only_derives_week_buckets_w1_to_w5(
    month_only_html: str,
) -> None:
    page = ArchivePage(
        list_id="EXAMPLE",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2304&L=EXAMPLE",
        period="2023-04",
    )
    messages, next_urls = parse_archive_page(month_only_html, page)

    assert next_urls == []
    assert [m.week for m in messages] == [
        "2023-04-W1",
        "2023-04-W2",
        "2023-04-W3",
        "2023-04-W4",
        "2023-04-W5",
    ]
    msg_apr_29 = messages[-1]
    assert msg_apr_29.date == datetime(2023, 4, 29, 0, 0, 0)  # noqa: DTZ001
    assert msg_apr_29.date_precision == "date"
    assert msg_apr_29.week == "2023-04-W5"
    assert msg_apr_29.subject_tdocs == ("RP-2309999",)



def test_month_only_period_uses_displayed_local_date_across_utc_month_boundary() -> None:
    page = ArchivePage(
        list_id="EXAMPLE",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2603&L=EXAMPLE",
        period="2026-03",
    )
    html = """<!DOCTYPE html><html><body><table>
    <tr><th>Subject</th><th>From</th><th>Date</th></tr>
    <tr><td><a href="/scripts/wa.exe?A2=ind2603&amp;L=EXAMPLE&amp;P=1">Month boundary</a></td>
        <td>Alice</td><td>Sun, 01 Mar 2026 00:30:00 +0200</td></tr>
    </table></body></html>"""

    messages, _ = parse_archive_page(html, page)

    assert len(messages) == 1
    assert messages[0].date == datetime(
        2026, 3, 1, 0, 30, tzinfo=timezone(timedelta(hours=2))
    )
    assert messages[0].week == "2026-03-W1"


def test_archive_page_valid_empty_listing(empty_html: str) -> None:
    page = ArchivePage(
        list_id="EXAMPLE",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE",
        period="2026-10-W1",
    )
    messages, next_urls = parse_archive_page(empty_html, page)
    assert messages == []
    assert next_urls == []


def test_archive_page_valid_empty_with_no_messages_placeholder_row() -> None:
    html = """
    <html><body>
      <table>
        <tr><th>Subject</th><th>From</th><th>Date</th></tr>
        <tr><td colspan="3">No messages found in this period.</td></tr>
      </table>
    </body></html>
    """
    page = ArchivePage(
        list_id="EXAMPLE",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE",
        period="2026-10-W1",
    )
    messages, next_urls = parse_archive_page(html, page)
    assert messages == []
    assert next_urls == []


def test_malformed_or_login_document_raises_parse_error(malformed_html: str) -> None:
    assert issubclass(ListservArchiveParseError, ValueError)
    page = ArchivePage(
        list_id="EXAMPLE",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE",
        period="2026-10-W1",
    )
    with pytest.raises(ListservArchiveParseError):
        parse_archive_page(malformed_html, page)

    with pytest.raises(ListservArchiveParseError):
        parse_archive_index(malformed_html, BASE_INDEX_URL, "EXAMPLE")


@pytest.mark.parametrize(
    "bad_html",
    [
        "",
        "   ",
        "<html><body><p>Server maintenance in progress</p></body></html>",
        "<html><body><table><tr><th>Topic</th><th>Author</th></tr></table></body></html>",
        """<html><body><table>
            <tr><th>Subject</th><th>From</th><th>Date</th></tr>
            <tr><td>Missing anchor</td><td>[log in to unmask]</td><td>2026-10-01</td></tr>
        </table></body></html>""",
        """<html><body><table>
            <tr><th>Subject</th><th>From</th><th>Date</th></tr>
            <tr><td><a href="/scripts/wa.exe?A2=1">Subj</a></td><td>[log in to unmask]</td><td>not-a-date</td></tr>
        </table></body></html>""",
    ],
)
def test_archive_page_rejects_non_listing_and_corrupt_rows(bad_html: str) -> None:
    page = ArchivePage(
        list_id="EXAMPLE",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE",
        period="2026-10-W1",
    )
    with pytest.raises(ListservArchiveParseError):
        parse_archive_page(bad_html, page)


def test_archive_page_supports_rfc2822_and_human_dates_and_preserves_weekly_period_across_month_boundary() -> None:
    html = """
    <html><body>
      <table>
        <tr><td>Date</td><td>From</td><td>Subject</td></tr>
        <tr>
          <td>Thu, 1 May 2026 09:30:00 +0200</td>
          <td>[log in to unmask]</td>
          <td><a href="/scripts/wa.exe?A2=ind2604E&amp;L=EXAMPLE&amp;P=1">May message on April page</a></td>
        </tr>
        <tr>
          <td>2 May 2026</td>
          <td>[log in to unmask]</td>
          <td><a href="/scripts/wa.exe?A2=ind2604E&amp;L=EXAMPLE&amp;P=2">Human date-only row</a></td>
        </tr>
      </table>
    </body></html>
    """
    page = ArchivePage(
        list_id="EXAMPLE",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2604E&L=EXAMPLE",
        period="2026-04-W5",
    )
    messages, _ = parse_archive_page(html, page)
    assert len(messages) == 2
    assert messages[0].date == datetime(
        2026, 5, 1, 9, 30, 0, tzinfo=timezone(timedelta(hours=2))
    )
    assert messages[0].date_precision == "datetime"
    assert messages[0].week == "2026-04-W5"

    assert messages[1].date == datetime(2026, 5, 2, 0, 0, 0)  # noqa: DTZ001
    assert messages[1].date_precision == "date"
    assert messages[1].week == "2026-04-W5"
