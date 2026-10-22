"""SQLite coverage for durable, URL-owned TDoc document bundles."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import PurePosixPath

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from doc3gpp.models.tdoc_cr import (
    TDocCRDetails,
    TDocCRTTCNDetails,
    TDocExtractMeta,
)
from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
from doc3gpp.models.tdoc_document import TDocDocument
from doc3gpp.parsers.tdoc_zip import document_id_for
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import TDocORM
from doc3gpp.storage.db.session import get_engine, get_session_factory
from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
    SQLAlchemyTDocCrChangeDetailsRepository,
)
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
    SQLAlchemyTDocCrTtcnRepository,
)


def _repository():
    try:
        from doc3gpp.storage.repositories.tdoc_extraction_sql import (
            SQLAlchemyTDocExtractionRepository,
        )
    except ImportError:
        pytest.fail("SQLAlchemyTDocExtractionRepository is not implemented")
    return SQLAlchemyTDocExtractionRepository()


def _document(ftp_url: str, source_file: str, order: int, markdown: str):
    document_id = document_id_for(ftp_url, source_file)
    markdown_file = (
        f"documents/{document_id}/{PurePosixPath(source_file).stem}.md"
    )
    content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    return TDocDocument(
        document_id=document_id,
        source_file=source_file,
        markdown_file=markdown_file,
        file_order=order,
        markdown_text=markdown,
        content_hash=content_hash,
    )


def _bundle(
    *,
    ftp_url: str,
    tdoc_id: str,
    title: str,
    sources: tuple[tuple[str, str], ...],
    ttcn: TDocCRTTCNDetails | None = None,
    changes: TDocCRChangeDetails | None = None,
):
    main_source = sources[0][0]
    details = TDocCRDetails(
        tdoc_id=tdoc_id,
        ftp_url=ftp_url,
        title=title,
    )
    meta = TDocExtractMeta(
        ftp_url=ftp_url,
        tdoc_id=tdoc_id,
        cache_file=f"{tdoc_id}.zip",
        doc_filename=main_source,
    )
    documents = tuple(
        _document(ftp_url, source_file, order, markdown)
        for order, (source_file, markdown) in enumerate(sources)
    )
    return details, ttcn, changes, meta, documents


def _seed_tdoc(tdoc_id: str) -> None:
    with get_session_factory()() as session:
        session.add(
            TDocORM(
                tdoc_id=tdoc_id,
                ftp_url=f"source/{tdoc_id}.zip",
                meeting_id=None,
            )
        )
        session.commit()


def test_main_schema_has_completeness_and_document_columns(sqlite_env) -> None:
    create_schema("main")
    inspector = inspect(get_engine())

    parent_columns = {
        column["name"] for column in inspector.get_columns("tdoc_extracts")
    }
    assert {"extraction_version", "document_count"} <= parent_columns

    document_columns = {
        column["name"] for column in inspector.get_columns("tdoc_documents")
    }
    assert document_columns == {
        "document_id",
        "ftp_url",
        "source_file",
        "markdown_file",
        "file_order",
        "markdown_text",
        "content_hash",
    }
    unique_constraints = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("tdoc_documents")
    }
    assert ("ftp_url", "source_file") in unique_constraints
    assert ("ftp_url", "file_order") in unique_constraints
    assert any(
        foreign_key["constrained_columns"] == ["ftp_url"]
        and foreign_key["referred_table"] == "tdoc_extracts"
        and foreign_key["referred_columns"] == ["ftp_url"]
        and foreign_key.get("options", {}).get("ondelete") == "CASCADE"
        for foreign_key in inspector.get_foreign_keys("tdoc_documents")
    )


def test_documents_round_trip_by_url_order_and_tdoc_with_cascade(sqlite_env) -> None:
    create_schema("main")
    tdoc_id = "R5-DOCUMENT-1"
    _seed_tdoc(tdoc_id)
    repo = _repository()
    first_url = "archive/a/R5-DOCUMENT-1.zip"
    second_url = "archive/z/R5-DOCUMENT-1.zip"
    first = _bundle(
        ftp_url=first_url,
        tdoc_id=tdoc_id,
        title="First URL",
        sources=(
            ("z/main.docx", "main body"),
            ("a/shared.docx", "first support"),
            ("b/shared.docx", "second support"),
        ),
    )
    second = _bundle(
        ftp_url=second_url,
        tdoc_id=tdoc_id,
        title="Second URL",
        sources=(
            ("z/main.docx", "other main body"),
            ("annex/shared.docx", "other support"),
        ),
    )

    repo.replace_bundle(*first[:4], first[4])
    repo.replace_bundle(*second[:4], second[4])

    first_documents = repo.list_documents(first_url)
    assert first_documents == first[4]
    assert [document.source_file for document in first_documents] == [
        "z/main.docx",
        "a/shared.docx",
        "b/shared.docx",
    ]
    assert first_documents[1].document_id != first_documents[2].document_id
    assert first_documents[1].content_hash == hashlib.sha256(
        b"first support"
    ).hexdigest()
    assert repo.get_document(first_url, "b/shared.docx") == first_documents[2]
    assert repo.get_document(first_url, "missing.docx") is None
    assert repo.get_document(second_url, "z/main.docx") == second[4][0]
    assert repo.get_document(first_url, "z/main.docx") != second[4][0]
    assert repo.get_documents_for_tdoc(tdoc_id) == (*first[4], *second[4])

    first_meta = SQLAlchemyTDocCrRepository().get_extract_meta_by_url(first_url)
    assert first_meta is not None
    assert first_meta.extraction_version == 2
    assert first_meta.document_count == 3
    assert first_meta.complete is True

    with get_session_factory()() as session:
        parent = session.get(TDocORM, tdoc_id)
        assert parent is not None
        session.delete(parent)
        session.commit()

    assert repo.list_documents(first_url) == ()
    assert repo.list_documents(second_url) == ()
    assert repo.get_documents_for_tdoc(tdoc_id) == ()


def test_replace_bundle_is_atomic_and_removes_stale_content(sqlite_env) -> None:
    create_schema("main")
    tdoc_id = "R5-DOCUMENT-2"
    url = "archive/R5-DOCUMENT-2.zip"
    _seed_tdoc(tdoc_id)
    repo = _repository()
    cover_repo = SQLAlchemyTDocCrRepository()
    ttcn_repo = SQLAlchemyTDocCrTtcnRepository()
    changes_repo = SQLAlchemyTDocCrChangeDetailsRepository()
    initial_ttcn = TDocCRTTCNDetails(
        tdoc_id=tdoc_id,
        ftp_url=url,
        testcase="initial testcase",
        required_changes=[{"id": "old"}],
    )
    initial_changes = TDocCRChangeDetails(
        ftp_url=url,
        tdoc_id=tdoc_id,
        clauses=("5.1",),
        changes=({"clauses": ["5.1"], "text": "old body change"},),
    )
    initial = _bundle(
        ftp_url=url,
        tdoc_id=tdoc_id,
        title="Initial cover",
        sources=(("z/main.docx", "initial main"), ("support.docx", "initial support")),
        ttcn=initial_ttcn,
        changes=initial_changes,
    )
    repo.replace_bundle(*initial[:4], initial[4])

    attempted_ttcn = replace(initial_ttcn, testcase="uncommitted testcase")
    attempted_changes = replace(
        initial_changes,
        changes=({"clauses": ["5.2"], "text": "uncommitted body change"},),
    )
    attempted = _bundle(
        ftp_url=url,
        tdoc_id=tdoc_id,
        title="Uncommitted cover",
        sources=(
            ("z/main.docx", "uncommitted main"),
            ("new/support.docx", "new support"),
            ("new/support.docx", "duplicate support"),
        ),
        ttcn=attempted_ttcn,
        changes=attempted_changes,
    )

    with pytest.raises(IntegrityError):
        repo.replace_bundle(*attempted[:4], attempted[4])

    assert cover_repo.get_by_url(url).title == "Initial cover"
    assert repo.list_documents(url) == initial[4]
    assert ttcn_repo.get_by_url(url).testcase == "initial testcase"
    assert changes_repo.get_by_url(url).changes == initial_changes.changes
    old_meta = cover_repo.get_extract_meta_by_url(url)
    assert old_meta is not None
    assert old_meta.extraction_version == 2
    assert old_meta.document_count == 2

    replacement = _bundle(
        ftp_url=url,
        tdoc_id=tdoc_id,
        title="Replacement cover",
        sources=(("z/main.docx", "replacement main"),),
    )
    repo.replace_bundle(*replacement[:4], replacement[4])

    assert repo.list_documents(url) == replacement[4]
    assert cover_repo.get_by_url(url).title == "Replacement cover"
    assert ttcn_repo.get_by_url(url) is None
    assert changes_repo.get_by_url(url) is None
    new_meta = cover_repo.get_extract_meta_by_url(url)
    assert new_meta is not None
    assert new_meta.extraction_version == 2
    assert new_meta.document_count == 1


@pytest.mark.parametrize(
    ("invalid_case", "message"),
    [
        ("document_id", "document_id"),
        ("main_order", "file_order"),
        ("gap", "file_order"),
    ],
)
def test_replace_bundle_rejects_invalid_document_identity_and_order(
    sqlite_env,
    invalid_case: str,
    message: str,
) -> None:
    create_schema("main")
    tdoc_id = "R5-INVALID-BUNDLE"
    url = "archive/r5-invalid-bundle.zip"
    _seed_tdoc(tdoc_id)
    details, ttcn, changes, meta, documents = _bundle(
        ftp_url=url,
        tdoc_id=tdoc_id,
        title="Invalid bundle",
        sources=(("main.docx", "main"), ("support.docx", "support")),
    )
    if invalid_case == "document_id":
        documents = (replace(documents[0], document_id="0" * 64), *documents[1:])
    elif invalid_case == "main_order":
        documents = (
            replace(documents[0], file_order=1),
            replace(documents[1], file_order=2),
        )
    else:
        documents = (documents[0], replace(documents[1], file_order=2))

    with pytest.raises(ValueError, match=message):
        _repository().replace_bundle(details, ttcn, changes, meta, documents)

    assert _repository().list_documents(url) == ()
    assert SQLAlchemyTDocCrRepository().get_extract_meta_by_url(url) is None


def test_create_schema_migrates_legacy_extract_metadata_idempotently(
    sqlite_env,
) -> None:
    engine = get_engine()
    url = "legacy/r5-legacy.zip"
    tdoc_id = "R5-LEGACY"
    with engine.begin() as connection:
        from doc3gpp.storage.db.models import MeetingORM, TsgORM

        TsgORM.__table__.create(bind=connection)
        MeetingORM.__table__.create(bind=connection)
        TDocORM.__table__.create(bind=connection)
        connection.execute(
            text(
                """
                CREATE TABLE tdoc_extracts (
                    ftp_url VARCHAR(1024) PRIMARY KEY,
                    tdoc_id VARCHAR(64) NOT NULL
                        REFERENCES tdocs(tdoc_id) ON DELETE CASCADE,
                    cache_file VARCHAR(255) NOT NULL,
                    doc_filename VARCHAR(256) NOT NULL,
                    extracted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    parser_version VARCHAR(32) NOT NULL DEFAULT '1.0.0'
                )
                """
            )
        )
        connection.execute(
            text(
                "INSERT INTO tdocs (tdoc_id, ftp_url, meeting_id) "
                "VALUES (:tdoc_id, :ftp_url, NULL)"
            ),
            {"tdoc_id": tdoc_id, "ftp_url": url},
        )
        connection.execute(
            text(
                "INSERT INTO tdoc_extracts "
                "(ftp_url, tdoc_id, cache_file, doc_filename) "
                "VALUES (:url, :tdoc_id, :cache_file, :doc_filename)"
            ),
            {
                "url": url,
                "tdoc_id": tdoc_id,
                "cache_file": "R5-LEGACY.zip",
                "doc_filename": "main.docx",
            },
        )

    create_schema("main")
    create_schema("main")

    with engine.begin() as connection:
        column_names = [
            row[1]
            for row in connection.execute(text("PRAGMA table_info(tdoc_extracts)"))
        ]
        assert column_names.count("extraction_version") == 1
        assert column_names.count("document_count") == 1
        values = connection.execute(
            text(
                "SELECT extraction_version, document_count "
                "FROM tdoc_extracts WHERE ftp_url = :url"
            ),
            {"url": url},
        ).one()
    assert column_names.count("extraction_version") == 1
    assert column_names.count("document_count") == 1
    assert tuple(values) == (1, 0)

    migrated = SQLAlchemyTDocCrRepository().get_extract_meta_by_url(url)
    assert migrated is not None
    assert migrated.extraction_version == 1
    assert migrated.document_count == 0
    assert migrated.complete is False



@pytest.mark.parametrize(
    ("extraction_version", "document_count"),
    [(1, 0), (2, 3)],
)
def test_metadata_only_upsert_cannot_publish_complete_document_bundle(
    sqlite_env,
    extraction_version: int,
    document_count: int,
) -> None:
    create_schema("main")
    tdoc_id = "R5-METADATA-ONLY"
    url = "archive/r5-metadata-only.zip"
    _seed_tdoc(tdoc_id)
    meta = TDocExtractMeta(
        ftp_url=url,
        tdoc_id=tdoc_id,
        cache_file="R5-METADATA-ONLY.zip",
        doc_filename="main.docx",
        extraction_version=extraction_version,
        document_count=document_count,
    )
    repository = SQLAlchemyTDocCrRepository()

    repository.upsert_extract_meta(meta)

    stored = repository.get_extract_meta_by_url(url)
    assert stored is not None
    assert stored.extraction_version == 1
    assert stored.document_count == 0
    assert stored.complete is False
