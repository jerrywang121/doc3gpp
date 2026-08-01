"""Orchestration layer for the semantic (embedding + vector) search subsystem.

:class:`SemanticSearchService` owns four responsibilities:

1. **Read path** — :meth:`search` always embeds the natural-language
   ``query``; the FTS5 path is opt-in via ``fts5_query`` and is
   preprocessed by ``SearchQueryBuilder`` (NOT spaCy). When
   ``fts5_query`` is provided, the service runs FTS5 + vector fan-out
   and RRF with ``vector_weight = 1 - fts5_weight``; when omitted, the
   service returns pure vector KNN top-``limit`` results dressed as
   :class:`SemanticSearchHit` (no RRF, no FTS5 fan-out).
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
from doc3gpp.models.semantic_search import SemanticSearchHit
from doc3gpp.services.embedding.chunker import _chunks
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
        self,
        query: str,
        fts5_query: str | None,
        filters: SearchFilters,
        limit: int,
        fts5_weight: float,
    ) -> list[SemanticSearchHit]:
        """Vector-only or hybrid (FTS5 + vector) read path.

        ``query`` is always embedded; it never feeds FTS5. ``fts5_query``,
        when provided, runs through :class:`SearchQueryBuilder` (same
        preprocessing as ``doc3gpp search query``) and feeds the FTS5
        path; when ``None``, the FTS5 path and RRF are skipped — only
        vector KNN results return, ranked by cosine distance, dressed as
        :class:`SemanticSearchHit` with synthesized metadata stubs.

        ``fts5_weight`` is the FTS5 weight in the RRF blend; the
        vector weight is ``1 - fts5_weight``. Ignored when
        ``fts5_query is None``.
        """
        query_vec = self._embedder.encode([query])[0]
        if fts5_query is None:
            vec_hits = self._vec.knn(query_vec, limit=limit, filters=filters)
            if not vec_hits:
                return []
            # Rank vec hits by distance, dress as SemanticSearchHit with
            # rank_fts5=None, rrf_score=-distance. Same DTO so the CLI
            # renderer branches uniformly between the two paths.
            hits = [
                SemanticSearchHit(
                    tdoc_id=tdoc_id,
                    rrf_score=-distance,
                    rank_fts5=None,
                    rank_vec=rank,
                    min_chunk_distance=distance,
                    best_chunk_id=chunk_id,
                    fts5_hit=None,  # populated below
                )
                for rank, (tdoc_id, chunk_id, _idx, distance) in enumerate(vec_hits)
            ]
            hits.sort(key=lambda h: h.rrf_score, reverse=True)  # least-negative = best
            hits = hits[:limit]
            return self._populate_metadata_stubs(hits)

        fts5_expr = SearchQueryBuilder(fts5_query).build()
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
        vec_hits = self._vec.knn(query_vec, limit=internal_limit, filters=filters)
        merged = rrf_merge(
            fts5_hits, vec_hits,
            k=self._settings.semantic_search.rrf_k,
            vector_weight=1.0 - fts5_weight,
            limit=limit,
        )
        return self._populate_metadata_stubs(merged)

    def _populate_metadata_stubs(
        self,
        hits: list[SemanticSearchHit],
    ) -> list[SemanticSearchHit]:
        """Synthesize the ``fts5_hit`` SearchHit for hits missing one.

        Vector-only hits (no FTS5 fan-out hit, or FTS5 path was skipped)
        need real metadata or the CLI renders an empty stub. Fetch
        ``tdocs`` + ``meetings`` once for all such hits in a single
        batched SQL trip, then populate the synthesized ``fts5_hit`` from
        the JOIN result. Returns the same list with each ``fts5_hit``
        field filled in (either preserved from the original hit or
        populated from the JOIN).
        """
        missing_ids = [h.tdoc_id for h in hits if h.fts5_hit is None]
        if not missing_ids:
            return hits
        metadata_by_id = self._vec.get_tdocs_metadata(missing_ids)
        return [
            dataclasses.replace(
                h,
                fts5_hit=h.fts5_hit or _build_fts5_stub(
                    h.tdoc_id, metadata_by_id.get(h.tdoc_id),
                ),
            )
            for h in hits
        ]


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
