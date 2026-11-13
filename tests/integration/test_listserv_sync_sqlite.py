"""SQLite integration tests for coverage-aware LISTSERV sync orchestration."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from unittest.mock import patch

import httpx
import pytest

from doc3gpp.models.listserv import (
    ArchivePage,
    DateInterval,
    DateSet,
    ListservDetail,
    ListservFilters,
)
from doc3gpp.repository.listserv import ListservRepository
from doc3gpp.scraping.listserv_source import FetchedPage, archive_index_url
from doc3gpp.services.factory import build_listserv_sync_service
from doc3gpp.services.listserv_sync import ListservSyncService
from doc3gpp.settings.schema import Settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import ListservListORM
from doc3gpp.storage.db.session import get_session_factory
from doc3gpp.storage.repositories.listserv_sql import SQLAlchemyListservRepository


def _index_html(links: list[tuple[str, str]]) -> str:
    items = "\n".join(f'<li><a href="{href}">{label}</a></li>' for href, label in links)
    return f"<html><head><title>Archive Index</title></head><body><ul>{items}</ul></body></html>"


def _listing_html(
    rows: list[tuple[str, str, str, str]],
    *,
    next_links: list[tuple[str, str]] | None = None,
    empty_notice: str | None = None,
) -> str:
    tr_parts = ["<tr><th>Subject</th><th>From</th><th>Date</th></tr>"]
    if empty_notice is not None and not rows:
        tr_parts.append(f'<tr><td colspan="3">{empty_notice}</td></tr>')
    for detail_href, subject, sender, date_str in rows:
        tr_parts.append(
            f'<tr><td><a href="{detail_href}">{subject}</a></td>'
            f"<td>{sender}</td><td>{date_str}</td></tr>"
        )
    nav = ""
    if next_links:
        nav_anchors = " ".join(
            f'<a href="{href}" rel="next">{label}</a>' for href, label in next_links
        )
        nav = f"<div class='pagination'>{nav_anchors}</div>"
    return (
        "<html><head><title>Archive Listing</title></head><body>"
        f"{nav}<table>{''.join(tr_parts)}</table></body></html>"
    )


def _listserv_catalog_metadata(list_id: str) -> tuple[str | None, str | None] | None:
    with get_session_factory()() as session:
        row = session.get(ListservListORM, list_id)
        if row is None:
            return None
        return row.tsg, row.description


@dataclass
class RecordingFakeSource:
    """Thread-safe fake LISTSERV source that records fetched URLs and concurrency."""

    pages: dict[
        str,
        str | FetchedPage | Exception | Callable[[str], str | FetchedPage],
    ] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)
    active_fetches: int = 0
    max_active_fetches: int = 0
    on_fetch: Callable[[str], None] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def fetch(self, url: str) -> FetchedPage:
        with self._lock:
            self.requests.append(url)
            self.active_fetches += 1
            self.max_active_fetches = max(
                self.max_active_fetches, self.active_fetches
            )
        try:
            if self.on_fetch is not None:
                self.on_fetch(url)
            entry = self.pages.get(url)
            if entry is None:
                req = httpx.Request("GET", url)
                resp = httpx.Response(404, request=req)
                raise httpx.HTTPStatusError("HTTP 404", request=req, response=resp)
            if isinstance(entry, Exception):
                raise entry
            if isinstance(entry, FetchedPage):
                return entry
            result = entry(url) if callable(entry) else entry
            if isinstance(result, FetchedPage):
                return result
            return FetchedPage(text=result, content_type="text/html", final_url=url)
        finally:
            with self._lock:
                self.active_fetches -= 1

    def clear_requests(self) -> None:
        with self._lock:
            self.requests.clear()
            self.max_active_fetches = 0


@pytest.fixture()
def repository(sqlite_env) -> ListservRepository:
    create_schema("main")
    return SQLAlchemyListservRepository()


@pytest.fixture()
def april_may_url() -> str:
    return "https://list.etsi.org/scripts/wa.exe?A2=ind2604E&L=EXAMPLE&P=2"


@pytest.fixture()
def fake_source(april_may_url: str) -> RecordingFakeSource:
    index_url = archive_index_url("EXAMPLE")
    may_page_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2605A&L=EXAMPLE"
    april_page_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2604E&L=EXAMPLE"
    return RecordingFakeSource(
        pages={
            index_url: _index_html(
                [
                    (may_page_url, "May 2026, Week 1"),
                    (april_page_url, "April 2026, Week 5"),
                ]
            ),
            may_page_url: _listing_html(
                [
                    (
                        "https://list.etsi.org/scripts/wa.exe?A2=ind2605A&L=EXAMPLE&P=1",
                        "May week 1 message",
                        "Alice <[log in to unmask]>",
                        "2026-05-03 10:00:00 +0000",
                    )
                ]
            ),
            april_page_url: _listing_html(
                [
                    (
                        "https://list.etsi.org/scripts/wa.exe?A2=ind2604E&L=EXAMPLE&P=1",
                        "Late April message",
                        "Bob <[log in to unmask]>",
                        "2026-04-30 22:00:00 +0000",
                    ),
                    (
                        april_may_url,
                        "Cross-month boundary message R1-2605001",
                        "Carol <[log in to unmask]>",
                        "2026-05-01 01:15:00 +0200",
                    ),
                ]
            ),
        }
    )


@pytest.fixture()
def sync_service(
    repository: ListservRepository, fake_source: RecordingFakeSource
) -> ListservSyncService:
    return ListservSyncService(
        repository=repository,
        source=fake_source,
        today_fn=lambda: date(2026, 5, 10),
    )


def test_may_date_is_found_on_april_archive_page(
    sync_service: ListservSyncService,
    repository: ListservRepository,
    april_may_url: str,
) -> None:
    result = sync_service.sync("EXAMPLE", dates=["= 2026-05-01"])
    assert not result.failures
    assert repository.get(april_may_url).message.date.date() == date(2026, 5, 1)
    # Only the 2026-05-01 message is committed; the 2026-04-30 and 2026-05-03 rows are filtered out
    assert (
        repository.get("https://list.etsi.org/scripts/wa.exe?A2=ind2604E&L=EXAMPLE&P=1")
        is None
    )
    assert (
        repository.get("https://list.etsi.org/scripts/wa.exe?A2=ind2605A&L=EXAMPLE&P=1")
        is None
    )


def test_no_date_sync_stores_only_preceding_year_through_today_on_displayed_dates(
    repository: ListservRepository,
) -> None:
    index_url = archive_index_url("EXAMPLE")
    latest_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2610C&L=EXAMPLE"
    old_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2510C&L=EXAMPLE"

    msg_before_window = "https://list.etsi.org/scripts/wa.exe?A2=ind2510C&L=EXAMPLE&P=1"
    msg_on_start = "https://list.etsi.org/scripts/wa.exe?A2=ind2510C&L=EXAMPLE&P=2"
    msg_on_today_local = "https://list.etsi.org/scripts/wa.exe?A2=ind2610C&L=EXAMPLE&P=1"
    msg_after_today_local = "https://list.etsi.org/scripts/wa.exe?A2=ind2610C&L=EXAMPLE&P=2"

    source = RecordingFakeSource(
        pages={
            index_url: _index_html(
                [
                    (latest_url, "October 2026, Week 3"),
                    (old_url, "October 2025, Week 3"),
                ]
            ),
            latest_url: _listing_html(
                [
                    # Displayed date 2026-10-15 (even though UTC is 2026-10-16 06:30Z) -> IN window
                    (
                        msg_on_today_local,
                        "Today local negative offset",
                        "Alice",
                        "2026-10-15 23:30:00 -0700",
                    ),
                    # Displayed date 2026-10-16 (even though UTC is 2026-10-15 22:30Z) -> OUT of window
                    (
                        msg_after_today_local,
                        "Tomorrow local positive offset",
                        "Bob",
                        "2026-10-16 00:30:00 +0200",
                    ),
                ]
            ),
            old_url: _listing_html(
                [
                    (msg_before_window, "Too old", "Carol", "2025-10-14 23:59:59 +0000"),
                    (msg_on_start, "Exact anniversary", "Dave", "2025-10-15 00:00:00 +0000"),
                ]
            ),
        }
    )

    clock_calls = 0

    def fixed_clock() -> date:
        nonlocal clock_calls
        clock_calls += 1
        return date(2026, 10, 15)

    service = ListservSyncService(
        repository=repository,
        source=source,
        today_fn=fixed_clock,
    )
    result = service.sync("example")

    assert result.processed == 2
    assert result.skipped == 0
    assert result.failures == {}
    assert clock_calls == 1

    assert repository.get(msg_on_start) is not None
    assert repository.get(msg_on_today_local) is not None
    assert repository.get(msg_before_window) is None
    assert repository.get(msg_after_today_local) is None

    expected_cov = DateSet((DateInterval(date(2025, 10, 15), date(2026, 10, 15)),))
    assert (
        repository.get_coverage(ArchivePage("EXAMPLE", old_url, "2025-10-W3"))
        == expected_cov
    )
    assert (
        repository.get_coverage(ArchivePage("EXAMPLE", latest_url, "2026-10-W3"))
        == expected_cov
    )


def test_disjoint_ranges_and_not_equal_preserve_holes_and_changing_predicates_refetch(
    repository: ListservRepository,
) -> None:
    index_url = archive_index_url("EXAMPLE")
    newest_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE"
    march_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2603B&L=EXAMPLE"

    m_mar_05 = "https://list.etsi.org/scripts/wa.exe?A2=ind2603B&L=EXAMPLE&P=1"
    m_mar_15 = "https://list.etsi.org/scripts/wa.exe?A2=ind2603B&L=EXAMPLE&P=2"
    m_mar_25 = "https://list.etsi.org/scripts/wa.exe?A2=ind2603B&L=EXAMPLE&P=3"

    source = RecordingFakeSource(
        pages={
            index_url: _index_html(
                [
                    (newest_url, "October 2026, Week 1"),
                    (march_url, "March 2026, Week 2"),
                ]
            ),
            newest_url: _listing_html([], empty_notice="No messages"),
            march_url: _listing_html(
                [
                    (m_mar_05, "March 5", "Alice", "2026-03-05 10:00:00 +0000"),
                    (m_mar_15, "March 15", "Bob", "2026-03-15 10:00:00 +0000"),
                    (m_mar_25, "March 25", "Carol", "2026-03-25 10:00:00 +0000"),
                ]
            ),
        }
    )
    service = ListservSyncService(
        repository=repository,
        source=source,
        today_fn=lambda: date(2026, 10, 5),
    )

    # 1. First explicit range [2026-03-01..2026-03-10]
    r1 = service.sync("EXAMPLE", dates=[">= 2026-03-01", "<= 2026-03-10"])
    assert r1.processed == 2 and r1.skipped == 0
    assert repository.get(m_mar_05) is not None
    assert repository.get(m_mar_15) is None
    assert repository.get(m_mar_25) is None

    # 2. Disjoint second range [2026-03-20..2026-03-31] leaves hole [2026-03-11..2026-03-19]
    source.clear_requests()
    r2 = service.sync("EXAMPLE", dates=[">= 2026-03-20", "<= 2026-03-31"])
    assert r2.processed == 2 and r2.skipped == 0
    assert march_url in source.requests
    assert repository.get(m_mar_25) is not None
    assert repository.get(m_mar_15) is None

    # 3. Subset of already covered range [= 2026-03-05] skips historical march_url
    source.clear_requests()
    r3 = service.sync("EXAMPLE", dates=["= 2026-03-05"])
    assert r3.processed == 1 and r3.skipped == 1
    assert march_url not in source.requests

    # 4. Range with != 2026-03-15 covering [2026-03-11..2026-03-19] EXCEPT 2026-03-15
    source.clear_requests()
    r4 = service.sync(
        "EXAMPLE",
        dates=[">= 2026-03-11", "<= 2026-03-19", "!= 2026-03-15"],
    )
    assert r4.processed == 2 and r4.skipped == 0
    assert march_url in source.requests
    # 2026-03-15 was excluded by != 2026-03-15, so it is still not stored and remains a hole
    assert repository.get(m_mar_15) is None

    # 5. Querying [>= 2026-03-01, <= 2026-03-31] must NOT skip march_url because 2026-03-15 is a hole
    source.clear_requests()
    r5 = service.sync("EXAMPLE", dates=[">= 2026-03-01", "<= 2026-03-31"])
    assert r5.processed == 2 and r5.skipped == 0
    assert march_url in source.requests
    assert repository.get(m_mar_15) is not None

    # 6. Now the entire month of March 2026 is covered without holes; re-querying skips march_url
    source.clear_requests()
    r6 = service.sync("EXAMPLE", dates=["= 2026-03-15"])
    assert r6.processed == 1 and r6.skipped == 1
    assert march_url not in source.requests


def test_valid_empty_page_is_covered_and_split_weeks_newest_week_and_force_behave_independently(
    repository: ListservRepository,
) -> None:
    index_url = archive_index_url("EXAMPLE")
    newest_split_a = "https://list.etsi.org/scripts/wa.exe?A1=ind2610B_1&L=EXAMPLE"
    newest_split_b = "https://list.etsi.org/scripts/wa.exe?A1=ind2610B_2&L=EXAMPLE"
    hist_split_a = "https://list.etsi.org/scripts/wa.exe?A1=ind2608A_1&L=EXAMPLE"
    hist_split_b = "https://list.etsi.org/scripts/wa.exe?A1=ind2608A_2&L=EXAMPLE"
    empty_hist_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2607A&L=EXAMPLE"

    m_newest_a = "https://list.etsi.org/scripts/wa.exe?A2=ind2610B_1&L=EXAMPLE&P=1"
    m_newest_b = "https://list.etsi.org/scripts/wa.exe?A2=ind2610B_2&L=EXAMPLE&P=1"
    m_hist_a = "https://list.etsi.org/scripts/wa.exe?A2=ind2608A_1&L=EXAMPLE&P=1"
    m_hist_b = "https://list.etsi.org/scripts/wa.exe?A2=ind2608A_2&L=EXAMPLE&P=1"

    source = RecordingFakeSource(
        pages={
            index_url: _index_html(
                [
                    (newest_split_a, "October 2026, Week 2"),
                    (newest_split_b, "October 2026, Week 2"),
                    (hist_split_a, "August 2026, Week 1"),
                    (hist_split_b, "August 2026, Week 1"),
                    (empty_hist_url, "July 2026, Week 1"),
                ]
            ),
            newest_split_a: _listing_html(
                [(m_newest_a, "Newest A", "Alice", "2026-10-08 09:00:00 +0000")]
            ),
            newest_split_b: _listing_html(
                [(m_newest_b, "Newest B", "Bob", "2026-10-09 09:00:00 +0000")]
            ),
            hist_split_a: _listing_html(
                [(m_hist_a, "Hist A", "Carol", "2026-08-03 09:00:00 +0000")]
            ),
            # Initially hist_split_b fails so we can verify independent split-week coverage
            hist_split_b: RuntimeError("transient split-week failure"),
            empty_hist_url: _listing_html([], empty_notice="There are no messages"),
        }
    )
    service = ListservSyncService(
        repository=repository,
        source=source,
        today_fn=lambda: date(2026, 10, 10),
    )

    # Sync 1: 4 pages succeed (including empty_hist_url), hist_split_b fails
    res1 = service.sync("EXAMPLE")
    assert res1.processed == 4
    assert res1.skipped == 0
    assert list(res1.failures.keys()) == [hist_split_b]
    assert repository.get(m_hist_a) is not None
    assert repository.get(m_hist_b) is None
    assert (
        repository.get_coverage(ArchivePage("EXAMPLE", empty_hist_url, "2026-07-W1"))
        != DateSet()
    )

    # Fix hist_split_b; Sync 2 should skip hist_split_a and empty_hist_url,
    # retry hist_split_b, and re-fetch BOTH newest-week split URLs
    source.pages[hist_split_b] = _listing_html(
        [(m_hist_b, "Hist B", "Dave", "2026-08-04 09:00:00 +0000")]
    )
    source.clear_requests()
    res2 = service.sync("EXAMPLE")
    assert res2.processed == 3
    assert res2.skipped == 2
    assert res2.failures == {}
    assert set(source.requests) == {
        index_url,
        newest_split_a,
        newest_split_b,
        hist_split_b,
    }
    assert repository.get(m_hist_b) is not None

    # Sync 3 (force=False): all 3 historical pages skip; both newest-week pages re-fetch
    source.clear_requests()
    res3 = service.sync("EXAMPLE")
    assert res3.processed == 2
    assert res3.skipped == 3
    assert set(source.requests) == {index_url, newest_split_a, newest_split_b}

    # Sync 4 (force=True): all 5 selected pages re-fetch regardless of coverage
    source.clear_requests()
    res4 = service.sync("EXAMPLE", force=True)
    assert res4.processed == 5
    assert res4.skipped == 0
    assert set(source.requests) == {
        index_url,
        newest_split_a,
        newest_split_b,
        hist_split_a,
        hist_split_b,
        empty_hist_url,
    }


def test_month_only_latest_period_refreshes_conservatively(
    repository: ListservRepository,
) -> None:
    index_url = archive_index_url("MONTHLY_LIST")
    oct_month_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2610&L=MONTHLY_LIST"
    sep_month_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2609&L=MONTHLY_LIST"
    m_oct = "https://list.etsi.org/scripts/wa.exe?A2=ind2610&L=MONTHLY_LIST&P=1"
    m_sep = "https://list.etsi.org/scripts/wa.exe?A2=ind2609&L=MONTHLY_LIST&P=1"

    source = RecordingFakeSource(
        pages={
            index_url: _index_html(
                [
                    (oct_month_url, "October 2026"),
                    (sep_month_url, "September 2026"),
                ]
            ),
            oct_month_url: _listing_html(
                [(m_oct, "Oct monthly msg", "Alice", "2026-10-09 12:00:00 +0000")]
            ),
            sep_month_url: _listing_html(
                [(m_sep, "Sep monthly msg", "Bob", "2026-09-15 12:00:00 +0000")]
            ),
        }
    )
    service = ListservSyncService(
        repository=repository,
        source=source,
        today_fn=lambda: date(2026, 10, 15),
    )

    r1 = service.sync("MONTHLY_LIST")
    assert r1.processed == 2 and r1.skipped == 0
    assert repository.get(m_oct).message.week == "2026-10-W2"
    assert repository.get(m_sep).message.week == "2026-09-W3"

    # Second sync: historical September 2026 skips, but month-only latest period October 2026 refreshes
    source.clear_requests()
    r2 = service.sync("MONTHLY_LIST")
    assert r2.processed == 1 and r2.skipped == 1
    assert set(source.requests) == {index_url, oct_month_url}


def test_pagination_urls_traversed_and_covered_even_when_parent_is_already_covered(
    repository: ListservRepository,
) -> None:
    index_url = archive_index_url("EXAMPLE")
    newest_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE"
    parent_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2606A&L=EXAMPLE"
    page2_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2606A&L=EXAMPLE&S=page2"
    page3_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2606A&L=EXAMPLE&S=page3"

    m_p1 = "https://list.etsi.org/scripts/wa.exe?A2=ind2606A&L=EXAMPLE&P=1"
    m_p2 = "https://list.etsi.org/scripts/wa.exe?A2=ind2606A&L=EXAMPLE&P=2"
    m_p3 = "https://list.etsi.org/scripts/wa.exe?A2=ind2606A&L=EXAMPLE&P=3"

    source = RecordingFakeSource(
        pages={
            index_url: _index_html(
                [
                    (newest_url, "October 2026, Week 1"),
                    (parent_url, "June 2026, Week 1"),
                ]
            ),
            newest_url: _listing_html([], empty_notice="No messages"),
            parent_url: _listing_html(
                [(m_p1, "Page 1 msg", "Alice", "2026-06-02 10:00:00 +0000")],
                next_links=[(page2_url, "Next Page")],
            ),
            # Page 2 fails on Sync 1 so parent_url becomes covered while page2_url is uncovered
            page2_url: RuntimeError("page 2 temporary error"),
            page3_url: _listing_html(
                [(m_p3, "Page 3 msg", "Carol", "2026-06-02 12:00:00 +0000")]
            ),
        }
    )
    service = ListservSyncService(
        repository=repository,
        source=source,
        today_fn=lambda: date(2026, 10, 5),
    )

    # Sync 1: parent_url succeeds and persists next_urls=[page2_url]; page2_url fails
    r1 = service.sync("EXAMPLE", dates=["= 2026-06-02"])
    assert r1.processed == 2  # newest_url + parent_url
    assert r1.skipped == 0
    assert list(r1.failures.keys()) == [page2_url]
    assert repository.get(m_p1) is not None
    assert repository.get(m_p2) is None
    assert repository.get_next_urls(ArchivePage("EXAMPLE", parent_url, "2026-06-W1")) == (
        page2_url,
    )

    # Fix page2_url (which links to page3_url).
    # On Sync 2 with the same date, parent_url is ALREADY covered and must be skipped,
    # yet its persisted next_urls must still be traversed to fetch page2_url and page3_url!
    source.pages[page2_url] = _listing_html(
        [(m_p2, "Page 2 msg", "Bob", "2026-06-02 11:00:00 +0000")],
        next_links=[(page3_url, "Next Page")],
    )
    source.clear_requests()
    r2 = service.sync("EXAMPLE", dates=["= 2026-06-02"])
    assert parent_url not in source.requests
    assert page2_url in source.requests
    assert page3_url in source.requests
    assert r2.skipped == 1  # parent_url was skipped
    assert r2.processed == 3  # newest_url + page2_url + page3_url
    assert r2.failures == {}
    assert repository.get(m_p2) is not None
    assert repository.get(m_p3) is not None

    # Sync 3 with the same date: parent_url, page2_url, and page3_url are all skipped
    source.clear_requests()
    r3 = service.sync("EXAMPLE", dates=["= 2026-06-02"])
    assert r3.processed == 1  # only newest_url
    assert r3.skipped == 3  # parent_url, page2_url, page3_url
    assert set(source.requests) == {index_url, newest_url}


def test_failed_and_malformed_pages_remain_retryable_without_losing_siblings_or_leaking_body(
    repository: ListservRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    index_url = archive_index_url("EXAMPLE")
    good_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2604A&L=EXAMPLE"
    http_fail_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2604B&L=EXAMPLE"
    malformed_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2604C&L=EXAMPLE"
    m_good = "https://list.etsi.org/scripts/wa.exe?A2=ind2604A&L=EXAMPLE&P=1"
    m_http_recovered = "https://list.etsi.org/scripts/wa.exe?A2=ind2604B&L=EXAMPLE&P=1"
    m_malformed_recovered = "https://list.etsi.org/scripts/wa.exe?A2=ind2604C&L=EXAMPLE&P=1"

    secret_http_body = "CONFIDENTIAL_HTTP_RESPONSE_BODY_9001"
    secret_malformed_body = "CONFIDENTIAL_MALFORMED_ROW_BODY_9002"

    req = httpx.Request("GET", http_fail_url)
    resp = httpx.Response(500, content=secret_http_body.encode("utf-8"), request=req)
    http_exc = httpx.HTTPStatusError(
        f"Server error with body {secret_http_body}", request=req, response=resp
    )

    source = RecordingFakeSource(
        pages={
            index_url: _index_html(
                [
                    # Make ind2604C newest so ind2604A is historical
                    (malformed_url, "April 2026, Week 3"),
                    (http_fail_url, "April 2026, Week 2"),
                    (good_url, "April 2026, Week 1"),
                ]
            ),
            good_url: _listing_html(
                [(m_good, "Good sibling", "Alice", "2026-04-02 10:00:00 +0000")]
            ),
            http_fail_url: http_exc,
            malformed_url: (
                "<html><body><table>"
                "<tr><th>Subject</th><th>From</th><th>Date</th></tr>"
                f"<tr><td>{secret_malformed_body}</td></tr>"
                "</table></body></html>"
            ),
        }
    )
    progress_events: list[str] = []
    service = ListservSyncService(
        repository=repository,
        source=source,
        today_fn=lambda: date(2026, 4, 25),
    )

    with caplog.at_level(logging.DEBUG):
        r1 = service.sync("EXAMPLE", on_progress=progress_events.append)

    assert r1.processed == 1
    assert r1.skipped == 0
    assert set(r1.failures.keys()) == {http_fail_url, malformed_url}
    assert repository.get(m_good) is not None

    # Verify no response/body text leaked into failures, logs, or progress messages
    combined_failures = " ".join(r1.failures.values())
    combined_progress = " ".join(progress_events)
    assert secret_http_body not in combined_failures
    assert secret_malformed_body not in combined_failures
    assert secret_http_body not in caplog.text
    assert secret_malformed_body not in caplog.text
    assert secret_http_body not in combined_progress
    assert secret_malformed_body not in combined_progress

    # Failed pages must have empty coverage and remain retryable
    assert repository.get_coverage(ArchivePage("EXAMPLE", http_fail_url, "2026-04-W2")) == DateSet()
    assert repository.get_coverage(ArchivePage("EXAMPLE", malformed_url, "2026-04-W3")) == DateSet()

    # Repair both failed pages and re-sync: good_url skips, failed pages succeed
    source.pages[http_fail_url] = _listing_html(
        [(m_http_recovered, "Recovered HTTP", "Bob", "2026-04-10 10:00:00 +0000")]
    )
    source.pages[malformed_url] = _listing_html(
        [(m_malformed_recovered, "Recovered HTML", "Carol", "2026-04-18 10:00:00 +0000")]
    )
    source.clear_requests()
    r2 = service.sync("EXAMPLE")
    assert r2.failures == {}
    assert r2.skipped == 1  # good_url
    assert r2.processed == 2  # http_fail_url + malformed_url
    assert repository.get(m_good) is not None
    assert repository.get(m_http_recovered) is not None
    assert repository.get(m_malformed_recovered) is not None


def test_simulated_transaction_failure_rolls_back_messages_links_and_coverage(
    sqlite_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_schema("main")
    repo = SQLAlchemyListservRepository()

    index_url = archive_index_url("EXAMPLE")
    failing_page_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2604A&L=EXAMPLE"
    sibling_page_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2604B&L=EXAMPLE"
    next_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2604A&L=EXAMPLE&S=next"
    m_fail = "https://list.etsi.org/scripts/wa.exe?A2=ind2604A&L=EXAMPLE&P=1"
    m_sibling = "https://list.etsi.org/scripts/wa.exe?A2=ind2604B&L=EXAMPLE&P=1"

    source = RecordingFakeSource(
        pages={
            index_url: _index_html(
                [
                    (sibling_page_url, "April 2026, Week 2"),
                    (failing_page_url, "April 2026, Week 1"),
                ]
            ),
            failing_page_url: _listing_html(
                [(m_fail, "Should roll back", "Alice", "2026-04-02 10:00:00 +0000")],
                next_links=[(next_url, "Next Page")],
            ),
            sibling_page_url: _listing_html(
                [(m_sibling, "Sibling commits", "Bob", "2026-04-09 10:00:00 +0000")]
            ),
            next_url: _listing_html([], empty_notice="No messages"),
        }
    )

    orig_upsert_cov = repo._upsert_coverage_and_next_urls

    def flaky_upsert_cov(session, page: ArchivePage, coverage: DateSet, next_urls):
        if page.url == failing_page_url:
            raise RuntimeError("simulated DB failure after message flush")
        return orig_upsert_cov(session, page, coverage, next_urls)

    monkeypatch.setattr(repo, "_upsert_coverage_and_next_urls", flaky_upsert_cov)

    service = ListservSyncService(
        repository=repo,
        source=source,
        today_fn=lambda: date(2026, 4, 20),
    )
    result = service.sync("EXAMPLE")

    assert list(result.failures.keys()) == [failing_page_url]
    assert result.processed == 1
    assert repo.get(m_fail) is None
    assert repo.get_coverage(ArchivePage("EXAMPLE", failing_page_url, "2026-04-W1")) == DateSet()
    assert repo.get_next_urls(ArchivePage("EXAMPLE", failing_page_url, "2026-04-W1")) == ()
    assert next_url not in source.requests
    assert repo.get(m_sibling) is not None


def test_bounded_concurrency_and_cancellation_stops_scheduling_and_retains_pagination_links(
    repository: ListservRepository,
) -> None:
    index_url = archive_index_url("EXAMPLE")
    page_urls = [
        f"https://list.etsi.org/scripts/wa.exe?A1=ind2604W{i}&L=EXAMPLE"
        for i in range(1, 6)
    ]
    next_from_p1 = "https://list.etsi.org/scripts/wa.exe?A1=ind2604W1&L=EXAMPLE&S=2"

    pages_map: dict[str, str] = {
        index_url: _index_html(
            [(page_urls[i - 1], f"April 2026, Week {i}") for i in range(5, 0, -1)]
        ),
        next_from_p1: _listing_html(
            [
                (
                    "https://list.etsi.org/scripts/wa.exe?A2=ind2604W1&L=EXAMPLE&P=2",
                    "Paginated msg",
                    "Alice",
                    "2026-04-02 11:00:00 +0000",
                )
            ]
        ),
    }
    for i, url in enumerate(page_urls, start=1):
        next_links = [(next_from_p1, "Next Page")] if i == 5 else None
        pages_map[url] = _listing_html(
            [
                (
                    f"https://list.etsi.org/scripts/wa.exe?A2=ind2604W{i}&L=EXAMPLE&P=1",
                    f"Week {i} msg",
                    "Alice",
                    f"2026-04-0{i} 10:00:00 +0000",
                )
            ],
            next_links=next_links,
        )

    # Part 1: Verify bounded in-flight concurrency <= max_workers (2)
    def slow_fetch(url: str) -> None:
        if url != index_url:
            time.sleep(0.03)

    source_bounded = RecordingFakeSource(pages=dict(pages_map), on_fetch=slow_fetch)
    service_bounded = ListservSyncService(
        repository=repository,
        source=source_bounded,
        today_fn=lambda: date(2026, 4, 25),
        max_workers=2,
    )
    res_bounded = service_bounded.sync("EXAMPLE", force=True)
    assert res_bounded.processed == 6
    assert 1 <= source_bounded.max_active_fetches <= 2

    # Part 2: Verify cancellation stops scheduling new pages while committing in-flight page and its next_urls
    cancel_flag = False
    first_page_url = page_urls[4]  # April 2026, Week 5 (first in index, has next_from_p1)

    def cancel_on_first_page(url: str) -> None:
        nonlocal cancel_flag
        if url == first_page_url:
            cancel_flag = True

    source_cancel = RecordingFakeSource(pages=dict(pages_map), on_fetch=cancel_on_first_page)
    service_cancel = ListservSyncService(
        repository=repository,
        source=source_cancel,
        today_fn=lambda: date(2026, 4, 25),
        max_workers=1,
    )
    # Use a fresh list_id so coverage starts empty
    cancel_index_url = archive_index_url("CANCEL_LIST")
    c_p1 = "https://list.etsi.org/scripts/wa.exe?A1=ind2603W1&L=CANCEL_LIST"
    c_p2 = "https://list.etsi.org/scripts/wa.exe?A1=ind2604W2&L=CANCEL_LIST"
    c_p1_next = "https://list.etsi.org/scripts/wa.exe?A1=ind2603W1&L=CANCEL_LIST&S=2"
    m_c_p1 = "https://list.etsi.org/scripts/wa.exe?A2=ind2603W1&L=CANCEL_LIST&P=1"
    m_c_p1_next = "https://list.etsi.org/scripts/wa.exe?A2=ind2603W1&L=CANCEL_LIST&P=2"
    m_c_p2 = "https://list.etsi.org/scripts/wa.exe?A2=ind2604W2&L=CANCEL_LIST&P=1"

    cancel_flag = False

    def trigger_cancel_on_c_p1(url: str) -> None:
        nonlocal cancel_flag
        if url == c_p1:
            cancel_flag = True

    source_cancel.pages = {
        cancel_index_url: _index_html(
            [
                (c_p1, "March 2026, Week 1"),
                (c_p2, "April 2026, Week 2"),
            ]
        ),
        c_p1: _listing_html(
            [(m_c_p1, "Msg C1", "Alice", "2026-03-03 10:00:00 +0000")],
            next_links=[(c_p1_next, "Next Page")],
        ),
        c_p1_next: _listing_html(
            [(m_c_p1_next, "Msg C1 Next", "Bob", "2026-03-04 10:00:00 +0000")]
        ),
        c_p2: _listing_html(
            [(m_c_p2, "Msg C2", "Carol", "2026-04-10 10:00:00 +0000")]
        ),
    }
    source_cancel.on_fetch = trigger_cancel_on_c_p1
    source_cancel.clear_requests()

    res_cancelled = service_cancel.sync(
        "CANCEL_LIST",
        cancelled=lambda: cancel_flag,
    )
    # Only c_p1 was started; it finished and committed its message and next_urls,
    # while c_p2 and c_p1_next were never scheduled
    assert res_cancelled.processed == 1
    assert res_cancelled.skipped == 0
    assert source_cancel.requests == [cancel_index_url, c_p1]
    assert repository.get(m_c_p1) is not None
    assert repository.get(m_c_p1_next) is None
    assert repository.get(m_c_p2) is None
    assert repository.get_next_urls(
        ArchivePage("CANCEL_LIST", c_p1, "2026-03-W1")
    ) == (c_p1_next,)

    # Resuming without cancellation skips covered c_p1 and fetches retained c_p1_next and c_p2
    source_cancel.on_fetch = None
    source_cancel.clear_requests()
    res_resumed = service_cancel.sync("CANCEL_LIST")
    assert c_p1 not in source_cancel.requests
    assert c_p1_next in source_cancel.requests
    assert c_p2 in source_cancel.requests
    assert res_resumed.skipped == 1
    assert res_resumed.processed == 2
    assert repository.get(m_c_p1_next) is not None
    assert repository.get(m_c_p2) is not None


@pytest.mark.parametrize(
    ("bad_list_id", "bad_dates"),
    [
        ("EXAMPLE", ["invalid-date"]),
        ("EXAMPLE", ["= 2026-02-30"]),
        ("EXAMPLE", ["~= 2026-05-01"]),
        ("", ["= 2026-05-01"]),
        ("   ", []),
        ("BAD LIST", []),
        ("LIST&A0=OTHER", []),
    ],
)
def test_validation_precedes_network_and_database_calls(
    repository: ListservRepository,
    monkeypatch: pytest.MonkeyPatch,
    bad_list_id: str,
    bad_dates: list[str],
) -> None:
    source = RecordingFakeSource()
    db_called = False

    def forbid_db(*_args, **_kwargs):
        nonlocal db_called
        db_called = True
        raise AssertionError("Database should not be called before input validation")

    monkeypatch.setattr(repository, "get_coverage", forbid_db)
    monkeypatch.setattr(repository, "get_next_urls", forbid_db)
    monkeypatch.setattr(repository, "commit_page", forbid_db)

    service = ListservSyncService(repository=repository, source=source)
    with pytest.raises(ValueError):
        service.sync(bad_list_id, dates=bad_dates)

    assert source.requests == []
    assert db_called is False


def test_updated_subject_replaces_subject_tdocs_and_bumps_source_revision_in_transaction(
    repository: ListservRepository,
) -> None:
    index_url = archive_index_url("EXAMPLE")
    page_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE"
    msg_url = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=1"

    source = RecordingFakeSource(
        pages={
            index_url: _index_html([(page_url, "October 2026, Week 1")]),
            page_url: _listing_html(
                [
                    (
                        msg_url,
                        "Initial topic R1-111111 and RP-260001",
                        "Alice",
                        "2026-10-02 10:00:00 +0000",
                    )
                ]
            ),
        }
    )
    service = ListservSyncService(
        repository=repository,
        source=source,
        today_fn=lambda: date(2026, 10, 5),
    )

    service.sync("EXAMPLE")
    rec1 = repository.get(msg_url)
    assert rec1 is not None
    assert rec1.message.subject_tdocs == ("R1-111111", "RP-260001")
    assert rec1.tdocs == ("R1-111111", "RP-260001")
    assert rec1.source_revision == 1

    # Attach parsed detail with body TDoc R1-999999 (bumps source_revision to 2)
    repository.save_detail(
        ListservDetail(
            url=msg_url,
            attachments=(),
            tdocs=("R1-999999",),
            content="Body mentions R1-999999",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
        )
    )
    rec2 = repository.get(msg_url)
    assert rec2 is not None and rec2.source_revision == 2

    # Re-sync with unchanged subject: source_revision stays 2
    service.sync("EXAMPLE")
    rec_unchanged = repository.get(msg_url)
    assert rec_unchanged is not None and rec_unchanged.source_revision == 2

    # Upstream subject changes: R1-111111/RP-260001 replaced by R1-222222; source_revision bumps to 3 in transaction
    source.pages[page_url] = _listing_html(
        [
            (
                msg_url,
                "Updated topic R1-222222",
                "Alice",
                "2026-10-02 10:00:00 +0000",
            )
        ]
    )
    service.sync("EXAMPLE")
    rec3 = repository.get(msg_url)
    assert rec3 is not None
    assert rec3.message.subject == "Updated topic R1-222222"
    assert rec3.message.subject_tdocs == ("R1-222222",)
    assert rec3.tdocs == ("R1-222222", "R1-999999")
    assert rec3.source_revision == 3
    assert repository.select(ListservFilters(tdocs="%R1-111111%")) == []
    assert [r.message.url for r in repository.select(ListservFilters(tdocs="%R1-222222%"))] == [
        msg_url
    ]


def test_build_listserv_sync_service_passes_settings_to_source(sqlite_env) -> None:
    create_schema("main")
    custom_settings = Settings(
        http_verify=False,
        http_max_retries=6,
        http_retry_backoff=2.0,
    )
    with patch("doc3gpp.scraping.listserv_source.httpx.Client", wraps=httpx.Client) as client_spy:
        service = build_listserv_sync_service(settings=custom_settings)
        try:
            assert isinstance(service, ListservSyncService)
            assert client_spy.call_args.kwargs["verify"] is False
            assert service._source._max_retries == 6
            assert service._source._backoff == 2.0
        finally:
            service.close()


def test_relative_links_resolve_against_fetched_final_url_while_coverage_uses_discovered_url(
    repository: ListservRepository,
) -> None:
    index_request_url = archive_index_url("EXAMPLE")
    index_final_url = "https://list.etsi.org/archives/index/view.html"

    # Relative archive links resolved against index_final_url:
    discovered_newest_url = "https://list.etsi.org/archives/index/week1/listing.html"
    discovered_hist_url = "https://list.etsi.org/archives/index/hist/listing.html"

    # Listing page redirects to a different directory path:
    listing_final_url = "https://list.etsi.org/canonical/2026-09/page1.html"
    # Relative detail & next links on listing_final_url must resolve against listing_final_url:
    expected_msg1_url = "https://list.etsi.org/canonical/2026-09/messages/msg-1.html"
    wrong_msg1_url = "https://list.etsi.org/archives/index/hist/messages/msg-1.html"
    expected_next_url = "https://list.etsi.org/canonical/2026-09/sub/page2.html"

    # Next page also redirects and has a relative detail link:
    next_final_url = "https://list.etsi.org/canonical/2026-09/sub/final/index.html"
    expected_msg2_url = "https://list.etsi.org/canonical/2026-09/sub/final/msg-2.html"

    source = RecordingFakeSource(
        pages={
            index_request_url: FetchedPage(
                text=_index_html(
                    [
                        ("week1/listing.html", "October 2026, Week 1"),
                        ("hist/listing.html", "September 2026, Week 1"),
                    ]
                ),
                content_type="text/html",
                final_url=index_final_url,
            ),
            discovered_newest_url: FetchedPage(
                text=_listing_html([], empty_notice="No messages"),
                content_type="text/html",
                final_url=discovered_newest_url,
            ),
            discovered_hist_url: FetchedPage(
                text=_listing_html(
                    [
                        (
                            "messages/msg-1.html",
                            "Redirected page 1 R1-2609001",
                            "Alice <[log in to unmask]>",
                            "2026-09-03 10:00:00 +0000",
                        )
                    ],
                    next_links=[
                        ("sub/page2.html", "Next Page"),
                        # Self-link back to original discovered archive URL after redirect
                        ("/archives/index/hist/listing.html", "Page 1"),
                    ],
                ),
                content_type="text/html",
                final_url=listing_final_url,
            ),
            expected_next_url: FetchedPage(
                text=_listing_html(
                    [
                        (
                            "msg-2.html",
                            "Redirected page 2 R1-2609002",
                            "Bob <[log in to unmask]>",
                            "2026-09-03 11:30:00 +0000",
                        )
                    ]
                ),
                content_type="text/html",
                final_url=next_final_url,
            ),
        }
    )

    service = ListservSyncService(
        repository=repository,
        source=source,
        today_fn=lambda: date(2026, 10, 5),
    )

    result = service.sync("EXAMPLE", dates=["= 2026-09-03"])
    assert result.failures == {}
    assert result.processed == 3  # discovered_newest_url + discovered_hist_url + expected_next_url
    assert result.skipped == 0
    assert expected_next_url in source.requests

    # Detail URLs resolved against each page's FetchedPage.final_url
    rec1 = repository.get(expected_msg1_url)
    assert rec1 is not None
    assert rec1.message.subject == "Redirected page 1 R1-2609001"
    assert rec1.message.sender == "Alice <[log in to unmask]>"
    assert rec1.message.date.date() == date(2026, 9, 3)
    assert repository.get(wrong_msg1_url) is None

    rec2 = repository.get(expected_msg2_url)
    assert rec2 is not None
    assert rec2.message.subject == "Redirected page 2 R1-2609002"
    assert rec2.message.sender == "Bob <[log in to unmask]>"
    assert rec2.message.date.date() == date(2026, 9, 3)

    # Coverage and persisted next_urls remain keyed to originally discovered archive URL
    expected_cov = DateSet((DateInterval(date(2026, 9, 3), date(2026, 9, 3)),))
    orig_hist_page = ArchivePage("EXAMPLE", discovered_hist_url, "2026-09-W1")
    redirected_hist_page = ArchivePage("EXAMPLE", listing_final_url, "2026-09-W1")
    assert repository.get_coverage(orig_hist_page) == expected_cov
    assert repository.get_coverage(redirected_hist_page) == DateSet()
    assert repository.get_next_urls(orig_hist_page) == (expected_next_url,)
    assert repository.get_next_urls(redirected_hist_page) == ()

    # Repeat sync skips both discovered_hist_url and expected_next_url via their discovered URLs
    source.clear_requests()
    result2 = service.sync("EXAMPLE", dates=["= 2026-09-03"])
    assert result2.failures == {}
    assert result2.processed == 1  # only discovered_newest_url
    assert result2.skipped == 2  # discovered_hist_url + expected_next_url
    assert set(source.requests) == {index_request_url, discovered_newest_url}


class _RecordingIndexUpdater:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def update_records(
        self, records: list[object], *, operation: str
    ) -> dict[str, str]:
        self.calls.append((operation, tuple(records)))
        return {}


def test_sync_duplicate_final_outside_requested_date_is_authoritative(
    repository: ListservRepository,
) -> None:
    from doc3gpp.models.listserv import ListservMessage

    list_id = "DUPLICATE_OUT"
    index_url = archive_index_url(list_id)
    page_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2601W3&L=DUPLICATE_OUT"
    next_url = f"{page_url}&S=page2"
    existing_url = "https://list.etsi.org/scripts/wa.exe?A2=ind2601W3&L=DUPLICATE_OUT&P=1"
    new_url = "https://list.etsi.org/scripts/wa.exe?A2=ind2601W3&L=DUPLICATE_OUT&P=2"
    page = ArchivePage(list_id, page_url, "2026-01-W3")
    seed_message = ListservMessage(
        url=existing_url,
        list_id=list_id,
        subject="Existing subject",
        sender="Original sender",
        date=datetime(2026, 1, 14, 10, 0, tzinfo=timezone.utc),
        date_precision="datetime",
        week="2026-01-W3",
    )
    jan_14 = DateSet((DateInterval(date(2026, 1, 14), date(2026, 1, 14)),))
    repository.commit_page(page, [seed_message], jan_14, ())
    repository.save_detail(
        ListservDetail(
            url=existing_url,
            attachments=(),
            tdocs=(),
            content="preserved body",
            content_truncated=False,
            parser_version="test",
            parsed_at=datetime(2026, 1, 14, 12, 0, tzinfo=timezone.utc),
        )
    )
    existing_before = repository.get(existing_url)
    assert existing_before is not None and existing_before.source_revision == 2

    source = RecordingFakeSource(
        pages={
            index_url: _index_html([(page_url, "January 2026, Week 3")]),
            page_url: _listing_html(
                [
                    (existing_url, "Earlier matching row", "Alice", "2026-01-15 23:30:00 -0700"),
                    (existing_url, "Final outside row", "Bob", "2026-01-16 00:30:00 +0200"),
                    (new_url, "Earlier new row", "Carol", "2026-01-15 23:30:00 -0700"),
                    (new_url, "Final new row outside", "Dave", "2026-01-16 00:30:00 +0200"),
                ],
                next_links=[(next_url, "Next Page")],
            ),
            next_url: _listing_html([], empty_notice="No messages"),
        }
    )
    indexer = _RecordingIndexUpdater()
    service = ListservSyncService(
        repository=repository,
        source=source,
        index_service=indexer,
        today_fn=lambda: date(2026, 1, 20),
        max_workers=1,
    )

    result = service.sync(list_id, dates=["= 2026-01-15"], force=True)

    assert result.failures == {}
    assert result.processed == 2
    existing_after = repository.get(existing_url)
    assert existing_after is not None
    assert existing_after.message.subject == "Existing subject"
    assert existing_after.message.date.date() == date(2026, 1, 14)
    assert existing_after.source_revision == existing_before.source_revision
    assert existing_after.detail is not None
    assert existing_after.detail.content == "preserved body"
    assert repository.get(new_url) is None
    assert indexer.calls == []
    assert repository.get_coverage(page) == DateSet(
        (DateInterval(date(2026, 1, 14), date(2026, 1, 15)),)
    )
    assert repository.get_next_urls(page) == (next_url,)
    assert next_url in source.requests


def test_sync_duplicate_final_inside_local_date_is_persisted_and_indexed_once(
    repository: ListservRepository,
) -> None:
    from doc3gpp.models.listserv import ListservMessage

    list_id = "DUPLICATE_IN"
    index_url = archive_index_url(list_id)
    page_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2601W3&L=DUPLICATE_IN"
    next_url = f"{page_url}&S=page2"
    message_url = "https://list.etsi.org/scripts/wa.exe?A2=ind2601W3&L=DUPLICATE_IN&P=1"
    page = ArchivePage(list_id, page_url, "2026-01-W3")
    seed_message = ListservMessage(
        url=message_url,
        list_id=list_id,
        subject="Persisted subject",
        sender="Original sender",
        date=datetime(2026, 1, 14, 10, 0, tzinfo=timezone.utc),
        date_precision="datetime",
        week="2026-01-W3",
    )
    jan_14 = DateSet((DateInterval(date(2026, 1, 14), date(2026, 1, 14)),))
    repository.commit_page(page, [seed_message], jan_14, ())

    final_subject = "Final selected topic R1-260001"
    source = RecordingFakeSource(
        pages={
            index_url: _index_html([(page_url, "January 2026, Week 3")]),
            page_url: _listing_html(
                [
                    (message_url, "Earlier outside row", "Alice", "2026-01-16 00:30:00 +0200"),
                    (message_url, final_subject, "Bob", "2026-01-15 23:30:00 -0700"),
                ],
                next_links=[(next_url, "Next Page")],
            ),
            next_url: _listing_html([], empty_notice="No messages"),
        }
    )
    indexer = _RecordingIndexUpdater()
    service = ListservSyncService(
        repository=repository,
        source=source,
        index_service=indexer,
        today_fn=lambda: date(2026, 1, 20),
        max_workers=1,
    )

    result = service.sync(list_id, dates=["= 2026-01-15"], force=True)

    assert result.failures == {}
    assert result.processed == 2
    stored = repository.get(message_url)
    assert stored is not None
    assert stored.message.subject == final_subject
    assert stored.message.subject_tdocs == ("R1-260001",)
    assert stored.message.date.date() == date(2026, 1, 15)
    assert stored.message.date.astimezone(timezone.utc).date() == date(2026, 1, 16)
    assert stored.source_revision == 2
    assert len(indexer.calls) == 1
    operation, indexed_records = indexer.calls[0]
    assert operation == "sync"
    assert len(indexed_records) == 1
    assert indexed_records[0].message.url == message_url
    assert indexed_records[0].message.subject == final_subject
    assert indexed_records[0].source_revision == 2
    assert repository.get_coverage(page) == DateSet(
        (DateInterval(date(2026, 1, 14), date(2026, 1, 15)),)
    )
    assert repository.get_next_urls(page) == (next_url,)
    assert next_url in source.requests


def test_unknown_list_buffers_empty_page_until_later_matching_page(
    repository: ListservRepository,
) -> None:
    list_id = "3gpp_tsg_ran_wg2_buffered"
    normalized_list_id = "3GPP_TSG_RAN_WG2_BUFFERED"
    index_url = archive_index_url(list_id)
    empty_page_url = (
        "https://list.etsi.org/scripts/wa.exe?A1=ind2604A&L=3GPP_TSG_RAN_WG2_BUFFERED"
    )
    matching_page_url = f"{empty_page_url}&S=next"
    message_url = (
        "https://list.etsi.org/scripts/wa.exe?A2=ind2604B&L=3GPP_TSG_RAN_WG2_BUFFERED&P=1"
    )
    source = RecordingFakeSource(
        pages={
            index_url: _index_html([(empty_page_url, "April 2026, Week 1")]),
            empty_page_url: _listing_html(
                [], next_links=[(matching_page_url, "Next Page")], empty_notice="No messages"
            ),
            matching_page_url: _listing_html(
                [(message_url, "Matching message", "Alice", "2026-04-02 10:00:00 +0000")]
            ),
        }
    )
    service = ListservSyncService(
        repository=repository,
        source=source,
        today_fn=lambda: date(2026, 4, 10),
        max_workers=1,
    )

    result = service.sync(list_id, dates=["= 2026-04-02"])

    expected_coverage = DateSet(
        (DateInterval(date(2026, 4, 2), date(2026, 4, 2)),)
    )
    assert source.requests == [index_url, empty_page_url, matching_page_url]
    assert result.processed == 2
    assert result.skipped == 0
    assert result.failures == {}
    assert repository.has_list(normalized_list_id)
    assert _listserv_catalog_metadata(normalized_list_id) == ("R2", None)
    stored = repository.get(message_url)
    assert stored is not None and stored.message.list_id == normalized_list_id
    assert repository.get_coverage(ArchivePage(normalized_list_id, empty_page_url, "2026-04-W1")) == expected_coverage
    assert repository.get_coverage(ArchivePage(normalized_list_id, matching_page_url, "2026-04-W1")) == expected_coverage
    assert repository.get_next_urls(ArchivePage(normalized_list_id, empty_page_url, "2026-04-W1")) == (matching_page_url,)


@pytest.mark.parametrize(
    ("list_id", "expected_id"),
    [
        ("3GPP_TSG_CT_WG99_DISCOVERY_TEST", "3GPP_TSG_CT_WG99_DISCOVERY_TEST"),
        ("OTHER_LIST_DISCOVERY_TEST", "OTHER_LIST_DISCOVERY_TEST"),
    ],
)
def test_discovered_unknown_or_unrecognized_tsg_is_stored_as_null(
    repository: ListservRepository, list_id: str, expected_id: str
) -> None:
    index_url = archive_index_url(list_id)
    page_url = f"https://list.etsi.org/scripts/wa.exe?A1=ind2604A&L={expected_id}"
    message_url = f"https://list.etsi.org/scripts/wa.exe?A2=ind2604A&L={expected_id}&P=1"
    source = RecordingFakeSource(
        pages={
            index_url: _index_html([(page_url, "April 2026, Week 1")]),
            page_url: _listing_html(
                [(message_url, "Matching message", "Alice", "2026-04-02 10:00:00 +0000")]
            ),
        }
    )
    service = ListservSyncService(
        repository=repository,
        source=source,
        today_fn=lambda: date(2026, 4, 10),
        max_workers=1,
    )

    result = service.sync(list_id, dates=["= 2026-04-02"])

    assert result.processed == 1
    assert result.failures == {}
    assert repository.has_list(expected_id)
    assert _listserv_catalog_metadata(expected_id) == (None, None)
    assert repository.get(message_url) is not None


def test_discovery_does_not_overwrite_seeded_catalog_metadata(
    repository: ListservRepository,
) -> None:
    list_id = "3GPP_TSG_RAN_ADHOC"
    index_url = archive_index_url(list_id)
    page_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2604A&L=3GPP_TSG_RAN_ADHOC"
    message_url = "https://list.etsi.org/scripts/wa.exe?A2=ind2604A&L=3GPP_TSG_RAN_ADHOC&P=1"
    source = RecordingFakeSource(
        pages={
            index_url: _index_html([(page_url, "April 2026, Week 1")]),
            page_url: _listing_html(
                [(message_url, "Matching message", "Alice", "2026-04-02 10:00:00 +0000")]
            ),
        }
    )
    service = ListservSyncService(
        repository=repository,
        source=source,
        today_fn=lambda: date(2026, 4, 10),
        max_workers=1,
    )

    result = service.sync(list_id, dates=["= 2026-04-02"])

    assert result.processed == 1
    assert repository.has_list(list_id)
    assert _listserv_catalog_metadata(list_id) == (
        "R2",
        "3gpp_tsg_ran_adhoc: tsg ran ad hoc meetings",
    )


def test_discovery_preserves_locally_edited_catalog_metadata(
    repository: ListservRepository,
) -> None:
    list_id = "3GPP_TSG_RAN_WG2_LOCAL_EDIT"
    with get_session_factory()() as session:
        session.add(
            ListservListORM(
                list_id=list_id,
                tsg="SP",
                description="User-maintained description",
            )
        )
        session.commit()

    index_url = archive_index_url(list_id)
    page_url = f"https://list.etsi.org/scripts/wa.exe?A1=ind2604A&L={list_id}"
    message_url = f"https://list.etsi.org/scripts/wa.exe?A2=ind2604A&L={list_id}&P=1"
    source = RecordingFakeSource(
        pages={
            index_url: _index_html([(page_url, "April 2026, Week 1")]),
            page_url: _listing_html(
                [(message_url, "Matching message", "Alice", "2026-04-02 10:00:00 +0000")]
            ),
        }
    )
    service = ListservSyncService(
        repository=repository,
        source=source,
        today_fn=lambda: date(2026, 4, 10),
        max_workers=1,
    )

    result = service.sync(list_id, dates=["= 2026-04-02"])

    assert result.processed == 1
    assert repository.get(message_url) is not None
    assert _listserv_catalog_metadata(list_id) == (
        "SP",
        "User-maintained description",
    )




def test_unknown_list_without_matches_discards_pages_and_refetches_them(
    repository: ListservRepository,
) -> None:
    list_id = "UNKNOWN_EMPTY_REFETCH_TEST"
    index_url = archive_index_url(list_id)
    first_page_url = f"https://list.etsi.org/scripts/wa.exe?A1=ind2604A&L={list_id}"
    second_page_url = f"{first_page_url}&S=next"
    source = RecordingFakeSource(
        pages={
            index_url: _index_html([(first_page_url, "April 2026, Week 1")]),
            first_page_url: _listing_html(
                [],
                next_links=[(second_page_url, "Next Page")],
                empty_notice="No messages",
            ),
            second_page_url: _listing_html([], empty_notice="No messages"),
        }
    )
    service = ListservSyncService(
        repository=repository,
        source=source,
        today_fn=lambda: date(2026, 4, 10),
        max_workers=1,
    )

    first = service.sync(list_id, dates=["= 2026-04-02"])

    first_page = ArchivePage(list_id, first_page_url, "2026-04-W1")
    second_page = ArchivePage(list_id, second_page_url, "2026-04-W1")
    assert first.processed == 0
    assert first.failures == {}
    assert first_page_url in source.requests and second_page_url in source.requests
    assert not repository.has_list(list_id)
    assert repository.get_coverage(first_page) == DateSet()
    assert repository.get_coverage(second_page) == DateSet()
    assert repository.get_next_urls(first_page) == ()

    source.clear_requests()
    second = service.sync(list_id, dates=["= 2026-04-02"])

    assert second.processed == 0
    assert second.failures == {}
    assert first_page_url in source.requests and second_page_url in source.requests

def test_unknown_list_outside_date_filter_is_not_registered_or_covered(
    repository: ListservRepository,
) -> None:
    list_id = "OUTSIDE_DATE_DISCOVERY_TEST"
    index_url = archive_index_url(list_id)
    page_url = f"https://list.etsi.org/scripts/wa.exe?A1=ind2604A&L={list_id}"
    message_url = f"https://list.etsi.org/scripts/wa.exe?A2=ind2604A&L={list_id}&P=1"
    source = RecordingFakeSource(
        pages={
            index_url: _index_html([(page_url, "April 2026, Week 1")]),
            page_url: _listing_html(
                [(message_url, "Outside filter", "Alice", "2026-04-03 10:00:00 +0000")]
            ),
        }
    )
    service = ListservSyncService(
        repository=repository,
        source=source,
        today_fn=lambda: date(2026, 4, 10),
        max_workers=1,
    )

    result = service.sync(list_id, dates=["= 2026-04-02"])

    page = ArchivePage(list_id, page_url, "2026-04-W1")
    assert result.processed == 0
    assert result.failures == {}
    assert not repository.has_list(list_id)
    assert _listserv_catalog_metadata(list_id) is None
    assert repository.get(message_url) is None
    assert repository.get_coverage(page) == DateSet()
    assert repository.get_next_urls(page) == ()


def test_unknown_list_does_not_persist_buffered_coverage_after_page_failures(
    repository: ListservRepository,
) -> None:
    list_id = "FAILED_BUFFER_DISCOVERY_TEST"
    index_url = archive_index_url(list_id)
    page_url = f"https://list.etsi.org/scripts/wa.exe?A1=ind2604A&L={list_id}"
    fetch_failure_url = f"https://list.etsi.org/scripts/wa.exe?A1=ind2604B&L={list_id}"
    malformed_url = f"https://list.etsi.org/scripts/wa.exe?A1=ind2604C&L={list_id}"
    malformed_html = (
        "<html><body><table>"
        "<tr><th>Subject</th><th>From</th><th>Date</th></tr>"
        "<tr><td>Malformed row</td></tr>"
        "</table></body></html>"
    )
    source = RecordingFakeSource(
        pages={
            index_url: _index_html([(page_url, "April 2026, Week 1")]),
            page_url: _listing_html(
                [],
                next_links=[(fetch_failure_url, "Fetch failure"), (malformed_url, "Malformed")],
                empty_notice="No messages",
            ),
            fetch_failure_url: RuntimeError("temporary fetch failure"),
            malformed_url: malformed_html,
        }
    )
    service = ListservSyncService(
        repository=repository,
        source=source,
        today_fn=lambda: date(2026, 4, 10),
        max_workers=1,
    )

    result = service.sync(list_id, dates=["= 2026-04-02"])

    page = ArchivePage(list_id, page_url, "2026-04-W1")
    assert result.processed == 0
    assert set(result.failures) == {fetch_failure_url, malformed_url}
    assert not repository.has_list(list_id)
    assert _listserv_catalog_metadata(list_id) is None
    assert repository.get_coverage(page) == DateSet()
    assert repository.get_next_urls(page) == ()


def test_cancellation_before_unknown_list_match_discards_buffered_pages(
    repository: ListservRepository,
) -> None:
    list_id = "CANCEL_BUFFER_DISCOVERY_TEST"
    index_url = archive_index_url(list_id)
    page_url = f"https://list.etsi.org/scripts/wa.exe?A1=ind2604A&L={list_id}"
    matching_page_url = f"{page_url}&S=next"
    source = RecordingFakeSource(
        pages={
            index_url: _index_html([(page_url, "April 2026, Week 1")]),
            page_url: _listing_html(
                [], next_links=[(matching_page_url, "Next Page")], empty_notice="No messages"
            ),
            matching_page_url: _listing_html(
                [(
                    f"https://list.etsi.org/scripts/wa.exe?A2=ind2604B&L={list_id}&P=1",
                    "Would match",
                    "Alice",
                    "2026-04-02 10:00:00 +0000",
                )]
            ),
        }
    )
    cancelled = False

    def cancel_after_empty_page(url: str) -> None:
        nonlocal cancelled
        if url == page_url:
            cancelled = True

    source.on_fetch = cancel_after_empty_page
    service = ListservSyncService(
        repository=repository,
        source=source,
        today_fn=lambda: date(2026, 4, 10),
        max_workers=1,
    )

    result = service.sync(
        list_id, dates=["= 2026-04-02"], cancelled=lambda: cancelled
    )

    page = ArchivePage(list_id, page_url, "2026-04-W1")
    assert result.processed == 0
    assert result.failures == {}
    assert source.requests == [index_url, page_url]
    assert not repository.has_list(list_id)
    assert repository.get_coverage(page) == DateSet()
    assert repository.get_next_urls(page) == ()

def test_force_sync_commits_empty_coverage_for_seeded_list(
    repository: ListservRepository,
) -> None:
    list_id = "3GPP_TSG_RAN_WG1"
    index_url = archive_index_url(list_id)
    page_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2601A&L=3GPP_TSG_RAN_WG1"
    page = ArchivePage(list_id, page_url, "2026-01-W1")
    coverage = DateSet((DateInterval(date(2026, 1, 2), date(2026, 1, 2)),))
    repository.commit_page(page, (), coverage, ())
    source = RecordingFakeSource(
        pages={
            index_url: _index_html([(page_url, "January 2026, Week 1")]),
            page_url: _listing_html([], empty_notice="No messages"),
        }
    )
    service = ListservSyncService(
        repository=repository,
        source=source,
        today_fn=lambda: date(2026, 4, 10),
        max_workers=1,
    )

    result = service.sync(
        list_id, dates=["= 2026-01-02"], force=True
    )

    assert result.processed == 1
    assert result.failures == {}
    assert page_url in source.requests
    assert repository.has_list(list_id)
    assert repository.get_coverage(page) == coverage


def test_failed_buffered_page_commit_is_retryable_and_not_counted(
    repository: ListservRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    list_id = "BUFFER_COMMIT_RETRY_TEST"
    index_url = archive_index_url(list_id)
    empty_page_url = f"https://list.etsi.org/scripts/wa.exe?A1=ind2604A&L={list_id}"
    matching_page_url = f"{empty_page_url}&S=next"
    message_url = f"https://list.etsi.org/scripts/wa.exe?A2=ind2604B&L={list_id}&P=1"
    empty_page = ArchivePage(list_id, empty_page_url, "2026-04-W1")
    coverage = DateSet((DateInterval(date(2026, 4, 2), date(2026, 4, 2)),))
    source = RecordingFakeSource(
        pages={
            index_url: _index_html([(empty_page_url, "April 2026, Week 1")]),
            empty_page_url: _listing_html(
                [],
                next_links=[(matching_page_url, "Next Page")],
                empty_notice="No messages",
            ),
            matching_page_url: _listing_html(
                [(message_url, "Matching message", "Alice", "2026-04-02 10:00:00 +0000")]
            ),
        }
    )
    real_commit_page = repository.commit_page
    fail_buffered_page_once = True

    def fail_first_buffer_commit(page, messages, dates, next_urls) -> None:
        nonlocal fail_buffered_page_once
        if page.url == empty_page_url and fail_buffered_page_once:
            fail_buffered_page_once = False
            raise RuntimeError("temporary buffered-page commit failure")
        real_commit_page(page, messages, dates, next_urls)

    monkeypatch.setattr(repository, "commit_page", fail_first_buffer_commit)
    service = ListservSyncService(
        repository=repository,
        source=source,
        today_fn=lambda: date(2026, 4, 10),
        max_workers=1,
    )

    first = service.sync(list_id, dates=["= 2026-04-02"])

    assert first.processed == 1
    assert set(first.failures) == {empty_page_url}
    assert repository.has_list(list_id)
    assert repository.get(message_url) is not None
    assert repository.get_coverage(empty_page) == DateSet()
    assert repository.get_next_urls(empty_page) == ()

    source.clear_requests()
    second = service.sync(list_id, dates=["= 2026-04-02"])

    assert empty_page_url in source.requests
    assert second.failures == {}
    assert repository.get_coverage(empty_page) == coverage
    assert repository.get_next_urls(empty_page) == (matching_page_url,)


def test_concurrent_syncs_register_one_unknown_list_without_overwriting_metadata(
    repository: ListservRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    list_id = "3gpp_tsg_ran_wg2_concurrent_test"
    normalized_list_id = "3GPP_TSG_RAN_WG2_CONCURRENT_TEST"
    index_url = archive_index_url(list_id)
    page_url = f"https://list.etsi.org/scripts/wa.exe?A1=ind2604A&L={normalized_list_id}"
    message_urls = [
        f"https://list.etsi.org/scripts/wa.exe?A2=ind2604A&L={normalized_list_id}&P={n}"
        for n in (1, 2)
    ]
    sources = [
        RecordingFakeSource(
            pages={
                index_url: _index_html([(page_url, "April 2026, Week 1")]),
                page_url: _listing_html(
                    [(url, f"Message {n}", "Alice", "2026-04-02 10:00:00 +0000")]
                ),
            }
        )
        for n, url in enumerate(message_urls, start=1)
    ]
    services = [
        ListservSyncService(
            repository=repository,
            source=source,
            today_fn=lambda: date(2026, 4, 10),
            max_workers=1,
        )
        for source in sources
    ]
    original_has_list = repository.has_list
    catalog_check_barrier = threading.Barrier(2)

    def synchronized_initial_catalog_check(candidate_id: str) -> bool:
        exists = original_has_list(candidate_id)
        if candidate_id == normalized_list_id:
            catalog_check_barrier.wait(timeout=5)
        return exists

    monkeypatch.setattr(repository, "has_list", synchronized_initial_catalog_check)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                service.sync, list_id, dates=["= 2026-04-02"]
            )
            for service in services
        ]
        results = [future.result(timeout=15) for future in futures]
    monkeypatch.setattr(repository, "has_list", original_has_list)

    assert [result.processed for result in results] == [1, 1]
    assert all(result.failures == {} for result in results)
    assert repository.has_list(normalized_list_id)
    with get_session_factory()() as session:
        catalog_row_count = (
            session.query(ListservListORM)
            .filter_by(list_id=normalized_list_id)
            .count()
        )
    assert catalog_row_count == 1
    assert all(repository.get(url) is not None for url in message_urls)
    assert _listserv_catalog_metadata(normalized_list_id) == ("R2", None)
