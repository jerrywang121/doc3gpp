"""FTS5 search over spec-doc chunks. Mirrors SearchService (upsert/search/rebuild/status)."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime

from doc3gpp.models.search import RebuildProgress, SearchIndexStatus
from doc3gpp.models.spec_doc import SpecDocHit, SpecDocSearchFilters
from doc3gpp.repository.protocols import SpecDocSearchRepository

logger = logging.getLogger(__name__)


class SpecDocSearchService:
    """High-level API for the spec-doc FTS5 subsystem.

    Write paths (:meth:`upsert_for_version` / :meth:`remove_for_version`)
    are best-effort (a single bad version logs a warning); the read path
    (:meth:`search`) passes the raw query through — the repo builds the
    ``MATCH`` expression internally via ``SearchQueryBuilder``.
    :meth:`rebuild` is a generator yielding :class:`RebuildProgress`
    per version; :meth:`status` snapshots the index.
    """

    def __init__(
        self,
        repo: SpecDocSearchRepository | None = None,
        quiet: bool = False,
    ) -> None:
        if repo is None:
            from doc3gpp.storage.repositories.spec_doc_search_sql import (
                SQLAlchemySpecDocSearchRepository,
            )

            repo = SQLAlchemySpecDocSearchRepository()
        self._repo = repo
        self._quiet = quiet

    def upsert_for_version(self, spec_id: str, version: str) -> None:
        """Rebuild the FTS5 rows for ``(spec_id, version)`` (best-effort)."""
        try:
            self._repo.upsert_for_version(spec_id, version)
        except Exception as exc:  # noqa: BLE001 - one bad version must not abort the caller
            logger.warning(
                "spec-doc upsert failed for %s@%s: %s", spec_id, version, exc
            )

    def remove_for_version(self, spec_id: str, version: str) -> None:
        """Delete the FTS5 rows for ``(spec_id, version)`` (best-effort)."""
        try:
            self._repo.remove_for_version(spec_id, version)
        except Exception as exc:  # noqa: BLE001 - best-effort, mirrors upsert
            logger.warning(
                "spec-doc remove failed for %s@%s: %s", spec_id, version, exc
            )

    def search(
        self,
        query: str,
        filters: SpecDocSearchFilters,
        snippet_tokens: int | None = None,
    ) -> list[SpecDocHit]:
        """Run FTS5 ``MATCH`` + filters + ``bm25()`` scoring.

        ``query`` is raw text; ``snippet_tokens`` optionally overrides
        the configured ``Settings.search.snippet_tokens`` for one call.
        """
        if snippet_tokens is None:
            return self._repo.search(query, filters)
        return self._repo.search(query, filters, snippet_tokens=snippet_tokens)

    def rebuild(
        self, *, batch_size: int, resume: bool, stale_only: bool, quiet: bool
    ) -> Iterator[RebuildProgress]:
        """Yield progress updates while re-indexing ``(spec_id, version)`` pairs.

        ``resume=True`` picks up from the persisted
        ``spec_doc_search_meta`` cursor; otherwise any stale cursor is
        cleared first so the run starts at the first pair.
        ``stale_only=True`` processes only versions whose
        ``spec_doc_sources.parsed_at`` is newer than the
        ``last_indexed_parsed_at`` watermark. Stamps
        ``last_rebuild_at`` on completion (read by :meth:`status`).
        """
        after = self._repo.get_resume_cursor() if resume else None
        if not resume:
            self._repo.clear_resume_cursor()
            after = None
        total = self._repo.count_versions_to_index(
            stale_only=stale_only, after_id=after
        )
        processed, last_pct = 0, 0
        for batch in self._repo.rebuild_batch(
            batch_size=batch_size, after_id=after, stale_only=stale_only
        ):
            for spec_id, version in batch:
                self.upsert_for_version(spec_id, version)
                processed += 1
                pct = processed * 100 // total if total else 100
                if pct > last_pct:
                    yield RebuildProgress(
                        processed, total, f"{spec_id}@{version}"
                    )
                    last_pct = pct
            self._repo.set_resume_cursor(batch[-1][0] + "@" + batch[-1][1])
        self._touch_rebuild_at()
        if not quiet:
            logger.info(
                "spec-doc search rebuild complete: processed=%d total=%d",
                processed,
                total,
            )

    def status(self) -> SearchIndexStatus:
        return self._repo.status()

    def _touch_rebuild_at(self) -> None:
        from sqlalchemy import text

        from doc3gpp.storage.db.session import get_specdata_engine

        engine = get_specdata_engine()
        if engine.dialect.name != "sqlite":
            return
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO spec_doc_search_meta (key, value)
                    VALUES ('last_rebuild_at', :ts)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """
                ),
                {"ts": datetime.utcnow().isoformat(timespec="seconds")},
            )
