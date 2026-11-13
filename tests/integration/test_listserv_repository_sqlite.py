"""SQLite integration tests for LISTSERV repository, schema, and revisions."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from doc3gpp.models.listserv import (
    ArchivePage,
    DateInterval,
    DateSet,
    ListservAttachment,
    ListservDetail,
    ListservFilters,
    ListservMessage,
    coverage_contains,
    parse_date_set,
)
from doc3gpp.models.schema_info import RESOURCE_SCHEMAS, schema_payload
from doc3gpp.repository.listserv import ListservRepository
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import (
    get_engine,
    get_specdata_engine,
    get_testcase_engine,
)
from doc3gpp.storage.repositories.listserv_sql import SQLAlchemyListservRepository


@pytest.fixture()
def repo(sqlite_env) -> ListservRepository:
    create_schema("main")
    return SQLAlchemyListservRepository()


def test_catalog_filter_rich_sql_like_and_intersection(repo: ListservRepository) -> None:
    from doc3gpp.services.listserv_service import ListservService
    from doc3gpp.storage.db.models import ListservListORM
    from doc3gpp.storage.db.session import get_session_factory

    with get_session_factory()() as session:
        session.add_all(
            [
                ListservListORM(list_id="CATTEST_A", tsg="R1"),
                ListservListORM(list_id="CATTEST_B", tsg="R2"),
                ListservListORM(list_id="CATTEST_NULL", tsg=None),
            ]
        )
        session.commit()

    def ids(rows):
        return [row.list_id for row in rows]

    assert ids(repo.list_lists(list_id="CATTEST%", tsg="R1")) == ["CATTEST_A"]
    assert ids(repo.list_lists(list_id="CATTEST%", tsg="!R1")) == ["CATTEST_B"]
    assert ids(repo.list_lists(list_id="CATTEST%", tsg="null")) == ["CATTEST_NULL"]
    assert ids(repo.list_lists(list_id="CATTEST%", tsg="not-null")) == [
        "CATTEST_A", "CATTEST_B"
    ]
    assert ids(repo.list_lists(list_id="CATTEST__", tsg="R_")) == [
        "CATTEST_A", "CATTEST_B"
    ]
    assert ids(repo.list_lists(list_id="CATTEST_A")) == ["CATTEST_A"]
    assert repo.list_lists(list_id="CATTEST_A", tsg="R2") == []
    assert repo.list_lists(list_id="CATTEST%' OR 1=1 --") == []
    assert repo.list_lists(list_id="null") == []
    all_ids = ids(repo.list_lists())
    assert all_ids == sorted(all_ids)
    assert repo.list_ids() == all_ids
    assert ids(ListservService(repo).list_lists(list_id="CATTEST%", tsg="R1")) == [
        "CATTEST_A"
    ]


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


def _parsed_item_detail(url: str, content: str = "Body") -> ListservDetail:
    return ListservDetail(
        url=url,
        attachments=(),
        tdocs=(),
        content=content,
        content_truncated=False,
        parser_version="1",
        parsed_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
    )


def test_parsed_item_creates_catalog_and_detail_without_archive_coverage(
    repo: ListservRepository,
) -> None:
    from sqlalchemy import func, select

    from doc3gpp.storage.db.models import ListservArchivePageORM
    from doc3gpp.storage.db.session import get_session_factory

    list_id = "3GPP_TSG_RAN_WG2_PARSE_TEST"
    url = f"https://list.etsi.org/scripts/wa.exe?A2={list_id};abc.2610A&S="
    message = _make_message(url, list_id=list_id)
    detail = _parsed_item_detail(url)
    repo.save_parsed_item(message, detail)

    catalog = repo.list_lists(list_id=list_id)
    assert len(catalog) == 1 and catalog[0].tsg == "R2"
    record = repo.get(url)
    assert record is not None and record.message == message and record.detail == detail
    assert record.source_revision == 2
    with get_session_factory()() as session:
        assert session.scalar(select(func.count()).select_from(ListservArchivePageORM)) == 0

    repo.save_parsed_item(_make_message(url, list_id=list_id, subject="Do not overwrite"), detail)
    unchanged = repo.get(url)
    assert unchanged is not None and unchanged.message == message and unchanged.source_revision == 2
    repo.save_parsed_item(message, _parsed_item_detail(url, "Changed body"))
    changed = repo.get(url)
    assert changed is not None and changed.source_revision == 3


def test_parsed_item_preserves_catalog_metadata(repo: ListservRepository) -> None:
    from doc3gpp.storage.db.models import ListservListORM
    from doc3gpp.storage.db.session import get_session_factory

    list_id = "3GPP_TSG_RAN_WG2_EXISTING_TEST"
    url = f"https://list.etsi.org/scripts/wa.exe?A2={list_id};abc.2610A&S="
    with get_session_factory()() as session:
        session.add(ListservListORM(list_id=list_id, tsg="R1", description="Keep this"))
        session.commit()
    repo.save_parsed_item(_make_message(url, list_id=list_id), _parsed_item_detail(url))
    row = repo.list_lists(list_id=list_id)[0]
    assert (row.tsg, row.description) == ("R1", "Keep this")


def test_parsed_item_unknown_tsg_remains_null(repo: ListservRepository) -> None:
    list_id = "3GPP_TSG_RAN_WG99_UNKNOWN"
    url = f"https://list.etsi.org/scripts/wa.exe?A2={list_id};abc.2610A&S="
    repo.save_parsed_item(_make_message(url, list_id=list_id), _parsed_item_detail(url))
    assert repo.list_lists(list_id=list_id)[0].tsg is None


def test_parsed_item_rolls_back_new_and_existing_records(
    repo: ListservRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    list_id = "3GPP_TSG_RAN_WG2_ROLLBACK_TEST"
    url = f"https://list.etsi.org/scripts/wa.exe?A2={list_id};abc.2610A&S="
    message = _make_message(url, list_id=list_id)
    detail = _parsed_item_detail(url)

    def fail_after_flush(session, _detail):
        session.flush()
        raise RuntimeError("forced transaction failure")

    with monkeypatch.context() as patch:
        patch.setattr(repo, "_save_detail", fail_after_flush, raising=False)
        with pytest.raises(RuntimeError, match="forced transaction failure"):
            repo.save_parsed_item(message, detail)
    assert not repo.has_list(list_id)
    assert repo.get(url) is None

    repo.save_parsed_item(message, detail)
    before = repo.get(url)
    with monkeypatch.context() as patch:
        patch.setattr(repo, "_save_detail", fail_after_flush)
        with pytest.raises(RuntimeError, match="forced transaction failure"):
            repo.save_parsed_item(message, _parsed_item_detail(url, "Changed"))
    assert repo.get(url) == before


def test_parsed_item_rejects_mismatched_urls_before_writes(repo: ListservRepository) -> None:
    list_id = "3GPP_TSG_RAN_WG2_MISMATCH_TEST"
    url = f"https://list.etsi.org/scripts/wa.exe?A2={list_id};abc.2610A&S="
    with pytest.raises(ValueError):
        repo.save_parsed_item(_make_message(url, list_id=list_id), _parsed_item_detail(url + "x"))
    assert not repo.has_list(list_id)
    assert repo.get(url) is None


def test_parsed_item_sync_preserves_suffixless_key_and_detail(repo: ListservRepository) -> None:
    list_id = "3GPP_TSG_RAN_WG2_ALIAS_TEST"
    suffixless = f"https://list.etsi.org/scripts/wa.exe?A2={list_id};abc.2610A"
    canonical = suffixless + "&S="
    page = ArchivePage(list_id=list_id, url="https://list.etsi.org/page", period="2026-10-W1")
    repo.commit_page(page, [_make_message(suffixless, list_id=list_id)], DateSet(), ())
    repo.save_detail(_parsed_item_detail(suffixless))
    repo.commit_page(
        page, [_make_message(canonical, list_id=list_id, subject="Updated by sync")], DateSet(), ()
    )
    record = repo.get(suffixless)
    assert record is not None and record.message.subject == "Updated by sync"
    assert record.detail is not None and record.detail.content == "Body"
    assert record.source_revision == 3
    assert repo.get(canonical) is None


def test_empty_page_coverage_and_next_urls(repo: ListservRepository) -> None:
    page = ArchivePage(
        list_id="3gpp_tsg_ran_wg1",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1",
        period="2026-10-W1",
    )
    assert repo.get_coverage(page) == DateSet()
    assert repo.get_next_urls(page) == ()

    cov1 = parse_date_set([">= 2026-10-01", "<= 2026-10-04"])
    repo.commit_page(
        page,
        messages=(),
        coverage=cov1,
        next_urls=["https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1&S=b"],
    )
    assert repo.get_coverage(page) == cov1
    assert coverage_contains(repo.get_coverage(page), cov1)
    assert repo.get_next_urls(page) == (
        "https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1&S=b",
    )

    cov2 = parse_date_set([">= 2026-10-05", "<= 2026-10-07"])
    repo.commit_page(
        page,
        messages=(),
        coverage=cov2,
        next_urls=[
            "https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1&S=b",
            "https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1&S=c",
        ],
    )
    assert repo.get_coverage(page) == DateSet(
        (DateInterval(date(2026, 10, 1), date(2026, 10, 7)),)
    )
    assert repo.get_next_urls(page) == (
        "https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1&S=b",
        "https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1&S=c",
    )


def test_commit_page_registers_unknown_parent_with_matching_messages(
    repo: ListservRepository,
) -> None:
    list_id = "3gpp_tsg_ran_wg2_catalog_test"
    page = ArchivePage(
        list_id=list_id,
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG2_CATALOG_TEST",
        period="2026-10-W1",
    )
    message = _make_message(
        url="https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=3GPP_TSG_RAN_WG2_CATALOG_TEST&P=1",
        list_id=list_id,
    )
    coverage = parse_date_set(["= 2026-10-01"])
    next_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG2_CATALOG_TEST&S=next"

    assert not repo.has_list(list_id)
    repo.commit_page(page, [message], coverage, [next_url])

    assert repo.has_list(list_id)
    assert repo.get(message.url) is not None
    assert repo.get_coverage(page) == coverage
    assert repo.get_next_urls(page) == (next_url,)


def test_commit_page_does_not_register_unknown_list_for_empty_messages(
    repo: ListservRepository,
) -> None:
    page = ArchivePage(
        list_id="UNMATCHED_LIST",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=UNMATCHED_LIST",
        period="2026-10-W1",
    )

    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        repo.commit_page(
            page,
            messages=(),
            coverage=parse_date_set(["= 2026-10-01"]),
            next_urls=["https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=UNMATCHED_LIST&S=next"],
        )

    assert not repo.has_list(page.list_id)
    assert repo.get_coverage(page) == DateSet()
    assert repo.get_next_urls(page) == ()


def test_commit_page_failure_rolls_back_discovered_parent_and_child_rows(
    repo: ListservRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    list_id = "3GPP_TSG_CT_WG99_CATALOG_TEST"
    page = ArchivePage(
        list_id=list_id,
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_CT_WG99_CATALOG_TEST",
        period="2026-10-W1",
    )
    message = _make_message(
        url="https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=3GPP_TSG_CT_WG99_CATALOG_TEST&P=1",
        list_id=list_id,
    )

    def fail_after_parent_insert(*_args, **_kwargs) -> None:
        raise RuntimeError("simulated failure after discovery insert")

    monkeypatch.setattr(repo, "_upsert_coverage_and_next_urls", fail_after_parent_insert)
    with pytest.raises(RuntimeError, match="after discovery insert"):
        repo.commit_page(
            page,
            [message],
            parse_date_set(["= 2026-10-01"]),
            ["https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_CT_WG99_CATALOG_TEST&S=next"],
        )

    assert not repo.has_list(list_id)
    assert repo.get(message.url) is None
    assert repo.get_coverage(page) == DateSet()
    assert repo.get_next_urls(page) == ()


def test_commit_page_rolls_back_rows_and_coverage_on_exception(
    sqlite_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_schema("main")
    repo = SQLAlchemyListservRepository()
    page = ArchivePage(
        list_id="3GPP_TSG_RAN_WG1",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1",
        period="2026-10-W1",
    )
    msg = _make_message()
    cov = parse_date_set(["= 2026-10-01"])

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated failure inside commit_page")

    monkeypatch.setattr(repo, "_upsert_coverage_and_next_urls", _boom)
    with pytest.raises(RuntimeError, match="simulated failure"):
        repo.commit_page(page, [msg], cov, ["https://list.etsi.org/next"])

    assert repo.get(msg.url) is None
    assert repo.get_coverage(page) == DateSet()
    assert repo.get_next_urls(page) == ()


def test_detail_gzip_unicode_and_unparsed_vs_parsed_empty(repo: ListservRepository) -> None:
    page = ArchivePage(
        list_id="3GPP_TSG_RAN_WG1",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1",
        period="2026-10-W1",
    )
    msg_unparsed = _make_message("https://list.etsi.org/msg/1")
    msg_empty = _make_message("https://list.etsi.org/msg/2", subject="No TDocs", subject_tdocs=())
    msg_unicode = _make_message("https://list.etsi.org/msg/3")
    repo.commit_page(
        page,
        [msg_unparsed, msg_empty, msg_unicode],
        parse_date_set(["= 2026-10-01"]),
        (),
    )

    rec_unparsed = repo.get(msg_unparsed.url)
    assert rec_unparsed is not None
    assert rec_unparsed.detail is None

    parsed_at = datetime(2026, 10, 2, 10, 30, tzinfo=timezone.utc)
    repo.save_detail(
        ListservDetail(
            url=msg_empty.url,
            attachments=(),
            tdocs=(),
            content="",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=parsed_at,
        )
    )
    rec_empty = repo.get(msg_empty.url)
    assert rec_empty is not None
    assert rec_empty.detail is not None
    assert rec_empty.detail.content == ""
    assert rec_empty.detail.attachments == ()
    assert rec_empty.detail.tdocs == ()
    assert rec_empty.detail.content_truncated is False

    unicode_text = "5G NR 载波聚合 — café 🚀 Δφ\nLine 2"
    att = ListservAttachment(
        name="R1-2610793_notes_日本語.docx",
        url="https://list.etsi.org/att/R1-2610793.docx",
    )
    repo.save_detail(
        ListservDetail(
            url=msg_unicode.url,
            attachments=(att,),
            tdocs=("RP-2601234",),
            content=unicode_text,
            content_truncated=True,
            parser_version="1.0.0",
            parsed_at=parsed_at,
        )
    )
    rec_unicode = repo.get(msg_unicode.url)
    assert rec_unicode is not None
    assert rec_unicode.detail is not None
    assert rec_unicode.detail.content == unicode_text
    assert rec_unicode.detail.attachments == (att,)
    assert rec_unicode.detail.content_truncated is True
    assert rec_unicode.detail.parsed_at == parsed_at


def test_current_subject_tdocs_replace_old_subject_tdocs_preserving_body_tdocs(
    repo: ListservRepository,
) -> None:
    page = ArchivePage(
        list_id="3GPP_TSG_RAN_WG1",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1",
        period="2026-10-W1",
    )
    url = "https://list.etsi.org/msg/tdoc-update"
    msg_v1 = _make_message(
        url,
        subject="Initial R1-111111",
        subject_tdocs=("R1-111111",),
    )
    cov = parse_date_set(["= 2026-10-01"])
    repo.commit_page(page, [msg_v1], cov, ())

    repo.save_detail(
        ListservDetail(
            url=url,
            attachments=(),
            tdocs=("R1-222222", "R5s260009"),
            content="Body mentions R1-222222 and R5s260009",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )
    )
    rec1 = repo.get(url)
    assert rec1 is not None
    assert rec1.tdocs == ("R1-111111", "R1-222222", "R5S260009")

    # Listing update changes subject TDoc from R1-111111 to R1-333333
    msg_v2 = _make_message(
        url,
        subject="Updated R1-333333 and r5s260009",
        subject_tdocs=("R1-333333", "r5s260009"),
    )
    repo.commit_page(page, [msg_v2], cov, ())

    rec2 = repo.get(url)
    assert rec2 is not None
    assert "R1-111111" not in rec2.tdocs
    assert rec2.tdocs == ("R1-333333", "R5S260009", "R1-222222")

    # Filtering by tdocs reflects current union, not stale subject ID
    assert repo.select(ListservFilters(tdocs="%R1-111111%")) == []
    assert [r.message.url for r in repo.select(ListservFilters(tdocs="%R1-333333%"))] == [url]
    assert [r.message.url for r in repo.select(ListservFilters(tdocs="%R1-222222%"))] == [url]


def test_fk_integrity_rejects_orphan_detail(repo: ListservRepository) -> None:
    with pytest.raises(IntegrityError):
        repo.save_detail(
            ListservDetail(
                url="https://list.etsi.org/msg/missing",
                attachments=(),
                tdocs=(),
                content="orphan",
                content_truncated=False,
                parser_version="1.0.0",
                parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
            )
        )


def test_source_revisions_increment_only_on_actual_changes(repo: ListservRepository) -> None:
    page = ArchivePage(
        list_id="3GPP_TSG_RAN_WG1",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1",
        period="2026-10-W1",
    )
    url = "https://list.etsi.org/msg/rev"
    cov = parse_date_set(["= 2026-10-01"])
    msg = _make_message(url, subject="Subject v1")

    repo.commit_page(page, [msg], cov, ())
    r1 = repo.get(url)
    assert r1 is not None and r1.source_revision == 1

    # Identical listing upsert does not bump source_revision
    repo.commit_page(page, [msg], cov, ())
    r1_again = repo.get(url)
    assert r1_again is not None and r1_again.source_revision == 1

    # Changed listing bumps once
    msg_v2 = _make_message(url, subject="Subject v2")
    repo.commit_page(page, [msg_v2], cov, ())
    r2 = repo.get(url)
    assert r2 is not None and r2.source_revision == 2

    # Saving detail bumps once
    detail_v1 = ListservDetail(
        url=url,
        attachments=(ListservAttachment(name="a.txt", url="https://list.etsi.org/a.txt"),),
        tdocs=("R1-2610793",),
        content="Body v1",
        content_truncated=False,
        parser_version="1.0.0",
        parsed_at=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
    )
    repo.save_detail(detail_v1)
    r3 = repo.get(url)
    assert r3 is not None and r3.source_revision == 3

    # Identical detail upsert (even with different parsed_at timestamp) does not bump
    detail_v1_repeat = ListservDetail(
        url=url,
        attachments=(ListservAttachment(name="a.txt", url="https://list.etsi.org/a.txt"),),
        tdocs=("R1-2610793",),
        content="Body v1",
        content_truncated=False,
        parser_version="1.0.0",
        parsed_at=datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc),
    )
    repo.save_detail(detail_v1_repeat)
    r3_again = repo.get(url)
    assert r3_again is not None and r3_again.source_revision == 3

    # Changed detail content bumps once
    detail_v2 = ListservDetail(
        url=url,
        attachments=(ListservAttachment(name="a.txt", url="https://list.etsi.org/a.txt"),),
        tdocs=("R1-2610793",),
        content="Body v2",
        content_truncated=False,
        parser_version="1.0.0",
        parsed_at=datetime(2026, 10, 3, 12, 5, tzinfo=timezone.utc),
    )
    repo.save_detail(detail_v2)
    r4 = repo.get(url)
    assert r4 is not None and r4.source_revision == 4


def test_commit_page_duplicate_urls_use_final_value_and_bump_once(
    repo: ListservRepository,
) -> None:
    page = ArchivePage(
        list_id="3GPP_TSG_RAN_WG1",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1",
        period="2026-10-W1",
    )
    coverage = parse_date_set(["= 2026-10-01"])
    url = "https://list.etsi.org/msg/duplicate"
    original = _make_message(url, subject="Original")
    repo.commit_page(page, [original], coverage, ())
    assert repo.revisions() == {url: 1}

    modified = _make_message(url, subject="Intermediate")
    repo.commit_page(page, [modified, original], coverage, ())
    restored = repo.get(url)
    assert restored is not None and restored.message.subject == "Original"
    assert restored.source_revision == 1

    changed = _make_message(url, subject="Changed")
    changed_again = _make_message(url, subject="Final")
    repo.commit_page(page, [original, changed, changed_again], coverage, ())
    final = repo.get(url)
    assert final is not None and final.message.subject == "Final"
    assert final.source_revision == 2

    new_url = "https://list.etsi.org/msg/duplicate-new"
    new_first = _make_message(new_url, subject="New first")
    new_final = _make_message(new_url, subject="New final")
    repo.commit_page(page, [new_first, new_final], coverage, ())
    inserted = repo.get(new_url)
    assert inserted is not None and inserted.message.subject == "New final"
    assert inserted.source_revision == 1

    repo.commit_page(page, [new_first, new_final], coverage, ())
    unchanged = repo.get(new_url)
    assert unchanged is not None and unchanged.message.subject == "New final"
    assert unchanged.source_revision == 1


def test_commit_page_duplicate_failure_rolls_back_rows_revisions_coverage_and_urls(
    repo: ListservRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = ArchivePage(
        list_id="3GPP_TSG_RAN_WG1",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1",
        period="2026-10-W1",
    )
    url = "https://list.etsi.org/msg/duplicate-rollback"
    original = _make_message(url, subject="Stored")
    stored_coverage = parse_date_set(["= 2026-10-01"])
    stored_next = ("https://list.etsi.org/previous",)
    repo.commit_page(page, [original], stored_coverage, stored_next)

    def fail_after_messages(*_args, **_kwargs) -> None:
        raise RuntimeError("simulated failure after message processing")

    monkeypatch.setattr(repo, "_upsert_coverage_and_next_urls", fail_after_messages)
    with pytest.raises(RuntimeError, match="after message processing"):
        repo.commit_page(
            page,
            [_make_message(url, subject="Transient"), original],
            parse_date_set(["= 2026-10-02"]),
            ["https://list.etsi.org/new-next"],
        )

    restored = repo.get(url)
    assert restored is not None and restored.message.subject == "Stored"
    assert repo.revisions() == {url: 1}
    assert repo.get_coverage(page) == stored_coverage
    assert repo.get_next_urls(page) == stored_next


def test_datetime_roundtrip_and_local_date_filtering_and_ordering(
    repo: ListservRepository,
) -> None:
    page = ArchivePage(
        list_id="3GPP_TSG_RAN_WG1",
        url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1",
        period="2026-10-W1",
    )
    # 2026-10-01T23:30:00-07:00 is 2026-10-02T06:30:00Z in UTC,
    # but its displayed local date is 2026-10-01.
    tz_minus_7 = timezone(timedelta(hours=-7))
    tz_plus_2 = timezone(timedelta(hours=2))
    m_aware_late = _make_message(
        "https://list.etsi.org/msg/aware-minus7",
        dt=datetime(2026, 10, 1, 23, 30, 15, 123456, tzinfo=tz_minus_7),
        date_precision="datetime",
    )
    m_aware_early = _make_message(
        "https://list.etsi.org/msg/aware-plus2",
        dt=datetime(2026, 10, 1, 23, 30, 15, 123456, tzinfo=tz_plus_2),
        date_precision="datetime",
    )
    m_naive = _make_message(
        "https://list.etsi.org/msg/naive",
        dt=datetime(2026, 10, 1, 15, 45, 0),
        date_precision="datetime",
    )
    m_date_only = _make_message(
        "https://list.etsi.org/msg/date-only",
        dt=datetime(2026, 10, 1, 0, 0, 0),
        date_precision="date",
    )
    repo.commit_page(
        page,
        [m_date_only, m_naive, m_aware_early, m_aware_late],
        parse_date_set(["= 2026-10-01"]),
        (),
    )

    got_aware = repo.get(m_aware_late.url)
    assert got_aware is not None
    assert got_aware.message.date == m_aware_late.date
    assert got_aware.message.date.utcoffset() == timedelta(hours=-7)
    assert got_aware.message.date.isoformat() == "2026-10-01T23:30:15.123456-07:00"
    assert got_aware.message.date_precision == "datetime"

    got_naive = repo.get(m_naive.url)
    assert got_naive is not None
    assert got_naive.message.date == m_naive.date
    assert got_naive.message.date.tzinfo is None
    assert got_naive.message.date_precision == "datetime"

    got_date = repo.get(m_date_only.url)
    assert got_date is not None
    assert got_date.message.date == m_date_only.date
    assert got_date.message.date.tzinfo is None
    assert got_date.message.date_precision == "date"

    # Filtering by displayed day 2026-10-01 includes m_aware_late (even though UTC is Oct 2)
    oct1 = repo.select(ListservFilters(dates=parse_date_set(["= 2026-10-01"])))
    assert [r.message.url for r in oct1] == [
        m_aware_late.url,   # 2026-10-02 06:30 UTC
        m_aware_early.url,  # 2026-10-01 21:30 UTC
        m_naive.url,        # 2026-10-01 15:45 wall
        m_date_only.url,    # 2026-10-01 00:00 wall
    ]
    oct2 = repo.select(ListservFilters(dates=parse_date_set(["= 2026-10-02"])))
    assert oct2 == []


def test_select_filters_pagination_list_ids_and_iter_for_parse(
    repo: ListservRepository,
) -> None:
    p1 = ArchivePage("3gpp_tsg_ran_wg1", "https://list.etsi.org/p1", "2026-10-W1")
    p2 = ArchivePage("3gpp_tsg_sa_wg2", "https://list.etsi.org/p2", "2026-10-W1")
    ts = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    m1 = _make_message("https://list.etsi.org/m/b", list_id="3gpp_tsg_ran_wg1", sender="Alice", subject="Alpha", dt=ts)
    m2 = _make_message("https://list.etsi.org/m/a", list_id="3gpp_tsg_ran_wg1", sender="Bob", subject="Beta", dt=ts)
    m3 = _make_message("https://list.etsi.org/m/c", list_id="3gpp_tsg_sa_wg2", sender="Carol", subject="Gamma", dt=ts + timedelta(days=1))
    repo.commit_page(p1, [m1, m2], parse_date_set(["= 2026-10-01"]), ())
    repo.commit_page(p2, [m3], parse_date_set(["= 2026-10-02"]), ())

    list_ids = repo.list_ids()
    assert list_ids == sorted(list_ids)
    assert {"3GPP_TSG_RAN_WG1", "3GPP_TSG_SA_WG2"} <= set(list_ids)

    # Equal timestamp tie-break uses deterministic URL ascending
    ran_rows = repo.select(ListservFilters(list_id="3gpp_tsg_ran_wg1"))
    assert [r.message.url for r in ran_rows] == [m2.url, m1.url]

    # Pagination
    paged = repo.select(ListservFilters(limit=1, offset=1))
    assert [r.message.url for r in paged] == [m2.url]

    # iter_for_parse yields unparsed when force=False, all when force=True
    repo.save_detail(
        ListservDetail(
            url=m2.url,
            attachments=(),
            tdocs=(),
            content="parsed",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=ts,
        )
    )
    unparsed_urls = [r.message.url for r in repo.iter_for_parse("3gpp_tsg_ran_wg1", None, force=False)]
    assert unparsed_urls == [m1.url]

    forced_urls = [r.message.url for r in repo.iter_for_parse("3gpp_tsg_ran_wg1", None, force=True)]
    assert set(forced_urls) == {m1.url, m2.url}


def test_schema_is_main_only_idempotent_and_reported(sqlite_env) -> None:
    create_schema("all")
    create_schema("all")  # idempotent

    main_tables = set(inspect(get_engine()).get_table_names())
    tc_tables = set(inspect(get_testcase_engine()).get_table_names())
    spec_tables = set(inspect(get_specdata_engine()).get_table_names())

    expected_listserv_tables = {
        "listserv_list",
        "listserv_messages",
        "listserv_details",
        "listserv_archive_pages",
    }
    assert expected_listserv_tables <= main_tables
    assert expected_listserv_tables.isdisjoint(tc_tables)
    assert expected_listserv_tables.isdisjoint(spec_tables)

    assert "listserv" in RESOURCE_SCHEMAS
    rows = schema_payload("listserv")
    reported_tables = {r["table"] for r in rows}
    assert reported_tables == expected_listserv_tables

    from doc3gpp.storage.db.models import (
        ListservArchivePageORM,
        ListservDetailORM,
        ListservListORM,
        ListservMessageORM,
    )

    orm_by_table = {
        "listserv_list": ListservListORM,
        "listserv_messages": ListservMessageORM,
        "listserv_details": ListservDetailORM,
        "listserv_archive_pages": ListservArchivePageORM,
    }
    for table_schema in RESOURCE_SCHEMAS["listserv"]:
        cols = orm_by_table[table_schema.table].__table__.columns
        assert [f.name for f in table_schema.fields] == list(cols.keys())
        for f in table_schema.fields:
            assert f.nullable == cols[f.name].nullable
