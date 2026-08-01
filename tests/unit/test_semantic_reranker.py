"""Unit tests for SemanticReranker (no live model, no DB)."""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from doc3gpp.models.search import SearchHit
from doc3gpp.services.semantic_reranker import SemanticReranker


def _hit(t: str) -> SearchHit:
    return SearchHit(
        tdoc_id=t, score=0.0, previews={}, title="t", meeting="m",
        tsg="S1", uploaded_date="2026-01-01", ftp_url="https://x", wis=(),
    )


def _settings() -> MagicMock:
    s = MagicMock()
    s.search_fanout_factor = 4  # not read by reranker, but present
    return s


def _mock_embedder() -> MagicMock:
    e = MagicMock()
    e.encode.return_value = np.asarray([[0.0, 0.0, 0.0, 0.0]], dtype=np.float32)
    e.dim = 4
    return e


def test_empty_input_no_embedder_call():
    emb = _mock_embedder()
    vec = MagicMock()
    svc = SemanticReranker(emb, vec, _settings())
    out = svc.rerank("query", [])
    assert out == []
    emb.encode.assert_not_called()


def test_one_embedder_call_regardless_of_hit_count():
    emb = _mock_embedder()
    vec = MagicMock()
    vec.get_min_distance_for_tdocs.return_value = {
        f"R5-{i}": (0.1 * i, f"R5-{i}#0") for i in range(1, 6)
    }
    svc = SemanticReranker(emb, vec, _settings())
    hits = [_hit(f"R5-{i}") for i in range(1, 6)]
    svc.rerank("query", hits)
    emb.encode.assert_called_once_with(["query"])


def test_orders_by_negated_distance_desc():
    emb = _mock_embedder()
    vec = MagicMock()
    # Lower distance = better; -distance is the score we sort by.
    vec.get_min_distance_for_tdocs.return_value = {
        "R5-1": (0.5, "R5-1#0"),
        "R5-2": (0.1, "R5-2#0"),
        "R5-3": (0.9, "R5-3#0"),
    }
    svc = SemanticReranker(emb, vec, _settings())
    hits = [_hit("R5-1"), _hit("R5-2"), _hit("R5-3")]
    out = svc.rerank("query", hits)
    assert [h.tdoc_id for h in out] == ["R5-2", "R5-1", "R5-3"]


def test_stable_sort_preserves_input_order_on_ties():
    emb = _mock_embedder()
    vec = MagicMock()
    vec.get_min_distance_for_tdocs.return_value = {
        "R5-1": (0.1, "R5-1#0"),
        "R5-2": (0.1, "R5-2#0"),
        "R5-3": (0.1, "R5-3#0"),
    }
    svc = SemanticReranker(emb, vec, _settings())
    hits = [_hit("R5-1"), _hit("R5-2"), _hit("R5-3")]
    out = svc.rerank("query", hits)
    assert [h.tdoc_id for h in out] == ["R5-1", "R5-2", "R5-3"]


def test_final_limit_truncates():
    emb = _mock_embedder()
    vec = MagicMock()
    vec.get_min_distance_for_tdocs.return_value = {
        f"R5-{i}": (0.1 * i, f"R5-{i}#0") for i in range(1, 6)
    }
    svc = SemanticReranker(emb, vec, _settings())
    hits = [_hit(f"R5-{i}") for i in range(1, 6)]
    out = svc.rerank("query", hits, final_limit=2)
    assert [h.tdoc_id for h in out] == ["R5-1", "R5-2"]


def test_final_limit_none_returns_full_list():
    emb = _mock_embedder()
    vec = MagicMock()
    vec.get_min_distance_for_tdocs.return_value = {
        f"R5-{i}": (0.1 * i, f"R5-{i}#0") for i in range(1, 4)
    }
    svc = SemanticReranker(emb, vec, _settings())
    hits = [_hit(f"R5-{i}") for i in range(1, 4)]
    out = svc.rerank("query", hits)
    assert len(out) == 3


def test_missing_vector_row_sorts_below_real_scores():
    emb = _mock_embedder()
    vec = MagicMock()
    # R5-2 has no vector row.
    vec.get_min_distance_for_tdocs.return_value = {
        "R5-1": (0.1, "R5-1#0"),
        "R5-2": None,
    }
    svc = SemanticReranker(emb, vec, _settings())
    hits = [_hit("R5-1"), _hit("R5-2")]
    out = svc.rerank("query", hits)
    assert [h.tdoc_id for h in out] == ["R5-1", "R5-2"]


def test_all_missing_preserves_input_order():
    emb = _mock_embedder()
    vec = MagicMock()
    vec.get_min_distance_for_tdocs.return_value = {
        "R5-1": None,
        "R5-2": None,
    }
    svc = SemanticReranker(emb, vec, _settings())
    hits = [_hit("R5-1"), _hit("R5-2")]
    out = svc.rerank("query", hits)
    assert [h.tdoc_id for h in out] == ["R5-1", "R5-2"]


def test_missing_floor_constant_is_negative_infinity():
    assert SemanticReranker.MISSING_FLOOR == float("-inf")
