from __future__ import annotations

from typing import Protocol

from doc3gpp.models.meeting import Meeting
from doc3gpp.models.tdoc import TDoc


class TDocRepository(Protocol):
    """Storage operations used by service layer."""

    def upsert(self, tdoc: TDoc) -> None:
        ...

    def list(self, limit: int = 20) -> list[TDoc]:
        ...


class MeetingRepository(Protocol):
    """Storage operations used by meetings sync service."""

    def upsert_many(self, meetings: list[Meeting]) -> int:
        ...

    def list(self, limit: int = 50) -> list[Meeting]:
        ...
