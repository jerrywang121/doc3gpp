from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from doc3gpp.models.search import SearchFilters, SearchHit
from doc3gpp.models.semantic_search import SemanticSearchQueryError
from doc3gpp.services.semantic_search_service import SemanticSearchService


def _hit(tdoc_id: str) -> SearchHit:
    return SearchHit(
        tdoc_id=tdoc_id, score=-1.0, previews={"title": "t"}, title="t",
        meeting=None, tsg=None, uploaded_date=None, ftp_url=None, wis=None,
    )


def _settings():
    s = MagicMock()
    s.semantic_search.fanout_multiplier = 2
    s.semantic_search.rrf_k = 60
    s.semantic_search.chunk_size = 800
    s.semantic_search.chunk_overlap = 100
    s.semantic_search.max_chunks_per_tdoc = 32
    return s


def _mock_embedder():
    e = MagicMock()
    e.dim = 384
    e.encode.return_value = np.zeros((1, 384), dtype=np.float32)
    return e


def test_search_strips_query_for_fts5_and_uses_original_for_vector(monkeypatch):
    monkeypatch.setattr(
        "doc3gpp.services.semantic_search_service.strip_stopwords",
        lambda q: "CR touch NB-IoT power save",
    )
    fts5 = MagicMock()
    fts5.search.return_value = [_hit("R5-1")]
    vec = MagicMock()
    vec.knn.return_value = [("R5-1", "R5-1#0", 0, 0.1)]
    emb = _mock_embedder()
    svc = SemanticSearchService(fts5, emb, vec, _settings())
    out = svc.search("what CRs touch NB-IoT power saving", SearchFilters(), limit=10, vector_weight=0.7)
    assert len(out) == 1
    assert out[0].tdoc_id == "R5-1"
    # Embedder received the ORIGINAL query
    emb.encode.assert_called_once()
    assert emb.encode.call_args[0][0] == ["what CRs touch NB-IoT power saving"]


def test_search_raises_on_empty_after_strip(monkeypatch):
    monkeypatch.setattr(
        "doc3gpp.services.semantic_search_service.strip_stopwords",
        lambda q: "",
    )
    svc = SemanticSearchService(MagicMock(), _mock_embedder(), MagicMock(), _settings())
    with pytest.raises(SemanticSearchQueryError):
        svc.search("   ", SearchFilters(), limit=10, vector_weight=0.7)


def test_search_both_sides_empty_returns_empty(monkeypatch):
    monkeypatch.setattr(
        "doc3gpp.services.semantic_search_service.strip_stopwords",
        lambda q: "valid query",
    )
    fts5 = MagicMock()
    fts5.search.return_value = []
    vec = MagicMock()
    vec.knn.return_value = []
    svc = SemanticSearchService(fts5, _mock_embedder(), vec, _settings())
    out = svc.search("valid query", SearchFilters(), limit=10, vector_weight=0.7)
    assert out == []


def test_search_uses_fanout_multiplier(monkeypatch):
    monkeypatch.setattr(
        "doc3gpp.services.semantic_search_service.strip_stopwords",
        lambda q: "q",
    )
    fts5 = MagicMock()
    fts5.search.return_value = []
    vec = MagicMock()
    vec.knn.return_value = []
    svc = SemanticSearchService(fts5, _mock_embedder(), vec, _settings())
    svc.search("q", SearchFilters(), limit=20, vector_weight=0.7)
    # internal_limit = 20 * 2 = 40
    assert fts5.search.call_args[0][1].limit == 40
    assert vec.knn.call_args[1]["limit"] == 40


def test_index_for_tdoc_calls_upsert_chunks(monkeypatch):
    vec = MagicMock()
    fts5 = MagicMock()
    monkeypatch.setattr(
        "doc3gpp.services.semantic_search_service._build_embed_text",
        lambda tid: "long text " * 200,
    )
    svc = SemanticSearchService(fts5, _mock_embedder(), vec, _settings())
    svc.index_for_tdoc("R5-1")
    vec.upsert_chunks.assert_called_once()
    args = vec.upsert_chunks.call_args
    assert args[0][0] == "R5-1"
    # embeddings is a list of ndarrays
    assert isinstance(args[0][1], list)


def test_index_for_tdoc_encodes_all_chunks_in_one_call(monkeypatch):
    """Regression: chunks for one TDoc must be batched into a single
    ``embedder.encode([...])`` call, not one call per chunk. The
    sentence-transformers model has ~1s per-call overhead, so
    looping kills the rebuild (1 tdoc/sec instead of hundreds).
    """
    import numpy as np

    vec = MagicMock()
    fts5 = MagicMock()
    monkeypatch.setattr(
        "doc3gpp.services.semantic_search_service._build_embed_text",
        lambda tid: "long text " * 4000,  # -> 5 chunks of ~800 tokens
    )
    embedder = MagicMock()
    embedder.dim = 384
    # encode() returns one row per chunk; 4000 whitespace tokens at
    # default size=800 / overlap=100 yields 12 chunks.
    embedder.encode.return_value = np.zeros((12, 384), dtype=np.float32)
    svc = SemanticSearchService(fts5, embedder, vec, _settings())
    svc.index_for_tdoc("R5-1")
    # exactly one encode() call for all 12 chunks
    embedder.encode.assert_called_once()
    call_args = embedder.encode.call_args
    # single positional arg = list of chunks
    assert len(call_args[0][0]) == 12
    # and the resulting embeddings land in upsert_chunks
    args = vec.upsert_chunks.call_args
    assert args[0][0] == "R5-1"
    assert len(args[0][1]) == 12


def test_remove_for_tdoc_calls_repo():
    vec = MagicMock()
    svc = SemanticSearchService(MagicMock(), _mock_embedder(), vec, _settings())
    svc.remove_for_tdoc("R5-1")
    vec.remove_for_tdoc.assert_called_once_with("R5-1")


def test_rebuild_embeddings_yields_progress(monkeypatch):
    """Small corpora (total < 100) yield once per TDoc because
    integer-pct granularity exceeds 1.
    """
    vec = MagicMock()
    vec.count_tdocs_to_index.return_value = 3
    vec.rebuild_batch.return_value = iter([["R5-1", "R5-2", "R5-3"]])
    vec.get_resume_cursor.return_value = None
    monkeypatch.setattr(
        "doc3gpp.services.semantic_search_service._build_embed_text",
        lambda tid: "text",
    )
    svc = SemanticSearchService(MagicMock(), _mock_embedder(), vec, _settings())
    progress = list(svc.rebuild_embeddings(batch_size=10, stale_only=False, quiet=True))
    assert len(progress) == 3
    assert [p.processed for p in progress] == [1, 2, 3]
    assert all(p.total == 3 for p in progress)


def test_rebuild_embeddings_yields_per_one_percent_for_13k_corpus(monkeypatch):
    """Real corpus size (13,693 tdocs) must yield exactly 100 times
    — once per integer-pct crossing. The final yield must be at
    processed = 13,693 (the corpus total).
    """
    total = 13693
    vec = MagicMock()
    vec.count_tdocs_to_index.return_value = total
    vec.rebuild_batch.return_value = iter([
        [f"R5-{i:06d}" for i in range(1, total + 1)],
    ])
    vec.get_resume_cursor.return_value = None
    monkeypatch.setattr(
        "doc3gpp.services.semantic_search_service._build_embed_text",
        lambda tid: "text",
    )
    svc = SemanticSearchService(MagicMock(), _mock_embedder(), vec, _settings())
    progress = list(
        svc.rebuild_embeddings(batch_size=total, stale_only=False, quiet=True),
    )
    # Compute the actual integer-pct crossings from the same
    # formula the production code uses.
    expected_processed = []
    last_pct = 0
    for p in range(1, total + 1):
        pct = p * 100 // total
        if pct > last_pct:
            expected_processed.append(p)
            last_pct = pct
    assert len(progress) == 100
    assert [p.processed for p in progress] == expected_processed
    assert progress[-1].current_tdoc_id == f"R5-{total:06d}"


def test_rebuild_embeddings_processed_is_monotonic(monkeypatch):
    """rebuild_embeddings must yield processed values strictly
    increasing and total constant; the bar relies on processed
    climbing monotonically.
    """
    vec = MagicMock()
    vec.count_tdocs_to_index.return_value = 7
    vec.rebuild_batch.return_value = iter([["R5-1", "R5-2", "R5-3"], ["R5-4", "R5-5", "R5-6", "R5-7"]])
    vec.get_resume_cursor.return_value = None
    monkeypatch.setattr(
        "doc3gpp.services.semantic_search_service._build_embed_text",
        lambda tid: "text",
    )
    svc = SemanticSearchService(MagicMock(), _mock_embedder(), vec, _settings())
    progress = list(svc.rebuild_embeddings(batch_size=3, stale_only=False, quiet=True))
    # For total=7, pct crosses at every TDoc → 7 yields.
    assert len(progress) == 7
    assert [p.processed for p in progress] == [1, 2, 3, 4, 5, 6, 7]
    assert all(p.total == 7 for p in progress)
    # current_tdoc_id tracks the actual TDoc just embedded.
    assert [p.current_tdoc_id for p in progress] == [
        "R5-1", "R5-2", "R5-3", "R5-4", "R5-5", "R5-6", "R5-7",
    ]


def test_search_synthesizes_fts5_hit_for_vector_only_tdoc(monkeypatch):
    """Vector-only tdoc (not in FTS5 fan-out) must still carry a real SearchHit."""
    monkeypatch.setattr(
        "doc3gpp.services.semantic_search_service.strip_stopwords",
        lambda q: "valid",
    )
    fts5 = MagicMock()
    fts5.search.return_value = []
    vec = MagicMock()
    vec.knn.return_value = [("R5-1", "R5-1#0", 0, 0.1)]
    svc = SemanticSearchService(fts5, _mock_embedder(), vec, _settings())
    out = svc.search("valid", SearchFilters(), limit=10, vector_weight=0.7)
    assert len(out) == 1
    assert out[0].tdoc_id == "R5-1"
    assert out[0].fts5_hit is not None
    assert isinstance(out[0].fts5_hit, SearchHit)
    assert out[0].fts5_hit.tdoc_id == "R5-1"
