"""Unified search facade for spec-document results."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from doc3gpp.models.search import SearchUnavailableError
from doc3gpp.models.semantic_search import SemanticSearchUnavailableError
from doc3gpp.models.spec_doc import (
    SpecDocChunk,
    SpecDocHit,
    SpecDocSearchFilters,
    SpecDocSemanticHit,
)
from doc3gpp.models.unified_search import SearchMode, SpecDocSearchResult


class SpecDocSearchFacade:
    """Dispatch spec-document searches and flatten their result DTOs."""

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
        filters: SpecDocSearchFilters,
        snippet_tokens: int | None = None,
    ) -> list[SpecDocSearchResult]:
        text_value = text.strip() if text and text.strip() else None
        semantic_value = semantic.strip() if semantic and semantic.strip() else None

        if text_value and semantic_value:
            if self._get_fts5() is None:
                raise SearchUnavailableError("FTS5 search is not available")
            hits = self._semantic_hits(
                query=semantic_value,
                fts5_query=text_value,
                filters=filters,
            )
            return self._flatten_semantic(hits, SearchMode.HYBRID, filters)

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
            hits = self._semantic_hits(
                query=semantic_value,
                fts5_query=None,
                filters=filters,
            )
            return self._flatten_semantic(hits, SearchMode.SEMANTIC, filters)

        if self._source is None:
            raise SearchUnavailableError(
                "spec-document source search is not available"
            )
        rows = self._source.list_for_search(filters)
        return [self._from_source(row) for row in rows]

    def _flatten_semantic(
        self,
        hits: list[SpecDocSemanticHit],
        mode: SearchMode,
        filters: SpecDocSearchFilters,
    ) -> list[SpecDocSearchResult]:
        results: list[SpecDocSearchResult] = []
        for hit in self._page(hits, filters):
            result = self._from_semantic(hit, mode)
            if result is not None:
                results.append(result)
        return results

    def _semantic_hits(
        self,
        *,
        query: str,
        fts5_query: str | None,
        filters: SpecDocSearchFilters,
    ) -> list[SpecDocSemanticHit]:
        semantic_service = self._get_semantic(require_fts5=fts5_query is not None)
        if semantic_service is None:
            raise SemanticSearchUnavailableError(
                "semantic search is not available"
            )
        expanded_limit = filters.limit + filters.offset
        semantic_filters = replace(filters, limit=expanded_limit, offset=0)
        return semantic_service.search(
            query=query,
            fts5_query=fts5_query,
            filters=semantic_filters,
            limit=expanded_limit,
            fts5_weight=self._settings.semantic_search.fts5_weight,
        )

    @staticmethod
    def _page(
        hits: list[SpecDocSemanticHit], filters: SpecDocSearchFilters
    ) -> list[SpecDocSemanticHit]:
        return hits[filters.offset : filters.offset + filters.limit]

    @staticmethod
    def _from_fts5(hit: SpecDocHit) -> SpecDocSearchResult:
        return SpecDocSearchResult(
            chunk_id=hit.chunk_id,
            score=hit.score,
            search_mode=SearchMode.FTS5,
            previews=hit.previews,
            spec_id=hit.spec_id,
            version=hit.version,
            release=hit.release,
            sections=hit.sections,
            tables=hit.tables,
            chunk_index=hit.chunk_index,
            text=hit.text,
        )

    @staticmethod
    def _from_semantic(
        hit: SpecDocSemanticHit, mode: SearchMode
    ) -> SpecDocSearchResult | None:
        metadata = hit.hit
        if metadata is None:
            return None
        return SpecDocSearchResult(
            chunk_id=hit.chunk_id,
            score=(
                hit.min_chunk_distance
                if mode is SearchMode.SEMANTIC
                else hit.rrf_score
            ),
            search_mode=mode,
            previews=None,
            spec_id=metadata.spec_id,
            version=metadata.version,
            release=metadata.release,
            sections=metadata.sections,
            tables=metadata.tables,
            chunk_index=metadata.chunk_index,
            text=metadata.text,
        )

    @staticmethod
    def _from_source(row: SpecDocChunk) -> SpecDocSearchResult:
        return SpecDocSearchResult(
            chunk_id=row.chunk_id,
            score=None,
            search_mode=SearchMode.FILTER,
            previews=None,
            spec_id=row.spec_id,
            version=row.version,
            release=row.release,
            sections=row.sections,
            tables=row.tables,
            chunk_index=row.chunk_index,
            text=row.text,
        )


__all__ = ["SpecDocSearchFacade"]
