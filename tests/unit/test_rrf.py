from __future__ import annotations

from doc3gpp.models.search import SearchHit
from doc3gpp.services.semantic_search_service import rrf_merge


def _hit(tdoc_id: str, score: float = -1.0) -> SearchHit:
    return SearchHit(
        tdoc_id=tdoc_id, score=score, previews={"title": "t"}, title="t",
        meeting=None, tsg=None, uploaded_date=None, ftp_url=None, wis=None,
    )


def test_rrf_pure_fts5_when_vector_weight_zero():
    fts5 = [_hit("A"), _hit("B")]
    vec: list = []
    out = rrf_merge(fts5, vec, k=60, vector_weight=0.0, limit=10)
    assert [h.tdoc_id for h in out] == ["A", "B"]
    assert out[0].rank_fts5 == 0
    assert out[0].rank_vec is None


def test_rrf_pure_vector_when_vector_weight_one():
    fts5: list = []
    vec = [("A", "A#0", 0, 0.1), ("B", "B#0", 0, 0.2)]
    out = rrf_merge(fts5, vec, k=60, vector_weight=1.0, limit=10)
    assert [h.tdoc_id for h in out] == ["A", "B"]
    assert out[0].rank_vec == 0
    assert out[0].rank_fts5 is None


def test_rrf_blend_both_sides():
    fts5 = [_hit("A"), _hit("B")]
    vec = [("B", "B#0", 0, 0.1), ("C", "C#0", 0, 0.2)]
    # A: fts5 rank 0, no vec → score = 1/(60+0)*(1-W) = 1/60 * 0.3 = 0.005
    # B: fts5 rank 1, vec rank 0 → 1/61*0.3 + 1/60*0.7 = 0.00492 + 0.01167 = 0.01658
    # C: no fts5, vec rank 1 → 1/61*0.7 = 0.01148
    out = rrf_merge(fts5, vec, k=60, vector_weight=0.7, limit=10)
    # B should rank first (highest score)
    assert out[0].tdoc_id == "B"
    assert out[0].rank_fts5 == 1
    assert out[0].rank_vec == 0


def test_rrf_min_distance_across_chunks():
    # Same tdoc_id, multiple chunks → min distance wins
    fts5: list = []
    vec = [
        ("A", "A#0", 0, 0.5),
        ("A", "A#1", 1, 0.1),  # best chunk
        ("A", "A#2", 2, 0.3),
    ]
    out = rrf_merge(fts5, vec, k=60, vector_weight=1.0, limit=10)
    assert out[0].tdoc_id == "A"
    assert out[0].min_chunk_distance == 0.1
    assert out[0].best_chunk_id == "A#1"


def test_rrf_limit_truncation():
    fts5 = [_hit(f"T{i}") for i in range(10)]
    out = rrf_merge(fts5, [], k=60, vector_weight=0.0, limit=3)
    assert len(out) == 3


def test_rrf_empty_both_sides():
    out = rrf_merge([], [], k=60, vector_weight=0.5, limit=10)
    assert out == []


def test_rrf_synthesizes_hit_for_vector_only_tdoc():
    # When a tdoc is only in vector fan-out, the service synthesizes a
    # minimal SearchHit. rrf_merge itself does NOT synthesize — it
    # carries None and the service fills it. Test the contract:
    fts5: list = []
    vec = [("A", "A#0", 0, 0.1)]
    out = rrf_merge(fts5, vec, k=60, vector_weight=1.0, limit=10)
    assert out[0].tdoc_id == "A"
    # hit is None for vector-only; service fills it later
    assert out[0].hit is None
