from __future__ import annotations

from typing import Protocol

from doc3gpp.models.meeting import Meeting
from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tsg import Tsg


class TDocRepository(Protocol):
    """Storage operations used by service layer."""

    def upsert(self, tdoc: TDoc) -> None:
        """Insert or update a TDoc record in storage."""
        ...

    def list(
        self,
        limit: int = 20,
        tsg: str | None = None,
        meeting_like: str | None = None,
        year: int | None = None,
        source_like: str | None = None,
        spec_like: str | None = None,
        wi_like: str | None = None,
        title_like: str | None = None,
        cat_like: str | None = None,
        status_like: str | None = None,
        type_like: str | None = None,
    ) -> list[TDoc]:
        """Return a list of recent TDoc records with optional filters."""
        ...


class MeetingRepository(Protocol):
    """Storage operations used by meetings sync service."""

    def upsert_many(self, meetings: list[Meeting]) -> int:
        """Save or update multiple meeting records."""
        ...

    def list(self, limit: int = 50) -> list[Meeting]:
        """Return a list of recent meeting records."""
        ...

    def get_by_id(self, meeting_id: int) -> Meeting | None:
        """Return a meeting record by its numeric ID."""
        ...

    def get_by_name(self, meeting_name: str) -> Meeting | None:
        """Return a meeting record by its exact meeting name."""
        ...


class TsgRepository(Protocol):
    """Storage operations for 3GPP TSG reference records."""

    def upsert_many(self, tsgs: list[Tsg]) -> int:
        """Insert or update multiple TSG records keyed by ``tsg_name``."""
        ...

    def list_all(self) -> list[Tsg]:
        """Return all TSG records, ordered by ``tsg_name``."""
        ...

    def get_by_short_name(self, short_name: str) -> Tsg | None:
        """Return a TSG record by its short name (case-insensitive)."""
        ...

    def get_by_tsg_name(self, tsg_name: str) -> Tsg | None:
        """Return a TSG record by its full ``tsg_name`` (case-insensitive)."""
        ...

    def count(self) -> int:
        """Return the number of stored TSG records."""
        ...
