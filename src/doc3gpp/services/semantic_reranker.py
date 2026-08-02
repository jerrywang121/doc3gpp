"""Semantic rerank for the FTS5 hit list.

The :class:`SemanticReranker` is the embedding-backed impl of the
:class:`doc3gpp.repository.protocols.EmbeddingReranker` Protocol. The
FTS5 path in ``search query --sem-query`` fetches a wider candidate
bag; this class re-orders it by cosine similarity to a user-supplied
string.

Scoring source: :class:`doc3gpp.repository.protocols.VectorIndexRepository`
— specifically, :meth:`get_min_distance_for_tdocs`. Candidates with
no row in ``vec_tdoc_embeddings`` get
:attr:`MISSING_FLOOR <float("-inf")>` so they sort strictly below
every real score. When every candidate is missing the output is the
FTS5 input order (the caller logs the vector-empty warning).
"""
from __future__ import annotations

import logging


from doc3gpp.models.search import SearchHit
from doc3gpp.repository.protocols import Embedder, VectorIndexRepository

logger = logging.getLogger(__name__)


class SemanticReranker:
    """Rerank FTS5 hits by cosine distance to a user-supplied query.

    The class is duck-typed against the
    :class:`~doc3gpp.repository.protocols.EmbeddingReranker` Protocol
    (it implements the same
    ``rerank(semantic_query, hits, final_limit, quiet)`` signature).
    It is NOT declared as ``EmbeddingReranker`` in the
    type annotation because ``build_search_service`` constructs the
    instance lazily and tests inject mock embedders / vector repos
    via constructor injection.
    """

    MISSING_FLOOR: float = float("-inf")

    def __init__(
        self,
        embedder: Embedder,
        vector_repo: VectorIndexRepository,
        settings: object,
    ) -> None:
        self._embedder = embedder
        self._vector_repo = vector_repo
        self._settings = settings

    def rerank(
        self,
        semantic_query: str,
        hits: list[SearchHit],
        final_limit: int | None = None,
        quiet: bool = False,
    ) -> list[SearchHit]:
        if not hits:
            return []
        query_vec = self._embedder.encode([semantic_query])[0]
        scores = self._vector_repo.get_min_distance_for_tdocs(
            [h.tdoc_id for h in hits], query_vec,
        )
        decorated: list[tuple[float, int, SearchHit]] = []
        any_real = False
        for idx, hit in enumerate(hits):
            entry = scores.get(hit.tdoc_id)
            if entry is None:
                score = self.MISSING_FLOOR
            else:
                score = -entry[0]  # higher = better
                any_real = True
            decorated.append((score, idx, hit))
        if not any_real and not quiet:
            logger.warning(
                "semantic rerank: no rows in vec_tdoc_embeddings; "
                "falling back to FTS5 order"
            )
        # Sort by score desc; on ties preserve input order via ``idx``.
        decorated.sort(key=lambda t: (t[0], -t[1]), reverse=True)
        ordered = [h for _, _, h in decorated]
        if final_limit is not None:
            return ordered[:final_limit]
        return ordered
