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

    def list_recent(
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
        logger.debug(
            "Listing %s recent TDocs with filters tsg=%s meeting_like=%s year=%s source_like=%s spec_like=%s wi_like=%s title_like=%s cat_like=%s status_like=%s type_like=%s",
            limit,
            tsg,
            meeting_like,
            year,
            source_like,
            spec_like,
            wi_like,
            title_like,
            cat_like,
            status_like,
            type_like,
        )
        return self._repository.list(
            limit=limit,
            tsg=tsg,
            meeting_like=meeting_like,
            year=year,
            source_like=source_like,
            spec_like=spec_like,
            wi_like=wi_like,
            title_like=title_like,
            cat_like=cat_like,
            status_like=status_like,
            type_like=type_like,
        )

    def sync_from_meeting_ftp(self, ftp_url: str, meeting_id: int | None = None) -> int:
        logger.info("Syncing TDocs from FTP %s for meeting_id %s", ftp_url, meeting_id)
        tdocs = fetch_tdocs_from_meeting_ftp(ftp_url=ftp_url, meeting_id=meeting_id)
        for tdoc in tdocs:
            self._repository.upsert(tdoc)
        logger.info("Stored %s TDoc records", len(tdocs))
        return len(tdocs)
