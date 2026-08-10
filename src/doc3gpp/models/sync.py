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
    version_count: int | None = None


@dataclass(frozen=True, slots=True)
class BulkSyncFailure:
    """A single failure captured during a bulk sync sweep.

    Attributes:
        meeting_id: The meeting ID that failed.
        error: The exception class name.
        reason: The exception message.
    """

    meeting_id: int
    error: str
    reason: str


@dataclass(frozen=True, slots=True)
class BulkSyncOutcome:
    """Outcome of syncing every tracked meeting in a bulk sweep.

    Attributes:
        outcomes: Per-meeting ``SyncOutcome`` records (synced or skipped).
        failures: Per-meeting failures that did not abort the sweep.
    """

    outcomes: tuple[SyncOutcome, ...] = ()
    failures: tuple[BulkSyncFailure, ...] = ()

    @property
    def synced_count(self) -> int:
        """Return the number of meetings that were synced."""
        return sum(1 for outcome in self.outcomes if outcome.status == "synced")

    @property
    def skipped_count(self) -> int:
        """Return the number of meetings that were skipped."""
        return sum(1 for outcome in self.outcomes if outcome.status == "skipped")

    @property
    def failed_count(self) -> int:
        """Return the number of meetings that failed."""
        return len(self.failures)

    @property
    def total(self) -> int:
        """Return the total number of meetings processed."""
        return len(self.outcomes) + len(self.failures)

    def add_outcome(self, outcome: SyncOutcome) -> BulkSyncOutcome:
        """Return a new outcome with ``outcome`` appended."""
        return BulkSyncOutcome(
            outcomes=self.outcomes + (outcome,),
            failures=self.failures,
        )

    def add_failure(self, failure: BulkSyncFailure) -> BulkSyncOutcome:
        """Return a new outcome with ``failure`` appended."""
        return BulkSyncOutcome(
            outcomes=self.outcomes,
            failures=self.failures + (failure,),
        )
