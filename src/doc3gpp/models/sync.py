"""Shared result types for sync operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    """Outcome of a single sync attempt.

    Attributes:
        status: ``synced`` when upstream data was fetched and persisted,
            ``skipped`` when the sync interval rules decided no work was 
            needed.
        reason: Human-readable sentence surfaced on the CLI.
        synced_count: Number of rows written (meeting calendar rows for
            ``meeting sync``; TDoc rows for ``tdoc sync``). ``None`` when
            the sync was skipped.
        file_count: Number of auxiliary TDoc files written; only set for
            ``tdoc sync`` when synced.
    """

    status: Literal["synced", "skipped"]
    reason: str
    synced_count: int | None = None
    file_count: int | None = None
