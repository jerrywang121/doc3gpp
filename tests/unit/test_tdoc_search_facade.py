from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from doc3gpp.models.search import SearchFilters, SearchHit, SearchUnavailableError
from doc3gpp.models.semantic_search import (
    SemanticSearchHit,
    SemanticSearchUnavailableError,
)
from doc3gpp.models.tdoc import TDoc, TDocWithMeeting
from doc3gpp.models.unified_search import SearchMode, TDocSearchResult
from doc3gpp.services.tdoc_search_facade import TDocSearchFacade


class RecordingFts:
    def __init__(self, hits: list[SearchHit] | None = None) -> None:
        self.hits = hits or []
        self.calls: list[tuple[str, SearchFilters, int | None]] = []

    def search(
        self,
        query: str,
        filters: SearchFilters,
        *,
        snippet_tokens: int | None = None,
    ) -> list[SearchHit]:
        self.calls.append((query, filters, snippet_tokens))
        return self.hits


class RecordingSemantic:
    def __init__(self, hits: list[SemanticSearchHit] | None = None) -> None:
        self.hits = hits or []
        self.calls: list[dict[str, object]] = []

    def search(self, **kwargs: object) -> list[SemanticSearchHit]:
        self.calls.append(kwargs)
        return self.hits


class RecordingSource:
    def __init__(self, rows: list[TDocWithMeeting]) -> None:
        self.rows = rows
        self.calls: list[SearchFilters] = []

    def list_for_search(self, filters: SearchFilters) -> list[TDocWithMeeting]:
        self.calls.append(filters)
        return self.rows


def _settings(weight: float = 0.35) -> SimpleNamespace:
    return SimpleNamespace(semantic_search=SimpleNamespace(fts5_weight=weight))


def _search_hit() -> SearchHit:
    return SearchHit(
        tdoc_id="R5-000001",
        score=-2.5,
        previews={"title": "<<handover>>"},
        title="Handover CR",
        meeting="RAN#1",
        tsg="RAN",
        uploaded_date="2026-09-26",
        ftp_url="ran/doc.zip",
        wis="WI-1",
        type="CR",
        status="Agreed",
    )


def _semantic_hit(*, rrf_score: float = 0.25, distance: float = 0.12) -> SemanticSearchHit:
    return SemanticSearchHit(
        tdoc_id="R5-000001",
        rrf_score=rrf_score,
        hit=_search_hit(),
        min_chunk_distance=distance,
        best_chunk_id="R5-000001#0",
    )


def _facade(
    *,
    fts: RecordingFts | None,
    semantic: RecordingSemantic | None,
    source: RecordingSource | None = None,
) -> TDocSearchFacade:
    return TDocSearchFacade(
        fts5_service=fts,
        semantic_service=semantic,
        source_repo=source,
        settings=_settings(),
    )


def test_fts5_search_normalizes_and_flattens_with_snippet_override() -> None:
    fts = RecordingFts([_search_hit()])
    filters = SearchFilters(tsg="RAN", limit=7)
    facade = _facade(fts=fts, semantic=None)

    results = facade.search(
        text="  handover  ",
        semantic="  ",
        filters=filters,
        snippet_tokens=11,
    )

    assert fts.calls == [("handover", filters, 11)]
    assert results == [
        TDocSearchResult(
            tdoc_id="R5-000001",
            score=-2.5,
            search_mode=SearchMode.FTS5,
            previews={"title": "<<handover>>"},
            title="Handover CR",
            meeting="RAN#1",
            tsg="RAN",
            uploaded_date="2026-09-26",
            ftp_url="ran/doc.zip",
            wis="WI-1",
            type="CR",
            status="Agreed",
            best_chunk_id=None,
        )
    ]


def test_semantic_search_uses_semantic_query_and_distance_score() -> None:
    semantic = RecordingSemantic([_semantic_hit()])
    filters = SearchFilters(limit=4)
    facade = _facade(fts=None, semantic=semantic)

    results = facade.search(
        text=None,
        semantic="  natural language  ",
        filters=filters,
    )

    assert semantic.calls == [
        {
            "query": "natural language",
            "fts5_query": None,
            "filters": filters,
            "limit": 4,
            "fts5_weight": 0.35,
        }
    ]
    assert results == [
        TDocSearchResult(
            tdoc_id="R5-000001",
            score=0.12,
            search_mode=SearchMode.SEMANTIC,
            previews=None,
            title="Handover CR",
            meeting="RAN#1",
            tsg="RAN",
            uploaded_date="2026-09-26",
            ftp_url="ran/doc.zip",
            wis="WI-1",
            type="CR",
            status="Agreed",
            best_chunk_id="R5-000001#0",
        )
    ]


def test_hybrid_search_passes_both_queries_and_maps_rrf_score() -> None:
    fts = RecordingFts()
    semantic = RecordingSemantic([_semantic_hit(rrf_score=0.42)])
    filters = SearchFilters(limit=3)
    facade = _facade(fts=fts, semantic=semantic)

    results = facade.search(
        text="  title words ",
        semantic=" meaning words ",
        filters=filters,
    )

    assert semantic.calls == [
        {
            "query": "meaning words",
            "fts5_query": "title words",
            "filters": filters,
            "limit": 3,
            "fts5_weight": 0.35,
        }
    ]
    assert results == [
        TDocSearchResult(
            tdoc_id="R5-000001",
            score=0.42,
            search_mode=SearchMode.HYBRID,
            previews=None,
            title="Handover CR",
            meeting="RAN#1",
            tsg="RAN",
            uploaded_date="2026-09-26",
            ftp_url="ran/doc.zip",
            wis="WI-1",
            type="CR",
            status="Agreed",
            best_chunk_id="R5-000001#0",
        )
    ]


def test_filter_search_only_reads_source_repository() -> None:
    row = TDocWithMeeting(
        tdoc=TDoc(
            tdoc_id="R5-000002",
            title="Filtered CR",
            type="CR",
            status="Draft",
            uploaded_date=date(2026, 9, 26),
            related_wis="WI-2",
        ),
        meeting_name="RAN#2",
        meeting_tsg="RAN",
    )
    source = RecordingSource([row])
    filters = SearchFilters(tdoc_id="R5-000002")
    facade = _facade(fts=None, semantic=None, source=source)

    results = facade.search(text="", semantic=None, filters=filters)

    assert source.calls == [filters]
    assert results[0].score is None
    assert results[0].tsg == "RAN"
    assert results[0].search_mode is SearchMode.FILTER
    assert results[0].previews is None
    assert results[0].tdoc_id == "R5-000002"
    assert results[0].meeting == "RAN#2"
    assert results[0].tsg == "RAN"
    assert results[0].wis == "WI-2"
    assert results[0].best_chunk_id is None


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
        facade.search(text=text, semantic=semantic, filters=SearchFilters())
