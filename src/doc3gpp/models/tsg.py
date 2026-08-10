from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class Tsg:
    """A 3GPP Technical Specification Group (TSG) reference record.

    Attributes:
        tsg_name: Full human-readable name of the TSG (e.g. "RAN WG1").
        short_name: Canonical short code for the TSG (e.g. "R1"). Stored
            in uppercase and used as the primary identifier when matching
            user input such as ``--tsg``.
        description: Plain-text description of the group's scope.
        url: Optional URL to the 3GPP group page.
        meeting_last_sync: UTC timestamp of the last successful
            ``doc3gpp meeting sync`` for this TSG, or ``None`` if the
            calendar has never been synced.
        spec_last_sync: UTC timestamp of the last successful spec-list
            sync for this TSG, or ``None`` if the spec list has never
            been synced.
    """

    tsg_name: str
    short_name: str
    description: str
    url: str | None = None
    meeting_last_sync: datetime | None = None
    spec_last_sync: datetime | None = None
