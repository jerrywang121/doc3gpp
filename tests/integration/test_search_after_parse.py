"""Auto-index hook integration with ``TDocCrService``.

Verifies:

1. ``TDocCrService._index_after_parse(tdoc_id)`` calls
   ``SearchService.upsert_for_tdoc(tdoc_id)`` when both the search
   service is wired AND ``Settings.search.auto_index_on_parse`` is
   True.
2. ``Settings.search.auto_index_on_parse = False`` skips the hook.
3. ``_search_service = None`` skips the hook silently.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from doc3gpp.models.search import SearchFilters
from doc3gpp.repository.protocols import SearchIndexRepository
from doc3gpp.services.factory import build_tdoc_cr_service
from doc3gpp.services.search_service import PassthroughReranker, SearchService
from doc3gpp.settings.schema import SearchSettings, Settings


class CapturingRepo(SearchIndexRepository):
    def __init__(self) -> None:
        self.upserts: list[str] = []

    def upsert(self, tdoc_id: str) -> None:
        self.upserts.append(tdoc_id)

    def remove(self, tdoc_id: str) -> None: ...
    def search(
        self, query: str, filters: SearchFilters,
    ) -> list[Any]: return []
    def rebuild_batch(
        self, batch_size: int, after_id: str | None, stale_only: bool,
    ) -> Iterable[list[str]]: return iter([])
    def count_tdocs_to_index(self, stale_only: bool) -> int: return 0
    def get_resume_cursor(self) -> str | None: return None
    def set_resume_cursor(self, tdoc_id: str) -> None: ...
    def status(self) -> Any: ...


def test_hook_fires_after_parse(sqlite_env) -> None:
    repo = CapturingRepo()
    svc = SearchService(repo=repo, reranker=PassthroughReranker())
    service = build_tdoc_cr_service()
    service._search_service = svc  # type: ignore[attr-defined]
    service._settings = Settings()  # type: ignore[attr-defined]
    service._index_after_parse("R5-1234567")  # type: ignore[attr-defined]
    assert repo.upserts == ["R5-1234567"]


def test_hook_skips_when_disabled(sqlite_env) -> None:
    repo = CapturingRepo()
    svc = SearchService(repo=repo, reranker=PassthroughReranker())
    service = build_tdoc_cr_service()
    service._search_service = svc  # type: ignore[attr-defined]
    service._settings = Settings(  # type: ignore[attr-defined]
        search=SearchSettings(auto_index_on_parse=False),
    )
    service._index_after_parse("R5-1234567")  # type: ignore[attr-defined]
    assert repo.upserts == []


def test_hook_skips_when_service_none(sqlite_env) -> None:
    service = build_tdoc_cr_service()
    service._search_service = None  # type: ignore[attr-defined]
    # Should not raise even when no service is wired.
    service._index_after_parse("R5-1234567")  # type: ignore[attr-defined]
