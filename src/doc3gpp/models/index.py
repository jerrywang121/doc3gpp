"""Immutable request and result DTOs for resource index maintenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from doc3gpp.models.search import SearchIndexStatus


@dataclass(frozen=True, slots=True)
class IndexRequest:
    rebuild: bool = False
    rebuild_embeddings: bool = False
    rebuild_all: bool = False
    batch: int | None = None
    resume: bool = False
    stale_only: bool = False


@dataclass(frozen=True, slots=True)
class IndexComponentStatus:
    """Serializable status for one optional index component."""

    available: bool
    status: SearchIndexStatus | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "status": _status_to_dict(self.status),
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class IndexStatus:
    """Status snapshot containing independent FTS5 and vector components."""

    fts5: IndexComponentStatus
    vector: IndexComponentStatus

    def to_dict(self) -> dict[str, Any]:
        return {"fts5": self.fts5.to_dict(), "vector": self.vector.to_dict()}


@dataclass(frozen=True, slots=True)
class IndexRebuildResult:
    """Processed counts returned by a resource index rebuild."""

    fts5_processed: int = 0
    vector_processed: int = 0

    @property
    def processed_fts5(self) -> int:
        return self.fts5_processed

    @property
    def processed_vector(self) -> int:
        return self.vector_processed

    def to_dict(self) -> dict[str, int]:
        return {
            "fts5_processed": self.fts5_processed,
            "vector_processed": self.vector_processed,
        }


def _status_to_dict(status: SearchIndexStatus | None) -> dict[str, Any] | None:
    if status is None:
        return None
    return {
        "enabled": status.enabled,
        "row_count": status.row_count,
        "last_rebuild_at": _serialize_scalar(status.last_rebuild_at),
        "last_indexed_uploaded_date": _serialize_scalar(
            status.last_indexed_uploaded_date
        ),
        "latest_tdocs_uploaded_date": _serialize_scalar(
            status.latest_tdocs_uploaded_date
        ),
        "is_stale": status.is_stale,
        "embedding_dim": status.embedding_dim,
        "embedding_model": status.embedding_model,
    }


def _serialize_scalar(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


__all__ = [
    "IndexComponentStatus",
    "IndexRebuildResult",
    "IndexRequest",
    "IndexStatus",
]
