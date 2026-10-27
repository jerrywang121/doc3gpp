"""Filtered semantic-only search returns up to ``limit`` distinct matching TDocs.

Uses the real sqlite-vec index (``sqlite_env``) with explicit vectors so the
chunk window and the filter are exercised without a remote embeddings API.
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


def _seed_meeting(meeting_id, tdoc_ids, *, tsg=None):
    from doc3gpp.models.meeting import Meeting
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    SQLAlchemyMeetingRepository().upsert_many([
        Meeting(
            meeting_id=meeting_id, name=f"RAN#{meeting_id}", title=f"meeting {meeting_id}",
            location="Online", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2),
            tsg=tsg,
        ),
    ])
    SQLAlchemyTDocRepository().upsert_many(
        [TDoc(tdoc_id=tdoc_id, meeting_id=meeting_id) for tdoc_id in tdoc_ids],
    )


def _service(vec, *, fanout=1):
    from doc3gpp.services.semantic_search_service import SemanticSearchService

    settings = MagicMock()
    settings.semantic_search.fanout_multiplier = fanout
    settings.semantic_search.rrf_k = 60
    embedder = MagicMock()
    embedder.encode.return_value = np.stack([_query()])
    return SemanticSearchService(MagicMock(), embedder, vec, settings)


def test_filtered_semantic_only_returns_limit_matching_tdocs(vec_repo):
    from doc3gpp.models.search import SearchFilters

    near = [f"R5-A{i:02d}" for i in range(10)]
    far = [f"R5-B{i}" for i in range(5)]
    _seed_meeting(100, near)
    _seed_meeting(200, far)
    for i, tdoc_id in enumerate(near):
        vec_repo.upsert_chunks(tdoc_id, _labelled(tdoc_id, [_near(i)]))
    for i, tdoc_id in enumerate(far):
        vec_repo.upsert_chunks(tdoc_id, _labelled(tdoc_id, [_far(i)]))

    out = _service(vec_repo).search(
        "q", fts5_query=None, filters=SearchFilters(meeting_id=200), limit=3, fts5_weight=0.5,
    )

    assert [hit.tdoc_id for hit in out] == ["R5-B0", "R5-B1", "R5-B2"]


def test_filtered_pool_exhaustion_returns_available_matches_and_terminates(vec_repo):
    from doc3gpp.models.search import SearchFilters

    near = [f"R5-A{i:02d}" for i in range(10)]
    _seed_meeting(100, near)
    _seed_meeting(200, ["R5-B0"])
    for i, tdoc_id in enumerate(near):
        vec_repo.upsert_chunks(tdoc_id, _labelled(tdoc_id, [_near(i)]))
    vec_repo.upsert_chunks("R5-B0", _labelled("R5-B0", [_far(0)]))

    out = _service(vec_repo).search(
        "q", fts5_query=None, filters=SearchFilters(meeting_id=200), limit=3, fts5_weight=0.5,
    )

    assert [hit.tdoc_id for hit in out] == ["R5-B0"]


@pytest.mark.parametrize(("meeting_id", "expected"), [(999, []), (200, ["R5-B0"])])
def test_widening_stops_at_knn_cap(vec_repo, monkeypatch, meeting_id, expected):
    from doc3gpp.models.search import SearchFilters
    from doc3gpp.services import semantic_search_service as svc_mod

    monkeypatch.setattr(svc_mod, "_KNN_MAX_K", 8)
    near = [f"R5-A{i:02d}" for i in range(30)]
    _seed_meeting(100, near)
    _seed_meeting(200, ["R5-B0"])
    for i, tdoc_id in enumerate(near):
        vec_repo.upsert_chunks(tdoc_id, _labelled(tdoc_id, [_near(i)]))
    vec_repo.upsert_chunks("R5-B0", _labelled("R5-B0", [_query() for _ in range(30)]))
    spy = _SpyVecRepo(vec_repo)

    out = _service(spy).search(
        "q", fts5_query=None, filters=SearchFilters(meeting_id=meeting_id),
        limit=3, fts5_weight=0.5,
    )

    assert [hit.tdoc_id for hit in out] == expected
    assert spy.knn_limits
    assert max(spy.knn_limits) <= 8


def test_matching_tdoc_ids_applies_filter_grammar(vec_repo):
    from doc3gpp.models.search import SearchFilters
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    _seed_tsgs("RAN5", "RAN1")
    _seed_meeting(100, [], tsg="RAN5")
    _seed_meeting(200, [], tsg="RAN1")
    SQLAlchemyTDocRepository().upsert_many([
        TDoc(tdoc_id="R5-100", meeting_id=100, release="Rel-18"),
        TDoc(tdoc_id="R5-200", meeting_id=200),
        TDoc(tdoc_id="R5-NOMTG"),
    ])
    ids = ["R5-100", "R5-200", "R5-NOMTG", "R5-GHOST"]
    for i, tdoc_id in enumerate(["R5-100", "R5-200", "R5-NOMTG"]):
        vec_repo.upsert_chunks(tdoc_id, _labelled(tdoc_id, [_near(i)]))

    assert vec_repo.matching_tdoc_ids([], SearchFilters(tsg="RAN5")) == frozenset()
    assert vec_repo.matching_tdoc_ids(ids, SearchFilters(tsg="ran5")) == {"R5-100"}
    assert vec_repo.matching_tdoc_ids(ids, SearchFilters(meeting_id=200)) == {"R5-200"}
    assert vec_repo.matching_tdoc_ids(ids, SearchFilters(tdoc_id="R5-100")) == {"R5-100"}
    assert vec_repo.matching_tdoc_ids(ids, SearchFilters(release="Rel-18")) == {"R5-100"}
    assert vec_repo.matching_tdoc_ids(ids, SearchFilters()) == {"R5-100", "R5-200"}
    assert isinstance(vec_repo.matching_tdoc_ids(ids, SearchFilters()), frozenset)

    for filters in [
        SearchFilters(tsg="ran5"),
        SearchFilters(meeting_id=200),
        SearchFilters(tdoc_id="R5-100"),
        SearchFilters(tdoc_id="R5-100", meeting_id=200),
        SearchFilters(release="Rel-18"),
        SearchFilters(),
    ]:
        knn_ids = {row[0] for row in vec_repo.knn(_query(), limit=50, filters=filters)}
        assert vec_repo.matching_tdoc_ids(ids, filters) == knn_ids


def test_knn_filtered_results_unchanged(vec_repo):
    from doc3gpp.models.search import SearchFilters

    _seed_tsgs("RAN5")
    _seed_meeting(100, ["R5-100"], tsg="RAN5")
    _seed_meeting(200, ["R5-200"])
    vec_repo.upsert_chunks("R5-100", _labelled("R5-100", [_near(0)]))
    vec_repo.upsert_chunks("R5-200", _labelled("R5-200", [_near(1)]))

    def knn_ids(filters):
        return [row[0] for row in vec_repo.knn(_query(), limit=10, filters=filters)]

    assert knn_ids(None) == ["R5-100", "R5-200"]
    assert knn_ids(SearchFilters()) == ["R5-100", "R5-200"]
    assert knn_ids(SearchFilters(tsg="ran5")) == ["R5-100"]
    assert knn_ids(SearchFilters(meeting_id=200)) == ["R5-200"]
