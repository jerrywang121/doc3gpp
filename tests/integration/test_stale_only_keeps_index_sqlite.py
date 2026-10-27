"""``tdoc index --stale-only`` keeps fresh vectors; a model or dim change rebuilds everything.

Uses the real sqlite-vec index (``sqlite_env``) and a deterministic embedder fake so
the stale-only, model/dim-change, and full-rebuild paths run without a remote API.

``_A_ID`` carries a content document that changes between runs, so it is the stale
TDoc. ``_B_ID`` is metadata-only: it has no content document, so after a table reset
nothing marks it stale and only a full re-embed restores it.
"""

from __future__ import annotations

import hashlib
from datetime import date
from unittest.mock import MagicMock

import numpy as np
import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from doc3gpp.models.tdoc_content_families import TDOC_CONTENT_STORES
from doc3gpp.models.tdoc_cr import TDocExtractMeta
from doc3gpp.models.tdoc_document import TDocDocument, markdown_member_name
from doc3gpp.models.tdoc_identity import document_id_for
from doc3gpp.services.semantic_search_service import SemanticSearchService
from doc3gpp.services.tdoc_index_service import TDocIndexService
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import TDocORM
from doc3gpp.storage.db.session import get_engine, get_session_factory
from doc3gpp.storage.repositories.tdoc_content_sql import (
    SQLAlchemyTDocContentRepository,
)
from doc3gpp.storage.repositories.vector_sql import SQLAlchemyVectorIndexRepository

pytestmark = pytest.mark.semantic

WID = next(store for store in TDOC_CONTENT_STORES if store.key == "wid")
DIM = 384
MODEL = "nomic-embed-text"
_MEETING_ID = 100
_A_ID = "R5-2600201"
_A_URL = "r5/docs/r5-2600201.zip"
_B_ID = "R5-2600202"
_MAIN = "WID/main.docx"
_ANNEX = "WID/annex/support.docx"
_MAIN_TEXT = "# Main body\n\nplain main text"
_ANNEX_V1 = "# Annex\n\nversion one"
_ANNEX_V2 = "# Annex\n\nversion two"


class _Embedder:
    def __init__(self, model: str, dim: int = DIM) -> None:
        self.model_name = model
        self.dim = dim

    def encode(self, texts):
        vectors = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row in range(len(texts)):
            vectors[row, 0] = 1.0
        return vectors


def _service(model: str = MODEL, dim: int = DIM) -> SemanticSearchService:
    return SemanticSearchService(
        MagicMock(),
        _Embedder(model, dim),
        SQLAlchemyVectorIndexRepository(),
        get_settings(),
    )


def _rebuild(model: str = MODEL, *, stale_only: bool, dim: int = DIM) -> None:
    list(
        _service(model, dim).rebuild_embeddings(
            batch_size=10, stale_only=stale_only, quiet=True, resume=False,
        )
    )


def _seed() -> None:
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
    for tdoc_id in (_A_ID, _B_ID):
        with get_session_factory()() as session:
            session.add(
                TDocORM(
                    tdoc_id=tdoc_id,
                    title=f"Metadata title for {tdoc_id}",
                    meeting_id=_MEETING_ID,
                    type="WID new",
                    status="Agreed",
                    uploaded_date=date(2026, 1, 2),
                    release="Rel-18",
                    spec="38.300",
                )
            )
            session.commit()


def _sha(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def _write_family(annex_markdown: str) -> None:
    documents = ((_MAIN, _MAIN_TEXT), (_ANNEX, annex_markdown))
    bundle = tuple(
        TDocDocument(
            document_id=document_id_for(_A_URL, source),
            source_file=source,
            markdown_file=markdown_member_name(document_id_for(_A_URL, source), source),
            file_order=order,
            markdown_text=markdown,
            content_hash=_sha(markdown),
        )
        for order, (source, markdown) in enumerate(documents)
    )
    meta = TDocExtractMeta(
        ftp_url=_A_URL,
        tdoc_id=_A_ID,
        cache_file=f"{_A_ID.lower()}.zip",
        doc_filename=_MAIN,
        document_count=len(bundle),
    )
    SQLAlchemyTDocContentRepository().replace_family_bundle(WID, meta, bundle)


def _chunk_ids(tdoc_id: str) -> list[str]:
    with get_engine().begin() as connection:
        rows = connection.execute(
            text("SELECT chunk_id FROM vec_tdoc_embeddings WHERE tdoc_id = :tdoc_id"),
            {"tdoc_id": tdoc_id},
        ).all()
    return sorted(row[0] for row in rows)


def _indexed_tdoc_ids() -> set[str]:
    with get_engine().begin() as connection:
        rows = connection.execute(
            text("SELECT DISTINCT tdoc_id FROM vec_tdoc_embeddings")
        ).all()
    return {row[0] for row in rows}


def _stored(key: str) -> str | None:
    with get_engine().begin() as connection:
        return connection.execute(
            text("SELECT value FROM vec_meta WHERE key = :key"), {"key": key},
        ).scalar()


def _has_annex_hash(annex_markdown: str) -> bool:
    needle = f"#{document_id_for(_A_URL, _ANNEX)}#{_sha(annex_markdown)}#"
    return any(needle in chunk_id for chunk_id in _chunk_ids(_A_ID))


def test_stale_only_without_resume_keeps_fresh_embeddings(sqlite_env) -> None:
    _seed()
    _write_family(_ANNEX_V1)
    _rebuild(stale_only=False)
    b_before = _chunk_ids(_B_ID)
    assert b_before
    _write_family(_ANNEX_V2)

    _rebuild(stale_only=True)

    assert _chunk_ids(_B_ID) == b_before
    assert _has_annex_hash(_ANNEX_V2)
    assert not _has_annex_hash(_ANNEX_V1)


def test_stale_only_resets_when_model_differs(sqlite_env) -> None:
    _seed()
    _write_family(_ANNEX_V1)
    _rebuild("old-model", stale_only=False)
    _write_family(_ANNEX_V2)

    _rebuild("new-model", stale_only=True)

    assert _indexed_tdoc_ids() == {_A_ID, _B_ID}
    assert _stored("embedding_model") == "new-model"


def test_stale_only_resets_when_dim_differs(sqlite_env) -> None:
    _seed()
    _write_family(_ANNEX_V1)
    _rebuild(stale_only=False)
    _write_family(_ANNEX_V2)

    _rebuild(stale_only=True, dim=256)

    assert _indexed_tdoc_ids() == {_A_ID, _B_ID}
    assert _stored("embedding_dim") == "256"


def test_stale_only_resets_when_table_is_missing(sqlite_env) -> None:
    from doc3gpp.storage.backends.sqlite import load_sqlite_vec

    _seed()
    _write_family(_ANNEX_V1)
    _rebuild(stale_only=False)
    with get_engine().begin() as connection:
        load_sqlite_vec(connection.connection.driver_connection)
        connection.execute(text("DROP TABLE vec_tdoc_embeddings"))

    _rebuild(stale_only=True)

    assert _indexed_tdoc_ids() == {_A_ID, _B_ID}
    assert _stored("embedding_model") == MODEL


class _UntypedEmbedder:
    dim = DIM

    def encode(self, texts):
        return _Embedder(MODEL).encode(texts)


def test_stale_only_duck_typed_embedder_resets_and_reembeds_all(sqlite_env) -> None:
    _seed()
    _write_family(_ANNEX_V1)
    _rebuild(stale_only=False)
    service = SemanticSearchService(
        MagicMock(), _UntypedEmbedder(), SQLAlchemyVectorIndexRepository(), get_settings(),
    )

    list(service.rebuild_embeddings(batch_size=10, stale_only=True, quiet=True, resume=False))

    assert _indexed_tdoc_ids() == {_A_ID, _B_ID}


def test_full_rebuild_still_drops_and_reembeds_all(sqlite_env) -> None:
    _seed()
    _write_family(_ANNEX_V1)
    _rebuild(stale_only=False)
    assert _indexed_tdoc_ids() == {_A_ID, _B_ID}

    _rebuild(stale_only=False)

    assert _indexed_tdoc_ids() == {_A_ID, _B_ID}
    assert _has_annex_hash(_ANNEX_V1)


def test_cli_tdoc_index_stale_only_keeps_fresh_embeddings(sqlite_env, monkeypatch) -> None:
    from doc3gpp.cli import app

    _seed()
    _write_family(_ANNEX_V1)
    _rebuild(stale_only=False)
    b_before = _chunk_ids(_B_ID)
    _write_family(_ANNEX_V2)
    service = TDocIndexService(
        fts5_service=None, semantic_service=_service(), settings=get_settings(),
    )
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_tdoc_index_service", lambda: service,
    )

    result = CliRunner().invoke(
        app, ["tdoc", "index", "--rebuild-embeddings", "--stale-only", "--quiet"],
    )

    assert result.exit_code == 0, result.output
    assert _chunk_ids(_B_ID) == b_before
    assert _has_annex_hash(_ANNEX_V2)
