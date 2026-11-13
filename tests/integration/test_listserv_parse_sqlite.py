"""SQLite integration tests for explicit LISTSERV detail parsing and SQL-only reads."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
from unittest.mock import patch

import httpx
import pytest

from doc3gpp.models.listserv import (
    ArchivePage,
    ListservAttachment,
    ListservBatchResult,
    ListservDetail,
    ListservMessage,
    parse_date_set,
)
from doc3gpp.repository.listserv import ListservRepository
from doc3gpp.scraping.listserv_source import FetchedPage, ListservSource
from doc3gpp.services.factory import (
    build_listserv_parse_service,
    build_listserv_service,
)
from doc3gpp.services.listserv_parse import (
    LISTSERV_DETAIL_PARSER_VERSION,
    ListservParseService,
)
from doc3gpp.services.listserv_service import (
    ListservMessageNotFoundError,
    ListservService,
)
from doc3gpp.settings.schema import ListservSettings, Settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.listserv_sql import SQLAlchemyListservRepository


def _require_task6_modules():
    assert find_spec("doc3gpp.services.listserv_parse") is not None
    assert find_spec("doc3gpp.services.listserv_service") is not None
    return (
        LISTSERV_DETAIL_PARSER_VERSION,
        ListservParseService,
        ListservService,
        ListservMessageNotFoundError,
        build_listserv_parse_service,
        build_listserv_service,
    )


def _detail_html(
    *,
    body_links: list[tuple[str, str]] | None = None,
    attachments: list[tuple[str, str]] | None = None,
    iframe_src: str | None = None,
    inline_body_html: str | None = None,
) -> str:
    part_items: list[str] = []
    for href, label in body_links or []:
        part_items.append(f'<a href="{href}">{label}</a>')
    for href, label in attachments or []:
        part_items.append(f'<a href="{href}">{label}</a>')
    parts_row = (
        f"<tr><th>Parts/Attachments:</th><td>{' , '.join(part_items)}</td></tr>"
        if part_items
        else ""
    )
    iframe_block = (
        f'<div class="viewer"><iframe src="{iframe_src}"></iframe></div>'
        if iframe_src
        else ""
    )
    inline_block = inline_body_html or ""
    return (
        "<html><head><title>LISTSERV Detail</title></head><body>"
        '<table class="message-header">'
        "<tr><th>Subject:</th><td>Test Subject</td></tr>"
        "<tr><th>From:</th><td>Alice &lt;[log in to unmask]&gt;</td></tr>"
        "<tr><th>Date:</th><td>2026-10-01 12:00:00 +0000</td></tr>"
        f"{parts_row}"
        "</table>"
        f"{iframe_block}"
        f"{inline_block}"
        "</body></html>"
    )


def _make_message(
    url: str,
    *,
    list_id: str = "EXAMPLE",
    subject: str = "Topic R1-2600001",
    sender: str = "Alice <[log in to unmask]>",
    dt: datetime = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
    week: str = "2026-10-W1",
    subject_tdocs: tuple[str, ...] = ("R1-2600001",),
) -> ListservMessage:
    return ListservMessage(
        url=url,
        list_id=list_id,
        subject=subject,
        sender=sender,
        date=dt,
        date_precision="datetime",
        week=week,
        subject_tdocs=subject_tdocs,
    )


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
    closed: bool = False
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

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def repository(sqlite_env) -> ListservRepository:
    create_schema("main")
    return SQLAlchemyListservRepository()


@pytest.fixture()
def message_url() -> str:
    return "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=1"


@pytest.fixture()
def parse_service(
    repository: ListservRepository,
    message_url: str,
):
    _, ListservParseService, _, _, _, _ = _require_task6_modules()
    page = ArchivePage(
        list_id="EXAMPLE",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE",
        period="2026-10-W1",
    )
    repository.commit_page(
        page,
        [_make_message(message_url, subject="Short subject", subject_tdocs=())],
        parse_date_set(["= 2026-10-01"]),
        (),
    )
    body_url = (
        "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=101&T=text%2Fplain"
    )
    source = RecordingFakeSource(
        pages={
            message_url: _detail_html(
                body_links=[(body_url, "text/plain")],
                iframe_src=body_url,
            ),
            body_url: FetchedPage(
                text="0123456789 R1-2610793",
                content_type="text/plain; charset=utf-8",
                final_url=body_url,
            ),
        }
    )
    settings = Settings(listserv=ListservSettings(max_content_chars=10))
    return ListservParseService(
        repository=repository,
        source=source,
        settings=settings,
    )


ITEM_URL = "https://list.etsi.org/scripts/wa.exe?A2=NEW_LIST;abc.2610A&S="
ITEM_BODY = "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=NEW_LIST&P=101&T=text%2Fplain"


def test_parse_item_discovers_unknown_list_and_extracts_complete_detail(repository):
    attachment = "https://list.etsi.org/att/R1-2610793_summary.docx"
    html = _detail_html(
        body_links=[(ITEM_BODY, "text/plain")],
        attachments=[(attachment, "R1-2610793_summary.docx")],
    ).replace("Test Subject", "Subject RP-2601234")
    source = RecordingFakeSource(pages={
        ITEM_URL: html,
        ITEM_BODY: FetchedPage(
            text="0123456789 C6-250028", content_type="text/plain", final_url=ITEM_BODY
        ),
    })
    service = ListservParseService(repository=repository, source=source, max_content_chars=10)

    assert service.parse_item(ITEM_URL.removesuffix("&S=")) == ListservBatchResult(1, 0, {})
    assert source.requests == [ITEM_URL, ITEM_BODY]
    record = repository.get(ITEM_URL)
    assert record is not None and record.detail is not None
    assert record.message.subject == "Subject RP-2601234"
    assert record.message.sender == "Alice <[log in to unmask]>"
    assert record.message.week == "2026-10-W1"
    assert record.message.subject_tdocs == ("RP-2601234",)
    assert record.detail.content == "0123456789"
    assert record.detail.content_truncated
    assert record.detail.tdocs == ("C6-250028", "R1-2610793")
    assert record.detail.attachments == (ListservAttachment("R1-2610793_summary.docx", attachment),)
    assert repository.has_list("NEW_LIST")
    assert repository.get_coverage(ArchivePage(
        list_id="NEW_LIST", url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=NEW_LIST",
        period="2026-10-W1",
    )).intervals == ()
    service.close()
    assert source.closed


@pytest.mark.parametrize("stored_with_suffix", [False, True])
def test_parse_item_existing_alias_unparsed_skip_force_and_sync(repository, stored_with_suffix):
    stored = ITEM_URL if stored_with_suffix else ITEM_URL.removesuffix("&S=")
    supplied = ITEM_URL.removesuffix("&S=") if stored_with_suffix else ITEM_URL
    page = ArchivePage(
        list_id="NEW_LIST", url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=NEW_LIST",
        period="2026-10-W1",
    )
    repository.commit_page(page, [_make_message(stored, list_id="NEW_LIST", subject="Known")],
                           parse_date_set(["= 2026-10-01"]), ())
    source = RecordingFakeSource(pages={stored: _detail_html(inline_body_html="<p>First body</p>")})
    service = ListservParseService(repository=repository, source=source)
    assert service.parse_item(supplied) == ListservBatchResult(1, 0, {})
    assert source.requests == [stored]
    assert repository.get(stored).message.subject == "Known"
    assert repository.get(supplied) is None
    source.clear_requests()
    assert service.parse_item(supplied) == ListservBatchResult(0, 1, {})
    assert not source.requests
    assert service.parse_item(supplied, force=True) == ListservBatchResult(1, 0, {})
    assert source.requests == [stored]
    repository.commit_page(page, [_make_message(ITEM_URL, list_id="NEW_LIST", subject="Updated")],
                           parse_date_set(["= 2026-10-01"]), ())
    assert repository.get(stored).detail is not None
    if not stored_with_suffix:
        assert repository.get(ITEM_URL) is None


def test_parse_item_new_message_on_known_list(repository):
    first = ITEM_URL.replace(";abc.", ";first.")
    page = ArchivePage(list_id="NEW_LIST", url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=NEW_LIST", period="2026-10-W1")
    repository.commit_page(page, [_make_message(first, list_id="NEW_LIST")],
                           parse_date_set(["= 2026-10-01"]), ())
    source = RecordingFakeSource(pages={ITEM_URL: _detail_html()})
    result = ListservParseService(repository=repository, source=source).parse_item(ITEM_URL)
    assert result == ListservBatchResult(1, 0, {})
    assert repository.get(ITEM_URL).detail is not None
    assert repository.get(first) is not None


@pytest.mark.parametrize("html", [
    "<html>login required SECRET</html>",
    _detail_html().replace("<tr><th>From:</th><td>Alice &lt;[log in to unmask]&gt;</td></tr>", ""),
    _detail_html().replace("</table>", "<tr><th>Subject:</th><td>SECRET</td></tr></table>"),
    _detail_html().replace(
        "<tr><th>Subject:</th><td>Test Subject</td></tr>",
        "<tr><th>Subject:</th><td>Test Subject</td><th>Subject:</th><td>SECRET</td></tr>",
    ),
])
def test_parse_item_bad_headers_do_not_persist_or_leak(repository, html):
    source = RecordingFakeSource(pages={ITEM_URL: html})
    result = ListservParseService(repository=repository, source=source).parse_item(ITEM_URL)
    assert result.processed == 0 and result.failures
    assert "SECRET" not in str(result.failures)
    assert not repository.has_list("NEW_LIST")
    assert repository.get(ITEM_URL) is None


def test_parse_item_body_and_commit_failures_keep_database_clean(repository, monkeypatch):
    source = RecordingFakeSource(pages={ITEM_URL: _detail_html(body_links=[(ITEM_BODY, "text/plain")])})
    service = ListservParseService(repository=repository, source=source)
    assert service.parse_item(ITEM_URL).failures
    assert not repository.has_list("NEW_LIST") and repository.get(ITEM_URL) is None
    source.pages[ITEM_BODY] = FetchedPage(text="body SECRET", content_type="text/plain", final_url=ITEM_BODY)
    def broken_save(message, detail):
        raise RuntimeError("SECRET")
    monkeypatch.setattr(repository, "save_parsed_item", broken_save)
    result = service.parse_item(ITEM_URL)
    assert result.failures and "SECRET" not in str(result.failures)
    assert not repository.has_list("NEW_LIST") and repository.get(ITEM_URL) is None


def test_parse_item_forced_failure_cancellation_and_invalid_url(repository):
    source = RecordingFakeSource(pages={ITEM_URL: _detail_html(inline_body_html="<p>Good</p>")})
    service = ListservParseService(repository=repository, source=source)
    assert service.parse_item(ITEM_URL).processed == 1
    old = repository.get(ITEM_URL).detail
    source.pages[ITEM_URL] = _detail_html(body_links=[(ITEM_BODY, "text/plain")])
    assert service.parse_item(ITEM_URL, force=True).failures
    assert repository.get(ITEM_URL).detail == old
    source.clear_requests()
    assert service.parse_item(ITEM_URL, force=True, cancelled=lambda: True) == ListservBatchResult(0, 1, {})
    assert not source.requests
    with pytest.raises(ValueError):
        service.parse_item("https://evil.example/SECRET")
    assert not source.requests


def test_parse_item_index_failure_keeps_committed_source(repository):
    from doc3gpp.models.listserv_failures import is_index_failure

    class BrokenIndex:
        closed = False
        def update_records(self, records, *, operation):
            assert operation == "parse" and repository.get(ITEM_URL).detail is not None
            raise RuntimeError("SECRET")
        def close(self):
            self.closed = True

    index = BrokenIndex()
    source = RecordingFakeSource(pages={ITEM_URL: _detail_html()})
    service = ListservParseService(repository=repository, source=source,
                                   index_service=index, owns_index_service=True)
    result = service.parse_item(ITEM_URL)
    assert result.processed == 1 and is_index_failure(result.failures[ITEM_URL])
    assert "SECRET" not in str(result.failures)
    assert repository.has_list("NEW_LIST") and repository.get(ITEM_URL).detail is not None
    service.close()
    assert index.closed and source.closed


def test_parse_extracts_id_beyond_storage_limit(parse_service, repository, message_url):
    result = parse_service.parse("EXAMPLE")  # fixture body: ten characters followed by R1-2610793; limit=10
    detail = repository.get(message_url).detail
    assert not result.failures and detail.content_truncated
    assert len(detail.content) == 10 and "R1-2610793" in detail.tdocs


def test_unicode_ten_character_limit_truncates_only_when_character_removed(
    repository: ListservRepository,
) -> None:
    _, ListservParseService, _, _, _, _ = _require_task6_modules()
    page = ArchivePage(
        list_id="EXAMPLE",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE",
        period="2026-10-W1",
    )
    url_exact_10 = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=10"
    url_eleven = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=11"
    body_exact_10 = "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=110&T=text%2Fplain"
    body_eleven = "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=111&T=text%2Fplain"

    ten_unicode = "5G载波café🚀Δ"
    assert len(ten_unicode) == 10
    assert len(ten_unicode.encode("utf-8")) > 10

    repository.commit_page(
        page,
        [
            _make_message(url_exact_10, subject="Exact 10", subject_tdocs=()),
            _make_message(url_eleven, subject="Eleven chars", subject_tdocs=()),
        ],
        parse_date_set(["= 2026-10-01"]),
        (),
    )

    source = RecordingFakeSource(
        pages={
            url_exact_10: _detail_html(body_links=[(body_exact_10, "text/plain")]),
            body_exact_10: FetchedPage(
                text=ten_unicode,
                content_type="text/plain; charset=utf-8",
                final_url=body_exact_10,
            ),
            url_eleven: _detail_html(body_links=[(body_eleven, "text/plain")]),
            body_eleven: FetchedPage(
                text=ten_unicode + "!",
                content_type="text/plain; charset=utf-8",
                final_url=body_eleven,
            ),
        }
    )
    service = ListservParseService(
        repository=repository,
        source=source,
        settings=Settings(listserv=ListservSettings(max_content_chars=10)),
    )
    result = service.parse("EXAMPLE")
    assert result == ListservBatchResult(processed=2, skipped=0, failures={})

    rec_exact = repository.get(url_exact_10)
    assert rec_exact is not None and rec_exact.detail is not None
    assert rec_exact.detail.content == ten_unicode
    assert len(rec_exact.detail.content) == 10
    assert rec_exact.detail.content_truncated is False

    rec_eleven = repository.get(url_eleven)
    assert rec_eleven is not None and rec_eleven.detail is not None
    assert rec_eleven.detail.content == ten_unicode
    assert len(rec_eleven.detail.content) == 10
    assert rec_eleven.detail.content_truncated is True


def test_list_id_is_mandatory_case_insensitive_and_parse_never_implicitly_syncs(
    repository: ListservRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, ListservParseService, _, _, _, _ = _require_task6_modules()
    page_example = ArchivePage(
        list_id="EXAMPLE",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE",
        period="2026-10-W1",
    )
    page_other = ArchivePage(
        list_id="OTHER_LIST",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=OTHER_LIST",
        period="2026-10-W1",
    )
    msg_example = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=1"
    msg_other = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=OTHER_LIST&P=1"
    body_example = "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=101&T=text%2Fplain"

    repository.commit_page(
        page_example,
        [_make_message(msg_example, list_id="EXAMPLE")],
        parse_date_set(["= 2026-10-01"]),
        (),
    )
    repository.commit_page(
        page_other,
        [_make_message(msg_other, list_id="OTHER_LIST")],
        parse_date_set(["= 2026-10-01"]),
        (),
    )

    source = RecordingFakeSource(
        pages={
            msg_example: _detail_html(body_links=[(body_example, "text/plain")]),
            body_example: FetchedPage(
                text="Example body R1-2610793",
                content_type="text/plain",
                final_url=body_example,
            ),
        }
    )
    service = ListservParseService(repository=repository, source=source)

    # Case-insensitive list_id selects only EXAMPLE, never OTHER_LIST, and never fetches archive index
    res = service.parse("  example  ")
    assert res == ListservBatchResult(processed=1, skipped=0, failures={})
    assert source.requests == [msg_example, body_example]
    assert repository.get(msg_example).detail is not None
    assert repository.get(msg_other).detail is None

    # Unsynced list makes zero network requests (no implicit sync)
    source.clear_requests()
    res_unsynced = service.parse("UNSYNCED_LIST")
    assert res_unsynced == ListservBatchResult(processed=0, skipped=0, failures={})
    assert source.requests == []

    # Invalid list_id or date filter fails before any DB or network call
    db_called = False

    def forbid_db(*_args, **_kwargs):
        nonlocal db_called
        db_called = True
        raise AssertionError("DB must not be queried when validation fails")

    monkeypatch.setattr(repository, "iter_for_parse", forbid_db)
    for bad_list, bad_dates in [
        ("", ()),
        ("   ", ()),
        ("BAD LIST", ()),
        ("LIST&A0=OTHER", ()),
        ("EXAMPLE", ("invalid-date",)),
        ("EXAMPLE", ("= 2026-02-30",)),
    ]:
        with pytest.raises(ValueError):
            service.parse(bad_list, dates=bad_dates)
    with pytest.raises(ValueError):
        ListservParseService(repository=repository, source=source, max_workers=0)
    with pytest.raises(ValueError):
        ListservParseService(repository=repository, source=source, max_content_chars=0)
    assert db_called is False
    assert source.requests == []


def test_optional_dates_filter_and_already_parsed_rows_skip_without_entering_batch_counts(
    repository: ListservRepository,
) -> None:
    _, ListservParseService, _, _, _, _ = _require_task6_modules()
    page = ArchivePage(
        list_id="EXAMPLE",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE",
        period="2026-10-W1",
    )
    m_oct1 = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=1"
    m_oct2 = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=2"
    m_oct3 = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=3"

    b_oct1 = "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=101&T=text%2Fplain"
    b_oct2 = "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=102&T=text%2Fplain"
    b_oct3 = "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=103&T=text%2Fplain"

    repository.commit_page(
        page,
        [
            _make_message(
                m_oct1, dt=datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
            ),
            _make_message(
                m_oct2, dt=datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
            ),
            _make_message(
                m_oct3, dt=datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)
            ),
        ],
        parse_date_set([">= 2026-10-01", "<= 2026-10-03"]),
        (),
    )

    source = RecordingFakeSource(
        pages={
            m_oct1: _detail_html(body_links=[(b_oct1, "text/plain")]),
            b_oct1: FetchedPage(text="Oct 1 body", content_type="text/plain", final_url=b_oct1),
            m_oct2: _detail_html(body_links=[(b_oct2, "text/plain")]),
            b_oct2: FetchedPage(text="Oct 2 body", content_type="text/plain", final_url=b_oct2),
            m_oct3: _detail_html(body_links=[(b_oct3, "text/plain")]),
            b_oct3: FetchedPage(text="Oct 3 body", content_type="text/plain", final_url=b_oct3),
        }
    )
    service = ListservParseService(repository=repository, source=source)

    # 1. Parse only Oct 1..Oct 2
    r1 = service.parse("EXAMPLE", dates=[">= 2026-10-01", "<= 2026-10-02"])
    assert r1 == ListservBatchResult(processed=2, skipped=0, failures={})
    assert repository.get(m_oct1).detail.content == "Oct 1 body"
    assert repository.get(m_oct2).detail.content == "Oct 2 body"
    assert repository.get(m_oct3).detail is None

    # 2. Parse all dates with force=False: Oct 1 & Oct 2 are filtered before selection
    #    (not fetched and NOT included in skipped count); only Oct 3 is processed
    source.clear_requests()
    r2 = service.parse("EXAMPLE", force=False)
    assert r2 == ListservBatchResult(processed=1, skipped=0, failures={})
    assert source.requests == [m_oct3, b_oct3]
    assert repository.get(m_oct3).detail.content == "Oct 3 body"

    # 3. Repeat with force=False when all rows are already parsed: 0 processed, 0 skipped, 0 fetches
    source.clear_requests()
    r3 = service.parse("EXAMPLE", force=False)
    assert r3 == ListservBatchResult(processed=0, skipped=0, failures={})
    assert source.requests == []


def test_iframe_and_content_type_selection_never_fetches_attachments_and_resolves_against_final_url(
    repository: ListservRepository,
) -> None:
    _, ListservParseService, _, _, _, _ = _require_task6_modules()
    page = ArchivePage(
        list_id="EXAMPLE",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE",
        period="2026-10-W1",
    )
    msg_html_url = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=1"
    msg_iframe_plain_url = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=2"
    msg_xhtml_url = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=3"

    # Detail page 1 redirects to a directory path; relative links must resolve against final_url
    detail1_final_url = "https://list.etsi.org/canonical/2026-10/detail-1.html"
    resolved_plain_alt = "https://list.etsi.org/canonical/2026-10/parts/body.txt?A3=ind2610A&T=text%2Fplain"
    resolved_html_body = "https://list.etsi.org/canonical/2026-10/parts/body.html?A3=ind2610A&T=text%2Fhtml"
    resolved_multipart = "https://list.etsi.org/canonical/2026-10/parts/multi?A3=ind2610A&T=multipart%2Frelated"
    resolved_att_docx = (
        "https://list.etsi.org/canonical/2026-10/att/R1-2610793_summary.docx"
    )
    resolved_att_zip = (
        "https://list.etsi.org/canonical/2026-10/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=105&T=application%2Fzip&N=R4-2607922_slides.zip"
    )

    # Detail page 2 has only an iframe pointing to a body URL whose response content_type is text/plain
    iframe_only_url = "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=202"
    xhtml_body_url = "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=303&T=application%2Fxhtml%2Bxml"

    repository.commit_page(
        page,
        [
            _make_message(msg_html_url, subject="HTML msg", subject_tdocs=()),
            _make_message(msg_iframe_plain_url, subject="Iframe plain msg", subject_tdocs=()),
            _make_message(msg_xhtml_url, subject="XHTML msg", subject_tdocs=()),
        ],
        parse_date_set(["= 2026-10-01"]),
        (),
    )

    source = RecordingFakeSource(
        pages={
            msg_html_url: FetchedPage(
                text=_detail_html(
                    body_links=[
                        ("parts/body.txt?A3=ind2610A&T=text%2Fplain", "text/plain"),
                        ("parts/body.html?A3=ind2610A&T=text%2Fhtml", "text/html"),
                        ("parts/multi?A3=ind2610A&T=multipart%2Frelated", "multipart/related"),
                    ],
                    attachments=[
                        ("att/R1-2610793_summary.docx", "R1-2610793_summary.docx"),
                        (
                            "scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=105&T=application%2Fzip&N=R4-2607922_slides.zip",
                            "Archive_Slides.zip",
                        ),
                    ],
                    iframe_src="parts/body.html?A3=ind2610A&T=text%2Fhtml",
                ),
                content_type="text/html",
                final_url=detail1_final_url,
            ),
            resolved_html_body: FetchedPage(
                text=(
                    "<html><body>"
                    "<p>Current HTML body with <strong>RP-2601234</strong>.</p>"
                    "<p>On Mon, Oct 1, 2026 at 08:00 AM Someone wrote:<br>"
                    "&gt; Quoted HTML line with R1-111111 that must not survive</p>"
                    "</body></html>"
                ),
                content_type="text/html; charset=UTF-8",
                final_url=resolved_html_body,
            ),
            msg_iframe_plain_url: _detail_html(iframe_src=iframe_only_url),
            iframe_only_url: FetchedPage(
                text=(
                    "Plain text via iframe mentioning R5s260009.\n\n"
                    "On 2026-09-30 10:00, Prior wrote:\n"
                    "> Old plain quote R2-222222\n"
                ),
                content_type="text/plain; charset=utf-8",
                final_url=iframe_only_url,
            ),
            msg_xhtml_url: _detail_html(
                body_links=[(xhtml_body_url, "application/xhtml+xml")]
            ),
            xhtml_body_url: FetchedPage(
                text=(
                    "<html><body><p>XHTML body with <em>C6-250028</em>.</p>"
                    "<blockquote><p>Quoted R3-333333</p></blockquote></body></html>"
                ),
                content_type="application/xhtml+xml; charset=utf-8",
                final_url=xhtml_body_url,
            ),
        }
    )

    service = ListservParseService(repository=repository, source=source)
    result = service.parse("EXAMPLE")
    assert result == ListservBatchResult(processed=3, skipped=0, failures={})

    # Verify attachment URLs and non-preferred alternatives were NEVER fetched
    assert resolved_att_docx not in source.requests
    assert resolved_att_zip not in source.requests
    assert resolved_plain_alt not in source.requests
    assert resolved_multipart not in source.requests
    assert set(source.requests) == {
        msg_html_url,
        resolved_html_body,
        msg_iframe_plain_url,
        iframe_only_url,
        msg_xhtml_url,
        xhtml_body_url,
    }

    rec1 = repository.get(msg_html_url)
    assert rec1 is not None and rec1.detail is not None
    assert "**RP-2601234**" in rec1.detail.content
    assert "R1-111111" not in rec1.detail.content
    assert rec1.detail.attachments == (
        ListservAttachment(name="R1-2610793_summary.docx", url=resolved_att_docx),
        ListservAttachment(name="Archive_Slides.zip", url=resolved_att_zip),
    )
    # TDocs include current body + attachment name + attachment URL, excluding quoted R1-111111
    assert rec1.detail.tdocs == ("RP-2601234", "R1-2610793", "R4-2607922")

    rec2 = repository.get(msg_iframe_plain_url)
    assert rec2 is not None and rec2.detail is not None
    assert rec2.detail.content == "Plain text via iframe mentioning R5s260009."
    assert rec2.detail.tdocs == ("R5S260009",)

    rec3 = repository.get(msg_xhtml_url)
    assert rec3 is not None and rec3.detail is not None
    assert rec3.detail.content == "XHTML body with *C6-250028*."
    assert rec3.detail.tdocs == ("C6-250028",)


def test_empty_body_is_saved_as_parsed_not_null(
    repository: ListservRepository,
) -> None:
    LISTSERV_DETAIL_PARSER_VERSION, ListservParseService, _, _, _, _ = (
        _require_task6_modules()
    )
    page = ArchivePage(
        list_id="EXAMPLE",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE",
        period="2026-10-W1",
    )
    msg_quote_only = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=1"
    msg_no_body = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=2"
    body_quote_only = "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=101&T=text%2Fplain"

    repository.commit_page(
        page,
        [
            _make_message(msg_quote_only, subject="Quote only", subject_tdocs=()),
            _make_message(msg_no_body, subject="Header only", subject_tdocs=()),
        ],
        parse_date_set(["= 2026-10-01"]),
        (),
    )

    source = RecordingFakeSource(
        pages={
            msg_quote_only: _detail_html(body_links=[(body_quote_only, "text/plain")]),
            body_quote_only: FetchedPage(
                text="> Only quoted previous text R1-111111\n",
                content_type="text/plain",
                final_url=body_quote_only,
            ),
            msg_no_body: _detail_html(),
        }
    )
    service = ListservParseService(repository=repository, source=source)
    r1 = service.parse("EXAMPLE")
    assert r1 == ListservBatchResult(processed=2, skipped=0, failures={})

    for url in (msg_quote_only, msg_no_body):
        rec = repository.get(url)
        assert rec is not None
        assert rec.detail is not None
        assert rec.detail.content == ""
        assert rec.detail.content_truncated is False
        assert rec.detail.tdocs == ()
        assert rec.detail.parser_version == LISTSERV_DETAIL_PARSER_VERSION

    # Because empty body is stored as parsed (not null), re-running without force skips both
    source.clear_requests()
    r2 = service.parse("EXAMPLE", force=False)
    assert r2 == ListservBatchResult(processed=0, skipped=0, failures={})
    assert source.requests == []


def test_force_reparse_atomically_replaces_detail_and_failed_reparse_retains_prior_detail_without_body_leaks(
    repository: ListservRepository,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    LISTSERV_DETAIL_PARSER_VERSION, ListservParseService, _, _, _, _ = (
        _require_task6_modules()
    )
    page = ArchivePage(
        list_id="EXAMPLE",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE",
        period="2026-10-W1",
    )
    msg_url = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=1"
    body_url = "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=101&T=text%2Fplain"

    repository.commit_page(
        page,
        [_make_message(msg_url, subject="Subject R1-2600001", subject_tdocs=("R1-2600001",))],
        parse_date_set(["= 2026-10-01"]),
        (),
    )

    # Seed an older parsed detail (version 0.9.0)
    old_parsed_at = datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)
    repository.save_detail(
        ListservDetail(
            url=msg_url,
            attachments=(
                ListservAttachment(
                    name="old_R1-111111.docx",
                    url="https://list.etsi.org/att/old_R1-111111.docx",
                ),
            ),
            tdocs=("R1-111111",),
            content="Old truncated content",
            content_truncated=True,
            parser_version="0.9.0",
            parsed_at=old_parsed_at,
        )
    )
    assert repository.get(msg_url).source_revision == 2

    new_att_url = "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=104&T=application%2Fzip&N=R2-222222.zip"
    source = RecordingFakeSource(
        pages={
            msg_url: _detail_html(
                body_links=[(body_url, "text/plain")],
                attachments=[(new_att_url, "R2-222222.zip")],
            ),
            body_url: FetchedPage(
                text="Fresh full body mentioning R3-333333",
                content_type="text/plain",
                final_url=body_url,
            ),
        }
    )
    service = ListservParseService(repository=repository, source=source)

    # 1. Force reparse atomically replaces content, truncated flag, attachments, tdocs, and parser_version
    res_force = service.parse("EXAMPLE", force=True)
    assert res_force == ListservBatchResult(processed=1, skipped=0, failures={})
    updated = repository.get(msg_url)
    assert updated is not None and updated.detail is not None
    assert updated.detail.content == "Fresh full body mentioning R3-333333"
    assert updated.detail.content_truncated is False
    assert updated.detail.attachments == (
        ListservAttachment(name="R2-222222.zip", url=new_att_url),
    )
    assert updated.detail.tdocs == ("R3-333333", "R2-222222")
    assert updated.tdocs == ("R1-2600001", "R3-333333", "R2-222222")
    assert updated.detail.parser_version == LISTSERV_DETAIL_PARSER_VERSION
    assert updated.detail.parsed_at > old_parsed_at
    assert updated.source_revision == 3

    # 2. Failed force reparse (HTTP error with secret body on body_url) retains prior detail & leaks no body
    secret_body = "CONFIDENTIAL_UPSTREAM_ERROR_BODY_7788"
    req = httpx.Request("GET", body_url)
    resp = httpx.Response(502, content=secret_body.encode("utf-8"), request=req)
    source.pages[body_url] = httpx.HTTPStatusError(
        f"502 Bad Gateway: {secret_body}", request=req, response=resp
    )
    progress_msgs: list[str] = []
    with caplog.at_level(logging.DEBUG):
        res_fail = service.parse(
            "EXAMPLE", force=True, on_progress=progress_msgs.append
        )

    assert res_fail.processed == 0
    assert res_fail.skipped == 0
    assert list(res_fail.failures.keys()) == [msg_url]
    assert secret_body not in " ".join(res_fail.failures.values())
    assert secret_body not in " ".join(progress_msgs)
    assert secret_body not in caplog.text

    retained = repository.get(msg_url)
    assert retained == updated

    # 3. Simulated DB exception during save_detail also records failure and retains prior detail
    source.pages[body_url] = FetchedPage(
        text="Should fail at DB save R4-444444",
        content_type="text/plain",
        final_url=body_url,
    )
    orig_save = repository.save_detail

    def boom_save(_detail: ListservDetail) -> None:
        raise RuntimeError(f"DB error with secret {secret_body}")

    monkeypatch.setattr(repository, "save_detail", boom_save)
    res_db_fail = service.parse("EXAMPLE", force=True)
    assert res_db_fail.processed == 0
    assert list(res_db_fail.failures.keys()) == [msg_url]
    assert secret_body not in " ".join(res_db_fail.failures.values())
    monkeypatch.setattr(repository, "save_detail", orig_save)
    assert repository.get(msg_url) == updated


def test_rejects_hostile_persisted_message_and_body_urls_before_fetching(
    repository: ListservRepository,
) -> None:
    _, ListservParseService, _, _, _, _ = _require_task6_modules()
    page = ArchivePage(
        list_id="EXAMPLE",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE",
        period="2026-10-W1",
    )
    hostile_msg_url = "https://evil.example/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=1"
    attachment_msg_url = (
        "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=1&T=application%2Fzip&N=bad.zip"
    )
    msg_with_hostile_body = (
        "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=2"
    )
    hostile_body_url = "https://evil.example/body.html"
    msg_with_att_body = (
        "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=3"
    )
    att_body_url = (
        "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=99&N=exploit.zip"
    )
    good_msg_url = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=4"
    good_body_url = (
        "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=104&T=text%2Fplain"
    )

    repository.commit_page(
        page,
        [
            _make_message(hostile_msg_url),
            _make_message(attachment_msg_url),
            _make_message(msg_with_hostile_body),
            _make_message(msg_with_att_body),
            _make_message(good_msg_url),
        ],
        parse_date_set(["= 2026-10-01"]),
        (),
    )

    source = RecordingFakeSource(
        pages={
            hostile_msg_url: "SHOULD NEVER BE FETCHED",
            attachment_msg_url: "SHOULD NEVER BE FETCHED",
            msg_with_hostile_body: _detail_html(iframe_src=hostile_body_url),
            hostile_body_url: "SHOULD NEVER BE FETCHED",
            msg_with_att_body: _detail_html(iframe_src=att_body_url),
            att_body_url: "SHOULD NEVER BE FETCHED",
            good_msg_url: _detail_html(body_links=[(good_body_url, "text/plain")]),
            good_body_url: FetchedPage(
                text="Valid message body R1-2610793",
                content_type="text/plain",
                final_url=good_body_url,
            ),
        }
    )

    service = ListservParseService(repository=repository, source=source)
    result = service.parse("EXAMPLE")

    # Hostile message URLs and hostile/attachment body URLs must be rejected BEFORE source.fetch()
    assert hostile_msg_url not in source.requests
    assert attachment_msg_url not in source.requests
    assert hostile_body_url not in source.requests
    assert att_body_url not in source.requests

    assert result.processed == 1
    assert result.skipped == 0
    assert set(result.failures.keys()) == {
        hostile_msg_url,
        attachment_msg_url,
        msg_with_hostile_body,
        msg_with_att_body,
    }
    assert repository.get(good_msg_url).detail.content == "Valid message body R1-2610793"


def test_unpaginated_iteration_bounded_concurrency_and_cancellation(
    repository: ListservRepository,
) -> None:
    _, ListservParseService, _, _, _, _ = _require_task6_modules()
    page = ArchivePage(
        list_id="BULK_LIST",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=BULK_LIST",
        period="2026-10-W1",
    )
    # 160 messages exceeds ListservFilters max limit (150) and default limit (20)
    total_msgs = 160
    base_dt = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    messages = [
        _make_message(
            f"https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=BULK_LIST&P={i}",
            list_id="BULK_LIST",
            dt=base_dt + timedelta(minutes=i),
        )
        for i in range(1, total_msgs + 1)
    ]
    repository.commit_page(page, messages, parse_date_set(["= 2026-10-01"]), ())

    pages_map: dict[str, str | FetchedPage] = {}
    for i in range(1, total_msgs + 1):
        d_url = f"https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=BULK_LIST&P={i}"
        b_url = f"https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=BULK_LIST&P={i}&T=text%2Fplain"
        pages_map[d_url] = _detail_html(body_links=[(b_url, "text/plain")])
        pages_map[b_url] = FetchedPage(
            text=f"Body {i} with R1-2610{i:03d}",
            content_type="text/plain",
            final_url=b_url,
        )

    def brief_delay(url: str) -> None:
        if url.endswith(("&P=1", "&P=2", "&P=3", "&P=4")):
            time.sleep(0.02)

    source_bulk = RecordingFakeSource(pages=pages_map, on_fetch=brief_delay)
    service_bulk = ListservParseService(
        repository=repository,
        source=source_bulk,
        max_workers=2,
    )
    res_bulk = service_bulk.parse("BULK_LIST")
    assert res_bulk == ListservBatchResult(processed=total_msgs, skipped=0, failures={})
    assert 1 <= source_bulk.max_active_fetches <= 2

    # Part 2: Cancellation stops scheduling new records, lets in-flight finish & save,
    # and counts remaining unscheduled selected records in skipped
    cancel_page = ArchivePage(
        list_id="CANCEL_LIST",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=CANCEL_LIST",
        period="2026-10-W1",
    )
    cancel_msgs = [
        _make_message(
            f"https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=CANCEL_LIST&P={i}",
            list_id="CANCEL_LIST",
            dt=base_dt + timedelta(minutes=i),
        )
        for i in range(1, 5)
    ]
    repository.commit_page(
        cancel_page, cancel_msgs, parse_date_set(["= 2026-10-01"]), ()
    )

    cancel_pages: dict[str, str | FetchedPage] = {}
    for i in range(1, 5):
        d_url = f"https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=CANCEL_LIST&P={i}"
        b_url = f"https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=CANCEL_LIST&P={i}&T=text%2Fplain"
        cancel_pages[d_url] = _detail_html(body_links=[(b_url, "text/plain")])
        cancel_pages[b_url] = FetchedPage(
            text=f"Cancel body {i}",
            content_type="text/plain",
            final_url=b_url,
        )

    # iter_for_parse orders newest first, so P=4 is the first record scheduled
    first_detail_url = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=CANCEL_LIST&P=4"
    first_body_url = "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=CANCEL_LIST&P=4&T=text%2Fplain"
    cancel_flag = False

    def latch_cancel_on_first_detail(url: str) -> None:
        nonlocal cancel_flag
        if url == first_detail_url:
            cancel_flag = True

    source_cancel = RecordingFakeSource(
        pages=cancel_pages, on_fetch=latch_cancel_on_first_detail
    )
    service_cancel = ListservParseService(
        repository=repository,
        source=source_cancel,
        max_workers=1,
    )
    res_cancel = service_cancel.parse(
        "CANCEL_LIST", cancelled=lambda: cancel_flag
    )
    assert res_cancel == ListservBatchResult(processed=1, skipped=3, failures={})
    assert source_cancel.requests == [first_detail_url, first_body_url]
    assert repository.get(first_detail_url).detail.content == "Cancel body 4"
    for i in (1, 2, 3):
        unparsed_url = f"https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=CANCEL_LIST&P={i}"
        assert repository.get(unparsed_url).detail is None

    # Pre-cancelled run on selected batch skips all selected records without fetching
    source_cancel.clear_requests()
    res_precancel = service_cancel.parse(
        "CANCEL_LIST", cancelled=lambda: True
    )
    assert res_precancel == ListservBatchResult(processed=0, skipped=3, failures={})
    assert source_cancel.requests == []

    # Resume without cancellation: P=4 is already parsed (outside batch counts), P=1..3 are processed
    source_cancel.on_fetch = None
    source_cancel.clear_requests()
    res_resume = service_cancel.parse("CANCEL_LIST")
    assert res_resume == ListservBatchResult(processed=3, skipped=0, failures={})
    assert first_detail_url not in source_cancel.requests


def test_show_and_list_ids_make_no_network_calls_and_return_stored_data_and_unioned_tdocs(
    repository: ListservRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _,
        ListservParseService,
        ListservService,
        ListservMessageNotFoundError,
        _,
        _,
    ) = _require_task6_modules()

    p1 = ArchivePage(
        list_id="3GPP_TSG_RAN_WG1",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1",
        period="2026-10-W1",
    )
    p2 = ArchivePage(
        list_id="3GPP_TSG_SA_WG2",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_SA_WG2",
        period="2026-10-W1",
    )
    parsed_url = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=3GPP_TSG_RAN_WG1&P=1"
    unparsed_url = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=3GPP_TSG_SA_WG2&P=1"
    body_url = "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=3GPP_TSG_RAN_WG1&P=101&T=text%2Fplain"

    repository.commit_page(
        p1,
        [
            _make_message(
                parsed_url,
                list_id="3GPP_TSG_RAN_WG1",
                subject="Initial R1-100001 and R1-2610793",
                subject_tdocs=("R1-100001", "R1-2610793"),
            )
        ],
        parse_date_set(["= 2026-10-01"]),
        (),
    )
    repository.commit_page(
        p2,
        [
            _make_message(
                unparsed_url,
                list_id="3GPP_TSG_SA_WG2",
                subject="SA2 topic S2-2605184",
                subject_tdocs=("S2-2605184",),
            )
        ],
        parse_date_set(["= 2026-10-01"]),
        (),
    )

    source = RecordingFakeSource(
        pages={
            parsed_url: _detail_html(body_links=[(body_url, "text/plain")]),
            body_url: FetchedPage(
                text="0123456789 Tail mentions R1-2610793 and RP-2601234",
                content_type="text/plain",
                final_url=body_url,
            ),
        }
    )
    parse_svc = ListservParseService(
        repository=repository,
        source=source,
        settings=Settings(listserv=ListservSettings(max_content_chars=10)),
    )
    parse_svc.parse("3gpp_tsg_ran_wg1")

    # Update message subject so R1-100001 is replaced by R1-900009
    repository.commit_page(
        p1,
        [
            _make_message(
                parsed_url,
                list_id="3GPP_TSG_RAN_WG1",
                subject="Updated R1-900009",
                subject_tdocs=("R1-900009",),
            )
        ],
        parse_date_set(["= 2026-10-01"]),
        (),
    )

    # Forbid any network access during list_ids and show
    def forbid_network(*_args, **_kwargs):
        raise AssertionError("No network calls allowed in ListservService")

    monkeypatch.setattr(ListservSource, "fetch", forbid_network)
    monkeypatch.setattr(httpx.Client, "get", forbid_network)

    read_service = ListservService(repository=repository)
    list_ids = read_service.list_ids()
    assert list_ids == sorted(list_ids)
    assert {"3GPP_TSG_RAN_WG1", "3GPP_TSG_SA_WG2"} <= set(list_ids)

    rec_parsed = read_service.show(parsed_url)
    assert rec_parsed.detail is not None
    assert rec_parsed.detail.content == "0123456789"
    assert "Tail mentions" not in rec_parsed.detail.content
    assert rec_parsed.detail.content_truncated is True
    assert rec_parsed.detail.tdocs == ("R1-2610793", "RP-2601234")
    assert rec_parsed.tdocs == ("R1-900009", "R1-2610793", "RP-2601234")
    assert "R1-100001" not in rec_parsed.tdocs

    rec_unparsed = read_service.show(unparsed_url)
    assert rec_unparsed.detail is None
    assert rec_unparsed.tdocs == ("S2-2605184",)

    assert issubclass(ListservMessageNotFoundError, LookupError)
    with pytest.raises(ListservMessageNotFoundError):
        read_service.show("https://list.etsi.org/scripts/wa.exe?A2=missing")


def test_factory_builders_and_parse_service_source_lifetime(sqlite_env) -> None:
    (
        _,
        ListservParseService,
        ListservService,
        _,
        build_listserv_parse_service,
        build_listserv_service,
    ) = _require_task6_modules()
    create_schema("main")
    custom_settings = Settings(
        http_verify=False,
        http_max_retries=5,
        http_retry_backoff=1.5,
        listserv=ListservSettings(max_content_chars=42),
    )

    with patch("doc3gpp.scraping.listserv_source.httpx.Client", wraps=httpx.Client) as client_spy:
        parse_svc = build_listserv_parse_service(settings=custom_settings)
        try:
            assert isinstance(parse_svc, ListservParseService)
            assert client_spy.call_args.kwargs["verify"] is False
            assert parse_svc._source._max_retries == 5
            assert parse_svc._source._backoff == 1.5
            # Empty parse does not close the owned source
            parse_svc.parse("EXAMPLE")
            assert parse_svc._source._closed is False
        finally:
            parse_svc.close()
        assert parse_svc._source._closed is True

    read_svc = build_listserv_service(settings=custom_settings)
    assert isinstance(read_svc, ListservService)
    assert not hasattr(read_svc, "_source")
