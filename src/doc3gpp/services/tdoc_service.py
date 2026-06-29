from __future__ import annotations

from doc3gpp.models.tdoc import TDoc
from doc3gpp.repository.protocols import TDocRepository
from doc3gpp.scraping.ftp_source import fetch_tdocs_from_meeting_ftp


class TDocService:
    """Service methods for persisting and retrieving TDoc records."""

    def __init__(self, repository: TDocRepository) -> None:
        self._repository = repository

    def save(self, tdoc: TDoc) -> None:
        self._repository.upsert(tdoc)

    def list_recent(self, limit: int = 20) -> list[TDoc]:
        return self._repository.list(limit=limit)

    def sync_from_meeting_ftp(self, ftp_url: str, meeting: str | None = None) -> int:
        tdocs = fetch_tdocs_from_meeting_ftp(ftp_url=ftp_url, meeting=meeting)
        for tdoc in tdocs:
            self._repository.upsert(tdoc)
        return len(tdocs)
