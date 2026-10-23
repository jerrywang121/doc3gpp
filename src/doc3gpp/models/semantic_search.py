"""DTOs and error hierarchy for the semantic (embedding + vector) search subsystem.

The error tree extends :class:`doc3gpp.models.search.SearchError` so the
existing CLI catch-all still works, but each subclass gets its own
``except`` branch in ``cli.py`` for a friendly one-liner and its own
exit code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from doc3gpp.models.search import SearchError, SearchHit


class SemanticSearchError(SearchError):
    """Base class for every error raised by the semantic search subsystem."""


class SemanticSearchUnavailableError(SemanticSearchError):
    """The semantic stack is not available (no FTS5 foundation, or extra missing)."""


class SemanticSearchQueryError(SemanticSearchError):
    """The internal FTS5 text value yields no indexable tokens after
    ``SearchQueryBuilder.build()`` (for example, a stopwords-only query).
    Exit code 2."""


class EmbedderUnavailableError(SemanticSearchError):
    """The embedding model failed to load. Exit code 1."""


class VectorIndexUnavailableError(SemanticSearchError):
    """sqlite-vec is missing or the vector index cannot be opened. Exit code 1."""


@dataclass(slots=True, frozen=True)
class EmbeddedChunk:
    """One chunk ready for the vector index: its ID, position, and embedding."""

    chunk_id: str
    chunk_index: int
    embedding: Any


@dataclass(slots=True, frozen=True)
class SemanticDocumentHit:
    """Best chunk of one content document (CR annex or family member) of a TDoc."""

    document_id: str
    chunk_id: str
    distance: float


def parse_content_chunk_id(chunk_id: str) -> tuple[str, str] | None:
    """Return ``(document_id, content_hash)`` for a content chunk ID.

    Content chunks are ``<tdoc_id>#<document_id>#<content_hash>#<i>``. Metadata
    chunks (``<tdoc_id>#<i>``) return ``None``.
    """
    parts = chunk_id.split("#")
    if len(parts) != 4:
        return None
    return parts[1], parts[2]


@dataclass(slots=True, frozen=True)
class SemanticSearchHit:
    """A single merged hit from the RRF fusion of FTS5 + vector rankings.

    ``rank_fts5`` is the 0-based position in the FTS5 fan-out list and
    ``rank_vec`` the 1-based position of the TDoc among distinct TDocs in the
    vector fan-out (knn order). Either is ``None`` when the ``tdoc_id`` was not
    present in that side's fan-out. ``min_chunk_distance`` is the
    lowest cosine distance across all chunks for this ``tdoc_id``
    (``None`` when the tdoc had no vector rows). ``best_chunk_id`` is
    the chunk that produced the min distance (for ``--explain``
    rendering). ``hit`` is the existing :class:`SearchHit` sub-record
    carrying the TDoc's metadata bag (title, ftp_url, meeting, tsg,
    uploaded_date, wis, previews); when the tdoc was vector-only, the
    service synthesizes a minimal :class:`SearchHit` from the
    ``tdocs`` JOIN so the renderer can reuse the existing shape.
    """

    tdoc_id: str
    rrf_score: float
    hit: SearchHit
    rank_fts5: int | None = None
    rank_vec: int | None = None
    min_chunk_distance: float | None = None
    best_chunk_id: str | None = None
    document_hits: tuple[SemanticDocumentHit, ...] = ()


__all__ = [
    "EmbeddedChunk",
    "EmbedderUnavailableError",
    "SemanticDocumentHit",
    "SemanticSearchError",
    "SemanticSearchHit",
    "SemanticSearchQueryError",
    "SemanticSearchUnavailableError",
    "VectorIndexUnavailableError",
    "parse_content_chunk_id",
]
