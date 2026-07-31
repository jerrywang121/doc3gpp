"""End-to-end lifecycle tests for ``SQLAlchemyVectorIndexRepository``.

Uses sqlite-vec on a real sqlite engine (``sqlite_env`` fixture) and
pre-computed embedding arrays so the vector index path is exercised
without loading a sentence-transformers model.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.semantic


@pytest.fixture()
def repo(sqlite_env):
    from doc3gpp.storage.db.migrate import create_schema

    create_schema()
    from doc3gpp.storage.repositories.vector_sql import (
        SQLAlchemyVectorIndexRepository,
    )

    return SQLAlchemyVectorIndexRepository()


def test_upsert_then_knn_returns_expected_order(repo):
    import numpy as np

    # 3 chunks for tdoc A, 1 chunk for tdoc B
    a = [np.zeros(384, dtype=np.float32) for _ in range(3)]
    a[0][0] = 1.0  # chunk 0 close to query
    b = [np.zeros(384, dtype=np.float32)]
    b[0][0] = 0.5
    repo.upsert_chunks("R5-1", a)
    repo.upsert_chunks("R5-2", b)
    query = np.zeros(384, dtype=np.float32)
    query[0] = 1.0
    hits = repo.knn(query, limit=10)
    assert len(hits) >= 2
    # Closest chunk first; R5-1 chunk 0 has distance 0
    assert hits[0][0] == "R5-1"
    assert hits[0][1] == "R5-1#0"
    assert hits[0][3] == 0.0


def test_remove_for_tdoc(repo):
    import numpy as np

    repo.upsert_chunks("R5-1", [np.zeros(384, dtype=np.float32)])
    repo.remove_for_tdoc("R5-1")
    hits = repo.knn(np.zeros(384, dtype=np.float32), limit=10)
    assert not any(h[0] == "R5-1" for h in hits)


def test_upsert_replaces_existing_chunks(repo):
    import numpy as np

    repo.upsert_chunks("R5-1", [np.zeros(384, dtype=np.float32) for _ in range(8)])
    # re-parse with fewer chunks -> surplus deleted
    repo.upsert_chunks("R5-1", [np.zeros(384, dtype=np.float32) for _ in range(4)])
    # only 4 chunk rows for R5-1 now
    hits = repo.knn(np.zeros(384, dtype=np.float32), limit=100)
    r5_1_chunks = [h for h in hits if h[0] == "R5-1"]
    assert len(r5_1_chunks) == 4


def test_status_reports_row_count(repo):
    import numpy as np

    repo.upsert_chunks("R5-1", [np.zeros(384, dtype=np.float32) for _ in range(3)])
    status = repo.status()
    assert status.row_count >= 3


def test_dim_mismatch_raises(repo):
    import numpy as np

    bad = [np.zeros(128, dtype=np.float32)]  # 128 != 384
    from doc3gpp.models.semantic_search import VectorIndexUnavailableError

    with pytest.raises(VectorIndexUnavailableError):
        repo.upsert_chunks("R5-1", bad)


def test_resume_cursor_round_trip(repo):
    assert repo.get_resume_cursor() is None
    repo.set_resume_cursor("R5-123")
    assert repo.get_resume_cursor() == "R5-123"
