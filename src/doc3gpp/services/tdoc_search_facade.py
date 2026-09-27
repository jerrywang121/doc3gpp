"""Unified search facade for TDoc results."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from doc3gpp.models.search import SearchFilters, SearchHit, SearchUnavailableError
from doc3gpp.models.semantic_search import (
    SemanticSearchHit,
    SemanticSearchUnavailableError,
)
from doc3gpp.models.tdoc import TDocWithMeeting
from doc3gpp.models.unified_search import SearchMode, TDocSearchResult


class TDocSearchFacade:
    """Dispatch TDoc searches and expose flattened public results."""

    def __init__(
        self,
        fts5_service: Any | None,
        semantic_service: Any | None,
        source_repo: Any,
        settings: Any,
        fts5_factory: Callable[[], Any | None] | None = None,
        semantic_factory: Callable[[bool], Any | None] | None = None,
    ) -> None:
        self._fts5 = fts5_service
        self._semantic = semantic_service
        self._source = source_repo
        self._settings = settings
        self._fts5_factory = fts5_factory
        self._semantic_factory = semantic_factory

    def _get_fts5(self) -> Any | None:
        if self._fts5 is None and self._fts5_factory is not None:
            self._fts5 = self._fts5_factory()
            self._fts5_factory = None
        return self._fts5

    def _get_semantic(self, *, require_fts5: bool) -> Any | None:
        if self._semantic is None and self._semantic_factory is not None:
            self._semantic = self._semantic_factory(require_fts5)
            self._semantic_factory = None
        return self._semantic

    def search(
        self,
        *,
        text: str | None,
        semantic: str | None,
        filters: SearchFilters,
        snippet_tokens: int | None = None,
    ) -> list[TDocSearchResult]:
        text_value = text.strip() if text and text.strip() else None
        semantic_value = semantic.strip() if semantic and semantic.strip() else None

        if text_value and semantic_value:
            if self._get_fts5() is None:
                raise SearchUnavailableError("FTS5 search is not available")
            semantic_service = self._get_semantic(require_fts5=True)
            if semantic_service is None:
                raise SemanticSearchUnavailableError(
                    "semantic search is not available"
                )
            hits = semantic_service.search(
                query=semantic_value,
                fts5_query=text_value,
                filters=filters,
                limit=filters.limit,
                fts5_weight=self._settings.semantic_search.fts5_weight,
            )
            return [self._from_semantic(hit, SearchMode.HYBRID) for hit in hits]

        if text_value:
            fts5_service = self._get_fts5()
            if fts5_service is None:
                raise SearchUnavailableError("FTS5 search is not available")
            hits = fts5_service.search(
                text_value,
                filters,
                snippet_tokens=snippet_tokens,
            )
            return [self._from_fts5(hit) for hit in hits]

        if semantic_value:
            semantic_service = self._get_semantic(require_fts5=False)
            if semantic_service is None:
                raise SemanticSearchUnavailableError(
                    "semantic search is not available"
                )
            hits = semantic_service.search(
                query=semantic_value,
                fts5_query=None,
                filters=filters,
                limit=filters.limit,
                fts5_weight=self._settings.semantic_search.fts5_weight,
            )
            return [self._from_semantic(hit, SearchMode.SEMANTIC) for hit in hits]

        if self._source is None:
            raise SearchUnavailableError("TDoc source search is not available")
        rows = self._source.list_for_search(filters)
        return [self._from_source(row) for row in rows]

    def status(self):
        """Return the FTS5 status used for the CLI stale-index hint."""
        fts5_service = self._get_fts5()
        if fts5_service is None:
            return None
        return fts5_service.status()

    @staticmethod
    def _from_fts5(hit: SearchHit) -> TDocSearchResult:
        return TDocSearchResult(
            tdoc_id=hit.tdoc_id,
            score=hit.score,
            search_mode=SearchMode.FTS5,
            previews=hit.previews,
            title=hit.title,
            meeting=hit.meeting,
            tsg=hit.tsg,
            uploaded_date=hit.uploaded_date,
            ftp_url=hit.ftp_url,
            wis=hit.wis,
            type=hit.type,
            status=hit.status,
            best_chunk_id=None,
        )

    @staticmethod
    def _from_semantic(
        hit: SemanticSearchHit, mode: SearchMode
    ) -> TDocSearchResult:
        metadata = hit.hit
        return TDocSearchResult(
            tdoc_id=hit.tdoc_id,
            score=(
                hit.min_chunk_distance
                if mode is SearchMode.SEMANTIC
                else hit.rrf_score
            ),
            search_mode=mode,
            previews=None,
            title=metadata.title if metadata is not None else "",
            meeting=metadata.meeting if metadata is not None else None,
            tsg=metadata.tsg if metadata is not None else None,
            uploaded_date=(
                metadata.uploaded_date if metadata is not None else None
            ),
            ftp_url=metadata.ftp_url if metadata is not None else None,
            wis=metadata.wis if metadata is not None else None,
            type=metadata.type if metadata is not None else None,
            status=metadata.status if metadata is not None else None,
            best_chunk_id=hit.best_chunk_id,
        )

    @staticmethod
    def _from_source(row: TDocWithMeeting) -> TDocSearchResult:
        tdoc = row.tdoc
        return TDocSearchResult(
            tdoc_id=tdoc.tdoc_id,
            score=None,
            search_mode=SearchMode.FILTER,
            previews=None,
            title=tdoc.title or "",
            meeting=row.meeting_name,
            tsg=row.meeting_tsg,
            uploaded_date=tdoc.uploaded_date,
            ftp_url=tdoc.ftp_url,
            wis=tdoc.related_wis,
            type=tdoc.type,
            status=tdoc.status,
            best_chunk_id=None,
        )


__all__ = ["TDocSearchFacade"]
