"""Tests for ``SQLAlchemyVectorIndexRepository.get_min_distance_for_tdocs``.

Mirrors the working ``test_vector_index_lifecycle`` fixture idiom: a real
sqlite engine (``sqlite_env`` fixture) wired with the sqlite-vec
``vec0`` extension via a tiny vector schema bootstrap. The vector
dimension is pinned to 4 here so the test vectors stay short and the
distance arithmetic is obvious; production code keeps the
``DEFAULT_DIM = 384`` schema in ``_create_vector_schema``.

The 3 cases pin the batched-KNN contract the
:class:`SemanticReranker` will rely on:

1. Empty ``tdoc_ids`` -> empty dict without touching the DB.
2. A tdoc_id with no vector rows maps to ``None`` (so callers can apply
   their missing-candidate policy, e.g. ``MISSING_FLOOR``).
3. A tdoc_id with N rows maps to ``(min_distance, best_chunk_id)`` of
   the cosine-closest chunk.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.semantic


@pytest.fixture()
def repo_and_seeded_db(sqlite_env):
    """Build a sqlite-vec ``vec0``-backed repo with dim=4.

    Replicates :func:`doc3gpp.storage.db.migrate._create_vector_schema`
    inline so the dimension can be overridden to 4 — production schema
    is hardcoded to ``FLOAT[384]``; this fixture mirrors the loading
    sequence without depending on the production DDL.
    """
    import sqlite_vec
    from sqlalchemy import text

    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.vector_sql import (
        SQLAlchemyVectorIndexRepository,
    )

    engine = get_engine()
    with engine.begin() as conn:
        # vec_meta must be seeded BEFORE constructing the repo so its
        # ``_read_or_init_dim`` probe resolves to the test dim (4)
        # rather than the DEFAULT_DIM (384) in vector_sql.py.
        conn.execute(text("CREATE TABLE vec_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"))
        conn.execute(
            text(
                "INSERT INTO vec_meta (key, value) "
                "VALUES ('embedding_dim', '4')"
            )
        )
        sqlite_vec.load(conn.connection.driver_connection)
        conn.execute(
            text(
                """
                CREATE VIRTUAL TABLE vec_tdoc_embeddings USING vec0(
                    chunk_id TEXT PRIMARY KEY,
                    tdoc_id TEXT,
                    chunk_index INTEGER,
                    embedding FLOAT[4] distance_metric=cosine
                )
                """
            )
        )

    return SQLAlchemyVectorIndexRepository()


def _insert(repo, tdoc_id, vec):
    import numpy as np

    repo.upsert_chunks(tdoc_id, [np.asarray(vec, dtype=np.float32)])


def _upsert(repo, tdoc_id, vecs):
    import numpy as np

    repo.upsert_chunks(
        tdoc_id, [np.asarray(v, dtype=np.float32) for v in vecs],
    )


def test_empty_input_returns_empty_dict(repo_and_seeded_db):
    import numpy as np

    assert repo_and_seeded_db.get_min_distance_for_tdocs([], np.zeros(4)) == {}


def test_missing_tdocs_map_to_none(repo_and_seeded_db):
    import numpy as np

    out = repo_and_seeded_db.get_min_distance_for_tdocs(["R5-1"], np.zeros(4))
    assert out == {"R5-1": None}


def test_returns_min_distance_and_chunk_id(repo_and_seeded_db):
    import numpy as np

    repo = repo_and_seeded_db
    # 4-D vectors; store 2 chunks for R5-1, 1 for R5-2.
    _upsert(
        repo, "R5-1", [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
        ],
    )
    _insert(repo, "R5-2", [0.0, 0.0, 1.0, 0.0])
    out = repo.get_min_distance_for_tdocs(
        ["R5-1", "R5-2", "R5-3"],
        np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
    )
    assert out["R5-1"][0] == pytest.approx(0.0, abs=1e-3)
    assert out["R5-1"][1] == "R5-1#0"
    assert out["R5-2"][0] > 0.0
    assert out["R5-2"][1] == "R5-2#0"
    assert out["R5-3"] is None
