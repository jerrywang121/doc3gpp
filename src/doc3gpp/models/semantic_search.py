"""DTOs and error hierarchy for the semantic (embedding + vector) search subsystem.

The error tree extends :class:`doc3gpp.models.search.SearchError` so the
existing CLI catch-all still works, but each subclass gets its own
``except`` branch in ``cli.py`` for a friendly one-liner and its own
exit code.
"""

from __future__ import annotations

from dataclasses import dataclass

from doc3gpp.models.search import SearchError, SearchHit


class SemanticSearchError(SearchError):
    """Base class for every error raised by the semantic search subsystem."""


class SemanticSearchUnavailableError(SemanticSearchError):
    """The semantic stack is not available (no FTS5 foundation, or extra missing)."""


class SemanticSearchQueryError(SemanticSearchError):
    """The user-supplied query is empty after stopword stripping. Exit code 2."""


class EmbedderUnavailableError(SemanticSearchError):
    """The embedding model failed to load. Exit code 1."""


class VectorIndexUnavailableError(SemanticSearchError):
    """sqlite-vec is missing or the vector index cannot be opened. Exit code 1."""


@dataclass(slots=True, frozen=True)
class SemanticSearchHit:
    """A single merged hit from the RRF fusion of FTS5 + vector rankings.

    ``rank_fts5`` / ``rank_vec`` are the 0-based positions in the
    respective fan-out lists, or ``None`` when the ``tdoc_id`` was not
    present in that side's fan-out. ``min_chunk_distance`` is the
    lowest cosine distance across all chunks for this ``tdoc_id``
    (``None`` when the tdoc had no vector rows). ``best_chunk_id`` is
    the chunk that produced the min distance (for ``--explain``
    rendering). ``fts5_hit`` is the existing :class:`SearchHit`
    sub-record; when the tdoc was vector-only, the service synthesizes
    a minimal :class:`SearchHit` from the ``tdocs`` JOIN so the
    renderer can reuse the existing shape.
    """

    tdoc_id: str
    rrf_score: float
    fts5_hit: SearchHit
    rank_fts5: int | None = None
    rank_vec: int | None = None
    min_chunk_distance: float | None = None
    best_chunk_id: str | None = None


__all__ = [
    "EmbedderUnavailableError",
    "SemanticSearchError",
    "SemanticSearchHit",
    "SemanticSearchQueryError",
    "SemanticSearchUnavailableError",
    "VectorIndexUnavailableError",
] 
