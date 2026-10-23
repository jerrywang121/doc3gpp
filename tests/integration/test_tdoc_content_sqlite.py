"""SQLite coverage for the per-type TDoc content repository."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from sqlalchemy import func, select, text

from doc3gpp.models.tdoc_content_families import (
    TDOC_CONTENT_STORES,
    is_complete_cr_extraction,
    is_complete_family_extraction,
)
from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocCRTTCNDetails, TDocExtractMeta
from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
from doc3gpp.models.tdoc_document import TDocDocument, markdown_member_name
from doc3gpp.models.tdoc_identity import document_id_for
from doc3gpp.storage.compression import compress_json, decompress_json
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import (
    TDocCrAnnexOrm,
    TDocDiscussionContentOrm,
    TDocExtractOrm,
    TDocLsContentOrm,
    TDocORM,
    TDocWidContentOrm,
)
from doc3gpp.storage.db.session import get_engine, get_session_factory
from doc3gpp.storage.repositories.tdoc_content_sql import (
    SQLAlchemyTDocContentRepository,
)
from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
    SQLAlchemyTDocCrChangeDetailsRepository,
)
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
    SQLAlchemyTDocCrTtcnRepository,
)

STORES = {store.key: store for store in TDOC_CONTENT_STORES}
CR = STORES["cr"]
WID = STORES["wid"]
LS = STORES["ls"]
DISCUSSION = STORES["discussion"]
TDOC_ID = "R5s260009"
URL = "r5/docs/r5s260009.zip"
MAIN = "CRs/main/R5-000003.docx"


def _repo() -> SQLAlchemyTDocContentRepository:
    return SQLAlchemyTDocContentRepository()


def _seed_tdoc() -> None:
    with get_session_factory()() as session:
        session.add(TDocORM(tdoc_id=TDOC_ID))
        session.commit()


def _document(source_file: str, markdown: str) -> TDocDocument:
    document_id = document_id_for(URL, source_file)
    return TDocDocument(
        document_id=document_id,
        source_file=source_file,
        markdown_file=markdown_member_name(document_id, source_file),
        file_order=0,
        markdown_text=markdown,
        content_hash=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
    )


def _meta(doc_filename: str, document_count: int, **overrides) -> TDocExtractMeta:
    values = {
        "ftp_url": URL,
        "tdoc_id": TDOC_ID,
        "cache_file": "r5s260009.zip",
        "doc_filename": doc_filename,
        "document_count": document_count,
    }
    values.update(overrides)
    return TDocExtractMeta(**values)


def _cover(title: str = "Initial cover") -> TDocCRDetails:
    return TDocCRDetails(tdoc_id=TDOC_ID, ftp_url=URL, title=title)


def _count(orm) -> int:
    with get_session_factory()() as session:
        return session.scalar(select(func.count()).select_from(orm))


def test_replace_family_bundle_roundtrips_special_names(sqlite_env) -> None:
    create_schema("main")
    _seed_tdoc()
    main = _document(MAIN, "# Main body")
    unicode_annex = _document("annex/Ünïcode > x.docx", "# Unicode annex")
    spaced_annex = _document("annex/with space.docx", "# Spaced annex")

    _repo().replace_family_bundle(WID, _meta(MAIN, 3), (main, unicode_annex, spaced_annex))

    listed = _repo().list_documents(WID, URL)
    assert [document.source_file for document in listed] == [
        MAIN,
        "annex/with space.docx",
        "annex/Ünïcode > x.docx",
    ]
    assert [document.file_order for document in listed] == [0, 1, 2]
    assert [document.markdown_text for document in listed] == [
        "# Main body",
        "# Spaced annex",
        "# Unicode annex",
    ]
    assert listed[2].markdown_file == markdown_member_name(
        unicode_annex.document_id, "annex/Ünïcode > x.docx"
    )
    assert _repo().get_document(WID, URL, "annex/Ünïcode > x.docx") == listed[2]
    assert _repo().get_document(WID, URL, "annex/missing.docx") is None


def test_empty_markdown_roundtrips(sqlite_env) -> None:
    create_schema("main")
    _seed_tdoc()
    empty_main = _document(MAIN, "")
    empty_annex = _document("annex/blank.docx", "")

    _repo().replace_family_bundle(LS, _meta(MAIN, 2), (empty_main, empty_annex))

    listed = _repo().list_documents(LS, URL)
    assert [document.markdown_text for document in listed] == ["", ""]
    assert [document.content_hash for document in listed] == [
        hashlib.sha256(b"").hexdigest()
    ] * 2


def test_replace_cr_bundle_writes_annexes_only(sqlite_env) -> None:
    create_schema("main")
    _seed_tdoc()
    ttcn = TDocCRTTCNDetails(
        tdoc_id=TDOC_ID,
        ftp_url=URL,
        testcase="testcase one",
        required_changes=[{"id": "change-1"}],
    )
    changes = TDocCRChangeDetails(
        ftp_url=URL,
        tdoc_id=TDOC_ID,
        clauses=("5.1",),
        changes=({"clauses": ["5.1"], "text": "body change"},),
    )

    _repo().replace_cr_bundle(
        _cover(),
        ttcn,
        changes,
        _meta(MAIN, 1),
        (_document("a.docx", "# Annex"),),
    )

    listed = _repo().list_documents(CR, URL)
    assert [document.source_file for document in listed] == ["a.docx"]
    assert listed[0].markdown_text == "# Annex"
    assert listed[0].file_order == 0
    assert _count(TDocCrAnnexOrm) == 1
    assert SQLAlchemyTDocCrRepository().get_by_url(URL).title == "Initial cover"
    assert SQLAlchemyTDocCrTtcnRepository().get_by_url(URL).testcase == "testcase one"
    assert (
        SQLAlchemyTDocCrChangeDetailsRepository().get_by_url(URL).changes
        == changes.changes
    )
    meta = _repo().get_extract_meta(URL)
    assert meta is not None
    assert meta.doc_filename == MAIN
    assert meta.extraction_version == 2
    assert meta.document_count == 1


def test_cr_zero_annexes_bundle(sqlite_env) -> None:
    create_schema("main")
    _seed_tdoc()

    _repo().replace_cr_bundle(_cover(), None, None, _meta(MAIN, 0), ())

    assert _repo().list_documents(CR, URL) == ()
    meta = _repo().get_extract_meta(URL)
    assert meta is not None
    assert meta.document_count == 0
    assert is_complete_cr_extraction(meta, _repo().list_documents(CR, URL), has_cover=True)


def test_cr_annexes_list_in_path_order_without_main(sqlite_env) -> None:
    create_schema("main")
    _seed_tdoc()
    _repo().replace_cr_bundle(
        _cover(),
        None,
        None,
        _meta(MAIN, 2),
        (_document("b.docx", "# B"), _document("A/first.docx", "# A")),
    )

    listed = _repo().list_documents(CR, URL)
    assert [document.source_file for document in listed] == ["A/first.docx", "b.docx"]
    assert [document.file_order for document in listed] == [0, 1]
    assert _repo().get_document(CR, URL, "b.docx") == listed[1]


def test_validation_failure_writes_nothing(sqlite_env) -> None:
    create_schema("main")
    _seed_tdoc()
    previous_main = _document(MAIN, "# Previous main")
    previous_annex = _document("a.docx", "# Previous annex")
    _repo().replace_family_bundle(WID, _meta(MAIN, 2), (previous_main, previous_annex))
    previous_documents = _repo().list_documents(WID, URL)
    previous_meta = _repo().get_extract_meta(URL)

    with pytest.raises(ValueError, match="duplicate source_file"):
        _repo().replace_family_bundle(
            WID,
            _meta(MAIN, 3),
            (previous_main, previous_annex, _document("a.docx", "# Duplicate")),
        )
    with pytest.raises(ValueError, match="first document must be the main"):
        _repo().replace_family_bundle(
            WID,
            _meta(MAIN, 2),
            (previous_annex, _document(MAIN, "# Main after annex")),
        )
    with pytest.raises(ValueError, match="must not include the main document"):
        _repo().replace_cr_bundle(
            _cover(),
            None,
            None,
            _meta(MAIN, 2),
            (_document(MAIN, "# Main as annex"), _document("a.docx", "# Annex")),
        )
    with pytest.raises(ValueError, match="document_count must equal"):
        _repo().replace_family_bundle(WID, _meta(MAIN, 5), (previous_main,))
    with pytest.raises(ValueError, match="must contain a main document"):
        _repo().replace_family_bundle(WID, _meta(MAIN, 0), ())

    assert _repo().list_documents(WID, URL) == previous_documents
    assert _repo().get_extract_meta(URL) == previous_meta
    assert _count(TDocWidContentOrm) == 2


def test_replace_removes_members_no_longer_present(sqlite_env) -> None:
    create_schema("main")
    _seed_tdoc()
    _repo().replace_family_bundle(
        DISCUSSION,
        _meta(MAIN, 3),
        (
            _document(MAIN, "# Main"),
            _document("a.docx", "# A"),
            _document("b.docx", "# B"),
        ),
    )

    _repo().replace_family_bundle(DISCUSSION, _meta(MAIN, 1), (_document(MAIN, "# Main"),))

    assert [document.source_file for document in _repo().list_documents(DISCUSSION, URL)] == [
        MAIN
    ]
    assert _count(TDocDiscussionContentOrm) == 1
    meta = _repo().get_extract_meta(URL)
    assert meta is not None
    assert meta.document_count == 1


def test_one_parent_per_url_across_replacements(sqlite_env) -> None:
    create_schema("main")
    _seed_tdoc()
    _repo().replace_family_bundle(
        LS, _meta(MAIN, 2), (_document(MAIN, "# Main"), _document("a.docx", "# A"))
    )
    _repo().replace_family_bundle(LS, _meta(MAIN, 1), (_document(MAIN, "# Main"),))

    with get_session_factory()() as session:
        parent_count = session.scalar(
            select(func.count())
            .select_from(TDocExtractOrm)
            .where(TDocExtractOrm.ftp_url == URL)
        )
    assert parent_count == 1


def test_type_change_clears_previous_store(sqlite_env) -> None:
    create_schema("main")
    _seed_tdoc()
    _repo().replace_family_bundle(
        WID, _meta(MAIN, 2), (_document(MAIN, "# WID main"), _document("a.docx", "# A"))
    )
    _repo().replace_family_bundle(
        LS, _meta(MAIN, 1), (_document(MAIN, "# LS main"),)
    )

    assert _count(TDocWidContentOrm) == 0
    assert _repo().list_documents(WID, URL) == ()
    assert _count(TDocLsContentOrm) == 1
    assert [document.markdown_text for document in _repo().list_documents(LS, URL)] == [
        "# LS main"
    ]


def test_document_id_must_match_identity(sqlite_env) -> None:
    create_schema("main")
    _seed_tdoc()
    wrong_identity = replace(_document(MAIN, "# Main"), document_id="0" * 64)

    with pytest.raises(ValueError, match="document_id does not match"):
        _repo().replace_family_bundle(WID, _meta(MAIN, 1), (wrong_identity,))

    assert _repo().get_extract_meta(URL) is None
    assert _repo().list_documents(WID, URL) == ()


def test_content_hash_is_sha256_of_markdown(sqlite_env) -> None:
    create_schema("main")
    _seed_tdoc()
    markdown = "# Body ✓"

    _repo().replace_family_bundle(WID, _meta(MAIN, 1), (_document(MAIN, markdown),))

    with get_engine().connect() as connection:
        stored = connection.execute(
            text(
                "SELECT content_hash, content FROM tdoc_wid_contents "
                "WHERE ftp_url = :url"
            ),
            {"url": URL},
        ).one()
    assert stored.content_hash == hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    assert decompress_json(stored.content) == markdown
    assert decompress_json(compress_json(markdown)) == markdown


def test_skipped_sources_are_written_to_parent(sqlite_env) -> None:
    create_schema("main")
    _seed_tdoc()

    _repo().replace_family_bundle(
        WID,
        _meta(MAIN, 1, skipped_sources=("annex/bad.doc",)),
        (_document(MAIN, "# Main"),),
    )

    meta = _repo().get_extract_meta(URL)
    assert meta is not None
    assert meta.skipped_sources == ("annex/bad.doc",)
    assert not is_complete_family_extraction(meta, _repo().list_documents(WID, URL))
