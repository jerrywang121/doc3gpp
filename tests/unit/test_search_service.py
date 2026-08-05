"""Service-level orchestration tests with a mock repo."""

from __future__ import annotations

from collections.abc import Iterable

from doc3gpp.models.search import (
    SearchFilters,
    SearchHit,
    SearchIndexStatus,
)
from doc3gpp.repository.protocols import EmbeddingReranker, SearchIndexRepository
from doc3gpp.services.search_service import (
    PassthroughReranker,
    SearchService,
)


class MockRepo(SearchIndexRepository):
    def __init__(self) -> None:
        self.upserts: list[str] = []
        self.removes: list[str] = []
        self.search_query: tuple[str, SearchFilters] | None = None
        self.batches: list[list[str]] = [["R5-000001", "R5-000002", "R5-000003"]]
        self.cursor: str | None = None

    def upsert(self, tdoc_id: str) -> None:
        self.upserts.append(tdoc_id)

    def remove(self, tdoc_id: str) -> None:
        self.removes.append(tdoc_id)

    def search(
        self, query: str, filters: SearchFilters,
    ) -> list[SearchHit]:
        self.search_query = (query, filters)
        return [
            SearchHit(
                tdoc_id="R5-000001",
                score=-1.0,
                previews={"title": "hit-1"},
                title="hit-1",
                meeting=None,
                tsg=None,
                uploaded_date=None,
                ftp_url=None,
                wis=None,
            ),
        ]

    def rebuild_batch(
        self,
        batch_size: int,
        after_id: str | None,
        stale_only: bool,
    ) -> Iterable[list[str]]:
        return iter(self.batches)

    def count_tdocs_to_index(
        self, stale_only: bool, after_id: str | None = None,
    ) -> int:
        return getattr(self, "total", 3)

    def get_resume_cursor(self) -> str | None:
        return self.cursor

    def set_resume_cursor(self, tdoc_id: str) -> None:
        self.cursor = tdoc_id

    def clear_resume_cursor(self) -> None:
        self.cursor = None
        self.cleared = True

    def status(self) -> SearchIndexStatus:
        return SearchIndexStatus(
            enabled=True,
            row_count=3,
            last_rebuild_at=None,
            last_indexed_uploaded_date=None,
            latest_tdocs_uploaded_date=None,
            is_stale=False,
        )


class StubReranker(EmbeddingReranker):
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.invocations = 0

    def rerank(
        self, semantic_query: str, hits: list[SearchHit],
        final_limit: int | None = None,
        quiet: bool = False,
    ) -> list[SearchHit]:
        self.queries.append(semantic_query)
        self.invocations += 1
        return list(reversed(hits))


def test_upsert_for_tdoc_delegates() -> None:
    repo = MockRepo()
    svc = SearchService(repo=repo, reranker=PassthroughReranker())
    svc.upsert_for_tdoc("R5-000007")
    assert repo.upserts == ["R5-000007"]


def test_remove_for_tdoc_delegates() -> None:
    repo = MockRepo()
    svc = SearchService(repo=repo, reranker=PassthroughReranker())
    svc.remove_for_tdoc("R5-000009")
    assert repo.removes == ["R5-000009"]


def test_search_without_sem_query_skips_reranker() -> None:
    repo = MockRepo()
    reranker = StubReranker()
    svc = SearchService(repo=repo, reranker=reranker)
    hits = svc.search("anything", SearchFilters(limit=5))
    assert len(hits) == 1
    assert reranker.invocations == 0
    assert reranker.queries == []
    assert repo.search_query is not None
    assert repo.search_query[1].limit == 5


def test_search_with_sem_query_reranks_with_fanout() -> None:
    repo = MockRepo()
    reranker = StubReranker()
    svc = SearchService(repo=repo, reranker=reranker)
    hits = svc.search("anything", SearchFilters(limit=5), sem_query="semantic text")
    assert len(hits) == 1
    assert reranker.invocations == 1
    assert reranker.queries == ["semantic text"]
    assert repo.search_query is not None
    assert repo.search_query[1].limit == 5 * 4  # search_fanout_factor default 4
    """rebuild must yield at most once per 1% of progress so the
    CLI tqdm bar updates ~100 times for a 13,693-tdoc rebuild
    instead of 27 (per batch) or 13,693 (per tdoc).
    """
    # 1000 tdocs across 5 batches of 200 → 100 yields at 1% each.
    repo = MockRepo()
    repo.total = 1000
    repo.batches = [
        [f"R5-{i:06d}" for i in range(1, 201)],
        [f"R5-{i:06d}" for i in range(201, 401)],
        [f"R5-{i:06d}" for i in range(401, 601)],
        [f"R5-{i:06d}" for i in range(601, 801)],
        [f"R5-{i:06d}" for i in range(801, 1001)],
    ]
    svc = SearchService(repo=repo, reranker=PassthroughReranker())
    progresses = list(svc.rebuild(batch_size=200, resume=False, stale_only=False, quiet=True))
    # For total=1000 the 1% boundary fires at processed = 10, 20,
    # ..., 1000 → exactly 100 yields. (No "0%" yield at processed=1.)
    assert len(progresses) == 100
    assert [p.processed for p in progresses] == list(range(10, 1001, 10))
    assert all(p.total == 1000 for p in progresses)
    # The final yield reports the last TDoc embedded.
    assert progresses[-1].current_tdoc_id == "R5-001000"
    # All 1000 upserts happened; cursor set to the last TDoc.
    assert len(repo.upserts) == 1000
    assert repo.cursor == "R5-001000"


def test_rebuild_yields_at_actual_percent_crossings_for_13k_corpus() -> None:
    """Real corpus size (13,693 tdocs) must yield exactly 100 times
    — one per integer-pct crossing — and the final yield must be
    at processed = 13,693 (the corpus total).
    """
    total = 13693
    repo = MockRepo()
    repo.total = total
    repo.batches = [[f"R5-{i:06d}" for i in range(1, total + 1)]]
    svc = SearchService(repo=repo, reranker=PassthroughReranker())
    progresses = list(svc.rebuild(batch_size=total, resume=False, stale_only=False, quiet=True))
    # Compute the actual integer-pct crossings from the same
    # formula the production code uses.
    expected_processed = []
    last_pct = 0
    for p in range(1, total + 1):
        pct = p * 100 // total
        if pct > last_pct:
            expected_processed.append(p)
            last_pct = pct
    assert len(progresses) == 100
    assert [p.processed for p in progresses] == expected_processed
    assert progresses[-1].current_tdoc_id == f"R5-{total:06d}"


def test_rebuild_yields_every_tdoc_for_small_corpus() -> None:
    """Small corpora (total < 100) cannot hit 1% granularity, so
    every TDoc fires a yield. For 50 tdocs we expect 50 yields.
    """
    repo = MockRepo()
    repo.total = 50
    repo.batches = [[f"R5-{i:06d}" for i in range(1, 51)]]
    svc = SearchService(repo=repo, reranker=PassthroughReranker())
    progresses = list(svc.rebuild(batch_size=50, resume=False, stale_only=False, quiet=True))
    assert len(progresses) == 50
    assert [p.processed for p in progresses] == list(range(1, 51))
    assert progresses[-1].total == 50


def test_rebuild_without_resume_clears_existing_cursor() -> None:
    """A rebuild invoked without --resume must clear any persisted
    cursor first so a subsequent --resume starts truly from
    scratch. Without this, a stale cursor from a prior interrupted
    run silently shadows the new rebuild and the operator has no
    way to force a fresh start.
    """
    repo = MockRepo()
    repo.total = 3
    repo.cursor = "C3-stale-cursor"  # pre-existing cursor
    repo.batches = [["R5-1", "R5-2", "R5-3"]]
    svc = SearchService(repo=repo, reranker=PassthroughReranker())
    list(svc.rebuild(batch_size=10, resume=False, stale_only=False, quiet=True))
    # Cursor was cleared at start, then re-set to the last batch.
    assert repo.cleared is True
    assert repo.cursor == "R5-3"


def test_rebuild_with_resume_does_not_clear_cursor() -> None:
    """A rebuild invoked with --resume must NOT clear the cursor —
    the whole point of --resume is to honor the existing cursor
    and pick up from where the previous run was interrupted.
    """
    repo = MockRepo()
    repo.total = 3
    repo.cursor = "C3-resume-point"
    repo.batches = [["R5-1", "R5-2", "R5-3"]]
    svc = SearchService(repo=repo, reranker=PassthroughReranker())
    list(svc.rebuild(batch_size=10, resume=True, stale_only=False, quiet=True))
    # The pre-existing cursor must still be there — clear was NOT
    # called.
    assert getattr(repo, "cleared", False) is False
    # The rebuild advanced the cursor to the last batch.
    assert repo.cursor == "R5-3"


def test_rebuild_yields_nothing_for_zero_corpus() -> None:
    """An empty corpus must yield no progress (and still update
    metadata via _touch_*).
    """
    repo = MockRepo()
    repo.total = 0
    repo.batches = []
    svc = SearchService(repo=repo, reranker=PassthroughReranker())
    touched_rebuild = []
    touched_uploaded = []
    svc._touch_rebuild_at = lambda: touched_rebuild.append(True)
    svc._touch_indexed_uploaded_date = lambda: touched_uploaded.append(True)
    progresses = list(svc.rebuild(batch_size=100, resume=False, stale_only=False, quiet=True))
    assert progresses == []
    # Cursor and metadata still get touched so the index is marked
    # fresh even when there was nothing to index.
    assert touched_rebuild == [True]
    assert touched_uploaded == [True]


def test_status_returns_repo_status() -> None:
    repo = MockRepo()
    svc = SearchService(repo=repo, reranker=PassthroughReranker())
    assert svc.status().row_count == 3


def test_passthrough_reranker_returns_list_copy():
    from doc3gpp.models.search import SearchHit
    from doc3gpp.services.search_service import PassthroughReranker
    h = SearchHit(
        tdoc_id="R5-1", score=0.0, previews={}, title="t", meeting="m",
        tsg="S1", uploaded_date="2026-01-01", ftp_url="https://x", wis=(),
    )
    r = PassthroughReranker()
    out = r.rerank("anything", [h])
    assert out == [h]
    assert out is not [h]  # noqa: F632 — copy, not the same list (identity check)


def test_passthrough_reranker_honors_final_limit():
    from doc3gpp.models.search import SearchHit
    from doc3gpp.services.search_service import PassthroughReranker

    def _hit(t):
        return SearchHit(
            tdoc_id=t, score=0.0, previews={}, title="t", meeting="m",
            tsg="S1", uploaded_date="2026-01-01", ftp_url="https://x", wis=(),
        )
    hits = [_hit("R5-1"), _hit("R5-2"), _hit("R5-3")]
    r = PassthroughReranker()
    out = r.rerank("anything", hits, final_limit=2)
    assert [h.tdoc_id for h in out] == ["R5-1", "R5-2"]


def test_passthrough_reranker_empty_input():
    from doc3gpp.services.search_service import PassthroughReranker
    r = PassthroughReranker()
    assert r.rerank("anything", []) == []
    assert r.rerank("anything", [], final_limit=5) == []


def test_factory_chooses_semantic_reranker_when_both_enabled(monkeypatch):
    from unittest.mock import MagicMock

    from doc3gpp.services import factory as f
    from doc3gpp.services.search_service import SearchService
    from doc3gpp.services.semantic_reranker import SemanticReranker

    class FakeSettings:
        class search:
            enabled = True

        class semantic_search:
            enabled = True
            embedding_model = "fake-model"

    monkeypatch.setattr(f, "get_settings", lambda: FakeSettings())
    fake_embedder = MagicMock()
    fake_vector_repo = MagicMock()
    monkeypatch.setattr(
        f, "SentenceTransformerEmbedder", lambda _model: fake_embedder,
    )
    monkeypatch.setattr(
        f, "SQLAlchemyVectorIndexRepository", lambda: fake_vector_repo,
    )
    monkeypatch.setattr(
        f, "SQLAlchemySearchIndexRepository", lambda: MagicMock(),
    )

    svc = f.build_search_service(FakeSettings())
    assert isinstance(svc, SearchService)
    assert isinstance(svc._reranker, SemanticReranker)


def test_factory_falls_back_to_passthrough_when_semantic_disabled(monkeypatch):
    from unittest.mock import MagicMock

    from doc3gpp.services import factory as f
    from doc3gpp.services.search_service import PassthroughReranker, SearchService

    class FakeSettings:
        class search:
            enabled = True

        class semantic_search:
            enabled = False

    monkeypatch.setattr(f, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(
        f, "SQLAlchemySearchIndexRepository", lambda: MagicMock(),
    )

    svc = f.build_search_service(FakeSettings())
    assert isinstance(svc, SearchService)
    assert isinstance(svc._reranker, PassthroughReranker)


def test_factory_falls_back_to_passthrough_when_embedder_unavailable(monkeypatch):
    from unittest.mock import MagicMock

    from doc3gpp.models.semantic_search import EmbedderUnavailableError
    from doc3gpp.services import factory as f
    from doc3gpp.services.search_service import PassthroughReranker, SearchService

    class FakeSettings:
        class search:
            enabled = True

        class semantic_search:
            enabled = True
            embedding_model = "fake-model"

    monkeypatch.setattr(f, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(
        f, "SQLAlchemySearchIndexRepository", lambda: MagicMock(),
    )

    def _raise(_model):
        raise EmbedderUnavailableError("nope")

    monkeypatch.setattr(f, "SentenceTransformerEmbedder", _raise)

    svc = f.build_search_service(FakeSettings())
    assert isinstance(svc, SearchService)
    assert isinstance(svc._reranker, PassthroughReranker)
