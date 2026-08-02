from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from doc3gpp.models.search import SearchFilters, SearchHit
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
    s.semantic_search.chunk_size = 200
    s.semantic_search.chunk_overlap = 20
    s.semantic_search.max_chunks_per_tdoc = 8
    return s


def _mock_embedder(n_chunks: int = 1):
    e = MagicMock()
    e.dim = 384
    e.encode.return_value = np.zeros((n_chunks, 384), dtype=np.float32)
    return e


def test_search_strips_query_for_fts5_and_uses_original_for_vector():
    fts5 = MagicMock()
    fts5.search.return_value = [_hit("R5-1")]
    vec = MagicMock()
    vec.knn.return_value = [("R5-1", "R5-1#0", 0, 0.1)]
    emb = _mock_embedder()
    svc = SemanticSearchService(fts5, emb, vec, _settings())
    out = svc.search(
        "what CRs touch NB-IoT power saving",
        fts5_query="valid query",
        filters=SearchFilters(),
        limit=10,
        fts5_weight=0.7,
    )
    assert len(out) == 1
    assert out[0].tdoc_id == "R5-1"
    # Embedder received the ORIGINAL query
    emb.encode.assert_called_once()
    assert emb.encode.call_args[0][0] == ["what CRs touch NB-IoT power saving"]


def test_search_vector_only_hit_populates_metadata_from_tdocs():
    """When the vector KNN returns a hit that FTS5 missed (e.g.
    the TDoc has no parsed cover/extract — title-only indexing
    covers 12,561 of 13,693 TDocs in real corpora), the
    synthesized ``hit`` stub must still carry the real
    ``title``, ``ftp_url``, ``meeting``, ``tsg``, ``uploaded_date``,
    and ``wis`` from ``tdocs``/``meetings``. Otherwise the CLI
    shows ``title=""``, ``ftp_url=null`` and the user can't tell
    what the hit actually is.
    """
    from dataclasses import dataclass

    @dataclass
    class _Meta:
        title: str
        ftp_url: str
        wis: str | None
        meeting: str | None
        tsg: str | None
        uploaded_date: str | None

    class _VecRepo:
        def __init__(self) -> None:
            self.knn_calls = []
            self.metadata_calls = []

        def knn(self, qv, limit, filters):
            self.knn_calls.append((qv, limit, filters))
            return [("R4-2605982", "R4-2605982#0", 0, 0.78)]

        def get_tdocs_metadata(self, tdoc_ids):
            self.metadata_calls.append(list(tdoc_ids))
            return {
                "R4-2605982": _Meta(
                    title="CR of Introduction of PC1.5 for NB-IoT based IoT-NTN",
                    ftp_url="tsg_ran/WG4_Radio/TSGR4_119/Docs/R4-2605982.zip",
                    wis="NB-IOT_NTN",
                    meeting="TSG-RAN WG4 #119",
                    tsg="RAN",
                    uploaded_date="2026-07-22",
                ),
            }

    fts5 = MagicMock()
    fts5.search.return_value = []  # FTS5 missed this hit
    vec = _VecRepo()
    svc = SemanticSearchService(fts5, _mock_embedder(), vec, _settings())
    out = svc.search(
        "nb-iot",
        fts5_query="valid query",
        filters=SearchFilters(),
        limit=10,
        fts5_weight=0.7,
    )
    assert len(out) == 1
    hit = out[0]
    assert hit.tdoc_id == "R4-2605982"
    # The synthesized stub must now be populated, not empty.
    assert hit.hit is not None
    assert hit.hit.title == (
        "CR of Introduction of PC1.5 for NB-IoT based IoT-NTN"
    )
    assert hit.hit.ftp_url == (
        "tsg_ran/WG4_Radio/TSGR4_119/Docs/R4-2605982.zip"
    )
    assert hit.hit.wis == "NB-IOT_NTN"
    assert hit.hit.meeting == "TSG-RAN WG4 #119"
    assert hit.hit.tsg == "RAN"
    assert hit.hit.uploaded_date == "2026-07-22"
    # The metadata lookup must have been called exactly once,
    # with the vector-only tdoc_ids (not with tdoc_ids that
    # already had FTS5 hits).
    assert vec.metadata_calls == [["R4-2605982"]]


def test_search_mixed_hits_only_looks_up_metadata_for_vector_only():
    """Vector-only hits need the metadata JOIN; FTS5 hits already
    carry it. The service must not call the metadata lookup for
    tdoc_ids that already have FTS5 coverage — that would be
    wasted work.
    """
    from dataclasses import dataclass

    @dataclass
    class _Meta:
        title: str
        ftp_url: str | None
        wis: str | None
        meeting: str | None
        tsg: str | None
        uploaded_date: str | None

    class _VecRepo:
        def __init__(self) -> None:
            self.metadata_calls: list[list[str]] = []

        def knn(self, qv, limit, filters):
            # Two vector hits: R5-1 (already in FTS5) and R4-2 (vector-only).
            return [
                ("R5-1", "R5-1#0", 0, 0.1),
                ("R4-2", "R4-2#0", 1, 0.2),
            ]

        def get_tdocs_metadata(self, tdoc_ids):
            self.metadata_calls.append(list(tdoc_ids))
            return {
                "R4-2": _Meta(
                    title="R4-2 title", ftp_url="r4-2.zip", wis=None,
                    meeting=None, tsg=None, uploaded_date=None,
                ),
            }

    fts5 = MagicMock()
    fts5.search.return_value = [_hit("R5-1")]
    vec = _VecRepo()
    svc = SemanticSearchService(fts5, _mock_embedder(), vec, _settings())
    svc.search(
        "q",
        fts5_query="valid query",
        filters=SearchFilters(),
        limit=10,
        fts5_weight=0.7,
    )
    # Only R4-2 needed the lookup; R5-1 already had FTS5 coverage.
    assert vec.metadata_calls == [["R4-2"]]


def test_search_vector_only_hit_unknown_tdoc_leaves_stub_empty():
    """Edge case: the vector KNN returns a tdoc_id that no longer
    exists in ``tdocs`` (deleted between index and query). The
    stub stays as today — empty fields — so the CLI can still
    surface the hit (with whatever metadata the index knew),
    and no spurious "unknown tdoc" error blocks the result list.
    """
    class _VecRepo:
        def __init__(self) -> None:
            self.metadata_calls: list[list[str]] = []

        def knn(self, qv, limit, filters):
            return [("GHOST-1", "GHOST-1#0", 0, 0.5)]

        def get_tdocs_metadata(self, tdoc_ids):
            self.metadata_calls.append(list(tdoc_ids))
            return {}  # tdoc was deleted

    fts5 = MagicMock()
    fts5.search.return_value = []
    vec = _VecRepo()
    svc = SemanticSearchService(fts5, _mock_embedder(), vec, _settings())
    out = svc.search(
        "q",
        fts5_query="valid query",
        filters=SearchFilters(),
        limit=10,
        fts5_weight=0.7,
    )
    assert len(out) == 1
    # Stub is empty but the hit is still surfaced.
    assert out[0].hit is not None
    assert out[0].hit.title == ""
    assert out[0].hit.ftp_url is None


def test_search_both_sides_empty_returns_empty():
    fts5 = MagicMock()
    fts5.search.return_value = []
    vec = MagicMock()
    vec.knn.return_value = []
    svc = SemanticSearchService(fts5, _mock_embedder(), vec, _settings())
    out = svc.search(
        "valid query",
        fts5_query="valid query",
        filters=SearchFilters(),
        limit=10,
        fts5_weight=0.7,
    )
    assert out == []


def test_search_uses_fanout_multiplier():
    fts5 = MagicMock()
    fts5.search.return_value = []
    vec = MagicMock()
    vec.knn.return_value = []
    svc = SemanticSearchService(fts5, _mock_embedder(), vec, _settings())
    svc.search(
        "q",
        fts5_query="q",
        filters=SearchFilters(),
        limit=20,
        fts5_weight=0.7,
    )
    # internal_limit = 20 * 2 = 40
    assert fts5.search.call_args[0][1].limit == 40
    assert vec.knn.call_args[1]["limit"] == 40


def test_search_synthesizes_fts5_hit_for_vector_only_tdoc():
    """Vector-only tdoc (not in FTS5 fan-out) must still carry a real SearchHit."""
    fts5 = MagicMock()
    fts5.search.return_value = []
    vec = MagicMock()
    vec.knn.return_value = [("R5-1", "R5-1#0", 0, 0.1)]
    svc = SemanticSearchService(fts5, _mock_embedder(), vec, _settings())
    out = svc.search(
        "valid",
        fts5_query="valid",
        filters=SearchFilters(),
        limit=10,
        fts5_weight=0.7,
    )
    assert len(out) == 1
    assert out[0].tdoc_id == "R5-1"
    assert out[0].hit is not None
    assert isinstance(out[0].hit, SearchHit)
    assert out[0].hit.tdoc_id == "R5-1"


def test_index_for_tdoc_calls_upsert_chunks(monkeypatch):
    vec = MagicMock()
    fts5 = MagicMock()
    monkeypatch.setattr(
        "doc3gpp.services.semantic_search_service._build_embed_text",
        lambda tid: "long text " * 200,
    )
    svc = SemanticSearchService(fts5, _mock_embedder(n_chunks=3), vec, _settings())
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
        lambda tid: "long text " * 4000,  # -> 8 chunks capped by max_chunks_per_tdoc
    )
    embedder = MagicMock()
    embedder.dim = 384
    # encode() returns one row per chunk; 4000 whitespace tokens at
    # default size=200 / overlap=20 yields 22 chunks, capped at 8 by
    # max_chunks_per_tdoc.
    embedder.encode.return_value = np.zeros((8, 384), dtype=np.float32)
    svc = SemanticSearchService(fts5, embedder, vec, _settings())
    svc.index_for_tdoc("R5-1")
    # exactly one encode() call for all 8 chunks
    embedder.encode.assert_called_once()
    call_args = embedder.encode.call_args
    # single positional arg = list of chunks
    assert len(call_args[0][0]) == 8
    # and the resulting embeddings land in upsert_chunks
    args = vec.upsert_chunks.call_args
    assert args[0][0] == "R5-1"
    assert len(args[0][1]) == 8


def test_rebuild_embeddings_clears_cursor_when_resume_false() -> None:
    """When the vector rebuild is invoked with resume=False (the
    default), it must clear any existing cursor first so a fresh
    start picks up from the very first TDoc.
    """
    vec = MagicMock()
    vec.count_tdocs_to_index.return_value = 3
    vec.rebuild_batch.return_value = iter([["R5-1", "R5-2", "R5-3"]])
    vec.get_resume_cursor.return_value = "C3-stale"
    mp = pytest.MonkeyPatch()
    mp.setattr(
        "doc3gpp.services.semantic_search_service._build_embed_text",
        lambda tid: "text",
    )
    svc = SemanticSearchService(MagicMock(), _mock_embedder(), vec, _settings())
    list(
        svc.rebuild_embeddings(
            batch_size=10, stale_only=False, quiet=True, resume=False,
        ),
    )
    vec.clear_resume_cursor.assert_called_once()
    # After clear, get_resume_cursor must have been called and its
    # return value (None, after clear) used as after_id.
    vec.set_resume_cursor.assert_called_once_with("R5-3")
    mp.undo()


def test_rebuild_embeddings_honors_cursor_when_resume_true() -> None:
    """When the vector rebuild is invoked with resume=True, it must
    NOT clear the cursor and must use the cursor's value as
    after_id.
    """
    vec = MagicMock()
    vec.count_tdocs_to_index.return_value = 3
    vec.rebuild_batch.return_value = iter([["R5-1", "R5-2", "R5-3"]])
    vec.get_resume_cursor.return_value = "C3-resume-point"
    mp = pytest.MonkeyPatch()
    mp.setattr(
        "doc3gpp.services.semantic_search_service._build_embed_text",
        lambda tid: "text",
    )
    svc = SemanticSearchService(MagicMock(), _mock_embedder(), vec, _settings())
    list(
        svc.rebuild_embeddings(
            batch_size=10, stale_only=False, quiet=True, resume=True,
        ),
    )
    vec.clear_resume_cursor.assert_not_called()
    # set_resume_cursor was still called to advance the cursor
    # forward.
    vec.set_resume_cursor.assert_called_once_with("R5-3")
    mp.undo()


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


def test_search_with_fts5_query_runs_search_service_with_builder_output(monkeypatch):
    """When fts5_query is supplied, SemanticSearchService must:
    1. Run it through SearchQueryBuilder.
    2. Pass the builder's output to fts5_service.search, not the raw --fts5-query string.
    3. Pass the ORIGINAL query to the embedder, not the fts5_query.
    4. Call rrf_merge with vector_weight = 1 - fts5_weight.
    """
    from doc3gpp.cli_filters import SearchQueryBuilder
    captured = {}

    real_builder = SearchQueryBuilder.build

    def spy(self):
        captured["fts5_input"] = self._query
        captured["fts5_output"] = real_builder(self)
        return captured["fts5_output"]

    monkeypatch.setattr(SearchQueryBuilder, "build", spy)

    fts5 = MagicMock()
    fts5.search.return_value = [_hit("R5-1")]
    vec = MagicMock()
    vec.knn.return_value = [("R5-1", "R5-1#0", 0, 0.1)]
    emb = _mock_embedder()
    svc = SemanticSearchService(fts5, emb, vec, _settings())
    out = svc.search(
        "natural language prose",
        fts5_query="tsg:RP spec:38.300",
        filters=SearchFilters(),
        limit=10,
        fts5_weight=0.5,
    )
    assert len(out) == 1
    assert captured["fts5_input"] == "tsg:RP spec:38.300"
    # fts5_service.search sees the BUILDER OUTPUT (not the raw string).
    fts5.search.assert_called_once()
    assert fts5.search.call_args[0][0] == captured["fts5_output"]
    # The ORIGINAL query went to the embedder.
    assert emb.encode.call_args[0][0] == ["natural language prose"]


def test_search_without_fts5_query_skips_fts5_service(monkeypatch):
    """When fts5_query is None, the service MUST NOT call fts5_service.search,
    MUST NOT run SearchQueryBuilder, and MUST return top-`limit` vector hits
    dressed as SemanticSearchHit with rank_fts5=None and rrf_score=-distance.
    """
    vec = MagicMock()
    vec.knn.return_value = [
        ("R5-1", "R5-1#0", 0, 0.1),
        ("R5-2", "R5-2#0", 1, 0.4),
        ("R5-3", "R5-3#0", 2, 0.9),
    ]
    fts5 = MagicMock()
    emb = _mock_embedder()
    svc = SemanticSearchService(fts5, emb, vec, _settings())
    out = svc.search(
        "natural prose",
        fts5_query=None,
        filters=SearchFilters(),
        limit=10,
        fts5_weight=0.5,  # MUST be ignored
    )
    assert [h.tdoc_id for h in out] == ["R5-1", "R5-2", "R5-3"]
    fts5.search.assert_not_called()
    # Embedder was called with the natural-language query.
    assert emb.encode.call_args[0][0] == ["natural prose"]
    # All hits have rank_fts5=None, rank_vec set, rrf_score = -distance.
    assert all(h.rank_fts5 is None for h in out)
    assert [h.rank_vec for h in out] == [0, 1, 2]
    assert [h.min_chunk_distance for h in out] == [0.1, 0.4, 0.9]
    assert [h.rrf_score for h in out] == [-0.1, -0.4, -0.9]


def test_search_without_fts5_query_truncates_to_limit(monkeypatch):
    """The pure-vector path must still respect --limit (no internal fanout)."""
    vec = MagicMock()
    vec.knn.return_value = [
        (f"R5-{i}", f"R5-{i}#0", i, 0.1 * (i + 1)) for i in range(5)
    ]
    svc = SemanticSearchService(MagicMock(), _mock_embedder(), vec, _settings())
    out = svc.search("q", fts5_query=None, filters=SearchFilters(), limit=3, fts5_weight=0.5)
    # vec.knn called with limit=3 (no internal fanout when FTS5 is skipped).
    assert vec.knn.call_args[1]["limit"] == 3
    assert len(out) == 3


def test_search_without_fts5_query_vector_only_populates_metadata():
    """Even without FTS5, vector-only hits must still carry a synthesized
    SearchHit stub populated from tdocs/meetings via get_tdocs_metadata.
    """
    from dataclasses import dataclass

    @dataclass
    class _Meta:
        title: str
        ftp_url: str | None
        wis: str | None
        meeting: str | None
        tsg: str | None
        uploaded_date: str | None

    class _VecRepo:
        def knn(self, qv, limit, filters):
            return [("R5-1", "R5-1#0", 0, 0.1)]

        def get_tdocs_metadata(self, tdoc_ids):
            return {
                "R5-1": _Meta(
                    title="real title", ftp_url="real.zip", wis=None,
                    meeting=None, tsg=None, uploaded_date=None,
                ),
            }

    svc = SemanticSearchService(
        MagicMock(), _mock_embedder(), _VecRepo(), _settings(),
    )
    out = svc.search("q", fts5_query=None, filters=SearchFilters(), limit=10, fts5_weight=0.5)
    assert out[0].hit.title == "real title"
    assert out[0].hit.ftp_url == "real.zip"


def test_search_with_fts5_query_uses_one_minus_fts5_weight_for_rrf():
    """fts5_weight=0.7 in CLI must reach rrf_merge as vector_weight=0.3."""
    captured = {}
    real_merge = __import__(
        "doc3gpp.services.semantic_search_service",
        fromlist=["rrf_merge"],
    ).rrf_merge

    def spy_merge(fts5_hits, vec_hits, *, k, vector_weight, limit):
        captured["vector_weight"] = vector_weight
        return real_merge(
            fts5_hits, vec_hits, k=k,
            vector_weight=vector_weight, limit=limit,
        )

    import doc3gpp.services.semantic_search_service as svc_mod
    monkey = __import__("pytest").MonkeyPatch()
    monkey.setattr(svc_mod, "rrf_merge", spy_merge)
    try:
        fts5 = MagicMock()
        fts5.search.return_value = [_hit("R5-1")]
        vec = MagicMock()
        vec.knn.return_value = [("R5-1", "R5-1#0", 0, 0.1)]
        svc = SemanticSearchService(fts5, _mock_embedder(), vec, _settings())
        svc.search(
            "q", fts5_query="R5-1",
            filters=SearchFilters(), limit=10, fts5_weight=0.7,
        )
        assert captured["vector_weight"] == pytest.approx(0.3)
    finally:
        monkey.undo()


def test_search_without_fts5_query_empty_vector_returns_empty():
    vec = MagicMock()
    vec.knn.return_value = []
    svc = SemanticSearchService(MagicMock(), _mock_embedder(), vec, _settings())
    out = svc.search("q", fts5_query=None, filters=SearchFilters(), limit=10, fts5_weight=0.5)
    assert out == []
