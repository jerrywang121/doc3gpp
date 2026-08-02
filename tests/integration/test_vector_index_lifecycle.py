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


def test_constructor_raises_vector_index_unavailable_when_meta_missing(sqlite_env) -> None:
    """Construction must degrade via VectorIndexUnavailableError when
    the schema is not yet created (vec_meta missing), matching the
    documented contract (vector_sql.py:5-9) so the factory can catch it.
    """
    from doc3gpp.models.semantic_search import VectorIndexUnavailableError
    from doc3gpp.storage.repositories.vector_sql import (
        SQLAlchemyVectorIndexRepository,
    )

    with pytest.raises(VectorIndexUnavailableError):
        SQLAlchemyVectorIndexRepository()


def test_resume_cursor_round_trip(repo):
    assert repo.get_resume_cursor() is None
    repo.set_resume_cursor("R5-123")
    assert repo.get_resume_cursor() == "R5-123"


def test_rebuild_batch_walks_all_tdocs(sqlite_env, monkeypatch):
    """rebuild_batch must yield ALL TDocs in pages, not just the first batch.

    The vector repo previously yielded a single batch and stopped,
    breaking `search index --rebuild-embeddings` for any corpus larger
    than one batch. Mirror the FTS5 sibling (`search_sql.py:283-310`)
    that loops until `ids` is empty.

    Seed 7 tdocs with ascending ids, ask for 3 at a time, and expect
    three batches of [3, 3, 1].
    """
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.vector_sql import (
        SQLAlchemyVectorIndexRepository,
    )
    from sqlalchemy import text

    create_schema()
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tsgs (tsg_name, short_name, description) "
                "VALUES ('RAN WG1', 'RAN1', '')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO meetings (meeting_id, name, title, location, tsg, "
                "start_date, end_date, ftp_url, tdoc_list_last_sync) "
                "VALUES (1, 'RAN1#120', 'RAN1 #120', 'Athens', 'RAN1', "
                "'2026-03-01', '2026-03-05', 'https://x/ran1-120', "
                "'2026-03-05T00:00:00')"
            )
        )
        for tid in [f"R5-{i:06d}" for i in range(1, 8)]:
            conn.execute(
                text(
                    "INSERT INTO tdocs (tdoc_id, meeting_id, title, ftp_url, "
                    "type, source, uploaded_date, release, spec) "
                    "VALUES (:tid, 1, :t, :ftp, 'CR', 'TSG', "
                    "'2026-03-02', 'Rel-17', '38.300')"
                ),
                {"tid": tid, "t": f"title-{tid}", "ftp": f"https://x/{tid}.zip"},
            )

    repo = SQLAlchemyVectorIndexRepository()
    batches = list(
        repo.rebuild_batch(batch_size=3, after_id=None, stale_only=False),
    )
    assert [len(b) for b in batches] == [3, 3, 1], (
        f"expected 3+3+1 batches for 7 tdocs at batch_size=3, got {[len(b) for b in batches]}"
    )
    assert sum(len(b) for b in batches) == 7
    assert sorted(sum(batches, [])) == [f"R5-{i:06d}" for i in range(1, 8)]


def test_rebuild_batch_resumes_from_after_id(sqlite_env):
    """after_id cursor must skip already-seen tdocs in the next call."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.vector_sql import (
        SQLAlchemyVectorIndexRepository,
    )
    from sqlalchemy import text

    create_schema()
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tsgs (tsg_name, short_name, description) "
                "VALUES ('RAN WG1', 'RAN1', '')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO meetings (meeting_id, name, title, location, tsg, "
                "start_date, end_date, ftp_url, tdoc_list_last_sync) "
                "VALUES (1, 'RAN1#120', 'RAN1 #120', 'Athens', 'RAN1', "
                "'2026-03-01', '2026-03-05', 'https://x/ran1-120', "
                "'2026-03-05T00:00:00')"
            )
        )
        for tid in [f"R5-{i:06d}" for i in range(1, 6)]:
            conn.execute(
                text(
                    "INSERT INTO tdocs (tdoc_id, meeting_id, title, ftp_url, "
                    "type, source, uploaded_date, release, spec) "
                    "VALUES (:tid, 1, :t, :ftp, 'CR', 'TSG', "
                    "'2026-03-02', 'Rel-17', '38.300')"
                ),
                {"tid": tid, "t": f"title-{tid}", "ftp": f"https://x/{tid}.zip"},
            )

    repo = SQLAlchemyVectorIndexRepository()
    first = list(
        repo.rebuild_batch(batch_size=2, after_id=None, stale_only=False),
    )
    assert [len(b) for b in first] == [2, 2, 1]
    last_seen = sum(first, [])[-1]
    # 5 rows total; first call yielded all 5. A second call past the
    # last id must return an empty list (no more rows).
    second = list(
        repo.rebuild_batch(batch_size=2, after_id=last_seen, stale_only=False),
    )
    assert sum(second, []) == [], (
        f"after_id={last_seen} is past the last row; expected [], got {sum(second, [])}"
    )

    # Now check the real resume semantics: after the first batch of 2
    # rows, the next call with after_id=batch[-1] must yield only the
    # rows strictly past that cursor.
    mid_seen = first[0][-1]  # 'R5-000002'
    tail = list(
        repo.rebuild_batch(batch_size=2, after_id=mid_seen, stale_only=False),
    )
    assert sum(tail, []) == ["R5-000003", "R5-000004", "R5-000005"], (
        f"after_id={mid_seen} must skip R5-000001..R5-000002; got {sum(tail, [])}"
    )


def test_count_tdocs_to_index_respects_after_id(sqlite_env):
    """Regression: count_tdocs_to_index must honor the resume cursor.

    Without this, the tqdm bar reads total=13,693 but processed
    stops at 4,193 (the resume tail) — looks like 31% complete when
    the rebuild actually finished every TDoc past the cursor.
    """
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.vector_sql import (
        SQLAlchemyVectorIndexRepository,
    )
    from sqlalchemy import text

    create_schema()
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tsgs (tsg_name, short_name, description) "
                "VALUES ('RAN WG1', 'RAN1', '')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO meetings (meeting_id, name, title, location, tsg, "
                "start_date, end_date, ftp_url, tdoc_list_last_sync) "
                "VALUES (1, 'RAN1#120', 'RAN1 #120', 'Athens', 'RAN1', "
                "'2026-03-01', '2026-03-05', 'https://x/ran1-120', "
                "'2026-03-05T00:00:00')"
            )
        )
        for tid in [f"R5-{i:06d}" for i in range(1, 8)]:
            conn.execute(
                text(
                    "INSERT INTO tdocs (tdoc_id, meeting_id, title, ftp_url, "
                    "type, source, uploaded_date, release, spec) "
                    "VALUES (:tid, 1, :t, :ftp, 'CR', 'TSG', "
                    "'2026-03-02', 'Rel-17', '38.300')"
                ),
                {"tid": tid, "t": f"title-{tid}", "ftp": f"https://x/{tid}.zip"},
            )

    repo = SQLAlchemyVectorIndexRepository()
    # Without after_id: full count = 7
    assert repo.count_tdocs_to_index(stale_only=False, after_id=None) == 7
    # With after_id at the 3rd row: only rows strictly past it
    assert repo.count_tdocs_to_index(stale_only=False, after_id="R5-000003") == 4
    # With after_id at the last row: zero
    assert repo.count_tdocs_to_index(stale_only=False, after_id="R5-000007") == 0
