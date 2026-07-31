from __future__ import annotations

import pytest

from doc3gpp.models.search import SearchError, SearchHit
from doc3gpp.models.semantic_search import (
    EmbedderUnavailableError,
    SemanticSearchError,
    SemanticSearchHit,
    SemanticSearchQueryError,
    SemanticSearchUnavailableError,
    SpacyUnavailableError,
    VectorIndexUnavailableError,
)


def _hit(tdoc_id: str = "R5-1") -> SearchHit:
    return SearchHit(
        tdoc_id=tdoc_id, score=-1.0, previews={"title": "t"},
        title="t", meeting=None, tsg=None, uploaded_date=None,
        ftp_url=None, wis=None,
    )


def test_semantic_search_hit_is_frozen():
    h = SemanticSearchHit(
        tdoc_id="R5-1", rrf_score=0.5, rank_fts5=0, rank_vec=1,
        min_chunk_distance=0.2, best_chunk_id="R5-1#3", fts5_hit=_hit(),
    )
    with pytest.raises(Exception):
        h.tdoc_id = "R5-2"  # frozen dataclass
    assert h.rank_fts5 == 0
    assert h.fts5_hit.tdoc_id == "R5-1"


def test_semantic_search_hit_optional_ranks_default_none():
    h = SemanticSearchHit(
        tdoc_id="R5-1", rrf_score=0.5, fts5_hit=_hit(),
    )
    assert h.rank_fts5 is None
    assert h.rank_vec is None
    assert h.min_chunk_distance is None
    assert h.best_chunk_id is None


def test_error_hierarchy_extends_search_error():
    for cls in (
        SemanticSearchError,
        SemanticSearchUnavailableError,
        SemanticSearchQueryError,
        SpacyUnavailableError,
        EmbedderUnavailableError,
        VectorIndexUnavailableError,
    ):
        assert issubclass(cls, SearchError), cls
    assert issubclass(SemanticSearchUnavailableError, SemanticSearchError)
    assert issubclass(SemanticSearchQueryError, SemanticSearchError)
    assert issubclass(SpacyUnavailableError, SemanticSearchError)
    assert issubclass(EmbedderUnavailableError, SemanticSearchError)
    assert issubclass(VectorIndexUnavailableError, SemanticSearchError)
