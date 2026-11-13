"""Index lifecycle orchestration (incremental updates, status, rebuild) for LISTSERV archives."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from threading import Lock
from typing import Any, Literal

from doc3gpp.models.index import (
    IndexComponentStatus,
    IndexRebuildResult,
    IndexRequest,
    IndexStatus,
)
from doc3gpp.models.listserv import ListservRecord
from doc3gpp.models.search import SearchIndexStatus, SearchUnavailableError
from doc3gpp.models.semantic_chunking import chunking_fingerprint
from doc3gpp.models.semantic_search import (
    EmbedderUnavailableError,
    SemanticSearchUnavailableError,
    VectorIndexUnavailableError,
)
from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import Settings

logger = logging.getLogger(__name__)

_DEFAULT_BATCH = 100


class IndexRebuildCancelled(Exception):
    """Raised when a rebuild stops early on a cancellation request."""


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class ListservIndexService:
    """Coordinate LISTSERV FTS5 and vector index freshness against stored source revisions."""

    def __init__(
        self,
        *,
        source_repository: Any,
        fts_repository: Any | None = None,
        semantic_service: Any | None = None,
        fts_factory: Callable[[], Any | None] | None = None,
        semantic_factory: Callable[[], Any | None] | None = None,
        vector_status_factory: Callable[[], tuple[Any, str | None] | None] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._source = source_repository
        self._fts = fts_repository
        self._semantic = semantic_service
        self._fts_factory = fts_factory if fts_repository is None else None
        self._semantic_factory = semantic_factory if semantic_service is None else None
        self._owns_semantic_service = (
            semantic_service is None and semantic_factory is not None
        )
        self._semantic_service_closed = False
        self._semantic_lock = Lock()
        self._vector_status_factory = vector_status_factory
        self._settings = settings

    def _resolve_settings(self) -> Settings:
        return self._settings if self._settings is not None else get_settings()

    def _get_fts(self) -> Any | None:
        if self._fts is None and self._fts_factory is not None:
            factory, self._fts_factory = self._fts_factory, None
            self._fts = factory()
        return self._fts

    def _get_semantic(self) -> Any | None:
        if self._semantic is None:
            with self._semantic_lock:
                if self._semantic is None and self._semantic_factory is not None:
                    factory, self._semantic_factory = self._semantic_factory, None
                    self._semantic = factory()
                    self._semantic_service_closed = False
        return self._semantic

    def close(self) -> None:
        """Close a lazily created semantic service owned by this index facade."""
        if self._semantic_service_closed:
            return
        self._semantic_service_closed = True
        if not self._owns_semantic_service or self._semantic is None:
            return
        close_fn = getattr(self._semantic, "close", None)
        if callable(close_fn):
            close_fn()

    def _fingerprint(self) -> str:
        cfg = self._resolve_settings().semantic_search
        return chunking_fingerprint(cfg.chunk_size, cfg.chunk_overlap)

    # ------------------------------------------------------------------ status

    def status(self) -> IndexStatus:
        return IndexStatus(fts5=self._fts_status(), vector=self._vector_status())

    def _fts_status(self) -> IndexComponentStatus:
        fts = self._get_fts()
        if fts is None:
            return IndexComponentStatus(available=False, error="unavailable")
        settings = self._resolve_settings()
        source = self._source.revisions()
        indexed = fts.indexed_revisions()
        meta = fts.meta()
        stale = bool(self._fts_pending(source, indexed) or set(indexed) - set(source))
        return IndexComponentStatus(
            available=settings.search.enabled,
            status=SearchIndexStatus(
                enabled=settings.search.enabled,
                row_count=len(indexed),
                last_rebuild_at=_parse_ts(meta.get("last_rebuild_at")),
                last_indexed_uploaded_date=_parse_ts(meta.get("last_indexed_at")),
                latest_tdocs_uploaded_date=None,
                is_stale=stale,
            ),
        )

    def _vector_status(self) -> IndexComponentStatus:
        """Report vector freshness from SQL state and configured model/chunk settings only.

        Never resolves the live embedding dimension or encodes text; those remain
        confined to explicit rebuild/search/update operations.
        """
        settings = self._resolve_settings()
        if self._semantic is not None:
            vec = self._semantic.vector_repository
            model = getattr(self._semantic.embedder, "model_name", None)
        elif self._vector_status_factory is not None:
            resolved = self._vector_status_factory()
            if resolved is None:
                return IndexComponentStatus(available=False, error="unavailable")
            vec, model = resolved
        else:
            return IndexComponentStatus(available=False, error="unavailable")
        source = self._source.revisions()
        indexed = vec.indexed_revisions()
        meta = vec.meta()
        stale = bool(
            self._vector_pending(source, indexed)
            or set(indexed) - set(source)
            or (
                vec.stored_model is not None
                and model is not None
                and vec.stored_model != model
            )
        )
        return IndexComponentStatus(
            available=settings.semantic_search.enabled,
            status=SearchIndexStatus(
                enabled=settings.semantic_search.enabled,
                row_count=vec.chunk_count(),
                last_rebuild_at=_parse_ts(meta.get("last_rebuild_at")),
                last_indexed_uploaded_date=_parse_ts(meta.get("last_indexed_at")),
                latest_tdocs_uploaded_date=None,
                is_stale=stale,
                embedding_dim=vec.stored_dim,
                embedding_model=vec.stored_model,
            ),
        )

    @staticmethod
    def _fts_pending(
        source: dict[str, int], indexed: dict[str, int]
    ) -> list[str]:
        return sorted(u for u, rev in source.items() if indexed.get(u) != rev)

    def _vector_pending(
        self, source: dict[str, int], indexed: dict[str, tuple[int, str]]
    ) -> list[str]:
        fp = self._fingerprint()
        return sorted(u for u, rev in source.items() if indexed.get(u) != (rev, fp))

    # ------------------------------------------------------- incremental update

    def update_records(
        self,
        records: Sequence[ListservRecord],
        *,
        operation: Literal["sync", "parse"],
    ) -> dict[str, str]:
        """Update eligible indexes for committed ``records``; return sanitized failures."""
        failures: dict[str, list[str]] = {}
        if not records:
            return {}
        settings = self._resolve_settings()
        cfg = settings.listserv
        fts_on = settings.search.enabled and getattr(cfg, f"auto_index_on_{operation}")
        vec_on = settings.semantic_search.enabled and getattr(
            cfg, f"auto_embed_on_{operation}"
        )

        if fts_on:
            fts = self._get_fts()
            if fts is not None:
                try:
                    indexed = fts.indexed_revisions()
                    todo = [
                        r for r in records if indexed.get(r.message.url) != r.source_revision
                    ]
                    if todo:
                        try:
                            fts.upsert_many(todo)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "LISTSERV FTS5 update failed: %s", type(exc).__name__
                            )
                            for r in todo:
                                failures.setdefault(r.message.url, []).append(
                                    f"FTS5 index update failed ({type(exc).__name__})"
                                )
                except Exception as exc:  # noqa: BLE001
                    for r in records:
                        failures.setdefault(r.message.url, []).append(
                            f"FTS5 index update failed ({type(exc).__name__})"
                        )

        if vec_on:
            sem = self._get_semantic()
            if sem is not None:
                try:
                    indexed_vec = sem.vector_repository.indexed_revisions()
                    fp = self._fingerprint()
                    for r in records:
                        url = r.message.url
                        if indexed_vec.get(url) == (r.source_revision, fp):
                            continue
                        try:
                            sem.index_record(r)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "LISTSERV vector update failed: %s",
                                type(exc).__name__,
                            )
                            failures.setdefault(url, []).append(
                                f"vector index update failed ({type(exc).__name__})"
                            )
                except Exception as exc:  # noqa: BLE001
                    for r in records:
                        failures.setdefault(r.message.url, []).append(
                            f"vector index update failed ({type(exc).__name__})"
                        )

        return {url: "; ".join(parts) for url, parts in failures.items()}

    # ------------------------------------------------------------------ rebuild

    def rebuild(
        self,
        request: IndexRequest,
        *,
        quiet: bool = False,
        on_progress: Callable[[str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> IndexStatus | IndexRebuildResult:
        """Rebuild the requested index components.

        ``quiet`` is retained for coordinator signature compatibility; progress is
        emitted only through ``on_progress``.
        """
        if request.rebuild_all and (request.rebuild or request.rebuild_embeddings):
            raise ValueError(
                "rebuild_all is mutually exclusive with rebuild and rebuild_embeddings"
            )
        if request.batch is not None and request.batch < 1:
            raise ValueError("batch must be at least 1")
        if not (request.rebuild or request.rebuild_embeddings or request.rebuild_all):
            return self.status()

        batch = request.batch or self._default_batch()
        fts_processed = vector_processed = 0
        if request.rebuild or request.rebuild_all:
            fts_processed = self._rebuild_fts(batch, request, on_progress, cancelled)
        if request.rebuild_embeddings or request.rebuild_all:
            vector_processed = self._rebuild_vector(
                batch, request, on_progress, cancelled
            )
        return IndexRebuildResult(
            fts5_processed=fts_processed, vector_processed=vector_processed
        )

    def _default_batch(self) -> int:
        value = getattr(self._resolve_settings().search, "rebuild_batch_size", None)
        return int(value) if value else _DEFAULT_BATCH

    @staticmethod
    def _check_cancel(cancelled: Callable[[], bool] | None) -> None:
        if cancelled is not None and cancelled():
            raise IndexRebuildCancelled("LISTSERV index rebuild cancelled")

    def _rebuild_fts(
        self,
        batch: int,
        request: IndexRequest,
        on_progress: Callable[[str], None] | None,
        cancelled: Callable[[], bool] | None,
    ) -> int:
        fts = self._get_fts()
        if fts is None:
            raise SearchUnavailableError("LISTSERV FTS5 index is not available")
        full = not (request.resume or request.stale_only)
        if full:
            fts.clear()
        source = self._source.revisions()
        indexed = fts.indexed_revisions()
        for orphan in sorted(set(indexed) - set(source)):
            fts.remove(orphan)
        pending = self._fts_pending(source, {} if full else indexed)
        return self._run_batches(
            "listserv fts5",
            pending,
            batch,
            lambda records: fts.upsert_many(records, stamp=False),
            on_progress,
            cancelled,
            finalize=fts.mark_rebuilt,
        )

    def _rebuild_vector(
        self,
        batch: int,
        request: IndexRequest,
        on_progress: Callable[[str], None] | None,
        cancelled: Callable[[], bool] | None,
    ) -> int:
        sem = self._get_semantic()
        if sem is None:
            raise SemanticSearchUnavailableError(
                "LISTSERV vector index is not available"
            )
        vec = sem.vector_repository
        embedder = sem.embedder
        full = not (request.resume or request.stale_only)
        try:
            if full:
                vec.reset_for_rebuild(embedder.dim, getattr(embedder, "model_name", None))
            else:
                vec.verify_compatible(embedder.dim, getattr(embedder, "model_name", None))
        except (VectorIndexUnavailableError, EmbedderUnavailableError) as exc:
            raise SemanticSearchUnavailableError(str(exc)) from exc
        source = self._source.revisions()
        indexed = vec.indexed_revisions()
        for orphan in sorted(set(indexed) - set(source)):
            vec.remove(orphan)
        pending = self._vector_pending(source, {} if full else indexed)

        def _write(records: list[ListservRecord]) -> None:
            for record in records:
                sem.index_record(record, stamp=False)

        return self._run_batches(
            "listserv vector",
            pending,
            batch,
            _write,
            on_progress,
            cancelled,
            finalize=vec.mark_rebuilt,
        )

    def _run_batches(
        self,
        prefix: str,
        pending: list[str],
        batch: int,
        write: Callable[[list[ListservRecord]], None],
        on_progress: Callable[[str], None] | None,
        cancelled: Callable[[], bool] | None,
        *,
        finalize: Callable[[], None],
    ) -> int:
        total = len(pending)
        done = 0
        for start in range(0, total, batch):
            self._check_cancel(cancelled)
            urls = pending[start : start + batch]
            records = list(self._source.get_many(urls).values())
            write(records)
            done += len(urls)
            if on_progress is not None:
                on_progress(f"{prefix}: {done}/{total}")
        self._check_cancel(cancelled)
        finalize()
        return done


__all__ = ["IndexRebuildCancelled", "ListservIndexService"]
