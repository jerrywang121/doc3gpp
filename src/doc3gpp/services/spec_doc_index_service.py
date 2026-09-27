"""Resource-level orchestration for spec-document search indexes."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from doc3gpp.models.index import (
    IndexComponentStatus,
    IndexRebuildResult,
    IndexRequest,
    IndexStatus,
)
from doc3gpp.models.search import RebuildProgress, SearchUnavailableError
from doc3gpp.models.semantic_search import (
    SemanticSearchUnavailableError,
    VectorIndexUnavailableError,
)


class SpecDocIndexService:
    """Coordinate spec-document FTS5 and vector index maintenance."""

    def __init__(
        self,
        fts5_service: Any | None,
        semantic_service: Any | None,
        settings: Any,
    ) -> None:
        self._fts5 = fts5_service
        self._semantic = semantic_service
        self._settings = settings

    def status(self) -> IndexStatus:
        return IndexStatus(
            fts5=_component_status(self._fts5, SearchUnavailableError),
            vector=_component_status(
                self._semantic,
                SemanticSearchUnavailableError,
                VectorIndexUnavailableError,
            ),
        )

    def rebuild(
        self,
        request: IndexRequest,
        *,
        quiet: bool = False,
        on_progress: Callable[[str], None] | None = None,
    ) -> IndexStatus | IndexRebuildResult:
        _validate_request(request)
        if not (request.rebuild or request.rebuild_embeddings or request.rebuild_all):
            return self.status()

        batch_size = request.batch or _batch_size(self._settings)
        fts5_processed = 0
        vector_processed = 0
        if request.rebuild or request.rebuild_all:
            fts5_processed = _consume(
                self._run_fts5(
                    batch_size=batch_size,
                    request=request,
                    quiet=quiet,
                ),
                prefix="spec doc fts5",
                on_progress=on_progress,
            )
        if request.rebuild_embeddings or request.rebuild_all:
            vector_processed = _consume(
                self._run_vector(
                    batch_size=batch_size,
                    request=request,
                    quiet=quiet,
                ),
                prefix="spec doc vector",
                on_progress=on_progress,
            )
        return IndexRebuildResult(
            fts5_processed=fts5_processed,
            vector_processed=vector_processed,
        )

    def _run_fts5(
        self,
        *,
        batch_size: int,
        request: IndexRequest,
        quiet: bool,
    ) -> Iterable[RebuildProgress]:
        if self._fts5 is None:
            raise SearchUnavailableError("spec doc FTS5 index is not available")
        return self._fts5.rebuild(
            batch_size=batch_size,
            resume=request.resume,
            stale_only=request.stale_only,
            quiet=quiet,
        )

    def _run_vector(
        self,
        *,
        batch_size: int,
        request: IndexRequest,
        quiet: bool,
    ) -> Iterable[RebuildProgress]:
        if self._semantic is None:
            raise SemanticSearchUnavailableError(
                "spec doc vector index is not available"
            )
        try:
            yield from self._semantic.rebuild_embeddings(
                batch_size=batch_size,
                resume=request.resume,
                stale_only=request.stale_only,
                quiet=quiet,
            )
        except VectorIndexUnavailableError as exc:
            raise SemanticSearchUnavailableError(str(exc)) from exc


def _batch_size(settings: Any) -> int:
    spec_doc = getattr(settings, "spec_doc", None)
    configured = getattr(spec_doc, "rebuild_batch_size", None)
    if configured is not None:
        return configured
    return settings.search.rebuild_batch_size


def _consume(
    progress: Iterable[RebuildProgress],
    *,
    prefix: str,
    on_progress: Callable[[str], None] | None,
) -> int:
    processed = 0
    for update in progress:
        processed = max(processed, update.processed)
        if on_progress is not None:
            on_progress(
                f"{prefix}: {update.processed}/{update.total} "
                f"{update.current_tdoc_id}"
            )
    return processed


def _component_status(
    service: Any | None,
    *unavailable_errors: type[Exception],
) -> IndexComponentStatus:
    if service is None:
        return IndexComponentStatus(available=False, error="unavailable")
    try:
        status = service.status()
    except unavailable_errors as exc:
        return IndexComponentStatus(available=False, error=str(exc))
    return IndexComponentStatus(available=status.enabled, status=status)


def _validate_request(request: IndexRequest) -> None:
    if request.rebuild_all and (request.rebuild or request.rebuild_embeddings):
        raise ValueError(
            "rebuild_all is mutually exclusive with rebuild and "
            "rebuild_embeddings"
        )


__all__ = ["SpecDocIndexService"]
