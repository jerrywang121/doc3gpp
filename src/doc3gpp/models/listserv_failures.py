"""Shared classification for LISTSERV derived-index failures."""

from __future__ import annotations

INDEX_FAILURE_PREFIXES: tuple[str, ...] = (
    "index update failed (",
    "fts5 index update failed (",
    "vector index update failed (",
)


def is_index_failure(reason: str) -> bool:
    """Return whether ``reason`` describes a failure after source commit."""
    return reason.lower().startswith(INDEX_FAILURE_PREFIXES)


__all__ = ["INDEX_FAILURE_PREFIXES", "is_index_failure"]
