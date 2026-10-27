"""``spec doc index --stale-only`` keeps fresh vectors; a model, dim, or table change rebuilds everything.

Uses the real sqlite-vec index (``sqlite_env``) and a deterministic embedder fake so
the stale-only, model/dim-change, and full-rebuild paths run without a remote API.
Mirrors ``test_stale_only_keeps_index_sqlite.py`` on the TDoc side.
"""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import text

from doc3gpp.models.spec_doc import ChunkDraft
from doc3gpp.services.spec_doc_search_service import SpecDocSearchService
from doc3gpp.services.spec_doc_semantic_service import SpecDocSemanticService
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.backends.sqlite import load_sqlite_vec
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_specdata_engine
from doc3gpp.storage.repositories.spec_doc_sql import SQLAlchemySpecDocRepository
from doc3gpp.storage.repositories.spec_doc_vector_sql import (
    SQLAlchemySpecDocVectorRepository,
)

pytest.importorskip("sqlite_vec")
pytestmark = pytest.mark.semantic

_SPEC = "38.331"
_PAIRS = ("18.0.0", "19.0.0")
_LATE = "20.0.0"


class _Embedder:
    def __init__(self, model: str = "fake-model", dim: int = 4) -> None:
        self.model_name = model
        self.dim = dim
        self.calls: list[list[str]] = []

    def encode(self, texts):
        self.calls.append(list(texts))
        vectors = np.zeros((len(texts), self.dim), dtype=np.float32)
        vectors[:, 0] = 1.0
        return vectors


def _seed_parsed(version: str) -> None:
    repo = SQLAlchemySpecDocRepository()
    repo.record_download(
        _SPEC, version, release="Rel-18", ftp_url="https://x", docx_count=1
    )
    repo.replace_chunks(
        _SPEC,
        version,
        release="Rel-18",
        drafts=[ChunkDraft(0, "a.docx", "5 Scope", None, f"handover {version}")],
    )
    repo.record_parsed(_SPEC, version, chunk_count=1)


def _text(version: str) -> str:
    return f"5 Scope\nhandover {version}"


def _rebuild(embedder: _Embedder, *, stale_only: bool) -> None:
    service = SpecDocSemanticService(
        fts5_service=SpecDocSearchService(),
        embedder=embedder,
        vector_repo=SQLAlchemySpecDocVectorRepository(),
        settings=get_settings(),
    )
    list(
        service.rebuild_embeddings(
            batch_size=10, stale_only=stale_only, quiet=True, resume=False,
        )
    )


def _embedded_texts(embedder: _Embedder) -> list[str]:
    return sorted(item for call in embedder.calls for item in call)


def _vec_rows(sql: str) -> list:
    with get_specdata_engine().begin() as connection:
        load_sqlite_vec(connection.connection.driver_connection)
        return connection.execute(text(sql)).all()


def _indexed_versions() -> set[str]:
    return {row[0] for row in _vec_rows("SELECT DISTINCT version FROM vec_spec_doc_embeddings")}


def _stored(key: str) -> str | None:
    with get_specdata_engine().begin() as connection:
        return connection.execute(
            text("SELECT value FROM vec_spec_doc_meta WHERE key = :key"), {"key": key},
        ).scalar()


def test_stale_only_keeps_fresh_pairs_and_skips_reembed(sqlite_env) -> None:
    create_schema("all")
    for version in _PAIRS:
        _seed_parsed(version)
    _rebuild(_Embedder(), stale_only=False)
    rows_before = _vec_rows(
        "SELECT version, COUNT(*) FROM vec_spec_doc_embeddings GROUP BY version"
    )

    embedder = _Embedder()
    _rebuild(embedder, stale_only=True)

    assert _embedded_texts(embedder) == []
    assert _indexed_versions() == set(_PAIRS)
    assert _vec_rows(
        "SELECT version, COUNT(*) FROM vec_spec_doc_embeddings GROUP BY version"
    ) == rows_before


def test_stale_only_embeds_only_pairs_parsed_after_cutoff(sqlite_env) -> None:
    create_schema("all")
    for version in _PAIRS:
        _seed_parsed(version)
    _rebuild(_Embedder(), stale_only=False)
    _seed_parsed(_LATE)

    embedder = _Embedder()
    _rebuild(embedder, stale_only=True)

    assert embedder.calls == [[_text(_LATE)]]
    assert _indexed_versions() == {*_PAIRS, _LATE}


def test_stale_only_after_model_change_resets_and_reembeds_all(sqlite_env) -> None:
    create_schema("all")
    for version in _PAIRS:
        _seed_parsed(version)
    _rebuild(_Embedder("old-model"), stale_only=False)

    embedder = _Embedder("new-model")
    _rebuild(embedder, stale_only=True)

    assert _embedded_texts(embedder) == sorted(_text(v) for v in _PAIRS)
    assert _indexed_versions() == set(_PAIRS)
    assert _stored("embedding_model") == "new-model"


def test_stale_only_with_missing_table_resets_and_reembeds_all(sqlite_env) -> None:
    create_schema("all")
    for version in _PAIRS:
        _seed_parsed(version)
    _rebuild(_Embedder(), stale_only=False)
    with get_specdata_engine().begin() as connection:
        load_sqlite_vec(connection.connection.driver_connection)
        connection.execute(text("DROP TABLE vec_spec_doc_embeddings"))

    embedder = _Embedder()
    _rebuild(embedder, stale_only=True)

    assert _embedded_texts(embedder) == sorted(_text(v) for v in _PAIRS)
    assert _indexed_versions() == set(_PAIRS)
    assert _stored("embedding_model") == "fake-model"


def test_full_rebuild_still_resets_and_reembeds_all(sqlite_env) -> None:
    create_schema("all")
    for version in _PAIRS:
        _seed_parsed(version)
    _rebuild(_Embedder(), stale_only=False)

    embedder = _Embedder()
    _rebuild(embedder, stale_only=False)

    assert _embedded_texts(embedder) == sorted(_text(v) for v in _PAIRS)
    assert _indexed_versions() == set(_PAIRS)


def test_keeps_index_false_for_model_none_and_mismatch(sqlite_env) -> None:
    create_schema("all")
    SQLAlchemySpecDocVectorRepository().reset_for_rebuild(4, "model-a")

    vec = SQLAlchemySpecDocVectorRepository()
    assert vec.keeps_index(4, "model-a") is True
    assert vec.keeps_index(4, None) is False
    assert vec.keeps_index(4, "model-b") is False
    assert vec.keeps_index(8, "model-a") is False

    with get_specdata_engine().begin() as connection:
        load_sqlite_vec(connection.connection.driver_connection)
        connection.execute(text("DROP TABLE vec_spec_doc_embeddings"))
    assert SQLAlchemySpecDocVectorRepository().keeps_index(4, "model-a") is False
