"""Orchestration layer for the semantic (embedding + vector) search subsystem.

:class:`SemanticSearchService` owns four responsibilities:

1. **Read path** — :meth:`search` strips stopwords for the FTS5 query,
   embeds the ORIGINAL query for the vector path, fans out to both
   indexes with an enlarged ``internal_limit = limit * fanout``,
   then merges via :func:`rrf_merge` and truncates to ``limit``.
2. **Write paths** — :meth:`index_for_tdoc` builds the embed text,
   chunks it, embeds the chunks, and upserts. :meth:`remove_for_tdoc`
   deletes the chunk rows.
3. **Maintenance** — :meth:`rebuild_embeddings` is a generator that
   mirrors :meth:`doc3gpp.services.search_service.SearchService.rebuild`.
4. **Status** — :meth:`status` snapshots the vector index.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterator

from doc3gpp.cli_filters import SearchQueryBuilder
from doc3gpp.models.search import (
    RebuildProgress, SearchFilters, SearchHit, SearchIndexStatus,
)
from doc3gpp.models.semantic_search import (
    SemanticSearchHit,
    SemanticSearchQueryError,
)
from doc3gpp.services.embedding.chunker import _chunks
from doc3gpp.services.embedding.stopwords import strip_stopwords
from doc3gpp.services.search_service import SearchService
from doc3gpp.storage.repositories.vector_sql import _build_embed_text

logger = logging.getLogger(__name__)


def rrf_merge(
    fts5_hits: list[SearchHit],
    vec_hits: list[tuple[str, str, int, float]],
    *,
    k: int = 60,
    vector_weight: float = 0.7,
    limit: int = 20,
) -> list[SemanticSearchHit]:
    """Reciprocal-rank fusion across FTS5 and vector rankings.

    Each tdoc_id is ranked by FTS5 position (if present) and by the
    best (lowest-distance) vector chunk. Final score::

        rrf = 1/(k + rank_fts5) * (1 - W) + 1/(k + rank_vec) * W

    A tdoc_id present in only one side contributes 0 from the other
    side's rank. ``fts5_hit`` is ``None`` for vector-only tdogs; the
    service synthesizes a minimal :class:`SearchHit` from the JOIN
    before returning to the CLI.
    """
    fts5_rank: dict[str, int] = {h.tdoc_id: i for i, h in enumerate(fts5_hits)}
    fts5_by_id: dict[str, SearchHit] = {h.tdoc_id: h for h in fts5_hits}
    # Reduce vec chunks to min distance per tdoc_id, preserving chunk rank
    vec_best: dict[str, tuple[int, str, float]] = {}  # tdoc_id -> (rank, best_chunk_id, min_dist)
    for rank, (tdoc_id, chunk_id, _chunk_idx, dist) in enumerate(vec_hits):
        prev = vec_best.get(tdoc_id)
        if prev is None or dist < prev[2]:
            vec_best[tdoc_id] = (rank, chunk_id, dist)
    all_ids = set(fts5_rank) | set(vec_best)
    scored: list[SemanticSearchHit] = []
    for tdoc_id in all_ids:
        r_fts = fts5_rank.get(tdoc_id)
        r_vec_tup = vec_best.get(tdoc_id)
        r_vec = r_vec_tup[0] if r_vec_tup is not None else None
        w = vector_weight
        fts5_term = (1.0 / (k + r_fts)) * (1.0 - w) if r_fts is not None else 0.0
        vec_term = (1.0 / (k + r_vec)) * w if r_vec is not None else 0.0
        score = fts5_term + vec_term
        scored.append(SemanticSearchHit(
            tdoc_id=tdoc_id,
            rrf_score=score,
            fts5_hit=fts5_by_id.get(tdoc_id),  # None for vector-only
            rank_fts5=r_fts,
            rank_vec=r_vec,
            min_chunk_distance=r_vec_tup[2] if r_vec_tup else None,
            best_chunk_id=r_vec_tup[1] if r_vec_tup else None,
        ))
    scored.sort(key=lambda h: h.rrf_score, reverse=True)
    return scored[:limit]


def _build_fts5_stub(tdoc_id: str, meta):
    """Synthesize a ``SearchHit`` from ``TDocMeta`` (or empty if missing).

    When ``meta`` is ``None`` (tdoc was deleted between index and
    query), returns the empty stub so the CLI still surfaces the
    hit — operators can decide whether to dig deeper. ``SearchHit``
    is frozen so we have to construct rather than mutate.
    """
    if meta is None:
        return SearchHit(
            tdoc_id=tdoc_id, score=0.0, previews={},
            title="", meeting=None, tsg=None,
            uploaded_date=None, ftp_url=None, wis=None,
        )
    return SearchHit(
        tdoc_id=tdoc_id, score=0.0, previews={},
        title=meta.title, meeting=meta.meeting, tsg=meta.tsg,
        uploaded_date=meta.uploaded_date,
        ftp_url=meta.ftp_url, wis=meta.wis,
    )


class SemanticSearchService:
    def __init__(
        self,
        fts5_service: SearchService,
        embedder,
        vector_repo,
        settings,
    ) -> None:
        self._fts5 = fts5_service
        self._embedder = embedder
        self._vec = vector_repo
        self._settings = settings

    def search(
        self, query: str, filters: SearchFilters,
        limit: int, vector_weight: float,
    ) -> list[SemanticSearchHit]:
        stripped = strip_stopwords(query)
        if not stripped:
            raise SemanticSearchQueryError(
                "query has no content after stopword stripping"
            )
        fts5_expr = SearchQueryBuilder(stripped).build()
        fanout = self._settings.semantic_search.fanout_multiplier
        internal_limit = max(limit * fanout, 0)
        fts5_filters = SearchFilters(
            tsg=filters.tsg, meeting=filters.meeting,
            meeting_id=filters.meeting_id, tdoc_id=filters.tdoc_id,
            release=filters.release, spec=filters.spec,
            since=filters.since, until=filters.until,
            limit=internal_limit,
        )
        fts5_hits = self._fts5.search(fts5_expr, fts5_filters)
        query_vec = self._embedder.encode([query])[0]
        vec_hits = self._vec.knn(query_vec, limit=internal_limit, filters=filters)
        merged = rrf_merge(
            fts5_hits, vec_hits,
            k=self._settings.semantic_search.rrf_k,
            vector_weight=vector_weight,
            limit=limit,
        )
        # Vector-only hits (FTS5 missed them — common for the
        # 12,561 title-only TDocs that have no parsed cover or
        # extract) need real metadata or the CLI renders an
        # empty stub. Fetch ``tdocs`` + ``meetings`` once for all
        # such hits in a single batched SQL trip, then populate
        # the synthesized ``fts5_hit`` from the join result.
        vector_only_ids = [
            h.tdoc_id for h in merged if h.fts5_hit is None
        ]
        metadata_by_id = self._vec.get_tdocs_metadata(vector_only_ids)
        merged = [
            dataclasses.replace(
                h,
                fts5_hit=_build_fts5_stub(h.tdoc_id, metadata_by_id.get(h.tdoc_id)),
            ) if h.fts5_hit is None else h
            for h in merged
        ]
        return merged

    def index_for_tdoc(self, tdoc_id: str) -> None:
        embed_text = _build_embed_text(tdoc_id)
        if embed_text is None:
            self._vec.remove_for_tdoc(tdoc_id)
            return
        chunks = _chunks(
            embed_text,
            self._settings.semantic_search.chunk_size,
            self._settings.semantic_search.chunk_overlap,
        )
        max_chunks = getattr(self._settings.semantic_search, "max_chunks_per_tdoc", 32)
        if len(chunks) > max_chunks:
            chunks = chunks[:max_chunks]
        if not chunks:
            self._vec.remove_for_tdoc(tdoc_id)
            return
        # Batch all chunks for one TDoc into a single embedder call.
        # sentence-transformers has ~1s per-call overhead, so calling
        # encode() per chunk would be O(chunks) wall-time per TDoc.
        embeddings_array = self._embedder.encode(chunks)
        embeddings = [embeddings_array[i] for i in range(len(chunks))]
        self._vec.upsert_chunks(tdoc_id, embeddings)

    def remove_for_tdoc(self, tdoc_id: str) -> None:
        self._vec.remove_for_tdoc(tdoc_id)

    def rebuild_embeddings(
        self,
        batch_size: int,
        stale_only: bool,
        quiet: bool,
        resume: bool = False,
    ) -> Iterator[RebuildProgress]:
        """Yield per-1%-of-progress updates during an embedding
        rebuild.

        For a corpus of ``N`` TDocs, yields ~100 times (one per 1%
        boundary crossed), plus a guaranteed final yield at 100%.
        The CLI renders this as a tqdm bar with `bar.update(delta)`.
        Cursor is persisted to ``vec_meta`` per batch so a crashed
        rebuild can resume.

        ``resume=True`` picks up from the persisted cursor;
        ``resume=False`` (default) clears the cursor first so a
        fresh start processes every TDoc from the very first one.
        """
        if resume:
            after_id = self._vec.get_resume_cursor()
        else:
            # Force a fresh start regardless of any stale cursor.
            self._vec.clear_resume_cursor()
            after_id = None
        total = self._vec.count_tdocs_to_index(
            stale_only=stale_only, after_id=after_id,
        )
        # Start at 0 so we don't fire a "0%" yield when processed=1
        # and total=13693 (1*100//13693=0).
        last_yielded_pct = 0
        processed = 0
        batches = self._vec.rebuild_batch(
            batch_size=batch_size, after_id=after_id, stale_only=stale_only,
        )
        for batch in batches:
            for tdoc_id in batch:
                try:
                    self.index_for_tdoc(tdoc_id)
                except Exception as exc:  # noqa: BLE001 - per-tdoc failures must not abort the batch; logged for observability
                    logger.warning(
                        "embedding rebuild failed for tdoc_id=%s: %s",
                        tdoc_id, exc,
                    )
                processed += 1
                pct = (processed * 100 // total) if total > 0 else 100
                if pct > last_yielded_pct:
                    yield RebuildProgress(
                        processed=processed,
                        total=total,
                        current_tdoc_id=tdoc_id,
                    )
                    last_yielded_pct = pct
            self._vec.set_resume_cursor(batch[-1])

    def status(self) -> SearchIndexStatus:
        return self._vec.status()
