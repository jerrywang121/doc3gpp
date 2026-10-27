"""SQLite coverage for the ``token_count`` column on the TDoc content tables.

``token_count`` is the whitespace token count of the stored Markdown. Vector status
reads it instead of decompressing every content document, so a NULL value (not yet
computed, or undecodable) marks its TDoc stale. Legacy databases get the column and a
backfill from ``_migrate_content_token_count``.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from unittest.mock import MagicMock

import numpy as np
import pytest
from sqlalchemy import inspect, text

from doc3gpp.models.tdoc_content_families import TDOC_CONTENT_STORES
from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
from doc3gpp.models.tdoc_document import TDocDocument, markdown_member_name
from doc3gpp.models.tdoc_identity import document_id_for
from doc3gpp.services.semantic_search_service import SemanticSearchService
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.compression import compress_json
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import TDocExtractOrm, TDocORM
from doc3gpp.storage.db.session import get_engine, get_session_factory
from doc3gpp.storage.repositories.tdoc_content_sql import (
    SQLAlchemyTDocContentRepository,
)
from doc3gpp.storage.repositories.vector_sql import SQLAlchemyVectorIndexRepository

pytestmark = pytest.mark.semantic

STORES = {store.key: store for store in TDOC_CONTENT_STORES}
WID = STORES["wid"]
CR = STORES["cr"]
DIM = 384
_MEETING_ID = 100
_WID_ID = "R5-2600301"
_WID_URL = "r5/docs/r5-2600301.zip"
_WID_MAIN = "WID/main.docx"
_WID_ANNEX = "WID/annex/support.docx"
_CR_ID = "R5-2600302"
_CR_URL = "r5/docs/r5-2600302.zip"
_CR_MAIN = "CRs/main.docx"
_CR_ANNEX = "CRs/annex/support.docx"
_MAIN_TEXT = "# Main body\n\nplain main text"
_ANNEX_TEXT = "# Annex\n\none two three"
_LEGACY_TEXT = "# Legacy main body"


class _Embedder:
    dim = DIM

    def encode(self, texts):
        vectors = np.zeros((len(texts), DIM), dtype=np.float32)
        for row in range(len(texts)):
            vectors[row, 0] = 1.0
        return vectors


def _service() -> SemanticSearchService:
    return SemanticSearchService(
        MagicMock(), _Embedder(), SQLAlchemyVectorIndexRepository(), get_settings(),
    )


def _rebuild() -> None:
    list(
        _service().rebuild_embeddings(
            batch_size=10, stale_only=False, quiet=True, resume=False,
        )
    )


def _seed_corpus() -> None:
    create_schema()
    with get_engine().begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tsgs (tsg_name, short_name, description) "
                "VALUES ('RAN WG1', 'RAN1', 'TSG RAN WG1')"
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO meetings (
                    meeting_id, name, title, location, tsg, start_date,
                    end_date, ftp_url, tdoc_list_last_sync
                ) VALUES (
                    100, 'RAN1#100', 'RAN1#100', 'Online', 'RAN1',
                    '2026-01-01', '2026-01-05',
                    'https://www.3gpp.org/ftp/meetings/RAN1_100',
                    '2026-01-05T00:00:00'
                )
                """
            )
        )
    for tdoc_id, tdoc_type in ((_WID_ID, "WID new"), (_CR_ID, "CR")):
        with get_session_factory()() as session:
            session.add(
                TDocORM(
                    tdoc_id=tdoc_id,
                    title=f"Metadata title for {tdoc_id}",
                    meeting_id=_MEETING_ID,
                    type=tdoc_type,
                    status="Agreed",
                    uploaded_date=date(2026, 1, 2),
                    release="Rel-18",
                    spec="38.300",
                )
            )
            session.commit()


def _sha(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def _document(ftp_url: str, source_file: str, markdown: str, file_order: int) -> TDocDocument:
    document_id = document_id_for(ftp_url, source_file)
    return TDocDocument(
        document_id=document_id,
        source_file=source_file,
        markdown_file=markdown_member_name(document_id, source_file),
        file_order=file_order,
        markdown_text=markdown,
        content_hash=_sha(markdown),
    )


def _meta(tdoc_id: str, ftp_url: str, doc_filename: str, document_count: int) -> TDocExtractMeta:
    return TDocExtractMeta(
        ftp_url=ftp_url,
        tdoc_id=tdoc_id,
        cache_file=f"{tdoc_id.lower()}.zip",
        doc_filename=doc_filename,
        document_count=document_count,
    )


def _write_family(
    documents: tuple[tuple[str, str], ...] = ((_WID_MAIN, _MAIN_TEXT), (_WID_ANNEX, _ANNEX_TEXT)),
) -> None:
    bundle = tuple(
        _document(_WID_URL, source, markdown, order)
        for order, (source, markdown) in enumerate(documents)
    )
    SQLAlchemyTDocContentRepository().replace_family_bundle(
        WID, _meta(_WID_ID, _WID_URL, _WID_MAIN, len(bundle)), bundle,
    )


def _write_cr() -> None:
    bundle = (_document(_CR_URL, _CR_ANNEX, _ANNEX_TEXT, 0),)
    SQLAlchemyTDocContentRepository().replace_cr_bundle(
        TDocCRDetails(tdoc_id=_CR_ID, ftp_url=_CR_URL, title="CR cover"),
        None,
        None,
        _meta(_CR_ID, _CR_URL, _CR_MAIN, len(bundle)),
        bundle,
    )


def _mark_date_checkpoint() -> None:
    with get_engine().begin() as connection:
        latest = connection.execute(text("SELECT MAX(uploaded_date) FROM tdocs")).scalar()
        checkpoint = date.fromisoformat(str(latest)) + timedelta(days=1)
        connection.execute(
            text(
                "INSERT INTO vec_meta (key, value) "
                "VALUES ('last_indexed_uploaded_date', :value) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            ),
            {"value": checkpoint.isoformat()},
        )


def _token_counts(table: str) -> dict[str, int | None]:
    with get_engine().connect() as connection:
        rows = connection.execute(
            text(f"SELECT doc_filename, token_count FROM {table}")
        ).all()
    return {row[0]: row[1] for row in rows}


def _seed_legacy_wid_rows(*rows: tuple[str, bytes]) -> None:
    with get_session_factory()() as session:
        session.add(
            TDocExtractOrm(
                ftp_url=_WID_URL,
                tdoc_id=_WID_ID,
                cache_file="r5-2600301.zip",
                doc_filename=_WID_MAIN,
                parser_version="1.0.0",
            )
        )
        session.commit()
    with get_engine().begin() as connection:
        connection.execute(text("ALTER TABLE tdoc_wid_contents DROP COLUMN token_count"))
        for index, (doc_filename, content) in enumerate(rows):
            connection.execute(
                text(
                    "INSERT INTO tdoc_wid_contents ("
                    " ftp_url, doc_filename, document_id, tdoc_id, content, content_hash"
                    ") VALUES ("
                    " :ftp_url, :doc_filename, :document_id, :tdoc_id, :content, :content_hash"
                    ")"
                ),
                {
                    "ftp_url": _WID_URL,
                    "doc_filename": doc_filename,
                    "document_id": f"{index + 1}" * 64,
                    "tdoc_id": _WID_ID,
                    "content": content,
                    "content_hash": "a" * 64,
                },
            )


def test_fresh_schema_has_token_count_on_content_tables(sqlite_env) -> None:
    create_schema("main")
    inspector = inspect(get_engine())

    for table in (
        "tdoc_cr_annexes",
        "tdoc_wid_contents",
        "tdoc_ls_contents",
        "tdoc_discussion_contents",
    ):
        columns = {column["name"]: column for column in inspector.get_columns(table)}
        assert "token_count" in columns, table
        assert columns["token_count"]["nullable"] is True, table


def test_repository_write_sets_token_count(sqlite_env) -> None:
    _seed_corpus()
    _write_family(
        ((_WID_MAIN, _MAIN_TEXT), (_WID_ANNEX, _ANNEX_TEXT), ("WID/annex/blank.docx", ""))
    )
    _write_cr()

    assert _token_counts("tdoc_wid_contents") == {
        _WID_MAIN: len(_MAIN_TEXT.split()),
        _WID_ANNEX: len(_ANNEX_TEXT.split()),
        "WID/annex/blank.docx": 0,
    }
    assert _token_counts("tdoc_cr_annexes") == {_CR_ANNEX: len(_ANNEX_TEXT.split())}


def test_migration_adds_column_and_backfills_legacy_rows(sqlite_env) -> None:
    from doc3gpp.storage.db.migrate import _migrate_content_token_count

    _seed_corpus()
    _seed_legacy_wid_rows(
        (_WID_MAIN, compress_json(_LEGACY_TEXT)),
        ("WID/annex/undecodable.docx", b"not a json payload"),
    )

    _migrate_content_token_count()

    assert "token_count" in {
        column["name"] for column in inspect(get_engine()).get_columns("tdoc_wid_contents")
    }
    assert _token_counts("tdoc_wid_contents") == {
        _WID_MAIN: len(_LEGACY_TEXT.split()),
        "WID/annex/undecodable.docx": None,
    }


def test_create_schema_adds_and_backfills_token_count_on_upgrade(sqlite_env) -> None:
    _seed_corpus()
    _seed_legacy_wid_rows((_WID_MAIN, compress_json(_LEGACY_TEXT)))

    create_schema("main")

    assert _token_counts("tdoc_wid_contents") == {_WID_MAIN: len(_LEGACY_TEXT.split())}


def test_backfill_pages_past_undecodable_rows(sqlite_env, monkeypatch) -> None:
    from doc3gpp.storage.db.migrate import _migrate_content_token_count

    _seed_corpus()
    _seed_legacy_wid_rows(
        ("WID/annex/a.docx", compress_json("alpha one")),
        ("WID/annex/b.docx", b"not a json payload"),
        ("WID/annex/c.docx", compress_json("gamma one two")),
    )
    monkeypatch.setattr("doc3gpp.storage.db.migrate._CONTENT_TOKEN_COUNT_BATCH", 1)

    _migrate_content_token_count()
    _migrate_content_token_count()

    assert _token_counts("tdoc_wid_contents") == {
        "WID/annex/a.docx": 2,
        "WID/annex/b.docx": None,
        "WID/annex/c.docx": 3,
    }


def test_migration_is_idempotent_and_skips_missing_tables(sqlite_env) -> None:
    from doc3gpp.storage.db.migrate import _migrate_content_token_count

    _seed_corpus()
    _write_family()
    before = _token_counts("tdoc_wid_contents")

    _migrate_content_token_count()
    _migrate_content_token_count()

    assert _token_counts("tdoc_wid_contents") == before
    with get_engine().begin() as connection:
        connection.execute(text("DROP TABLE tdoc_ls_contents"))

    _migrate_content_token_count()

    assert not inspect(get_engine()).has_table("tdoc_ls_contents")
    assert _token_counts("tdoc_wid_contents") == before


def _forbidden_decompress(blob):
    raise AssertionError("vector status must not decompress stored content")


def test_status_does_not_decompress_content(sqlite_env, monkeypatch) -> None:
    _seed_corpus()
    _write_family()
    _write_cr()
    _rebuild()
    _mark_date_checkpoint()
    repo = SQLAlchemyVectorIndexRepository()
    monkeypatch.setattr(
        "doc3gpp.storage.repositories.vector_sql.decompress_json", _forbidden_decompress,
    )

    assert repo.status().is_stale is False
    assert repo.count_tdocs_to_index(stale_only=True) == 0


def test_null_token_count_marks_tdoc_stale(sqlite_env) -> None:
    _seed_corpus()
    _write_family()
    _write_cr()
    _rebuild()
    _mark_date_checkpoint()
    repo = SQLAlchemyVectorIndexRepository()
    assert repo.status().is_stale is False

    with get_engine().begin() as connection:
        connection.execute(
            text("UPDATE tdoc_wid_contents SET token_count = NULL WHERE tdoc_id = :tdoc_id"),
            {"tdoc_id": _WID_ID},
        )

    assert repo.status().is_stale is True
    assert repo.count_tdocs_to_index(stale_only=True) == 1
    stale = [
        tdoc_id
        for batch in repo.rebuild_batch(batch_size=10, after_id=None, stale_only=True)
        for tdoc_id in batch
    ]
    assert stale == [_WID_ID]
