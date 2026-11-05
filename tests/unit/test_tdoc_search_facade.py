from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from doc3gpp.models.search import (
    MatchedChunk,
    MatchedDocument,
    SearchFilters,
    SearchHit,
    SearchUnavailableError,
)
from doc3gpp.models.semantic_search import (
    SemanticDocumentHit,
    SemanticSearchHit,
    SemanticSearchUnavailableError,
)
from doc3gpp.models.tdoc import TDoc, TDocWithMeeting
from doc3gpp.models.tdoc_identity import document_id_for
from doc3gpp.models.unified_search import SearchMode, TDocSearchResult
from doc3gpp.services.tdoc_search_facade import TDocSearchFacade
from doc3gpp.storage.db.migrate import create_schema


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
    def __init__(
        self,
        rows: list[TDocWithMeeting],
        documents: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        self.rows = rows
        self.documents = documents or {}
        self.calls: list[SearchFilters] = []
        self.document_source_calls: list[frozenset[str]] = []

    def list_for_search(self, filters: SearchFilters) -> list[TDocWithMeeting]:
        self.calls.append(filters)
        return self.rows

    def document_sources_by_id(
        self, document_ids: frozenset[str],
    ) -> dict[str, tuple[str, str]]:
        self.document_source_calls.append(frozenset(document_ids))
        return {
            document_id: self.documents[document_id]
            for document_id in document_ids
            if document_id in self.documents
        }


def _settings(weight: float = 0.35) -> SimpleNamespace:
    return SimpleNamespace(semantic_search=SimpleNamespace(fts5_weight=weight))


def _matched_documents() -> tuple[MatchedDocument, ...]:
    return (
        MatchedDocument(
            document_id="doc-main",
            ftp_url="https://www.3gpp.org/ftp/R5-000001.zip",
            source_file="docs/main.docx",
            score=-4.2,
            previews={"content_text": "<<handover>> procedure"},
        ),
        MatchedDocument(
            document_id="doc-appendix",
            ftp_url="https://www.3gpp.org/ftp/R5-000001.zip",
            source_file="support/appendix.docx",
            score=-2.1,
            previews={"content_text": "<<handover>> appendix"},
        ),
    )


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
        matched_documents=_matched_documents(),
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
            matched_documents=_matched_documents(),
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
    assert results[0].matched_documents == ()


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
            matched_documents=_matched_documents(),
        )
    ]


def test_vector_only_hybrid_hit_serializes_empty_matched_documents() -> None:
    from dataclasses import replace

    from doc3gpp.models.unified_search import tdoc_search_result_to_dict

    vector_only_hit = SemanticSearchHit(
        tdoc_id="R5-000001",
        rrf_score=0.31,
        hit=replace(_search_hit(), matched_documents=()),
        rank_fts5=None,
        rank_vec=0,
        min_chunk_distance=0.12,
        best_chunk_id="R5-000001#0",
    )
    facade = _facade(
        fts=RecordingFts(),
        semantic=RecordingSemantic([vector_only_hit]),
    )

    results = facade.search(
        text="no FTS hit",
        semantic="vector match",
        filters=SearchFilters(limit=1),
    )

    assert tdoc_search_result_to_dict(results[0])["matched_documents"] == []


def test_chunk_hit_provenance_resolves_through_source_repository(sqlite_env) -> None:
    create_schema()
    chunk_id = "R5-000001#doc-main#hash-main#0"
    semantic = RecordingSemantic([
        SemanticSearchHit(
            tdoc_id="R5-000001",
            rrf_score=0.25,
            hit=_search_hit(),
            min_chunk_distance=0.1,
            best_chunk_id=chunk_id,
            document_hits=(
                SemanticDocumentHit(
                    document_id="doc-main", chunk_id=chunk_id, distance=0.1,
                ),
            ),
        ),
    ])
    source = RecordingSource(
        [], documents={"doc-main": ("ran/doc.zip", "docs/main.docx")},
    )
    facade = _facade(fts=None, semantic=semantic, source=source)

    results = facade.search(text=None, semantic="meaning", filters=SearchFilters(limit=3))

    assert source.document_source_calls == [frozenset({"doc-main"})]
    assert results[0].matched_documents == (
        MatchedDocument(
            document_id="doc-main",
            ftp_url="ran/doc.zip",
            source_file="docs/main.docx",
            score=0.1,
            previews={},
        ),
    )

def test_hybrid_unions_distinct_fts_and_vector_presentations() -> None:
    fts_url = "ran/fts.zip"
    fts_source = "Annexes/Radio.PPTX"
    fts_document_id = document_id_for(fts_url, fts_source)
    fts_document = MatchedDocument(
        document_id=fts_document_id,
        ftp_url=fts_url,
        source_file=fts_source,
        score=-2.0,
        previews={"content_text": "<<radio>>"},
        matched_chunk=MatchedChunk(
            chunk_id=f"R5-000001#{fts_document_id}#fts-hash#0",
            chunk_index=0,
            text="Radio presentation content",
        ),
    )

    vector_url = "ran/vector.zip"
    vector_source = "legacy/Deck.ppt"
    vector_document_id = document_id_for(vector_url, vector_source)
    vector_chunk = MatchedChunk(
        chunk_id=f"R5-000001#{vector_document_id}#vector-hash#0",
        chunk_index=0,
        text="Legacy deck content",
    )
    semantic_hit = SemanticSearchHit(
        tdoc_id="R5-000001",
        rrf_score=0.4,
        hit=replace(_search_hit(), matched_documents=(fts_document,)),
        min_chunk_distance=0.1,
        best_chunk_id="R5-000001#0",
        document_hits=(
            SemanticDocumentHit(
                document_id=vector_document_id,
                chunk_id=vector_chunk.chunk_id,
                distance=0.1,
                matched_chunk=vector_chunk,
            ),
        ),
    )
    source = RecordingSource(
        [], documents={vector_document_id: (vector_url, vector_source)},
    )
    facade = _facade(
        fts=RecordingFts(),
        semantic=RecordingSemantic([semantic_hit]),
        source=source,
    )

    results = facade.search(
        text="radio",
        semantic="deck",
        filters=SearchFilters(limit=5),
    )

    matched = results[0].matched_documents
    assert [document.document_id for document in matched] == [
        vector_document_id,
        fts_document_id,
    ]
    assert [document.source_file for document in matched] == [
        vector_source,
        fts_source,
    ]
    assert len({document.document_id for document in matched}) == 2
    assert matched[1].previews == fts_document.previews


@pytest.mark.parametrize(
    ("vector_text", "expected_text"),
    [
        ("verified vector snapshot", "verified vector snapshot"),
        (None, "verified FTS snapshot"),
    ],
)
def test_hybrid_same_document_preserves_preview_and_best_chunk(
    vector_text: str | None,
    expected_text: str,
) -> None:
    ftp_url = "ran/shared.zip"
    source_file = "Annexes/Radio.PPTX"
    document_id = document_id_for(ftp_url, source_file)
    fts_chunk = MatchedChunk(
        chunk_id=f"R5-000001#{document_id}#fts-hash#0",
        chunk_index=0,
        text="verified FTS snapshot",
    )
    fts_previews = {"content_text": "<<radio>> procedure"}
    fts_document = MatchedDocument(
        document_id=document_id,
        ftp_url=ftp_url,
        source_file=source_file,
        score=-2.0,
        previews=fts_previews,
        matched_chunk=fts_chunk,
    )
    vector_chunk_id = f"R5-000001#{document_id}#vector-hash#1"
    vector_chunk = (
        MatchedChunk(
            chunk_id=vector_chunk_id,
            chunk_index=1,
            text=vector_text,
        )
        if vector_text is not None
        else None
    )
    semantic_hit = SemanticSearchHit(
        tdoc_id="R5-000001",
        rrf_score=0.4,
        hit=replace(_search_hit(), matched_documents=(fts_document,)),
        min_chunk_distance=0.05,
        best_chunk_id="R5-000001#1",
        document_hits=(
            SemanticDocumentHit(
                document_id=document_id,
                chunk_id=vector_chunk_id,
                distance=0.05,
                matched_chunk=vector_chunk,
            ),
        ),
    )
    source = RecordingSource(
        [], documents={document_id: (ftp_url, source_file)},
    )
    facade = _facade(
        fts=RecordingFts(),
        semantic=RecordingSemantic([semantic_hit]),
        source=source,
    )

    result = facade.search(
        text="radio",
        semantic="deck",
        filters=SearchFilters(limit=5),
    )[0]

    assert len(result.matched_documents) == 1
    matched = result.matched_documents[0]
    assert matched.matched_chunk is not None
    assert matched.matched_chunk.text == expected_text
    assert matched.previews == fts_previews


def test_hybrid_same_member_in_different_zip_urls() -> None:
    source_file = "Annexes/Radio.PPTX"
    fts_url = "ran/older.zip"
    vector_url = "ran/newer.zip"
    fts_document_id = document_id_for(fts_url, source_file)
    vector_document_id = document_id_for(vector_url, source_file)
    assert fts_document_id != vector_document_id

    fts_document = MatchedDocument(
        document_id=fts_document_id,
        ftp_url=fts_url,
        source_file=source_file,
        score=-1.0,
        previews={"content_text": "<<radio>> older version"},
    )
    vector_chunk = MatchedChunk(
        chunk_id=f"R5-000001#{vector_document_id}#vector-hash#0",
        chunk_index=0,
        text="radio newer version",
    )
    semantic_hit = SemanticSearchHit(
        tdoc_id="R5-000001",
        rrf_score=0.4,
        hit=replace(_search_hit(), matched_documents=(fts_document,)),
        min_chunk_distance=0.1,
        best_chunk_id="R5-000001#0",
        document_hits=(
            SemanticDocumentHit(
                document_id=vector_document_id,
                chunk_id=vector_chunk.chunk_id,
                distance=0.1,
                matched_chunk=vector_chunk,
            ),
        ),
    )
    source = RecordingSource(
        [], documents={vector_document_id: (vector_url, source_file)},
    )
    facade = _facade(
        fts=RecordingFts(),
        semantic=RecordingSemantic([semantic_hit]),
        source=source,
    )

    result = facade.search(
        text="radio",
        semantic="radio",
        filters=SearchFilters(limit=5),
    )[0]

    assert [document.document_id for document in result.matched_documents] == [
        vector_document_id,
        fts_document_id,
    ]
    assert [document.ftp_url for document in result.matched_documents] == [
        vector_url,
        fts_url,
    ]


def test_semantic_unverified_chunk_serializes_null() -> None:
    from doc3gpp.models.unified_search import tdoc_search_result_to_dict

    ftp_url = "ran/unverified.zip"
    source_file = "slides/Radio.pptx"
    document_id = document_id_for(ftp_url, source_file)
    semantic_hit = SemanticSearchHit(
        tdoc_id="R5-000001",
        rrf_score=0.4,
        hit=_search_hit(),
        min_chunk_distance=0.1,
        best_chunk_id="R5-000001#0",
        document_hits=(
            SemanticDocumentHit(
                document_id=document_id,
                chunk_id=f"R5-000001#{document_id}#stale-hash#0",
                distance=0.1,
                matched_chunk=None,
            ),
        ),
    )
    source = RecordingSource(
        [], documents={document_id: (ftp_url, source_file)},
    )
    facade = _facade(
        fts=None,
        semantic=RecordingSemantic([semantic_hit]),
        source=source,
    )

    result = facade.search(
        text=None,
        semantic="radio",
        filters=SearchFilters(limit=5),
    )[0]
    payload = json.loads(json.dumps(tdoc_search_result_to_dict(result)))

    assert payload["matched_documents"][0]["matched_chunk"] is None


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
    assert results[0].matched_documents == ()


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
