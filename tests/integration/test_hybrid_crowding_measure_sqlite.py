"""Hybrid TDoc search keeps ``limit`` distinct matching TDocs when near chunks crowd the knn window.

Ten crowder TDocs own eight chunks each, all nearer the query than five match
TDocs. ``release`` is a LIKE filter, so it runs after the chunk window; ``meeting_id``
is pushed into vec0 before ``k``. Two FTS terms share one corpus: ``ALL_MATCH_TERM``
matches all five matches, ``ONE_MATCH_TERM`` matches only the first.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import numpy as np
import pytest

from tests.integration.test_semantic_filtered_knn_sqlite import (
    _far,
    _labelled,
    _near,
    _query,
)

pytestmark = pytest.mark.semantic

CROWDERS = tuple(f"R5-C{i:02d}" for i in range(10))
MATCHES = tuple(f"R5-M{i}" for i in range(5))
LIMIT = 3
ONE_MATCH_TERM = "zyxqone"
ALL_MATCH_TERM = "zyxqall"
FANOUTS = (1, 4, 8)
FILTER_NAMES = ("none", "meeting_id", "release")
EXPECTED: dict[tuple[int, str], tuple[int, int, int, int]] = {
    # (semantic count, hybrid one-hit count, rows returned by hybrid knn calls, hybrid all-hit count)
    (1, "none"): (3, 3, 45, 3),
    (1, "meeting_id"): (3, 3, 3, 3),
    (1, "release"): (3, 3, 178, 3),
    (4, "none"): (3, 3, 36, 3),
    (4, "meeting_id"): (3, 3, 5, 3),
    (4, "release"): (3, 3, 169, 3),
    (8, "none"): (3, 3, 24, 3),
    (8, "meeting_id"): (3, 3, 5, 3),
    (8, "release"): (3, 3, 157, 3),
}


@pytest.fixture()
def vec_repo(sqlite_env):
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.vector_sql import SQLAlchemyVectorIndexRepository

    create_schema()
    return SQLAlchemyVectorIndexRepository()


class _Spy:
    """Delegates to the real index and records ``(limit, rows returned)`` per knn call."""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[tuple[int, int]] = []

    def knn(self, query_vec, limit, filters=None):
        rows = self._inner.knn(query_vec, limit, filters)
        self.calls.append((limit, len(rows)))
        return rows

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _seed() -> None:
    from doc3gpp.models.meeting import Meeting
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
    from doc3gpp.storage.repositories.search_sql import SQLAlchemySearchIndexRepository
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    SQLAlchemyMeetingRepository().upsert_many([
        Meeting(
            meeting_id=meeting_id, name=f"RAN#{meeting_id}", title=f"meeting {meeting_id}",
            location="Online", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2),
            tsg=None,
        )
        for meeting_id in (100, 200)
    ])
    tdocs = [
        TDoc(tdoc_id=tdoc_id, title=f"crowder {tdoc_id}", meeting_id=100, release="Rel-17")
        for tdoc_id in CROWDERS
    ]
    tdocs += [
        TDoc(
            tdoc_id=tdoc_id,
            title=f"{ALL_MATCH_TERM} match {tdoc_id}"
            + (f" {ONE_MATCH_TERM}" if i == 0 else ""),
            meeting_id=200, release="Rel-18",
        )
        for i, tdoc_id in enumerate(MATCHES)
    ]
    SQLAlchemyTDocRepository().upsert_many(tdocs)
    search = SQLAlchemySearchIndexRepository()
    for tdoc in tdocs:
        search.upsert(tdoc.tdoc_id)


def _index_vectors(vec) -> None:
    for i, tdoc_id in enumerate(CROWDERS):
        vec.upsert_chunks(tdoc_id, _labelled(tdoc_id, [_near(i)] * 8))
    for i, tdoc_id in enumerate(MATCHES):
        vec.upsert_chunks(tdoc_id, _labelled(tdoc_id, [_far(i)]))


def _embedder():
    embedder = MagicMock()
    embedder.encode.return_value = np.stack([_query()])
    return embedder


def _service(vec, fanout):
    from doc3gpp.services.factory import build_search_service
    from doc3gpp.services.semantic_search_service import SemanticSearchService

    settings = MagicMock()
    settings.semantic_search.fanout_multiplier = fanout
    settings.semantic_search.rrf_k = 60
    return SemanticSearchService(build_search_service(), _embedder(), vec, settings)


def _filters(name):
    from doc3gpp.models.search import SearchFilters

    return {
        "none": SearchFilters(),
        "meeting_id": SearchFilters(meeting_id=200),
        "release": SearchFilters(release="Rel-18"),
    }[name]


def test_hybrid_crowding_measurement(vec_repo):
    _seed()
    _index_vectors(vec_repo)
    measured: dict[tuple[int, str], tuple[int, int, int, int]] = {}
    for fanout in FANOUTS:
        for name in FILTER_NAMES:
            spy = _Spy(vec_repo)
            svc = _service(spy, fanout)
            filters = _filters(name)
            semantic = svc.search(
                "q", fts5_query=None, filters=filters, limit=LIMIT, fts5_weight=0.5,
            )
            spy.calls.clear()
            one = svc.search(
                "q", fts5_query=ONE_MATCH_TERM, filters=filters, limit=LIMIT, fts5_weight=0.5,
            )
            one_rows = sum(rows for _limit, rows in spy.calls)
            spy.calls.clear()
            everything = svc.search(
                "q", fts5_query=ALL_MATCH_TERM, filters=filters, limit=LIMIT, fts5_weight=0.5,
            )
            measured[(fanout, name)] = (len(semantic), len(one), one_rows, len(everything))
    assert measured == EXPECTED


def test_hybrid_returns_limit_distinct_matches_when_crowded(vec_repo):
    from doc3gpp.models.search import SearchFilters
    from doc3gpp.services.factory import build_search_service
    from doc3gpp.services.semantic_search_service import SemanticSearchService
    from doc3gpp.settings.loader import get_settings

    _seed()
    _index_vectors(vec_repo)
    svc = SemanticSearchService(
        build_search_service(), _embedder(), vec_repo, get_settings(),
    )

    out = svc.search(
        "q", fts5_query=ONE_MATCH_TERM, filters=SearchFilters(release="Rel-18"),
        limit=LIMIT, fts5_weight=0.5,
    )

    tdoc_ids = {hit.tdoc_id for hit in out}
    assert len(tdoc_ids) == LIMIT
    assert tdoc_ids <= set(MATCHES)
