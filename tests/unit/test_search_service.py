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

    def count_tdocs_to_index(self, stale_only: bool) -> int:
        return 3

    def get_resume_cursor(self) -> str | None:
        return self.cursor

    def set_resume_cursor(self, tdoc_id: str) -> None:
        self.cursor = tdoc_id

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
        self, query: str, hits: list[SearchHit],
    ) -> list[SearchHit]:
        self.queries.append(query)
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


def test_search_runs_reranker() -> None:
    repo = MockRepo()
    reranker = StubReranker()
    svc = SearchService(repo=repo, reranker=reranker)
    hits = svc.search("anything", SearchFilters(limit=5))
    assert len(hits) == 1
    assert reranker.invocations == 1
    assert reranker.queries == ["anything"]


def test_rebuild_yields_progress_per_batch() -> None:
    repo = MockRepo()
    repo.batches = [
        ["R5-000001", "R5-000002", "R5-000003"],
        ["R5-000004", "R5-000005"],
    ]
    svc = SearchService(repo=repo, reranker=PassthroughReranker())
    progresses = list(svc.rebuild(batch_size=3, resume=False, stale_only=False, quiet=True))
    assert len(progresses) == 2
    assert progresses[0].processed == 3
    assert progresses[0].total == 3
    assert progresses[1].processed == 5
    assert repo.upserts == [
        "R5-000001",
        "R5-000002",
        "R5-000003",
        "R5-000004",
        "R5-000005",
    ]
    assert repo.cursor == "R5-000005"


def test_status_returns_repo_status() -> None:
    repo = MockRepo()
    svc = SearchService(repo=repo, reranker=PassthroughReranker())
    assert svc.status().row_count == 3
