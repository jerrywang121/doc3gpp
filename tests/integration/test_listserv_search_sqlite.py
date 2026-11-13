"""SQLite integration tests for LISTSERV source filters, FTS5 search, and payloads."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from importlib.util import find_spec

import httpx
import pytest
from sqlalchemy import text

from doc3gpp.cli_filters import SearchQueryBuilder
from doc3gpp.models.listserv import (
    ArchivePage,
    ListservAttachment,
    ListservDetail,
    ListservFilters,
    ListservMessage,
    parse_date_set,
)
from doc3gpp.models.search import SearchQueryError, SearchUnavailableError
from doc3gpp.models.semantic_search import SemanticSearchUnavailableError
from doc3gpp.repository.listserv import ListservRepository
from doc3gpp.scraping.listserv_source import ListservSource
from doc3gpp.settings.schema import SearchSettings, SemanticSearchSettings, Settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.listserv_sql import SQLAlchemyListservRepository


def _require_task7_modules():
    assert find_spec("doc3gpp.storage.repositories.listserv_search_sql") is not None
    assert find_spec("doc3gpp.services.listserv_search") is not None
    assert find_spec("doc3gpp.models.listserv_payloads") is not None
    from doc3gpp.models.listserv_payloads import (
        indexable_text,
        listserv_record_payload,
        listserv_search_payload,
    )
    from doc3gpp.services.factory import build_listserv_search_service
    from doc3gpp.services.listserv_search import ListservSearchService
    from doc3gpp.storage.repositories.listserv_search_sql import (
        SQLAlchemyListservSearchRepository,
    )

    return (
        SQLAlchemyListservSearchRepository,
        ListservSearchService,
        indexable_text,
        listserv_record_payload,
        listserv_search_payload,
        build_listserv_search_service,
    )


def _make_page(
    list_id: str = "3GPP_TSG_RAN_WG1",
    url: str = "https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1",
    period: str = "2026-10-W1",
) -> ArchivePage:
    return ArchivePage(list_id=list_id, url=url, period=period)


def _make_message(
    url: str = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=3GPP_TSG_RAN_WG1&P=1",
    *,
    list_id: str = "3GPP_TSG_RAN_WG1",
    subject: str = "Topic R1-2610793",
    sender: str = "Alice <[log in to unmask]>",
    dt: datetime = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
    date_precision: str = "datetime",
    week: str = "2026-10-W1",
    subject_tdocs: tuple[str, ...] = ("R1-2610793",),
) -> ListservMessage:
    return ListservMessage(
        url=url,
        list_id=list_id,
        subject=subject,
        sender=sender,
        date=dt,
        date_precision=date_precision,  # type: ignore[arg-type]
        week=week,
        subject_tdocs=subject_tdocs,
    )


@pytest.fixture()
def repository(sqlite_env) -> ListservRepository:
    create_schema("main")
    return SQLAlchemyListservRepository()


class _LazySearchServiceFixture:
    def search(self, *args, **kwargs):
        (
            _SearchRepo,
            _SearchService,
            _indexable_text,
            _record_payload,
            _search_payload,
            build_listserv_search_service,
        ) = _require_task7_modules()
        return build_listserv_search_service().search(*args, **kwargs)


def _build_or_lazy_search_service():
    if (
        find_spec("doc3gpp.storage.repositories.listserv_search_sql") is None
        or find_spec("doc3gpp.services.listserv_search") is None
        or find_spec("doc3gpp.models.listserv_payloads") is None
    ):
        return _LazySearchServiceFixture()
    from doc3gpp.services.factory import build_listserv_search_service

    return build_listserv_search_service()


@pytest.fixture()
def search_service(repository: ListservRepository):
    page = _make_page()
    msg_preview = _make_message(
        "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=3GPP_TSG_RAN_WG1&P=10",
        subject="preview-example",
        subject_tdocs=(),
    )
    repository.commit_page(
        page,
        [msg_preview],
        parse_date_set(["= 2026-10-01"]),
        (),
    )
    repository.save_detail(
        ListservDetail(
            url=msg_preview.url,
            attachments=(),
            tdocs=(),
            content="a" * 201,
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )
    )
    return _build_or_lazy_search_service()


@pytest.fixture()
def thread_search_service(repository: ListservRepository):
    page = _make_page()
    tz_plus_2 = timezone(timedelta(hours=2))
    msg_morning = _make_message(
        "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=3GPP_TSG_RAN_WG1&P=20",
        subject="thread-example",
        dt=datetime(2026, 10, 1, 9, 0, 0, tzinfo=tz_plus_2),
        subject_tdocs=(),
    )
    msg_afternoon = _make_message(
        "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=3GPP_TSG_RAN_WG1&P=21",
        subject="thread-example",
        dt=datetime(2026, 10, 1, 14, 30, 0, tzinfo=tz_plus_2),
        subject_tdocs=(),
    )
    repository.commit_page(
        page,
        [msg_morning, msg_afternoon],
        parse_date_set(["= 2026-10-01"]),
        (),
    )
    return _build_or_lazy_search_service()


def test_search_distinguishes_preview_from_storage_truncation(search_service):
    result, = search_service.search(filters=ListservFilters(subject="preview-example"))
    assert result.content_preview == "a" * 200 and result.preview_truncated is True
    assert result.record.detail.content_truncated is False  # stored content is 201 characters


def test_same_day_thread_messages_are_newest_first(thread_search_service):
    results = thread_search_service.search(filters=ListservFilters(subject="thread-example"))
    assert [item.record.message.date.isoformat() for item in results] == [
        "2026-10-01T14:30:00+02:00", "2026-10-01T09:00:00+02:00"
    ]


def test_tdocs_filter_exact_subject_only_detail_only_null_not_null_and_negation(
    repository: ListservRepository,
) -> None:
    page = _make_page()
    msg_subj_only = _make_message(
        "https://list.etsi.org/msg/subj-only",
        subject="Subject has R1-2610793",
        subject_tdocs=("R1-2610793",),
        dt=datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc),
    )
    msg_detail_only = _make_message(
        "https://list.etsi.org/msg/detail-only",
        subject="Subject has no tdoc",
        subject_tdocs=(),
        dt=datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc),
    )
    msg_both = _make_message(
        "https://list.etsi.org/msg/both",
        subject="Subject has R1-111111",
        subject_tdocs=("R1-111111",),
        dt=datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc),
    )
    msg_neither_unparsed = _make_message(
        "https://list.etsi.org/msg/neither-unparsed",
        subject="Plain unparsed",
        subject_tdocs=(),
        dt=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
    )
    msg_neither_parsed = _make_message(
        "https://list.etsi.org/msg/neither-parsed",
        subject="Plain parsed",
        subject_tdocs=(),
        dt=datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc),
    )
    repository.commit_page(
        page,
        [
            msg_subj_only,
            msg_detail_only,
            msg_both,
            msg_neither_unparsed,
            msg_neither_parsed,
        ],
        parse_date_set(["= 2026-10-01"]),
        (),
    )
    parsed_at = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)
    repository.save_detail(
        ListservDetail(
            url=msg_detail_only.url,
            attachments=(),
            tdocs=("RP-2601234",),
            content="Body mentions RP-2601234",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=parsed_at,
        )
    )
    repository.save_detail(
        ListservDetail(
            url=msg_both.url,
            attachments=(),
            tdocs=("R5s260009",),
            content="Body mentions R5s260009",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=parsed_at,
        )
    )
    repository.save_detail(
        ListservDetail(
            url=msg_neither_parsed.url,
            attachments=(),
            tdocs=(),
            content="No TDocs here",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=parsed_at,
        )
    )

    # Exact match when detail side is empty/unparsed (subject-only)
    assert [
        r.message.url
        for r in repository.select(ListservFilters(tdocs="R1-2610793"))
    ] == [msg_subj_only.url]
    assert [
        r.message.url
        for r in repository.select(ListservFilters(tdocs="r1-2610793"))
    ] == [msg_subj_only.url]

    # Exact match when subject side is empty (detail-only)
    assert [
        r.message.url
        for r in repository.select(ListservFilters(tdocs="RP-2601234"))
    ] == [msg_detail_only.url]
    assert [
        r.message.url
        for r in repository.select(ListservFilters(tdocs="rp-2601234"))
    ] == [msg_detail_only.url]

    # Exact match on either side when both sides are populated
    assert [
        r.message.url
        for r in repository.select(ListservFilters(tdocs="R1-111111"))
    ] == [msg_both.url]
    assert [
        r.message.url
        for r in repository.select(ListservFilters(tdocs="R5s260009"))
    ] == [msg_both.url]

    # null and not-null semantics across union of subject and detail TDocs
    assert [
        r.message.url for r in repository.select(ListservFilters(tdocs="null"))
    ] == [msg_neither_unparsed.url, msg_neither_parsed.url]
    assert [
        r.message.url
        for r in repository.select(ListservFilters(tdocs="not-null"))
    ] == [msg_subj_only.url, msg_detail_only.url, msg_both.url]

    # Negated pattern (!pattern) negates the entire OR across subject and detail
    assert [
        r.message.url
        for r in repository.select(ListservFilters(tdocs="!R1-111111"))
    ] == [
        msg_subj_only.url,
        msg_detail_only.url,
        msg_neither_unparsed.url,
        msg_neither_parsed.url,
    ]
    assert [
        r.message.url
        for r in repository.select(ListservFilters(tdocs="!R5s260009"))
    ] == [
        msg_subj_only.url,
        msg_detail_only.url,
        msg_neither_unparsed.url,
        msg_neither_parsed.url,
    ]
    assert [
        r.message.url
        for r in repository.select(ListservFilters(tdocs="!%R1-%"))
    ] == [
        msg_detail_only.url,
        msg_neither_unparsed.url,
        msg_neither_parsed.url,
    ]


def test_tdocs_contains_filter_matches_one_id_in_comma_delimited_detail_value(
    repository: ListservRepository,
) -> None:
    page = _make_page()
    message = _make_message(
        url="https://list.etsi.org/scripts/wa.exe?A2=multi-tdoc-value",
        subject="Multiple TDoc values",
        subject_tdocs=(),
    )
    repository.commit_page(page, [message], parse_date_set(["= 2026-10-01"]), ())
    repository.save_detail(
        ListservDetail(
            url=message.url,
            attachments=(),
            tdocs=("R1-2610793", "R1-2610792"),
            content="Body mentions multiple TDocs",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )
    )

    matches = repository.select(ListservFilters(tdocs="%R1-2610793%"))

    assert [record.message.url for record in matches] == [message.url]


def test_metadata_only_discoverable_before_parse_and_truncation_states_and_payloads(
    repository: ListservRepository,
) -> None:
    (
        SQLAlchemyListservSearchRepository,
        ListservSearchService,
        _indexable_text,
        listserv_record_payload,
        listserv_search_payload,
        _build_listserv_search_service,
    ) = _require_task7_modules()

    page = _make_page()
    msg_unparsed = _make_message(
        "https://list.etsi.org/msg/unparsed",
        subject="Agenda item R1-2610793 before parse",
        subject_tdocs=("R1-2610793",),
        dt=datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc),
    )
    msg_empty_parsed = _make_message(
        "https://list.etsi.org/msg/empty-parsed",
        subject="Empty body message",
        subject_tdocs=(),
        dt=datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc),
    )
    msg_storage_truncated = _make_message(
        "https://list.etsi.org/msg/storage-truncated",
        subject="Short storage cap message",
        subject_tdocs=(),
        dt=datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc),
    )
    msg_preview_truncated = _make_message(
        "https://list.etsi.org/msg/preview-truncated",
        subject="Long body message",
        subject_tdocs=(),
        dt=datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc),
    )
    repository.commit_page(
        page,
        [
            msg_unparsed,
            msg_empty_parsed,
            msg_storage_truncated,
            msg_preview_truncated,
        ],
        parse_date_set(["= 2026-10-01"]),
        (),
    )

    parsed_at = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    repository.save_detail(
        ListservDetail(
            url=msg_empty_parsed.url,
            attachments=(),
            tdocs=(),
            content="",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=parsed_at,
        )
    )
    repository.save_detail(
        ListservDetail(
            url=msg_storage_truncated.url,
            attachments=(
                ListservAttachment(
                    name="notes.docx",
                    url="https://list.etsi.org/att/notes.docx",
                ),
            ),
            tdocs=("RP-2601234",),
            content="b" * 50,
            content_truncated=True,
            parser_version="1.0.0",
            parsed_at=parsed_at,
        )
    )
    repository.save_detail(
        ListservDetail(
            url=msg_preview_truncated.url,
            attachments=(),
            tdocs=(),
            content="a" * 201,
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=parsed_at,
        )
    )

    fts_repo = SQLAlchemyListservSearchRepository()
    fts_repo.rebuild()
    service = ListservSearchService(repository=repository, fts_repository=fts_repo)

    # Metadata-only message is discoverable before parse in both filter-only and FTS
    filter_unparsed = service.search(filters=ListservFilters(tdocs="R1-2610793"))
    assert [r.record.message.url for r in filter_unparsed] == [msg_unparsed.url]
    unparsed_hit = filter_unparsed[0]
    assert unparsed_hit.record.detail is None
    assert unparsed_hit.content_preview is None
    assert unparsed_hit.preview_truncated is None
    assert unparsed_hit.snippet is None
    assert unparsed_hit.score is None
    assert unparsed_hit.matched_source is None

    fts_unparsed = service.search(text="R1-2610793")
    assert [r.record.message.url for r in fts_unparsed] == [msg_unparsed.url]
    assert fts_unparsed[0].record.detail is None
    assert fts_unparsed[0].content_preview is None
    assert fts_unparsed[0].preview_truncated is None
    assert fts_unparsed[0].score is not None
    assert fts_unparsed[0].snippet is not None
    assert "R1-2610793" in fts_unparsed[0].snippet

    # Empty parsed message has "" and false flags
    empty_hit, = service.search(filters=ListservFilters(subject="Empty body message"))
    assert empty_hit.record.detail is not None
    assert empty_hit.content_preview == ""
    assert empty_hit.preview_truncated is False
    assert empty_hit.record.detail.content_truncated is False
    assert empty_hit.snippet is None

    # Storage-truncated (50 chars) has preview_truncated=False and content_truncated=True
    storage_hit, = service.search(
        filters=ListservFilters(subject="Short storage cap message")
    )
    assert storage_hit.record.detail is not None
    assert storage_hit.content_preview == "b" * 50
    assert storage_hit.preview_truncated is False
    assert storage_hit.record.detail.content_truncated is True

    # Payloads: search payload omits full content and source_revision; show payload has full content
    preview_hit, = service.search(filters=ListservFilters(subject="Long body message"))
    search_json = listserv_search_payload(preview_hit)
    assert search_json["url"] == msg_preview_truncated.url
    assert search_json["content_preview"] == "a" * 200
    assert search_json["preview_truncated"] is True
    assert search_json["content_truncated"] is False
    assert "content" not in search_json
    assert "source_revision" not in search_json

    unparsed_search_json = listserv_search_payload(unparsed_hit)
    assert unparsed_search_json["content_preview"] is None
    assert unparsed_search_json["preview_truncated"] is None
    assert unparsed_search_json["content_truncated"] is None
    assert unparsed_search_json["attachments"] is None
    assert unparsed_search_json["parser_version"] is None
    assert unparsed_search_json["parsed_at"] is None
    assert "content" not in unparsed_search_json
    assert "source_revision" not in unparsed_search_json

    empty_search_json = listserv_search_payload(empty_hit)
    assert empty_search_json["content_preview"] == ""
    assert empty_search_json["preview_truncated"] is False
    assert empty_search_json["content_truncated"] is False
    assert empty_search_json["attachments"] == []

    # Show payloads via listserv_record_payload
    full_rec = repository.get(msg_preview_truncated.url)
    assert full_rec is not None
    show_json = listserv_record_payload(full_rec)
    assert show_json["content"] == "a" * 201
    assert show_json["content_truncated"] is False
    assert "preview_truncated" not in show_json
    assert "source_revision" not in show_json

    unparsed_rec = repository.get(msg_unparsed.url)
    assert unparsed_rec is not None
    unparsed_show_json = listserv_record_payload(unparsed_rec)
    assert unparsed_show_json["content"] is None
    assert unparsed_show_json["content_truncated"] is None
    assert unparsed_show_json["attachments"] is None
    assert unparsed_show_json["tdocs"] == ["R1-2610793"]
    assert "source_revision" not in unparsed_show_json


def test_fts_url_exclusion_attachment_url_tdoc_and_indexed_fields(
    repository: ListservRepository,
) -> None:
    (
        SQLAlchemyListservSearchRepository,
        ListservSearchService,
        indexable_text,
        _record_payload,
        listserv_search_payload,
        _build_listserv_search_service,
    ) = _require_task7_modules()

    page = _make_page(
        list_id="3GPP_TSG_SA_WG2",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610B&L=3GPP_TSG_SA_WG2",
        period="2026-10-W2",
    )
    msg = _make_message(
        "https://list.etsi.org/scripts/wa.exe?A2=ind2610B&L=3GPP_TSG_SA_WG2&P=777",
        list_id="3GPP_TSG_SA_WG2",
        subject="Architecture discussion for 38.300",
        sender="Dr. BobHarbor <[log in to unmask]>",
        dt=datetime(2026, 10, 8, 11, 15, tzinfo=timezone.utc),
        week="2026-10-W2",
        subject_tdocs=(),
    )
    repository.commit_page(page, [msg], parse_date_set(["= 2026-10-08"]), ())

    stored_markdown = (
        "Please review [contribution slides](https://portal.etsi.org/secret-host-token/path-only-token/deck.pptx) "
        "and bare link https://example.org/hidden-url-segment/report.pdf "
        "or &lt;https://list.etsi.org/scripts/wa.exe?A2=othersecret&gt; "
        "regarding MIMO beamforming."
    )
    att = ListservAttachment(
        name="contribution_deck.pptx",
        url="https://list.etsi.org/scripts/wa.exe?A3=ind2610B&L=3GPP_TSG_SA_WG2&N=R1-2610793.zip& secret_att_token=1",
    )
    repository.save_detail(
        ListservDetail(
            url=msg.url,
            attachments=(att,),
            tdocs=("R1-2610793",),
            content=stored_markdown,
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc),
        )
    )

    record = repository.get(msg.url)
    assert record is not None
    # Stored markdown is not modified by indexing sanitization
    assert record.detail is not None
    assert "https://portal.etsi.org/secret-host-token/path-only-token/deck.pptx" in record.detail.content

    sanitized = indexable_text(record)
    assert "https://" not in sanitized
    assert "http://" not in sanitized
    assert "secret-host-token" not in sanitized
    assert "path-only-token" not in sanitized
    assert "hidden-url-segment" not in sanitized
    assert "othersecret" not in sanitized
    assert "secret_att_token" not in sanitized
    assert "wa.exe" not in sanitized
    # Metadata, attachment name, extracted TDoc ID, week, date, and prose remain
    assert "3GPP_TSG_SA_WG2" in sanitized
    assert "Architecture discussion" in sanitized
    assert "BobHarbor" in sanitized
    assert "2026-10-08" in sanitized
    assert "2026-10-W2" in sanitized
    assert "R1-2610793" in sanitized
    assert "contribution_deck.pptx" in sanitized
    assert "contribution slides" in sanitized
    assert "MIMO beamforming" in sanitized

    fts_repo = SQLAlchemyListservSearchRepository()
    fts_repo.upsert(record)
    service = ListservSearchService(repository=repository, fts_repository=fts_repo)

    # URL host/path tokens never match FTS
    for forbidden_token in (
        "secret-host-token",
        "path-only-token",
        "hidden-url-segment",
        "othersecret",
        "secret_att_token",
        "wa.exe",
        "ind2610B",
        "portal.etsi.org",
    ):
        assert service.search(text=forbidden_token) == []

    # TDoc extracted from attachment URL matches FTS
    tdoc_hits = service.search(text="R1-2610793")
    assert [h.record.message.url for h in tdoc_hits] == [msg.url]
    assert tdoc_hits[0].matched_source == "tdocs"
    assert tdoc_hits[0].snippet is not None and "R1-2610793" in tdoc_hits[0].snippet

    # Attachment name, list_id, subject, sender, week, date, and body content match FTS
    att_hits = service.search(text="contribution_deck.pptx")
    assert [h.record.message.url for h in att_hits] == [msg.url]
    assert att_hits[0].matched_source == "attachments"

    list_hits = service.search(text="3GPP_TSG_SA_WG2")
    assert [h.record.message.url for h in list_hits] == [msg.url]
    assert list_hits[0].matched_source == "list_id"

    subj_hits = service.search(text="38.300")
    assert [h.record.message.url for h in subj_hits] == [msg.url]
    assert subj_hits[0].matched_source == "subject"

    sender_hits = service.search(text="BobHarbor")
    assert [h.record.message.url for h in sender_hits] == [msg.url]
    assert sender_hits[0].matched_source == "sender"

    week_hits = service.search(text="2026-10-W2")
    assert [h.record.message.url for h in week_hits] == [msg.url]
    assert week_hits[0].matched_source == "week"

    date_hits = service.search(text="2026-10-08")
    assert [h.record.message.url for h in date_hits] == [msg.url]
    assert date_hits[0].matched_source == "date"

    body_hits = service.search(text="MIMO AND beamforming")
    assert [h.record.message.url for h in body_hits] == [msg.url]
    assert body_hits[0].matched_source == "content"
    assert body_hits[0].snippet is not None and "<<" in body_hits[0].snippet

    # SearchQueryBuilder pre-built MATCH expression also works
    prebuilt = SearchQueryBuilder('MIMO AND "contribution slides"').build()
    assert [h.record.message.url for h in service.search(text=prebuilt)] == [msg.url]

    # Response metadata still preserves message URL and attachment URL
    payload = listserv_search_payload(body_hits[0])
    assert payload["url"] == msg.url
    assert payload["attachments"] == [{"name": att.name, "url": att.url}]

    # Invalid / stopwords-only FTS queries raise SearchQueryError
    with pytest.raises(SearchQueryError):
        service.search(text="the a")
    with pytest.raises(SearchQueryError):
        service.search(text='"unbalanced')


@pytest.mark.parametrize(
    "url_tdoc",
    ["R1-2610793", "R1%2D2610793"],
    ids=("plain", "percent-encoded"),
)
def test_attachment_url_only_tdocs_are_indexed_without_indexing_url_or_fetching(
    repository: ListservRepository,
    monkeypatch: pytest.MonkeyPatch,
    url_tdoc: str,
) -> None:
    from doc3gpp.models import listserv_payloads

    (
        SQLAlchemyListservSearchRepository,
        ListservSearchService,
        indexable_text,
        _record_payload,
        _search_payload,
        _build_listserv_search_service,
    ) = _require_task7_modules()

    page = _make_page()
    msg = _make_message(
        "https://list.etsi.org/msg/attachment-url-only",
        subject="Ordinary architecture discussion",
        subject_tdocs=(),
    )
    repository.commit_page(page, [msg], parse_date_set(["= 2026-10-01"]), ())

    attachment_url = (
        "https://list.etsi.org/archive/uniqueattachmenturltoken/"
        f"?N={url_tdoc}.zip"
    )
    attachment = ListservAttachment(name="ordinary-notes.bin", url=attachment_url)
    repository.save_detail(
        ListservDetail(
            url=msg.url,
            attachments=(attachment,),
            tdocs=(),
            content="Ordinary message content without a TDoc identifier.",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )
    )
    record = repository.get(msg.url)
    assert record is not None
    assert record.message.subject_tdocs == ()
    assert record.detail is not None
    assert record.detail.tdocs == ()
    assert record.detail.attachments == (attachment,)

    def _forbid_network(*_args, **_kwargs):
        raise AssertionError("LISTSERV search must not fetch attachment URLs")

    monkeypatch.setattr(ListservSource, "fetch", _forbid_network)
    monkeypatch.setattr(httpx.Client, "send", _forbid_network)

    sanitized = indexable_text(record)
    assert attachment_url not in sanitized
    assert "uniqueattachmenturltoken" not in sanitized

    fts_repo = SQLAlchemyListservSearchRepository()
    fts_repo.upsert(record)
    service = ListservSearchService(repository=repository, fts_repository=fts_repo)

    hits = service.search(text="R1-2610793")
    assert [hit.record.message.url for hit in hits] == [msg.url]
    assert hits[0].record.tdocs == ("R1-2610793",)
    assert hits[0].matched_source == "tdocs"
    assert service.search(text="uniqueattachmenturltoken") == []

    with monkeypatch.context() as extraction_patch:
        extraction_patch.setattr(
            listserv_payloads, "_extract_attachment_url_tdocs", lambda _urls: ()
        )
        fts_repo.upsert(record)
        assert service.search(text="R1-2610793") == []

    fts_repo.upsert(record)
    restored_hits = service.search(text="R1-2610793")
    assert [hit.record.message.url for hit in restored_hits] == [msg.url]
    assert restored_hits[0].record.tdocs == ("R1-2610793",)


def test_filters_current_subject_tdoc_updates_and_sql_injection_resistance(
    repository: ListservRepository,
) -> None:
    (
        SQLAlchemyListservSearchRepository,
        ListservSearchService,
        _indexable_text,
        _record_payload,
        _search_payload,
        _build_listserv_search_service,
    ) = _require_task7_modules()

    page1 = _make_page("3GPP_TSG_RAN_WG1", "https://list.etsi.org/p1", "2026-10-W1")
    page2 = _make_page("3GPP_TSG_SA_WG2", "https://list.etsi.org/p2", "2026-10-W1")
    m1 = _make_message(
        "https://list.etsi.org/msg/1",
        list_id="3GPP_TSG_RAN_WG1",
        subject="Initial subject R1-111111",
        sender="Alice",
        dt=datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc),
        subject_tdocs=("R1-111111",),
    )
    m2 = _make_message(
        "https://list.etsi.org/msg/2",
        list_id="3GPP_TSG_RAN_WG1",
        subject="Excluded day message R1-222222",
        sender="Alice",
        dt=datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc),
        subject_tdocs=("R1-222222",),
    )
    m3 = _make_message(
        "https://list.etsi.org/msg/3",
        list_id="3GPP_TSG_SA_WG2",
        subject="SA2 topic R1-333333",
        sender="Bob",
        dt=datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc),
        subject_tdocs=("R1-333333",),
    )
    repository.commit_page(
        page1,
        [m1, m2],
        parse_date_set([">= 2026-10-01", "<= 2026-10-05"]),
        (),
    )
    repository.commit_page(
        page2,
        [m3],
        parse_date_set([">= 2026-10-01", "<= 2026-10-05"]),
        (),
    )
    repository.save_detail(
        ListservDetail(
            url=m1.url,
            attachments=(),
            tdocs=("R5s260009",),
            content="Shared keyword alpha in m1",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
        )
    )
    repository.save_detail(
        ListservDetail(
            url=m2.url,
            attachments=(),
            tdocs=(),
            content="Shared keyword alpha in m2",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
        )
    )
    repository.save_detail(
        ListservDetail(
            url=m3.url,
            attachments=(),
            tdocs=(),
            content="Shared keyword alpha in m3",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
        )
    )

    fts_repo = SQLAlchemyListservSearchRepository()
    fts_repo.rebuild()
    service = ListservSearchService(repository=repository, fts_repository=fts_repo)

    # Intersected date filter with hole + LIKE filters in both filter-only and FTS
    hole_dates = parse_date_set([">= 2026-10-01", "!= 2026-10-03", "<= 2026-10-05"])
    assert [
        r.record.message.url
        for r in service.search(filters=ListservFilters(dates=hole_dates))
    ] == [m3.url, m1.url]
    assert [
        r.record.message.url
        for r in service.search(text="alpha", filters=ListservFilters(dates=hole_dates))
    ] == [m3.url, m1.url]

    # list_id, sender, subject LIKE filters on FTS and filter-only
    assert [
        r.record.message.url
        for r in service.search(
            text="alpha",
            filters=ListservFilters(list_id="3gpp_tsg_ran%", sender="Al%", subject="!%Excluded%"),
        )
    ] == [m1.url]

    # Update m1 subject to replace R1-111111 with R1-999999 while keeping detail R5s260009
    m1_updated = _make_message(
        m1.url,
        list_id="3GPP_TSG_RAN_WG1",
        subject="Updated subject R1-999999",
        sender="Alice",
        dt=m1.date,
        subject_tdocs=("R1-999999",),
    )
    repository.commit_page(page1, [m1_updated], parse_date_set(["= 2026-10-02"]), ())
    fts_repo.upsert(m1.url)

    assert service.search(filters=ListservFilters(tdocs="R1-111111")) == []
    assert service.search(text="R1-111111") == []
    hits_new = service.search(filters=ListservFilters(tdocs="R1-999999"))
    assert [h.record.message.url for h in hits_new] == [m1.url]
    assert hits_new[0].record.tdocs == ("R1-999999", "R5S260009")
    assert [
        h.record.message.url
        for h in service.search(text="alpha", filters=ListservFilters(tdocs="R5s260009"))
    ] == [m1.url]

    # SQL injection attempts are bound safely and never match all rows or corrupt tables
    injection_filters = [
        ListservFilters(list_id="' OR '1'='1"),
        ListservFilters(sender="Alice' OR 1=1 --"),
        ListservFilters(subject="'; DROP TABLE listserv_messages; --"),
        ListservFilters(tdocs="R1-999999' OR '1'='1"),
    ]
    for inj_filter in injection_filters:
        assert service.search(filters=inj_filter) == []
        assert service.search(text="alpha", filters=inj_filter) == []

    assert len(service.search()) == 3


def test_ordering_timezones_fallbacks_equal_score_ties_and_pagination(
    repository: ListservRepository,
) -> None:
    (
        SQLAlchemyListservSearchRepository,
        ListservSearchService,
        _indexable_text,
        _record_payload,
        _search_payload,
        _build_listserv_search_service,
    ) = _require_task7_modules()

    page = _make_page()
    tz_minus_5 = timezone(timedelta(hours=-5))
    tz_plus_2 = timezone(timedelta(hours=2))

    # Instant 1 (newest): 2026-10-01T18:00:00-05:00 == 2026-10-01T23:00:00Z
    m_offset_late = _make_message(
        "https://list.etsi.org/msg/offset-late",
        subject="tz-order",
        dt=datetime(2026, 10, 1, 18, 0, 0, tzinfo=tz_minus_5),
        subject_tdocs=(),
    )
    # Instant 2: 2026-10-01T22:00:00+02:00 == 2026-10-01T20:00:00Z
    m_offset_early = _make_message(
        "https://list.etsi.org/msg/offset-early",
        subject="tz-order",
        dt=datetime(2026, 10, 1, 22, 0, 0, tzinfo=tz_plus_2),
        subject_tdocs=(),
    )
    # Fallback 3 (naive wall-clock): 2026-10-01T15:00:00 (naive)
    m_naive = _make_message(
        "https://list.etsi.org/msg/naive",
        subject="tz-order",
        dt=datetime(2026, 10, 1, 15, 0, 0),
        date_precision="datetime",
        subject_tdocs=(),
    )
    # Fallback 4 (day-only midnight): 2026-10-01T00:00:00 (date precision, tie-break b > a)
    m_day_b = _make_message(
        "https://list.etsi.org/msg/day-b",
        subject="tz-order",
        dt=datetime(2026, 10, 1, 0, 0, 0),
        date_precision="date",
        subject_tdocs=(),
    )
    m_day_a = _make_message(
        "https://list.etsi.org/msg/day-a",
        subject="tz-order",
        dt=datetime(2026, 10, 1, 0, 0, 0),
        date_precision="date",
        subject_tdocs=(),
    )

    repository.commit_page(
        page,
        [m_day_b, m_naive, m_offset_early, m_offset_late, m_day_a],
        parse_date_set(["= 2026-10-01"]),
        (),
    )
    for m in (m_day_b, m_naive, m_offset_early, m_offset_late, m_day_a):
        repository.save_detail(
            ListservDetail(
                url=m.url,
                attachments=(),
                tdocs=(),
                content="identical ranked body token syncword",
                content_truncated=False,
                parser_version="1.0.0",
                parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
            )
        )

    fts_repo = SQLAlchemyListservSearchRepository()
    fts_repo.rebuild()
    service = ListservSearchService(repository=repository, fts_repository=fts_repo)

    expected_order = [
        m_offset_late.url,
        m_offset_early.url,
        m_naive.url,
        m_day_a.url,
        m_day_b.url,
    ]

    # Filter-only ordering and preserved local datetime values
    filter_hits = service.search(filters=ListservFilters(subject="tz-order"))
    assert [h.record.message.url for h in filter_hits] == expected_order
    assert filter_hits[0].record.message.date.isoformat() == "2026-10-01T18:00:00-05:00"
    assert filter_hits[1].record.message.date.isoformat() == "2026-10-01T22:00:00+02:00"
    assert filter_hits[2].record.message.date.isoformat() == "2026-10-01T15:00:00"
    assert filter_hits[2].record.message.date.tzinfo is None
    assert filter_hits[3].record.message.date_precision == "date"

    # Equal-score ranked FTS ties use timestamp newest first then URL ascending
    fts_hits = service.search(text="syncword")
    assert [h.record.message.url for h in fts_hits] == expected_order

    # Pagination across pages does not duplicate or omit results in either mode
    for text_query in (None, "syncword"):
        p0 = service.search(text=text_query, filters=ListservFilters(limit=2, offset=0))
        p1 = service.search(text=text_query, filters=ListservFilters(limit=2, offset=2))
        p2 = service.search(text=text_query, filters=ListservFilters(limit=2, offset=4))
        paged_urls = [h.record.message.url for h in (*p0, *p1, *p2)]
        assert paged_urls == expected_order


def test_filter_only_without_fts_or_vector_semantic_unavailable_and_no_network(
    repository: ListservRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _SearchRepo,
        ListservSearchService,
        _indexable_text,
        _record_payload,
        _search_payload,
        build_listserv_search_service,
    ) = _require_task7_modules()

    page = _make_page()
    msg = _make_message("https://list.etsi.org/msg/offline", subject="offline-check")
    repository.commit_page(page, [msg], parse_date_set(["= 2026-10-01"]), ())

    # Drop listserv_search table and disable both FTS and semantic in settings
    with get_engine().begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS listserv_search"))

    disabled_settings = Settings(
        search=SearchSettings(enabled=False),
        semantic_search=SemanticSearchSettings(enabled=False),
    )

    def _forbid_network(*_args, **_kwargs):
        raise AssertionError("Network must never be called during LISTSERV search")

    monkeypatch.setattr(ListservSource, "fetch", _forbid_network)
    monkeypatch.setattr(httpx.Client, "send", _forbid_network)

    service = build_listserv_search_service(settings=disabled_settings)
    results = service.search(filters=ListservFilters(subject="offline-check"))
    assert [r.record.message.url for r in results] == [msg.url]

    # FTS raises SearchUnavailableError when disabled
    with pytest.raises(SearchUnavailableError):
        service.search(text="offline-check")

    # Semantic-only and hybrid raise SemanticSearchUnavailableError until Task 8
    live_service = ListservSearchService(repository=repository)
    with pytest.raises(SemanticSearchUnavailableError):
        live_service.search(semantic="beamforming")
    with pytest.raises(SemanticSearchUnavailableError):
        live_service.search(text="offline-check", semantic="beamforming")


def test_fts_deduplicates_multiple_chunk_matches_per_email_before_pagination(
    repository: ListservRepository,
) -> None:
    (
        SQLAlchemyListservSearchRepository,
        ListservSearchService,
        _indexable_text,
        _record_payload,
        _search_payload,
        _build_listserv_search_service,
    ) = _require_task7_modules()

    page = _make_page()
    m1 = _make_message(
        "https://list.etsi.org/msg/multi-chunk-1",
        subject="Multi chunk first",
        dt=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        subject_tdocs=(),
    )
    m2 = _make_message(
        "https://list.etsi.org/msg/multi-chunk-2",
        subject="Multi chunk second",
        dt=datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc),
        subject_tdocs=(),
    )
    repository.commit_page(page, [m1, m2], parse_date_set(["= 2026-10-01"]), ())
    for m in (m1, m2):
        repository.save_detail(
            ListservDetail(
                url=m.url,
                attachments=(),
                tdocs=(),
                content="chunkmatchtoken paragraph one",
                content_truncated=False,
                parser_version="1.0.0",
                parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
            )
        )

    fts_repo = SQLAlchemyListservSearchRepository()
    fts_repo.rebuild()
    # Insert a second FTS chunk row for m1 that also matches chunkmatchtoken
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO listserv_search (
                    url, list_id, subject, sender, date, week,
                    tdocs, attachments, content, chunk_index, source_revision
                ) VALUES (
                    :url, :list_id, :subject, :sender, :date, :week,
                    '', '', 'chunkmatchtoken chunkmatchtoken paragraph two', 1, '2'
                )
                """
            ),
            {
                "url": m1.url,
                "list_id": m1.list_id,
                "subject": m1.subject,
                "sender": m1.sender,
                "date": "2026-10-01",
                "week": m1.week,
            },
        )

    service = ListservSearchService(repository=repository, fts_repository=fts_repo)
    all_hits = service.search(text="chunkmatchtoken")
    assert [h.record.message.url for h in all_hits] == [m1.url, m2.url]
    # Offset=1 after deduplicating m1's two chunk hits must yield m2, not a second copy of m1
    second_page = service.search(
        text="chunkmatchtoken",
        filters=ListservFilters(limit=1, offset=1),
    )
    assert [h.record.message.url for h in second_page] == [m2.url]
