"""SQLite schema coverage for the per-type TDoc content tables."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, func, inspect, select, text

from doc3gpp.models.tdoc_cr import TDocExtractMeta
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import (
    TDocExtractOrm,
    TDocORM,
    TDocWidContentOrm,
)
from doc3gpp.storage.db.session import get_engine, get_session_factory
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository

CONTENT_TABLES = (
    "tdoc_cr_annexes",
    "tdoc_wid_contents",
    "tdoc_ls_contents",
    "tdoc_discussion_contents",
)
CONTENT_COLUMNS = {
    "ftp_url",
    "doc_filename",
    "document_id",
    "tdoc_id",
    "content",
    "content_hash",
}
NEW_PARENT_COLUMNS = {"extraction_version", "document_count", "skipped_sources"}
URL = "r5/docs/r5s260009.zip"


def _meta(**overrides) -> TDocExtractMeta:
    values = {
        "ftp_url": URL,
        "tdoc_id": "R5s260009",
        "cache_file": "r5s260009.zip",
        "doc_filename": "R5s260009.docx",
    }
    values.update(overrides)
    return TDocExtractMeta(**values)


def _seed_tdoc(tdoc_id: str = "R5s260009") -> None:
    with get_session_factory()() as session:
        session.add(TDocORM(tdoc_id=tdoc_id))
        session.commit()


def test_create_schema_creates_four_content_tables(sqlite_env) -> None:
    create_schema()
    inspector = inspect(get_engine())

    for table in CONTENT_TABLES:
        assert inspector.has_table(table), table
        columns = {column["name"] for column in inspector.get_columns(table)}
        assert columns == CONTENT_COLUMNS, table
        primary_key = inspector.get_pk_constraint(table)["constrained_columns"]
        assert primary_key == ["ftp_url", "doc_filename"], table

        foreign_keys = inspector.get_foreign_keys(table)
        tdoc_fks = [fk for fk in foreign_keys if fk["referred_table"] == "tdocs"]
        assert len(tdoc_fks) == 1, table
        assert tdoc_fks[0]["constrained_columns"] == ["tdoc_id"], table
        assert tdoc_fks[0]["referred_columns"] == ["tdoc_id"], table
        assert tdoc_fks[0]["options"]["ondelete"] == "CASCADE", table

        extract_fks = [
            fk for fk in foreign_keys if fk["referred_table"] == "tdoc_extracts"
        ]
        assert len(extract_fks) == 1, table
        assert extract_fks[0]["constrained_columns"] == ["ftp_url"], table
        assert extract_fks[0]["referred_columns"] == ["ftp_url"], table
        assert extract_fks[0]["options"]["ondelete"] == "CASCADE", table

        indexed_columns = {
            tuple(index["column_names"]) for index in inspector.get_indexes(table)
        }
        assert ("tdoc_id",) in indexed_columns, table


def test_no_tdoc_documents_table(sqlite_env) -> None:
    create_schema()
    assert not inspect(get_engine()).has_table("tdoc_documents")


def test_tdoc_extracts_has_nullable_skipped_sources(sqlite_env) -> None:
    create_schema()
    columns = {
        column["name"]: column
        for column in inspect(get_engine()).get_columns("tdoc_extracts")
    }
    assert columns["skipped_sources"]["nullable"] is True
    assert columns["extraction_version"]["nullable"] is False
    assert columns["document_count"]["nullable"] is False


def test_create_schema_is_idempotent(sqlite_env) -> None:
    def snapshot() -> tuple[list[str], list[str]]:
        inspector = inspect(get_engine())
        return (
            sorted(inspector.get_table_names()),
            sorted(column["name"] for column in inspector.get_columns("tdoc_extracts")),
        )

    create_schema()
    first = snapshot()
    create_schema()
    assert snapshot() == first


def test_deleting_tdoc_cascades_to_content(sqlite_env) -> None:
    create_schema()
    _seed_tdoc()
    with get_session_factory()() as session:
        session.add(
            TDocExtractOrm(
                ftp_url=URL,
                tdoc_id="R5s260009",
                cache_file="r5s260009.zip",
                doc_filename="R5s260009.docx",
                parser_version="1.0.0",
            )
        )
        session.add(
            TDocWidContentOrm(
                ftp_url=URL,
                doc_filename="R5s260009.docx",
                document_id="doc-main",
                tdoc_id="R5s260009",
                content=b"compressed-markdown",
                content_hash="a" * 64,
            )
        )
        session.commit()

    with get_session_factory()() as session:
        before = session.scalar(select(func.count()).select_from(TDocWidContentOrm))
        session.execute(delete(TDocORM).where(TDocORM.tdoc_id == "R5s260009"))
        session.commit()
        after = session.scalar(select(func.count()).select_from(TDocWidContentOrm))

    assert before == 1
    assert after == 0


def test_pre_branch_upgrade_adds_new_columns(sqlite_env) -> None:
    create_schema()
    engine = get_engine()
    with engine.begin() as conn:
        for column in ("extraction_version", "document_count", "skipped_sources"):
            conn.execute(text(f"ALTER TABLE tdoc_extracts DROP COLUMN {column}"))
        for table in CONTENT_TABLES:
            conn.execute(text(f"DROP TABLE {table}"))
        conn.execute(text("INSERT INTO tdocs (tdoc_id) VALUES ('R5s260009')"))
        conn.execute(
            text(
                "INSERT INTO tdoc_extracts "
                "(ftp_url, tdoc_id, cache_file, doc_filename, parser_version) "
                "VALUES (:url, 'R5s260009', 'r5s260009.zip', "
                "'R5s260009.docx', '1.0.0')"
            ),
            {"url": URL},
        )

    create_schema()
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("tdoc_extracts")}
    assert NEW_PARENT_COLUMNS <= columns
    for table in CONTENT_TABLES:
        assert inspector.has_table(table), table

    legacy_row = text(
        "SELECT extraction_version, document_count, skipped_sources "
        "FROM tdoc_extracts WHERE ftp_url = :url"
    )
    with engine.connect() as conn:
        assert tuple(conn.execute(legacy_row, {"url": URL}).one()) == (1, 0, None)

    meta = SQLAlchemyTDocCrRepository().get_extract_meta_by_url(URL)
    assert meta is not None
    assert meta.extraction_version == 1
    assert meta.document_count == 0
    assert meta.skipped_sources == ()
    assert meta.complete is False

    create_schema()
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("tdoc_extracts")}
    assert NEW_PARENT_COLUMNS <= columns
    with engine.connect() as conn:
        assert tuple(conn.execute(legacy_row, {"url": URL}).one()) == (1, 0, None)


def test_skipped_sources_roundtrip(sqlite_env) -> None:
    create_schema()
    _seed_tdoc()
    repo = SQLAlchemyTDocCrRepository()

    repo.upsert_extract_meta(_meta(skipped_sources=("annex/bad.docx",)))
    stored = repo.get_extract_meta_by_url(URL)
    assert stored is not None
    assert stored.skipped_sources == ("annex/bad.docx",)

    repo.upsert_extract_meta(_meta())
    stored = repo.get_extract_meta_by_url(URL)
    assert stored is not None
    assert stored.skipped_sources == ()

    with get_engine().connect() as conn:
        raw = conn.execute(
            text("SELECT skipped_sources FROM tdoc_extracts WHERE ftp_url = :url"),
            {"url": URL},
        ).scalar_one()
    assert raw is None

    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE tdoc_extracts SET skipped_sources = '[]' WHERE ftp_url = :url"),
            {"url": URL},
        )
    with get_engine().connect() as conn:
        raw = conn.execute(
            text("SELECT skipped_sources FROM tdoc_extracts WHERE ftp_url = :url"),
            {"url": URL},
        ).scalar_one()
    assert raw == "[]"
    stored = repo.get_extract_meta_by_url(URL)
    assert stored is not None
    assert stored.skipped_sources == ()


def test_malformed_skipped_sources_raise_on_read(sqlite_env) -> None:
    create_schema()
    _seed_tdoc()
    repo = SQLAlchemyTDocCrRepository()
    repo.upsert_extract_meta(_meta())

    for raw in ('{"annex": "bad.docx"}', "[1]", '[""]', '"annex/bad.docx"'):
        with get_engine().begin() as conn:
            conn.execute(
                text("UPDATE tdoc_extracts SET skipped_sources = :raw WHERE ftp_url = :url"),
                {"raw": raw, "url": URL},
            )
        with pytest.raises(ValueError, match="skipped_sources"):
            repo.get_extract_meta_by_url(URL)
