"""Public flattened result DTOs for unified search surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any


class SearchMode(str, Enum):
    FTS5 = "fts5"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"
    FILTER = "filter"


@dataclass(frozen=True, slots=True)
class TDocSearchResult:
    tdoc_id: str
    score: float | None
    search_mode: SearchMode
    previews: dict[str, str] | None
    title: str
    meeting: str | None
    tsg: str | None
    uploaded_date: str | date | datetime | None
    ftp_url: str | None
    wis: str | None
    type: str | None
    status: str | None
    best_chunk_id: str | None


@dataclass(frozen=True, slots=True)
class SpecDocSearchResult:
    chunk_id: str
    score: float | None
    search_mode: SearchMode
    previews: dict[str, str] | None
    spec_id: str
    version: str
    release: str | None
    sections: str | None
    tables: str | None
    chunk_index: int
    text: str


def _serialize_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def tdoc_search_result_to_dict(result: TDocSearchResult) -> dict[str, Any]:
    return {
        "tdoc_id": result.tdoc_id,
        "score": result.score,
        "search_mode": result.search_mode.value,
        "previews": result.previews if result.search_mode is SearchMode.FTS5 else None,
        "title": result.title,
        "meeting": result.meeting,
        "tsg": result.tsg,
        "uploaded_date": _serialize_scalar(result.uploaded_date),
        "ftp_url": result.ftp_url,
        "wis": result.wis,
        "type": result.type,
        "status": result.status,
        "best_chunk_id": result.best_chunk_id,
    }


def spec_doc_search_result_to_dict(result: SpecDocSearchResult) -> dict[str, Any]:
    return {
        "chunk_id": result.chunk_id,
        "score": result.score,
        "search_mode": result.search_mode.value,
        "previews": result.previews if result.search_mode is SearchMode.FTS5 else None,
        "spec_id": result.spec_id,
        "version": result.version,
        "release": result.release,
        "sections": result.sections,
        "tables": result.tables,
        "chunk_index": result.chunk_index,
        "text": result.text,
    }


__all__ = [
    "SearchMode",
    "SpecDocSearchResult",
    "TDocSearchResult",
    "spec_doc_search_result_to_dict",
    "tdoc_search_result_to_dict",
]
