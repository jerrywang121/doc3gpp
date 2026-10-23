"""Filtered semantic KNN is exact before ``k``: metadata filters run inside vec0.

Uses the real sqlite-vec index (``sqlite_env``) with explicit vectors so the
pushed predicates, the meetingless guard, the missing-date guard, and the
vec0 metadata layout are exercised without a remote embeddings API.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import numpy as np
import pytest
from sqlalchemy import text

pytestmark = pytest.mark.semantic

DIM = 384


def _labelled(tdoc_id, vectors):
    from doc3gpp.models.semantic_search import EmbeddedChunk

    return [
        EmbeddedChunk(chunk_id=f"{tdoc_id}#{i}", chunk_index=i, embedding=vec)
        for i, vec in enumerate(vectors)
    ]


def _query():
    query = np.zeros(DIM, dtype=np.float32)
    query[0] = 1.0
    return query


def _near(i):
    vec = np.zeros(DIM, dtype=np.float32)
    vec[0], vec[1] = 1.0, 0.01 * (i + 1)
    return vec


def _far(index):
    vec = np.zeros(DIM, dtype=np.float32)
    vec[0], vec[2] = 1.0, 0.5 + 0.1 * index
    return vec


class _SpyVecRepo:
    """Delegates to the real index and records the ``limit`` of every knn call."""

    def __init__(self, inner):
        self._inner = inner
        self.knn_limits: list[int] = []

    def knn(self, query_vec, limit, filters=None):
        self.knn_limits.append(limit)
        return self._inner.knn(query_vec, limit, filters)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture()
def vec_repo(sqlite_env):
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.vector_sql import (
        SQLAlchemyVectorIndexRepository,
    )

    create_schema()
    return SQLAlchemyVectorIndexRepository()


def _seed_tsgs(*short_names):
    from doc3gpp.storage.db.session import get_engine

    with get_engine().begin() as conn:
        for name in short_names:
            conn.execute(
                text(
                    "INSERT INTO tsgs (tsg_name, short_name, description) "
                    "VALUES (:name, :name, :name)"
                ),
                {"name": name},
            )


def _seed_meeting(meeting_id, *, tsg=None):
    from doc3gpp.models.meeting import Meeting
    from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository

    SQLAlchemyMeetingRepository().upsert_many([
        Meeting(
            meeting_id=meeting_id, name=f"RAN#{meeting_id}", title=f"meeting {meeting_id}",
            location="Online", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2),
            tsg=tsg,
        ),
    ])


def _seed_tdocs(meeting_id, tdoc_ids, uploaded_date=None, release=None):
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    SQLAlchemyTDocRepository().upsert_many([
        TDoc(
            tdoc_id=tdoc_id, meeting_id=meeting_id,
            uploaded_date=uploaded_date, release=release,
        )
        for tdoc_id in tdoc_ids
    ])


def _service(vec, *, fanout=1):
    from doc3gpp.services.semantic_search_service import SemanticSearchService

    settings = MagicMock()
    settings.semantic_search.fanout_multiplier = fanout
    settings.semantic_search.rrf_k = 60
    embedder = MagicMock()
    embedder.encode.return_value = np.stack([_query()])
    return SemanticSearchService(MagicMock(), embedder, vec, settings)


def test_filtered_knn_is_exact_before_k_for_meeting(vec_repo):
    from doc3gpp.models.search import SearchFilters

    near = [f"R5-A{i:02d}" for i in range(10)]
    far = [f"R5-B{i}" for i in range(5)]
    _seed_meeting(100)
    _seed_meeting(200)
    _seed_tdocs(100, near, date(2026, 1, 5))
    _seed_tdocs(200, far, date(2026, 1, 5))
    for i, tdoc_id in enumerate(near):
        vec_repo.upsert_chunks(tdoc_id, _labelled(tdoc_id, [_near(i)]))
    for i, tdoc_id in enumerate(far):
        vec_repo.upsert_chunks(tdoc_id, _labelled(tdoc_id, [_far(i)]))

    out = vec_repo.knn(_query(), limit=3, filters=SearchFilters(meeting_id=200))

    assert [row[0] for row in out] == ["R5-B0", "R5-B1", "R5-B2"]


def test_tsg_and_since_until_pushed_before_k(vec_repo):
    from doc3gpp.models.search import SearchFilters

    near = [f"R5-A{i:02d}" for i in range(10)]
    far = [f"R5-B{i}" for i in range(5)]
    _seed_tsgs("RAN5", "RAN2")
    _seed_meeting(100, tsg="RAN5")
    _seed_meeting(200, tsg="RAN2")
    _seed_tdocs(100, near, date(2026, 1, 5))
    _seed_tdocs(200, far, date(2026, 6, 5))
    for i, tdoc_id in enumerate(near):
        vec_repo.upsert_chunks(tdoc_id, _labelled(tdoc_id, [_near(i)]))
    for i, tdoc_id in enumerate(far):
        vec_repo.upsert_chunks(tdoc_id, _labelled(tdoc_id, [_far(i)]))

    by_tsg = vec_repo.knn(_query(), limit=3, filters=SearchFilters(tsg="ran2"))
    by_dates = vec_repo.knn(
        _query(), limit=3,
        filters=SearchFilters(since="2026-03-01", until="2026-12-31"),
    )

    assert [row[0] for row in by_tsg] == ["R5-B0", "R5-B1", "R5-B2"]
    assert [row[0] for row in by_dates] == ["R5-B0", "R5-B1", "R5-B2"]


def test_meetingless_tdoc_excluded_when_filters_given(vec_repo):
    from doc3gpp.models.search import SearchFilters

    _seed_meeting(200)
    _seed_tdocs(None, ["R5-NOMTG"], date(2026, 6, 5))
    _seed_tdocs(200, ["R5-B0"], date(2026, 6, 5))
    vec_repo.upsert_chunks(
        "R5-NOMTG", _labelled("R5-NOMTG", [_near(0), _near(1), _near(2)]),
    )
    vec_repo.upsert_chunks(
        "R5-B0", _labelled("R5-B0", [_far(0), _far(1), _far(2)]),
    )

    out = vec_repo.knn(_query(), limit=3, filters=SearchFilters(since="2026-01-01"))

    assert [row[0] for row in out] == ["R5-B0", "R5-B0", "R5-B0"]


def test_old_vec_layout_is_recreated_empty(sqlite_env):
    from doc3gpp.storage.backends.sqlite import load_sqlite_vec
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine

    create_schema()
    engine = get_engine()
    with engine.begin() as conn:
        load_sqlite_vec(conn.connection.driver_connection)
        conn.execute(text("DROP TABLE vec_tdoc_embeddings"))
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE vec_tdoc_embeddings USING vec0(\n"
                "    chunk_id TEXT PRIMARY KEY,\n"
                "    tdoc_id TEXT,\n"
                "    chunk_index INTEGER,\n"
                "    embedding FLOAT[4] distance_metric=cosine\n"
                ")"
            )
        )
        conn.execute(
            text(
                "INSERT INTO vec_tdoc_embeddings (chunk_id, tdoc_id, chunk_index, embedding) "
                "VALUES ('R5-A#0', 'R5-A', 0, :emb)"
            ),
            {"emb": np.zeros(4, dtype=np.float32).tobytes()},
        )

    create_schema()

    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(vec_tdoc_embeddings)"))}
        ddl = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE name = 'vec_tdoc_embeddings'")
        ).scalar_one()
        count = conn.execute(text("SELECT COUNT(*) FROM vec_tdoc_embeddings")).scalar_one()

    assert {"meeting_id", "tsg", "uploaded_date"} <= columns
    assert "FLOAT[4]" in ddl
    assert count == 0


def test_upsert_chunks_stamps_metadata(vec_repo):
    from doc3gpp.storage.db.session import get_engine

    _seed_tsgs("RAN5")
    _seed_meeting(100, tsg="RAN5")
    _seed_meeting(300)
    _seed_tdocs(100, ["R5-A"], date(2026, 1, 5))
    _seed_tdocs(300, ["R5-C"], None)
    _seed_tdocs(None, ["R5-NOMTG"], date(2026, 1, 5))
    for tdoc_id in ("R5-A", "R5-C", "R5-NOMTG"):
        vec_repo.upsert_chunks(tdoc_id, _labelled(tdoc_id, [_near(0)]))

    with get_engine().begin() as conn:
        rows = conn.execute(
            text("SELECT tdoc_id, meeting_id, tsg, uploaded_date FROM vec_tdoc_embeddings")
        ).all()

    stamped = {row[0]: tuple(row[1:]) for row in rows}
    assert stamped == {
        "R5-A": (100, "RAN5", "2026-01-05"),
        "R5-C": (300, "", ""),
        "R5-NOMTG": (-1, "", "2026-01-05"),
    }


def test_metadata_drift_marks_tdoc_content_stale(vec_repo):
    from doc3gpp.models.search import SearchFilters
    from doc3gpp.storage.db.session import get_engine

    _seed_tsgs("RAN5")
    _seed_meeting(100, tsg="RAN5")
    _seed_meeting(200, tsg="RAN5")
    _seed_tdocs(100, ["R5-A"], date(2026, 1, 5))
    vec_repo.upsert_chunks("R5-A", _labelled("R5-A", [_near(0)]))
    assert vec_repo.count_tdocs_to_index(stale_only=True) == 0

    with get_engine().begin() as conn:
        conn.execute(text("UPDATE tdocs SET meeting_id = 200 WHERE tdoc_id = 'R5-A'"))

    assert vec_repo.count_tdocs_to_index(stale_only=True) == 1
    for batch in vec_repo.rebuild_batch(batch_size=10, after_id=None, stale_only=True):
        for tdoc_id in batch:
            vec_repo.upsert_chunks(tdoc_id, _labelled(tdoc_id, [_near(0)]))

    out = vec_repo.knn(_query(), limit=3, filters=SearchFilters(meeting_id=200))
    assert [row[0] for row in out] == ["R5-A"]


def test_until_only_excludes_meeting_tdoc_without_date(vec_repo):
    from doc3gpp.models.search import SearchFilters

    _seed_meeting(100)
    _seed_tdocs(100, ["R5-NODATE"], None)
    _seed_tdocs(100, ["R5-DATED"], date(2026, 1, 5))
    vec_repo.upsert_chunks(
        "R5-NODATE", _labelled("R5-NODATE", [_near(0), _near(1), _near(2)]),
    )
    vec_repo.upsert_chunks(
        "R5-DATED", _labelled("R5-DATED", [_far(0), _far(1), _far(2)]),
    )

    out = vec_repo.knn(_query(), limit=3, filters=SearchFilters(until="2026-12-31"))

    assert [row[0] for row in out] == ["R5-DATED", "R5-DATED", "R5-DATED"]


def test_meeting_filter_keeps_tdoc_without_date(vec_repo):
    from doc3gpp.models.search import SearchFilters

    _seed_meeting(100)
    _seed_tdocs(100, ["R5-NODATE"], None)
    vec_repo.upsert_chunks("R5-NODATE", _labelled("R5-NODATE", [_near(0)]))

    out = vec_repo.knn(_query(), limit=3, filters=SearchFilters(meeting_id=100))

    assert [row[0] for row in out] == ["R5-NODATE"]


def test_filtered_semantic_search_uses_one_exact_knn_round(vec_repo):
    from doc3gpp.models.search import SearchFilters

    near = [f"R5-A{i:02d}" for i in range(10)]
    far = [f"R5-B{i}" for i in range(5)]
    _seed_meeting(100)
    _seed_meeting(200)
    _seed_tdocs(100, near, date(2026, 1, 5))
    _seed_tdocs(200, far, date(2026, 1, 5))
    for i, tdoc_id in enumerate(near):
        vec_repo.upsert_chunks(tdoc_id, _labelled(tdoc_id, [_near(i)]))
    for i, tdoc_id in enumerate(far):
        vec_repo.upsert_chunks(tdoc_id, _labelled(tdoc_id, [_far(i)]))
    spy = _SpyVecRepo(vec_repo)

    out = _service(spy).search(
        "q", fts5_query=None, filters=SearchFilters(meeting_id=200), limit=3, fts5_weight=0.5,
    )

    assert [hit.tdoc_id for hit in out] == ["R5-B0", "R5-B1", "R5-B2"]
    assert spy.knn_limits == [3]


def test_like_filter_still_post_filters_after_pushed_window(vec_repo):
    from doc3gpp.models.search import SearchFilters

    near = [f"R5-A{i:02d}" for i in range(10)]
    far = [f"R5-B{i}" for i in range(5)]
    _seed_meeting(100)
    _seed_meeting(200)
    _seed_tdocs(100, near, date(2026, 1, 5), release="Rel-17")
    _seed_tdocs(200, far, date(2026, 1, 5), release="Rel-18")
    for i, tdoc_id in enumerate(near):
        vec_repo.upsert_chunks(tdoc_id, _labelled(tdoc_id, [_near(i)]))
    for i, tdoc_id in enumerate(far):
        vec_repo.upsert_chunks(tdoc_id, _labelled(tdoc_id, [_far(i)]))

    out = _service(vec_repo).search(
        "q", fts5_query=None, filters=SearchFilters(release="Rel-18"), limit=3, fts5_weight=0.5,
    )

    assert [hit.tdoc_id for hit in out] == ["R5-B0", "R5-B1", "R5-B2"]
