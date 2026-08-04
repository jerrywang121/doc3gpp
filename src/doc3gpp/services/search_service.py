"""Orchestration layer for the FTS5 search subsystem.

The :class:`SearchService` owns three responsibilities:

1. **Write paths** — :meth:`upsert_for_tdoc` and
   :meth:`remove_for_tdoc` delegate to the injected repo.
2. **Read path** — :meth:`search` runs the FTS5 query, then passes
   the hits through the injected :class:`EmbeddingReranker`. The
   default :class:`PassthroughReranker` returns hits verbatim so the
   service contract is testable before the embedding spec lands.
3. **Maintenance** — :meth:`rebuild` is a generator that yields
   :class:`RebuildProgress` per batch (the CLI's ``--quiet`` flag
   controls whether the consumer prints each batch);
   :meth:`status` snapshots the index for ``search index`` (no
   flags).

Both ``rebuild`` and ``upsert_for_tdoc`` update
``tdoc_search_meta`` for resume / staleness tracking. The repo
already writes ``last_indexed_at`` per upsert; ``rebuild`` adds the
``last_rebuild_at`` and ``last_indexed_uploaded_date`` keys here.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime

from doc3gpp.models.search import (
    RebuildProgress,
    SearchFilters,
    SearchHit,
    SearchIndexStatus,
)
from doc3gpp.repository.protocols import (
    EmbeddingReranker,
    SearchIndexRepository,
)

logger = logging.getLogger(__name__)


class PassthroughReranker(EmbeddingReranker):
    """Default reranker that returns hits unchanged.

    Used when the semantic search stack is not available
    (no ``[semantic]`` extra, no sqlite-vec, ``enabled=False``).
    Returns a *copy* of the input so callers may mutate the
    reranker's output without disturbing the upstream list.
    Honors ``final_limit`` by slicing.
    """

    def rerank(
        self,
        semantic_query: str,
        hits: list[SearchHit],
        final_limit: int | None = None,
        quiet: bool = False,
    ) -> list[SearchHit]:
        _ = semantic_query
        _ = quiet  # Passthrough never warns; flag accepted for Protocol parity.
        copied = list(hits)
        if final_limit is not None:
            return copied[:final_limit]
        return copied


class SearchService:
    """High-level API for the FTS5 search subsystem."""

    def __init__(
        self,
        repo: SearchIndexRepository,
        reranker: EmbeddingReranker,
        quiet: bool = False,
    ) -> None:
        self._repo = repo
        self._reranker = reranker
        self._quiet = quiet

    def upsert_for_tdoc(self, tdoc_id: str) -> None:
        """Rebuild the FTS5 row for ``tdoc_id``.

        Best-effort: a single bad row logs a warning and does not
        abort the caller. The CLI / service layers that invoke this
        already wrap it in their own broad ``except`` for the
        not-our-problem cases (no DB, no FTS5, missing optional
        extras).
        """
        try:
            self._repo.upsert(tdoc_id)
        except Exception as exc:
            logger.warning(
                "search upsert failed for tdoc_id=%s: %s", tdoc_id, exc
            )

    def remove_for_tdoc(self, tdoc_id: str) -> None:
        try:
            self._repo.remove(tdoc_id)
        except Exception as exc:
            logger.warning(
                "search remove failed for tdoc_id=%s: %s", tdoc_id, exc
            )

    def search(
        self, query: str, filters: SearchFilters,
    ) -> list[SearchHit]:
        """Run FTS5 ``MATCH`` + rerank + return hits.

        The raw query is normalised into a valid FTS5 ``MATCH``
        expression via :class:`SearchQueryBuilder` (the same path the
        CLI uses) so jargon like ``nb-iot`` is quoted rather than
        parsed as a column-minus-token; a stopwords-only or empty
        query raises :class:`SearchQueryError`. The *raw* query is
        forwarded to :meth:`EmbeddingReranker.rerank` because the
        semantic reranker embeds the user's text verbatim.

        Forwards :attr:`_quiet` to :meth:`EmbeddingReranker.rerank` so
        the :class:`SemanticReranker`'s one-shot empty-vector warning
        is suppressed under ``--quiet``. ``PassthroughReranker``
        accepts and ignores the flag.
        """
        from doc3gpp.cli_filters import SearchQueryBuilder

        match_expr = SearchQueryBuilder(query).build()
        hits = self._repo.search(match_expr, filters)
        return self._reranker.rerank(query, hits, quiet=self._quiet)

    def rebuild(
        self,
        batch_size: int,
        resume: bool,
        stale_only: bool,
        quiet: bool,
    ) -> Iterator[RebuildProgress]:
        """Yield per-1%-of-progress updates during an index rebuild.

        For a corpus of ``N`` TDocs, yields ~100 times (one per 1%
        boundary crossed), plus a guaranteed final yield at 100%.
        The CLI renders this as a tqdm bar with `bar.update(delta)`.
        Cursor is persisted to ``tdoc_search_meta`` per batch so a
        crashed rebuild can resume.
        """
        after_id = self._repo.get_resume_cursor() if resume else None
        # When the operator runs without --resume, they want a
        # truly fresh start. Clear any stale cursor so a subsequent
        # crash + --resume picks up where this rebuild was
        # interrupted, not from some long-ago cursor.
        if not resume:
            self._repo.clear_resume_cursor()
            after_id = None
        total = self._repo.count_tdocs_to_index(
            stale_only=stale_only, after_id=after_id,
        )
        # Last percent value we yielded at (so we yield once per
        # 1% crossing). Start at 0 so we don't fire a "0%" yield
        # when processed=1 and total=13693 (1*100//13693=0).
        last_yielded_pct = 0
        processed = 0
        batches = self._repo.rebuild_batch(
            batch_size=batch_size, after_id=after_id, stale_only=stale_only,
        )
        for batch in batches:
            for tdoc_id in batch:
                self.upsert_for_tdoc(tdoc_id)
                processed += 1
                # Guard against total=0 (shouldn't happen if there
                # is work, but stay defensive).
                pct = (processed * 100 // total) if total > 0 else 100
                if pct > last_yielded_pct:
                    yield RebuildProgress(
                        processed=processed,
                        total=total,
                        current_tdoc_id=tdoc_id,
                    )
                    last_yielded_pct = pct
            self._repo.set_resume_cursor(batch[-1])
        self._touch_rebuild_at()
        self._touch_indexed_uploaded_date()
        if not quiet:
            logger.info(
                "search rebuild complete: processed=%d total=%d",
                processed,
                total,
            )

    def status(self) -> SearchIndexStatus:
        return self._repo.status()

    # ------------------------------------------------------------------
    # Internal meta-table helpers
    # ------------------------------------------------------------------

    def _touch_rebuild_at(self) -> None:
        from sqlalchemy import text
        from doc3gpp.storage.db.session import get_engine

        engine = get_engine()
        if engine.dialect.name != "sqlite":
            return
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO tdoc_search_meta (key, value)
                    VALUES ('last_rebuild_at', :ts)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """
                ),
                {"ts": datetime.utcnow().isoformat(timespec="seconds")},
            )

    def _touch_indexed_uploaded_date(self) -> None:
        from sqlalchemy import text
        from doc3gpp.storage.db.session import get_engine

        engine = get_engine()
        if engine.dialect.name != "sqlite":
            return
        with engine.begin() as conn:
            latest = conn.execute(
                text("SELECT MAX(uploaded_date) FROM tdocs")
            ).scalar()
            if latest is None:
                return
            conn.execute(
                text(
                    """
                    INSERT INTO tdoc_search_meta (key, value)
                    VALUES ('last_indexed_uploaded_date', :ts)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """
                ),
                {"ts": str(latest)},
            )
