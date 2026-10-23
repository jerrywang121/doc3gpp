"""SQLite coverage for TDoc list selection by eligibility and completeness."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_content_families import (
    TDOC_CONTENT_STORES,
    eligible_type_values,
    is_complete_cr_extraction,
    is_complete_family_extraction,
)
from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
from doc3gpp.models.tdoc_document import (
    TDocDocument,
    markdown_member_name,
    ordered_sources,
)
from doc3gpp.models.tdoc_identity import document_id_for
from doc3gpp.storage.compression import compress_json
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import (
    CONTENT_ORM_BY_STORE_KEY,
    TDocCrDetailOrm,
    TDocExtractOrm,
)
from doc3gpp.storage.db.session import get_session_factory
from doc3gpp.storage.repositories.tdoc_content_sql import (
    SQLAlchemyTDocContentRepository,
)
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

STORES = {store.key: store for store in TDOC_CONTENT_STORES}
WID_STORE = STORES["wid"]


def _url(tdoc_id: str) -> str:
    return f"docs/{tdoc_id.lower()}.zip"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seed_tdoc(tdoc_id: str, tdoc_type: str | None) -> None:
    # The current URL is the TDoc's extract URL, as the portal sync stores it.
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id=tdoc_id, type=tdoc_type, ftp_url=_url(tdoc_id))
    )


def _seed_extract(
    tdoc_id: str,
    *,
    store: str,
    rows: tuple[str, ...],
    main: str,
    document_count: int,
    extraction_version: int = 2,
    skipped: str | None = None,
    cover: bool = False,
    tdoc_type: str = "CR",
) -> None:
    """Store one parent extract with optional cover and content rows.

    Writes raw ORM rows so tests can build states the writer refuses to
    produce.
    """
    url = _url(tdoc_id)
    _seed_tdoc(tdoc_id, tdoc_type)
    with get_session_factory()() as session:
        session.add(
            TDocExtractOrm(
                ftp_url=url,
                tdoc_id=tdoc_id,
                cache_file=f"{tdoc_id}.zip",
                doc_filename=main,
                extraction_version=extraction_version,
                document_count=document_count,
                skipped_sources=skipped,
            )
        )
        if cover:
            session.add(TDocCrDetailOrm(ftp_url=url, tdoc_id=tdoc_id))
        session.flush()
        orm = CONTENT_ORM_BY_STORE_KEY[store]
        for source in rows:
            text = f"markdown for {source}"
            session.add(
                orm(
                    ftp_url=url,
                    doc_filename=source,
                    document_id=document_id_for(url, source),
                    tdoc_id=tdoc_id,
                    content=compress_json(text),
                    content_hash=_sha(text),
                )
            )
        session.commit()


def _ids(rows) -> set[str]:
    return {row.tdoc_id for row in rows}


def _by_id(rows) -> dict[str, TDoc]:
    return {row.tdoc_id: row for row in rows}


def test_default_eligibility_excludes_other(sqlite_env) -> None:
    create_schema()
    _seed_tdoc("R5s260001", "CR")
    _seed_tdoc("R5s260002", "WID new")
    _seed_tdoc("R5s260003", "LS out")
    _seed_tdoc("R5s260004", "discussion")
    _seed_tdoc("R5s260005", "other")
    repo = SQLAlchemyTDocRepository()

    eligible = repo.list(limit=50, eligible_types=eligible_type_values())
    unfiltered = repo.list(limit=50)

    assert _ids(eligible) == {"R5s260001", "R5s260002", "R5s260003", "R5s260004"}
    assert _ids(unfiltered) == {
        "R5s260001",
        "R5s260002",
        "R5s260003",
        "R5s260004",
        "R5s260005",
    }


def test_eligibility_is_case_insensitive(sqlite_env) -> None:
    create_schema()
    _seed_tdoc("R5s260011", "wid revised")
    _seed_tdoc("R5s260012", "Discussion")
    _seed_tdoc("R5s260013", "pCR")

    eligible = SQLAlchemyTDocRepository().list(
        limit=50, eligible_types=eligible_type_values()
    )

    assert _ids(eligible) == {"R5s260011", "R5s260012"}


def test_explicit_type_intersects_with_eligibility(sqlite_env) -> None:
    create_schema()
    _seed_tdoc("R5s260021", "pCR")
    _seed_tdoc("R5s260022", "WID new")
    _seed_tdoc("R5s260023", "WID revised")
    _seed_tdoc("R5s260024", "WID other")
    repo = SQLAlchemyTDocRepository()

    assert repo.list(
        limit=50, tdoc_type="pCR", eligible_types=eligible_type_values()
    ) == []
    wid = repo.list(limit=50, tdoc_type="WID%", eligible_types=eligible_type_values())
    assert _ids(wid) == {"R5s260022", "R5s260023"}


def test_parsed_filter_for_family_and_cr(sqlite_env) -> None:
    create_schema()
    _seed_extract(
        "R5s260031",
        store="wid",
        rows=("main.docx", "annex.docx"),
        main="main.docx",
        document_count=2,
        tdoc_type="WID new",
    )
    _seed_extract(
        "R5s260032",
        store="wid",
        rows=("main.docx", "annex.docx"),
        main="main.docx",
        document_count=2,
        skipped='["broken.docx"]',
        tdoc_type="WID new",
    )
    repo = SQLAlchemyTDocRepository()

    complete = _by_id(repo.list(limit=50, parsed=True))
    incomplete = _by_id(repo.list(limit=50, parsed=False))

    assert complete["R5s260031"].parsed is True
    assert complete["R5s260031"].document_count == 2
    assert incomplete["R5s260032"].parsed is False
    assert incomplete["R5s260032"].document_count is None
    assert set(complete) == {"R5s260031"}
    assert set(incomplete) == {"R5s260032"}


def test_exclude_parsed_drops_complete_family_and_cr(sqlite_env) -> None:
    create_schema()
    _seed_extract(
        "R5s260041",
        store="ls",
        rows=("main.docx",),
        main="main.docx",
        document_count=1,
        tdoc_type="LS in",
    )
    _seed_extract(
        "R5s260042",
        store="cr",
        rows=("annex.docx",),
        main="CRs/main.docx",
        document_count=1,
        cover=True,
    )
    _seed_extract(
        "R5s260043",
        store="cr",
        rows=("annex.docx",),
        main="CRs/main.docx",
        document_count=1,
        extraction_version=1,
        cover=True,
    )
    repo = SQLAlchemyTDocRepository()

    pending = repo.list(limit=50, exclude_parsed=True)
    pending_with_meeting = repo.list_with_meeting(limit=50, exclude_parsed=True)

    assert _ids(pending) == {"R5s260043"}
    assert {row.tdoc.tdoc_id for row in pending_with_meeting} == {"R5s260043"}
    assert _ids(repo.list(limit=50, exclude_parsed=False)) == {
        "R5s260041",
        "R5s260042",
        "R5s260043",
    }


def test_parsed_filter_applies_before_limit(sqlite_env) -> None:
    create_schema()
    for tdoc_id in ("R5s260096", "R5s260095"):
        _seed_extract(
            tdoc_id,
            store="cr",
            rows=("annex.docx",),
            main="CRs/main.docx",
            document_count=1,
            cover=True,
        )
    for tdoc_id in ("R5s260094", "R5s260093"):
        _seed_extract(
            tdoc_id,
            store="cr",
            rows=("annex.docx",),
            main="CRs/main.docx",
            document_count=1,
        )
    repo = SQLAlchemyTDocRepository()

    pending = repo.list(limit=2, parsed=False)
    pending_with_meeting = repo.list_with_meeting(limit=2, parsed=False)

    assert [row.tdoc_id for row in pending] == ["R5s260094", "R5s260093"]
    assert [row.tdoc.tdoc_id for row in pending_with_meeting] == [
        "R5s260094",
        "R5s260093",
    ]
    assert [row.tdoc_id for row in repo.list(limit=2, parsed=True)] == [
        "R5s260096",
        "R5s260095",
    ]


def test_cr_with_cover_and_no_v2_is_not_parsed(sqlite_env) -> None:
    create_schema()
    _seed_extract(
        "R5s260051",
        store="cr",
        rows=(),
        main="CRs/main.docx",
        document_count=0,
        extraction_version=1,
        cover=True,
    )

    row = _by_id(SQLAlchemyTDocRepository().list(limit=10))["R5s260051"]

    assert row.parsed is False
    assert row.document_count is None


def test_cr_with_annexes_is_parsed_with_count(sqlite_env) -> None:
    create_schema()
    _seed_extract(
        "R5s260061",
        store="cr",
        rows=("a/annex.docx", "b/annex.docx"),
        main="CRs/main.docx",
        document_count=2,
        cover=True,
    )

    row = _by_id(SQLAlchemyTDocRepository().list(limit=10))["R5s260061"]

    assert row.parsed is True
    assert row.document_count == 2


def test_zero_annex_cr_is_parsed(sqlite_env) -> None:
    create_schema()
    _seed_extract(
        "R5s260071",
        store="cr",
        rows=(),
        main="CRs/main.docx",
        document_count=0,
        cover=True,
    )

    row = _by_id(SQLAlchemyTDocRepository().list(limit=10))["R5s260071"]

    assert row.parsed is True
    assert row.document_count == 0


def test_tdoc_without_extract_row_is_not_parsed(sqlite_env) -> None:
    create_schema()
    _seed_tdoc("R5s260081", "WID new")

    row = _by_id(SQLAlchemyTDocRepository().list(limit=10))["R5s260081"]

    assert row.parsed is False
    assert row.document_count is None


def test_repository_written_bundles_are_parsed(sqlite_env) -> None:
    create_schema()
    repo = SQLAlchemyTDocContentRepository()
    cr_url = _url("R5s260091")
    family_url = _url("R5s260092")
    _seed_tdoc("R5s260091", "CR")
    _seed_tdoc("R5s260092", "WID new")
    cr_meta = TDocExtractMeta(
        ftp_url=cr_url,
        tdoc_id="R5s260091",
        cache_file="R5s260091.zip",
        doc_filename="CRs/main.docx",
    )
    repo.replace_cr_bundle(
        TDocCRDetails(tdoc_id="R5s260091", ftp_url=cr_url),
        None,
        None,
        cr_meta,
        (),
    )
    family_meta = TDocExtractMeta(
        ftp_url=family_url,
        tdoc_id="R5s260092",
        cache_file="R5s260092.zip",
        doc_filename="main.docx",
        document_count=1,
    )
    repo.replace_family_bundle(
        WID_STORE,
        family_meta,
        (_document(family_url, "main.docx"),),
    )

    rows = _by_id(SQLAlchemyTDocRepository().list(limit=10, parsed=True))

    assert set(rows) == {"R5s260091", "R5s260092"}
    assert rows["R5s260091"].document_count == 0
    assert rows["R5s260092"].document_count == 1


def _document(url: str, source_file: str) -> TDocDocument:
    text = f"markdown for {source_file}"
    document_id = document_id_for(url, source_file)
    return TDocDocument(
        document_id=document_id,
        source_file=source_file,
        markdown_file=markdown_member_name(document_id, source_file),
        file_order=0,
        markdown_text=text,
        content_hash=_sha(text),
    )


@dataclass(frozen=True, slots=True)
class _State:
    tdoc_id: str
    tdoc_type: str
    store: str
    main: str
    document_count: int
    rows: tuple[str, ...]
    extraction_version: int = 2
    skipped: str | None = None
    cover: bool = False


_PARITY_STATES = (
    _State("R5s260101", "CR", "cr", "CRs/main.docx", 2,
           ("a/annex.docx", "b/annex.docx"), cover=True),
    _State("R5s260102", "CR", "cr", "CRs/main.docx", 0, (), cover=True),
    _State("R5s260103", "CR", "cr", "CRs/main.docx", 0, (),
           extraction_version=1, cover=True),
    _State("R5s260104", "CR", "cr", "CRs/main.docx", 1, ("a/annex.docx",)),
    _State("R5s260105", "CR", "cr", "CRs/main.docx", 2, ("a/annex.docx",), cover=True),
    _State("R5s260106", "CR", "cr", "CRs/main.docx", 1, ("CRs/main.docx",), cover=True),
    _State("R5s260107", "CR", "cr", "CRs/main.docx", 1, ("a/annex.docx",),
           skipped='["b/bad.docx"]', cover=True),
    _State("R5s260108", "CR", "cr", "CRs/main.docx", 1, ("a/annex.docx",),
           skipped="[]", cover=True),
    _State("R5s260109", "WID new", "wid", "main.docx", 2, ("main.docx", "a/annex.docx")),
    _State("R5s260110", "LS out", "ls", "main.docx", 2, ("a/annex.docx", "b/other.docx")),
    _State("R5s260111", "discussion", "discussion", "main.docx", 0, ()),
    _State("R5s260112", "WID revised", "wid", "main.docx", 1, ("main.docx",),
           extraction_version=1),
    _State("R5s260113", "LS in", "ls", "main.docx", 1, ("main.docx",)),
    _State("R5s260114", "discussion", "discussion", "main.docx", 2,
           ("main.docx", "a.docx"), skipped='["b.docx"]'),
    _State("R5s260115", "WID new", "wid", "main.docx", 1, ("main.docx", "a.docx")),
)


def _python_expected(state: _State) -> bool:
    url = _url(state.tdoc_id)
    skipped = tuple(json.loads(state.skipped)) if state.skipped else ()
    meta = TDocExtractMeta(
        ftp_url=url,
        tdoc_id=state.tdoc_id,
        cache_file=f"{state.tdoc_id}.zip",
        doc_filename=state.main,
        extraction_version=state.extraction_version,
        document_count=state.document_count,
        skipped_sources=skipped,
    )
    documents = tuple(
        _document(url, source) for source in ordered_sources(state.rows, main=state.main)
    )
    if state.store == "cr":
        return is_complete_cr_extraction(meta, documents, has_cover=state.cover)
    return is_complete_family_extraction(meta, documents)


def test_parity_with_python_rules(sqlite_env) -> None:
    create_schema()
    for state in _PARITY_STATES:
        _seed_extract(
            state.tdoc_id,
            store=state.store,
            rows=state.rows,
            main=state.main,
            document_count=state.document_count,
            extraction_version=state.extraction_version,
            skipped=state.skipped,
            cover=state.cover,
            tdoc_type=state.tdoc_type,
        )

    expected = {state.tdoc_id: _python_expected(state) for state in _PARITY_STATES}
    stored = _by_id(SQLAlchemyTDocRepository().list(limit=100))

    assert set(expected.values()) == {True, False}
    for state in _PARITY_STATES:
        row = stored[state.tdoc_id]
        assert row.parsed is expected[state.tdoc_id], state.tdoc_id
        if row.parsed:
            assert row.document_count == len(state.rows), state.tdoc_id
        else:
            assert row.document_count is None, state.tdoc_id
    complete_ids = {tdoc_id for tdoc_id, parsed in expected.items() if parsed}
    assert _ids(SQLAlchemyTDocRepository().list(limit=100, parsed=True)) == complete_ids
    assert _ids(SQLAlchemyTDocRepository().list(limit=100, parsed=False)) == (
        set(expected) - complete_ids
    )


def test_document_count_and_parsed_follow_the_current_url_only(sqlite_env) -> None:
    create_schema()
    repo = SQLAlchemyTDocContentRepository()
    tdoc_id = "R5s260095"
    old_url = "docs/r5s260095_rev1.zip"
    current_url = "docs/r5s260095_rev2.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id=tdoc_id, type="WID new", ftp_url=current_url)
    )
    repo.replace_family_bundle(
        WID_STORE,
        TDocExtractMeta(
            ftp_url=old_url,
            tdoc_id=tdoc_id,
            cache_file="r5s260095_rev1.zip",
            doc_filename="main.docx",
            document_count=2,
        ),
        (_document(old_url, "main.docx"), _document(old_url, "annex.docx")),
    )

    row = _by_id(SQLAlchemyTDocRepository().list(limit=10))[tdoc_id]
    assert row.parsed is False
    assert row.document_count is None

    repo.replace_family_bundle(
        WID_STORE,
        TDocExtractMeta(
            ftp_url=current_url,
            tdoc_id=tdoc_id,
            cache_file="r5s260095_rev2.zip",
            doc_filename="main.docx",
            document_count=1,
        ),
        (_document(current_url, "main.docx"),),
    )

    row = _by_id(SQLAlchemyTDocRepository().list(limit=10))[tdoc_id]
    assert row.parsed is True
    assert row.document_count == 1
