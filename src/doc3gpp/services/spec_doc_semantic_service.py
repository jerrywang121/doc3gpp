"""Hybrid FTS5+vector RRF over spec-doc chunks. Chunk-level fusion (unlike tdoc-level)."""
from __future__ import annotations

import logging

from doc3gpp.models.spec_doc import (
    SpecDocHit,
    SpecDocSearchFilters,
    SpecDocSemanticHit,
)

logger = logging.getLogger(__name__)


def rrf_merge(
    fts5_hits: list[SpecDocHit],
    vec_hits: list[tuple[str, float]],
    *,
    k: int = 60,
    vector_weight: float = 0.5,
    limit: int = 20,
) -> list[SpecDocSemanticHit]:
    """Reciprocal-rank fusion across FTS5 and vector chunk rankings.

    Chunk-level (unlike the tdoc-level
    :func:`doc3gpp.services.semantic_search_service.rrf_merge`): each
    ``chunk_id`` is ranked by FTS5 position (if present) and by vector
    KNN position. Final score::

        rrf = 1/(k + rank_fts5) * (1 - W) + 1/(k + rank_vec) * W

    A chunk present on only one side contributes 0 from the other
    side. ``hit`` is ``None`` for vector-only chunks.
    """
    fts_rank = {h.chunk_id: i for i, h in enumerate(fts5_hits)}
    fts_by_id = {h.chunk_id: h for h in fts5_hits}
    vec_ids = {c for c, _ in vec_hits}
    scored: list[SpecDocSemanticHit] = []
    for rank, (cid, dist) in enumerate(vec_hits):
        rf = fts_rank.get(cid)
        score = (1.0 / (k + rf) * (1.0 - vector_weight) if rf is not None else 0.0) + 1.0 / (
            k + rank
        ) * vector_weight
        scored.append(
            SpecDocSemanticHit(
                chunk_id=cid,
                rrf_score=score,
                hit=fts_by_id.get(cid),
                rank_fts5=rf,
                rank_vec=rank,
                min_chunk_distance=dist,
            )
        )
    for cid, i in fts_rank.items():
        if cid not in vec_ids:
            scored.append(
                SpecDocSemanticHit(
                    chunk_id=cid,
                    rrf_score=1.0 / (k + i) * (1.0 - vector_weight),
                    hit=fts_by_id[cid],
                    rank_fts5=i,
                    rank_vec=None,
                    min_chunk_distance=None,
                )
            )
    scored.sort(key=lambda h: h.rrf_score, reverse=True)
    return scored[:limit]


class SpecDocSemanticService:
    """Hybrid semantic search over spec-doc chunks.

    Read path (:meth:`search`) always embeds ``query``; the FTS5 path
    is opt-in via ``fts5_query`` (raw text — the FTS5 repo builds the
    ``MATCH`` internally). With ``fts5_query`` both sides fan out to
    ``limit * fanout_multiplier`` and merge via :func:`rrf_merge`;
    without it only vector KNN returns, dressed as
    :class:`SpecDocSemanticHit` with ``rank_fts5=None``.

    The vector KNN side uses exact ``=`` for ``spec_id`` / ``version``
    and the repository's rich text semantics for ``sections`` /
    ``tables``. Release matching remains exact, as in the vector
    repository. Callers needing exact agreement should pass plain
    ``spec_id`` / ``version`` / ``release`` strings and rich-filter
    patterns for ``sections`` / ``tables``.
    """

    def __init__(self, *, fts5_service, embedder, vector_repo, settings) -> None:
        self._fts5 = fts5_service
        self._embedder = embedder
        self._vec = vector_repo
        self._settings = settings

    def search(
        self,
        query: str,
        *,
        fts5_query: str | None,
        filters: SpecDocSearchFilters,
        limit: int,
        fts5_weight: float,
    ) -> list[SpecDocSemanticHit]:
        """Vector-only or hybrid (FTS5 + vector) read path.

        ``query`` is always embedded and never feeds FTS5;
        ``fts5_query``, when provided, feeds the FTS5 side verbatim.
        ``fts5_weight`` is the FTS5 weight in the RRF blend; the
        vector weight is ``1 - fts5_weight``. Ignored when
        ``fts5_query is None``.
        """
        qvec = self._embedder.encode([query])[0]
        if fts5_query is None:
            vec_hits = self._vec.knn(qvec, limit=limit, filters=filters)
            return [
                SpecDocSemanticHit(
                    chunk_id=cid,
                    rrf_score=-dist,
                    hit=None,
                    rank_fts5=None,
                    rank_vec=r,
                    min_chunk_distance=dist,
                )
                for r, (cid, dist) in enumerate(vec_hits)
            ][:limit]
        fanout = self._settings.semantic_search.fanout_multiplier
        n = max(limit * fanout, 0)
        f = SpecDocSearchFilters(
            spec_id=filters.spec_id,
            release=filters.release,
            version=filters.version,
            sections=filters.sections,
            tables=filters.tables,
            limit=n,
            offset=0,
        )
        fts_hits = self._fts5.search(fts5_query, f)
        vec_hits = self._vec.knn(qvec, limit=n, filters=filters)
        return rrf_merge(
            fts_hits,
            vec_hits,
            k=self._settings.semantic_search.rrf_k,
            vector_weight=1.0 - fts5_weight,
            limit=limit,
        )

    def index_for_version(self, spec_id: str, version: str) -> None:
        """Embed every chunk of ``(spec_id, version)`` into the vector index.

        Chunk texts are already chunk-shaped (Task 7), so no further
        splitting is applied. A version with no chunks removes any
        stale vector rows instead of writing.
        """
        from doc3gpp.storage.repositories.spec_doc_sql import (
            SQLAlchemySpecDocRepository,
        )

        chunks = SQLAlchemySpecDocRepository().list_chunks(
            spec_id, version=version, limit=100000
        )
        texts = [
            f"{c.sections or ''}\n{c.tables or ''}\n{c.text}".strip()
            for c in chunks
        ]
        if not texts:
            self._vec.remove_for_version(spec_id, version)
            return
        embs = self._embedder.encode(texts)
        self._vec.upsert_for_version(
            spec_id, version, [embs[i] for i in range(len(texts))]
        )

    def remove_for_version(self, spec_id: str, version: str) -> None:
        """Delete all vector rows for ``(spec_id, version)``. No-op if absent."""
        self._vec.remove_for_version(spec_id, version)
