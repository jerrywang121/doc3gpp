from dataclasses import FrozenInstanceError, is_dataclass
from datetime import date, datetime, timezone

import pytest

from doc3gpp.models.unified_search import (
    SearchMode,
    SpecDocSearchResult,
    TDocSearchResult,
    spec_doc_search_result_to_dict,
    tdoc_search_result_to_dict,
)

TDOC_KEYS = [
    "tdoc_id",
    "score",
    "search_mode",
    "previews",
    "title",
    "meeting",
    "tsg",
    "uploaded_date",
    "ftp_url",
    "wis",
    "type",
    "status",
    "best_chunk_id",
]

SPECDOC_KEYS = [
    "chunk_id",
    "score",
    "search_mode",
    "previews",
    "spec_id",
    "version",
    "release",
    "sections",
    "tables",
    "chunk_index",
    "text",
]


def _tdoc_result(
    mode: SearchMode,
    *,
    previews: dict[str, str] | None,
    uploaded_date: str | date | datetime | None = "2025-01-10",
) -> TDocSearchResult:
    return TDocSearchResult(
        tdoc_id="R1-25-0001",
        score=1.25 if mode is not SearchMode.FILTER else None,
        search_mode=mode,
        previews=previews,
        title="Title",
        meeting="RAN1#120",
        tsg="RAN",
        uploaded_date=uploaded_date,
        ftp_url="ftp/path.zip",
        wis="38.331",
        type="CR",
        status="Agreed",
        best_chunk_id="R1-25-0001#0" if mode in (SearchMode.SEMANTIC, SearchMode.HYBRID) else None,
    )


@pytest.mark.parametrize("mode", list(SearchMode))
def test_tdoc_result_has_exact_keys_and_mode_specific_previews(mode: SearchMode):
    previews = {"title": "<<Title>>"}

    payload = tdoc_search_result_to_dict(_tdoc_result(mode, previews=previews))

    assert list(payload) == TDOC_KEYS
    assert payload["search_mode"] == mode.value
    assert payload["previews"] == (previews if mode is SearchMode.FTS5 else None)
    assert payload["type"] == "CR"
    assert payload["status"] == "Agreed"
    assert payload["score"] == (None if mode is SearchMode.FILTER else 1.25)
    assert not {
        "rank_fts5",
        "rank_vec",
        "rrf_score",
        "min_chunk_distance",
        "hit",
    } & payload.keys()


def test_tdoc_result_preserves_none_and_serializes_date_and_datetime():
    date_payload = tdoc_search_result_to_dict(
        _tdoc_result(SearchMode.FILTER, previews=None, uploaded_date=date(2025, 1, 10))
    )
    datetime_payload = tdoc_search_result_to_dict(
        _tdoc_result(
            SearchMode.FILTER,
            previews=None,
            uploaded_date=datetime(2025, 1, 10, 12, 30, tzinfo=timezone.utc),
        )
    )

    assert date_payload["uploaded_date"] == "2025-01-10"
    assert datetime_payload["uploaded_date"] == "2025-01-10T12:30:00+00:00"
    assert datetime_payload["meeting"] == "RAN1#120"
    assert datetime_payload["best_chunk_id"] is None


@pytest.mark.parametrize("mode", [SearchMode.SEMANTIC, SearchMode.HYBRID, SearchMode.FILTER])
def test_tdoc_serializer_normalizes_non_fts5_previews_to_none(mode: SearchMode):
    result = _tdoc_result(mode, previews={"title": "must not be public"})

    assert tdoc_search_result_to_dict(result)["previews"] is None


def test_search_mode_values_are_the_public_literals():
    assert [mode.value for mode in SearchMode] == [
        "fts5",
        "semantic",
        "hybrid",
        "filter",
    ]


def test_tdoc_serializer_returns_complete_public_payload():
    result = _tdoc_result(SearchMode.FTS5, previews={"title": "<<Title>>"})

    assert tdoc_search_result_to_dict(result) == {
        "tdoc_id": "R1-25-0001",
        "score": 1.25,
        "search_mode": "fts5",
        "previews": {"title": "<<Title>>"},
        "title": "Title",
        "meeting": "RAN1#120",
        "tsg": "RAN",
        "uploaded_date": "2025-01-10",
        "ftp_url": "ftp/path.zip",
        "wis": "38.331",
        "type": "CR",
        "status": "Agreed",
        "best_chunk_id": None,
    }


def test_search_result_dtos_are_frozen_and_slotted():
    tdoc_result = _tdoc_result(SearchMode.FILTER, previews=None)
    spec_doc_result = SpecDocSearchResult(
        chunk_id="38.331@18.5.0#0",
        score=None,
        search_mode=SearchMode.FILTER,
        previews=None,
        spec_id="38.331",
        version="18.5.0",
        release=None,
        sections=None,
        tables=None,
        chunk_index=0,
        text="Content",
    )

    assert is_dataclass(tdoc_result)
    assert is_dataclass(spec_doc_result)
    assert not hasattr(tdoc_result, "__dict__")
    assert not hasattr(spec_doc_result, "__dict__")
    with pytest.raises(FrozenInstanceError):
        tdoc_result.title = "Changed"
    with pytest.raises(FrozenInstanceError):
        spec_doc_result.text = "Changed"


def test_spec_doc_result_has_exact_keys_and_preserves_none():
    result = SpecDocSearchResult(
        chunk_id="38.331@18.5.0#0",
        score=None,
        search_mode=SearchMode.FILTER,
        previews=None,
        spec_id="38.331",
        version="18.5.0",
        release="Rel-18",
        sections="5.1 Handover",
        tables="Table 1 Values",
        chunk_index=0,
        text="Content",
    )

    payload = spec_doc_search_result_to_dict(result)

    assert list(payload) == SPECDOC_KEYS
    assert payload == {
        "chunk_id": "38.331@18.5.0#0",
        "score": None,
        "search_mode": "filter",
        "previews": None,
        "spec_id": "38.331",
        "version": "18.5.0",
        "release": "Rel-18",
        "sections": "5.1 Handover",
        "tables": "Table 1 Values",
        "chunk_index": 0,
        "text": "Content",
    }
    assert not {
        "rank_fts5",
        "rank_vec",
        "rrf_score",
        "min_chunk_distance",
        "hit",
    } & payload.keys()


@pytest.mark.parametrize("mode", list(SearchMode))
def test_spec_doc_result_serializes_mode_and_previews(mode: SearchMode):
    result = SpecDocSearchResult(
        chunk_id="38.331@18.5.0#0",
        score=2.5,
        search_mode=mode,
        previews={"text": "match"},
        spec_id="38.331",
        version="18.5.0",
        release=None,
        sections=None,
        tables=None,
        chunk_index=0,
        text="Content",
    )

    payload = spec_doc_search_result_to_dict(result)

    assert list(payload) == SPECDOC_KEYS
    assert payload["search_mode"] == mode.value
    assert payload["previews"] == ({"text": "match"} if mode is SearchMode.FTS5 else None)
    assert payload["release"] is None


@pytest.mark.parametrize("mode", [SearchMode.SEMANTIC, SearchMode.HYBRID, SearchMode.FILTER])
def test_spec_doc_serializer_normalizes_non_fts5_previews_to_none(mode: SearchMode):
    result = SpecDocSearchResult(
        chunk_id="38.331@18.5.0#0",
        score=None,
        search_mode=mode,
        previews={"text": "must not be public"},
        spec_id="38.331",
        version="18.5.0",
        release=None,
        sections=None,
        tables=None,
        chunk_index=0,
        text="Content",
    )

    assert spec_doc_search_result_to_dict(result)["previews"] is None
