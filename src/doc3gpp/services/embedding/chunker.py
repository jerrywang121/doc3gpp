"""Re-exports of the pure semantic chunking helpers.

The implementation lives in :mod:`doc3gpp.models.semantic_chunking` so the
storage layer can use it without importing services. Existing imports from
this module keep working.
"""

from __future__ import annotations

from doc3gpp.models.semantic_chunking import (
    CHUNK_OVERLAP_DEFAULT,
    CHUNK_SIZE_DEFAULT,
    _chunks,
    plan_content_chunks,
)

__all__ = [
    "CHUNK_OVERLAP_DEFAULT",
    "CHUNK_SIZE_DEFAULT",
    "_chunks",
    "plan_content_chunks",
]
