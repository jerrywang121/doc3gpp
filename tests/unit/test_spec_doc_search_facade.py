from __future__ import annotations

from types import SimpleNamespace

import pytest

from doc3gpp.models.search import SearchUnavailableError
from doc3gpp.models.semantic_search import SemanticSearchUnavailableError
from doc3gpp.models.spec_doc import (
    SpecDocChunk,
    SpecDocHit,
    SpecDocSearchFilters,
    SpecDocSemanticHit,
)
from doc3gpp.models.unified_search import SearchMode, SpecDocSearchResult
from doc3gpp.services.spec_doc_search_facade import SpecDocSearchFacade


class RecordingFts:
    def __init__(self, hits: list[SpecDocHit] | None = None) -> None:
        self.hits = hits or []
        self.calls: list[tuple[str, SpecDocSearchFilters, int | None]] = []

    def search(
        self,
        query: str,
        filters: SpecDocSearchFilters,
        *,
        snippet_tokens: int | None = None,
    ) -> list[SpecDocHit]:
        self.calls.append((query, filters, snippet_tokens))
        return self.hits


class RecordingSemantic:
    def __init__(self, hits: list[SpecDocSemanticHit] | None = None) -> None:
        self.hits = hits or []
        self.calls: list[dict[str, object]] = []

    def search(self, **kwargs: object) -> list[SpecDocSemanticHit]:
        self.calls.append(kwargs)
        return self.hits


class RecordingSource:
    def __init__(self, rows: list[SpecDocChunk]) -> None:
        self.rows = rows
        self.calls: list[SpecDocSearchFilters] = []

    def list_for_search(self, filters: SpecDocSearchFilters) -> list[SpecDocChunk]:
        self.calls.append(filters)
        return self.rows


def _settings(weight: float = 0.65) -> SimpleNamespace:
    return SimpleNamespace(semantic_search=SimpleNamespace(fts5_weight=weight))


def _hit(
    *,
    chunk_id: str = "38.331@18.5.0#0",
    score: float = -3.0,
    text: str = "handover text",
) -> SpecDocHit:
    return SpecDocHit(
        chunk_id=chunk_id,
        spec_id="38.331",
        version="18.5.0",
        release="Rel-18",
        sections="5.1 Handover",
        tables="Table 1 UE",
        chunk_index=int(chunk_id.rsplit("#", 1)[1]),
        text=text,
        score=score,
        previews={"text": "<<handover>>"},
    )


def _semantic_hit(
    index: int,
    *,
    rrf_score: float = 0.2,
    distance: float = 0.1,
    include_hit: bool = True,
) -> SpecDocSemanticHit:
    return SpecDocSemanticHit(
        chunk_id=f"38.331@18.5.0#{index}",
        rrf_score=rrf_score,
        hit=_hit(chunk_id=f"38.331@18.5.0#{index}") if include_hit else None,
        min_chunk_distance=distance,
    )


def _facade(
    *,
    fts: RecordingFts | None,
    semantic: RecordingSemantic | None,
    source: RecordingSource | None = None,
) -> SpecDocSearchFacade:
    return SpecDocSearchFacade(
        fts5_service=fts,
        semantic_service=semantic,
        source_repo=source,
        settings=_settings(),
    )


def test_fts5_search_normalizes_and_flattens_with_snippet_override() -> None:
    fts = RecordingFts([_hit()])
    filters = SpecDocSearchFilters(spec_id="38.331", limit=5, offset=2)
    facade = _facade(fts=fts, semantic=None)

    results = facade.search(
        text="  handover  ",
        semantic=" ",
        filters=filters,
        snippet_tokens=9,
    )

    assert fts.calls == [("handover", filters, 9)]
    assert results == [
        SpecDocSearchResult(
            chunk_id="38.331@18.5.0#0",
            score=-3.0,
            search_mode=SearchMode.FTS5,
            previews={"text": "<<handover>>"},
            spec_id="38.331",
            version="18.5.0",
            release="Rel-18",
            sections="5.1 Handover",
            tables="Table 1 UE",
            chunk_index=0,
            text="handover text",
        )
    ]


def test_semantic_search_requests_unpaged_window_then_slices_distance_results() -> None:
    semantic = RecordingSemantic(
        [_semantic_hit(0, distance=0.01), _semantic_hit(1, distance=0.02), _semantic_hit(2, distance=0.03)]
    )
    filters = SpecDocSearchFilters(limit=2, offset=1)
    facade = _facade(fts=None, semantic=semantic)

    results = facade.search(text=None, semantic="  handover meaning ", filters=filters)

    assert semantic.calls == [
        {
            "query": "handover meaning",
            "fts5_query": None,
            "filters": SpecDocSearchFilters(limit=3, offset=0),
            "limit": 3,
            "fts5_weight": 0.65,
        }
    ]
    assert [result.chunk_id for result in results] == [
        "38.331@18.5.0#1",
        "38.331@18.5.0#2",
    ]
    assert results == [
        SpecDocSearchResult(
            chunk_id="38.331@18.5.0#1",
            score=0.02,
            search_mode=SearchMode.SEMANTIC,
            previews=None,
            spec_id="38.331",
            version="18.5.0",
            release="Rel-18",
            sections="5.1 Handover",
            tables="Table 1 UE",
            chunk_index=1,
            text="handover text",
        ),
        SpecDocSearchResult(
            chunk_id="38.331@18.5.0#2",
            score=0.03,
            search_mode=SearchMode.SEMANTIC,
            previews=None,
            spec_id="38.331",
            version="18.5.0",
            release="Rel-18",
            sections="5.1 Handover",
            tables="Table 1 UE",
            chunk_index=2,
            text="handover text",
        ),
    ]


def test_hybrid_search_uses_same_pagination_window_and_rrf_score() -> None:
    semantic = RecordingSemantic(
        [_semantic_hit(0, rrf_score=0.1), _semantic_hit(1, rrf_score=0.2), _semantic_hit(2, rrf_score=0.3)]
    )
    filters = SpecDocSearchFilters(limit=2, offset=1)
    facade = _facade(fts=RecordingFts(), semantic=semantic)

    results = facade.search(
        text=" title ",
        semantic=" meaning ",
        filters=filters,
    )

    assert semantic.calls == [
        {
            "query": "meaning",
            "fts5_query": "title",
            "filters": SpecDocSearchFilters(limit=3, offset=0),
            "limit": 3,
            "fts5_weight": 0.65,
        }
    ]
    assert results == [
        SpecDocSearchResult(
            chunk_id="38.331@18.5.0#1",
            score=0.2,
            search_mode=SearchMode.HYBRID,
            previews=None,
            spec_id="38.331",
            version="18.5.0",
            release="Rel-18",
            sections="5.1 Handover",
            tables="Table 1 UE",
            chunk_index=1,
            text="handover text",
        ),
        SpecDocSearchResult(
            chunk_id="38.331@18.5.0#2",
            score=0.3,
            search_mode=SearchMode.HYBRID,
            previews=None,
            spec_id="38.331",
            version="18.5.0",
            release="Rel-18",
            sections="5.1 Handover",
            tables="Table 1 UE",
            chunk_index=2,
            text="handover text",
        ),
    ]


def test_missing_semantic_hit_metadata_is_discarded() -> None:
    semantic = RecordingSemantic([_semantic_hit(0, include_hit=False)])
    facade = _facade(fts=None, semantic=semantic)

    results = facade.search(
        text=None,
        semantic="query",
        filters=SpecDocSearchFilters(),
    )

    assert results == []


def test_semantic_metadata_lookup_failure_discards_vector_hit() -> None:
    class FailingSource(RecordingSource):
        def get_chunks_by_ids(self, _chunk_ids: list[str]) -> dict[str, SpecDocChunk]:
            raise RuntimeError("specdata unavailable")

    semantic = RecordingSemantic([_semantic_hit(0, include_hit=False)])
    facade = SpecDocSearchFacade(
        fts5_service=None,
        semantic_service=semantic,
        source_repo=FailingSource([]),
        settings=_settings(),
    )

    assert facade.search(
        text=None,
        semantic="query",
        filters=SpecDocSearchFilters(),
    ) == []


def test_filter_search_only_reads_source_repository() -> None:
    row = SpecDocChunk(
        file_order=0,
        source_file="a.docx",
        sections="5.1 Handover",
        tables="Table 1 UE",
        text="handover text",
        chunk_id="38.331@18.5.0#0",
        spec_id="38.331",
        version="18.5.0",
        release="Rel-18",
        chunk_index=0,
    )
    source = RecordingSource([row])
    filters = SpecDocSearchFilters(spec_id="38.331")
    facade = _facade(fts=None, semantic=None, source=source)

    result = facade.search(text=None, semantic="", filters=filters)[0]

    assert source.calls == [filters]
    assert result.score is None
    assert result.search_mode is SearchMode.FILTER
    assert result.previews is None
    assert result.chunk_id == row.chunk_id


@pytest.mark.parametrize(
    ("text", "semantic", "fts", "semantic_service", "error"),
    [
        ("query", None, None, RecordingSemantic(), SearchUnavailableError),
        (None, "query", None, None, SemanticSearchUnavailableError),
        ("query", "query", None, RecordingSemantic(), SearchUnavailableError),
        ("query", "query", RecordingFts(), None, SemanticSearchUnavailableError),
    ],
)
def test_requested_mode_requires_available_service(
    text: str | None,
    semantic: str | None,
    fts: RecordingFts | None,
    semantic_service: RecordingSemantic | None,
    error: type[Exception],
) -> None:
    facade = _facade(fts=fts, semantic=semantic_service)

    with pytest.raises(error):
        facade.search(
            text=text,
            semantic=semantic,
            filters=SpecDocSearchFilters(),
        )
