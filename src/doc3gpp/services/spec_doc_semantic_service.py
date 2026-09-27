"""Hybrid FTS5+vector RRF over spec-doc chunks. Chunk-level fusion (unlike tdoc-level)."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import replace

from doc3gpp.models.search import RebuildProgress
from doc3gpp.models.spec_doc import (
    SpecDocHit,
    SpecDocSearchFilters,
    SpecDocSemanticHit,
)
from doc3gpp.repository.protocols import SpecDocRepository

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

    Internal read path (:meth:`search`) always embeds ``query``; the optional
    internal ``fts5_query`` value is raw text and the FTS5 repo builds the
    ``MATCH`` internally. With it both sides fan out to
    ``limit * fanout_multiplier`` and merge via :func:`rrf_merge`;
    without it only vector KNN returns, dressed as
    :class:`SpecDocSemanticHit` with ``rank_fts5=None``. The value is not a
    public CLI, HTTP, or MCP parameter; public callers use
    ``SpecDocSearchFacade``.

    The vector KNN side uses exact ``=`` for ``spec_id`` / ``version``
    and the repository's rich text semantics for ``sections`` /
    ``tables``. Release matching remains exact, as in the vector
    repository. Callers needing exact agreement should pass plain
    ``spec_id`` / ``version`` / ``release`` strings and rich-filter
    patterns for ``sections`` / ``tables``.
    """

    def __init__(
        self,
        *,
        fts5_service,
        embedder,
        vector_repo,
        settings,
        doc_repo: SpecDocRepository | None = None,
    ) -> None:
        self._fts5 = fts5_service
        self._embedder = embedder
        self._vec = vector_repo
        self._settings = settings
        self._doc_repo = doc_repo

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
        internal ``fts5_query``, when provided, feeds the FTS5 side verbatim.
        ``fts5_weight`` is the FTS5 weight in the RRF blend; the
        vector weight is ``1 - fts5_weight``. Ignored when
        ``fts5_query is None``.
        """
        qvec = self._embedder.encode([query])[0]
        if fts5_query is None:
            vec_hits = self._vec.knn(qvec, limit=limit, filters=filters)
            hits = [
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
            return self._populate_chunk_metadata(hits)
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
        hits = rrf_merge(
            fts_hits,
            vec_hits,
            k=self._settings.semantic_search.rrf_k,
            vector_weight=1.0 - fts5_weight,
            limit=limit,
        )
        return self._populate_chunk_metadata(hits)

    def _populate_chunk_metadata(
        self, hits: list[SpecDocSemanticHit]
    ) -> list[SpecDocSemanticHit]:
        missing_ids = [hit.chunk_id for hit in hits if hit.hit is None]
        if not missing_ids:
            return hits
        try:
            chunks = self._get_doc_repo().get_chunks_by_ids(missing_ids)
        except Exception as exc:  # noqa: BLE001 - metadata is enrichment only
            logger.debug("spec-doc vector metadata lookup failed: %s", exc)
            return hits
        return [
            replace(hit, hit=self._chunk_to_hit(chunks[hit.chunk_id]))
            if hit.hit is None and hit.chunk_id in chunks
            else hit
            for hit in hits
        ]

    def _get_doc_repo(self) -> SpecDocRepository:
        if self._doc_repo is None:
            from doc3gpp.storage.repositories.spec_doc_sql import (
                SQLAlchemySpecDocRepository,
            )

            self._doc_repo = SQLAlchemySpecDocRepository()
        return self._doc_repo

    @staticmethod
    def _chunk_to_hit(chunk) -> SpecDocHit:
        return SpecDocHit(
            chunk_id=chunk.chunk_id,
            spec_id=chunk.spec_id,
            version=chunk.version,
            release=chunk.release,
            sections=chunk.sections,
            tables=chunk.tables,
            chunk_index=chunk.chunk_index,
            text=chunk.text,
            score=0.0,
            previews={},
        )

    def index_for_version(self, spec_id: str, version: str) -> None:
        """Embed every chunk of ``(spec_id, version)`` into the vector index.

        Chunk texts are already chunk-shaped (Task 7), so no further
        splitting is applied. A version with no chunks removes any
        stale vector rows instead of writing.
        """

        chunks = self._get_doc_repo().list_chunks(
            spec_id, version=version, limit=100000
        )
        texts = [
            "\n".join(part for part in (c.sections, c.tables, c.text) if part)
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

    def rebuild_embeddings(
        self,
        batch_size: int,
        stale_only: bool,
        quiet: bool,
        resume: bool = False,
    ) -> Iterator[RebuildProgress]:
        """Yield progress while embedding each parsed source-version pair."""
        model = getattr(self._embedder, "model_name", None)
        live_dim = getattr(self._embedder, "dim", None)
        if resume:
            if hasattr(self._vec, "verify_compatible") and live_dim is not None:
                self._vec.verify_compatible(live_dim, model)
            after_id = self._vec.get_resume_cursor()
        else:
            self._vec.clear_resume_cursor()
            if hasattr(self._vec, "reset_for_rebuild") and live_dim is not None:
                self._vec.reset_for_rebuild(live_dim, model)
            after_id = None

        total = self._vec.count_versions_to_index(
            stale_only=stale_only, after_id=after_id
        )
        processed, last_pct = 0, 0
        successful_pairs: set[tuple[str, str]] = set()
        for batch in self._vec.rebuild_batch(
            batch_size=batch_size, after_id=after_id, stale_only=stale_only
        ):
            for spec_id, version in batch:
                try:
                    self.index_for_version(spec_id, version)
                except Exception as exc:  # noqa: BLE001 - isolate one pair
                    record_failure = getattr(self._vec, "record_rebuild_failure", None)
                    if record_failure is not None:
                        record_failure(spec_id, version)
                    logger.warning(
                        "spec-doc embedding rebuild failed for %s@%s: %s",
                        spec_id,
                        version,
                        exc,
                    )
                else:
                    successful_pairs.add((spec_id, version))
                processed += 1
                pct = processed * 100 // total if total else 100
                if pct > last_pct:
                    yield RebuildProgress(
                        processed, total, f"{spec_id}@{version}"
                    )
                    last_pct = pct
            if batch:
                candidate = f"{batch[-1][0]}@{batch[-1][1]}"
                if after_id is None or candidate > after_id:
                    self._vec.set_resume_cursor(candidate)
        touch_rebuild_at = getattr(self._vec, "touch_rebuild_at", None)
        if touch_rebuild_at is not None:
            touch_rebuild_at()
        clear_failure = getattr(self._vec, "clear_rebuild_failure", None)
        if clear_failure is not None:
            for spec_id, version in successful_pairs:
                clear_failure(spec_id, version)
        if not quiet:
            logger.info(
                "spec-doc embedding rebuild complete: processed=%d total=%d",
                processed,
                total,
            )
