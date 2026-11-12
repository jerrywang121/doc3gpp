"""Search orchestration for ETSI LISTSERV archives (filter-only and FTS5, with Task 8 semantic seam)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol

from doc3gpp.models.listserv import (
    ListservFilters,
    ListservRecord,
    ListservSearchResult,
)
from doc3gpp.models.listserv_payloads import (
    effective_record_tdocs,
    indexable_fields,
    indexable_text,
    listserv_record_payload,
    listserv_search_payload,
    record_to_search_result,
    strip_urls,
)
from doc3gpp.models.search import SearchUnavailableError
from doc3gpp.models.semantic_search import SemanticSearchUnavailableError
from doc3gpp.repository.listserv import ListservRepository
from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import Settings

_UNSET = object()
_DEFAULT_FILTERS = ListservFilters()


class ListservFtsRepository(Protocol):
    """Protocol for the LISTSERV FTS5 index/search repository."""

    def upsert(self, record_or_url: ListservRecord | str) -> None: ...

    def upsert_many(
        self, records_or_urls: Iterable[ListservRecord | str]
    ) -> None: ...

    def index_records(self, records: Iterable[ListservRecord]) -> None: ...

    def remove(self, url: str) -> None: ...

    def rebuild(self) -> int: ...

    def search(
        self,
        query: str,
        filters: ListservFilters = _DEFAULT_FILTERS,
        *,
        snippet_tokens: int | None = None,
    ) -> list[ListservSearchResult]: ...


class ListservSearchService:
    """Unified search service for ETSI LISTSERV messages."""

    def __init__(
        self,
        repository: ListservRepository,
        fts_repository: ListservFtsRepository | None | object = _UNSET,
        *,
        fts_factory: Callable[[], ListservFtsRepository | None] | None = None,
        semantic_service: Any | None | object = _UNSET,
        semantic_factory: Callable[[], Any | None] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._repository = repository
        self._fts_repository = fts_repository
        self._fts_factory = fts_factory
        self._semantic_service = semantic_service
        self._semantic_factory = semantic_factory
        self._owns_semantic_service = (
            semantic_service is _UNSET and semantic_factory is not None
        )
        self._semantic_service_closed = False
        self._settings = settings

    def _resolve_settings(self) -> Settings:
        return self._settings if self._settings is not None else get_settings()

    def _get_fts_repository(self) -> ListservFtsRepository | None:
        settings = self._resolve_settings()
        if not settings.search.enabled:
            return None
        if self._fts_repository is _UNSET:
            if self._fts_factory is not None:
                self._fts_repository = self._fts_factory()
            else:
                self._fts_repository = None
        return self._fts_repository  # type: ignore[return-value]

    def _get_semantic_service(self) -> Any | None:
        settings = self._resolve_settings()
        if not settings.semantic_search.enabled:
            return None
        if self._semantic_service is _UNSET:
            if self._semantic_factory is not None:
                self._semantic_service = self._semantic_factory()
            else:
                self._semantic_service = None
            self._semantic_service_closed = False
        return self._semantic_service

    def close(self) -> None:
        """Close a lazily created semantic service owned by this facade."""
        if self._semantic_service_closed:
            return
        self._semantic_service_closed = True
        if not self._owns_semantic_service or self._semantic_service is _UNSET:
            return
        close_fn = getattr(self._semantic_service, "close", None)
        if callable(close_fn):
            close_fn()

    def search(
        self,
        *,
        text: str | None = None,
        semantic: str | None = None,
        filters: ListservFilters = _DEFAULT_FILTERS,
    ) -> list[ListservSearchResult]:
        """Search stored LISTSERV messages without network access.

        Dispatches explicitly between filter-only, FTS5-only, semantic-only, and
        hybrid (FTS5 + vector RRF) after normalizing whitespace-only inputs to absent.
        """
        text_value = text.strip() if text and text.strip() else None
        semantic_value = (
            semantic.strip() if semantic and semantic.strip() else None
        )

        if text_value and semantic_value:
            semantic_service = self._get_semantic_service()
            if semantic_service is None:
                raise SemanticSearchUnavailableError(
                    "semantic search is not available"
                )
            fts_repo = self._get_fts_repository()
            if fts_repo is None:
                raise SearchUnavailableError("FTS5 search is not available")
            return semantic_service.search(
                query=semantic_value,
                filters=filters,
                fts_query=text_value,
                fts_repository=fts_repo,
            )

        if semantic_value:
            semantic_service = self._get_semantic_service()
            if semantic_service is None:
                raise SemanticSearchUnavailableError(
                    "semantic search is not available"
                )
            return semantic_service.search(
                query=semantic_value,
                filters=filters,
            )

        if text_value:
            fts_repo = self._get_fts_repository()
            if fts_repo is None:
                raise SearchUnavailableError("FTS5 search is not available")
            return fts_repo.search(text_value, filters)

        records = self._repository.select(filters)
        return [
            record_to_search_result(
                record,
                score=None,
                snippet=None,
                matched_source=None,
            )
            for record in records
        ]

    def upsert(self, record_or_url: ListservRecord | str) -> None:
        """Upsert one record or URL into the FTS5 index."""
        fts_repo = self._get_fts_repository()
        if fts_repo is None:
            raise SearchUnavailableError("FTS5 search is not available")
        fts_repo.upsert(record_or_url)

    def upsert_many(
        self, records_or_urls: Iterable[ListservRecord | str]
    ) -> None:
        """Upsert multiple records or URLs into the FTS5 index."""
        fts_repo = self._get_fts_repository()
        if fts_repo is None:
            raise SearchUnavailableError("FTS5 search is not available")
        fts_repo.upsert_many(records_or_urls)

    def index(
        self,
        records_or_urls: ListservRecord | str | Iterable[ListservRecord | str],
    ) -> None:
        """Index one or more records or canonical URLs into FTS5."""
        if isinstance(records_or_urls, (ListservRecord, str)):
            self.upsert(records_or_urls)
        else:
            self.upsert_many(records_or_urls)

    def rebuild_fts(self) -> int:
        """Rebuild the FTS5 index from stored SQL records."""
        fts_repo = self._get_fts_repository()
        if fts_repo is None:
            raise SearchUnavailableError("FTS5 search is not available")
        return fts_repo.rebuild()


__all__ = [
    "ListservFtsRepository",
    "ListservSearchService",
    "effective_record_tdocs",
    "indexable_fields",
    "indexable_text",
    "listserv_record_payload",
    "listserv_search_payload",
    "record_to_search_result",
    "strip_urls",
]
