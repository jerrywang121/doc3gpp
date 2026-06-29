from __future__ import annotations

import logging

from doc3gpp.models.tdoc import TDoc
from doc3gpp.repository.protocols import TDocRepository
from doc3gpp.scraping.ftp_source import fetch_tdocs_from_meeting_ftp

logger = logging.getLogger(__name__)


class TDocService:
    """Service methods for persisting and retrieving TDoc records."""

    def __init__(self, repository: TDocRepository) -> None:
        """Initialize the TDoc service with a repository backing the TDoc storage."""
        self._repository = repository

    def save(self, tdoc: TDoc) -> None:
        """Save or update a single TDoc record through the repository."""
        logger.debug("Saving TDoc %s", tdoc.tdoc_id)
        self._repository.upsert(tdoc)

    def list_recent(self, limit: int = 20) -> list[TDoc]:
        logger.debug("Listing %s recent TDocs", limit)
        return self._repository.list(limit=limit)

    def sync_from_meeting_ftp(self, ftp_url: str, meeting: str | None = None) -> int:
        logger.info("Syncing TDocs from FTP %s for meeting %s", ftp_url, meeting)
        tdocs = fetch_tdocs_from_meeting_ftp(ftp_url=ftp_url, meeting=meeting)
        for tdoc in tdocs:
            self._repository.upsert(tdoc)
        logger.info("Stored %s TDoc records", len(tdocs))
        return len(tdocs)
