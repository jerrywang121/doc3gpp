from __future__ import annotations

import logging

from doc3gpp.models.tdoc import TDocWithMeeting
from doc3gpp.repository.protocols import TDocRepository
from doc3gpp.scraping.ftp_source import fetch_tdocs_from_meeting_ftp

logger = logging.getLogger(__name__)


class TDocService:
    """Service methods for persisting and retrieving TDoc records."""

    def __init__(self, repository: TDocRepository) -> None:
        """Initialize the TDoc service with a repository backing the TDoc storage."""
        self._repository = repository

    def list_recent_with_meeting(
        self,
        limit: int = 20,
        tsg: str | None = None,
        meeting_like: str | None = None,
        meeting_id: int | None = None,
        year: int | None = None,
        status: str | None = None,
        cr_cat: str | None = None,
        spec: str | None = None,
        wi: str | None = None,
        revision_of: str | None = None,
        revised_to: str | None = None,
        title: str | None = None,
        ftp_url: str | None = None,
        source: str | None = None,
        tdoc_type: str | None = None,
        uploaded_date: str | None = None,
    ) -> list[TDocWithMeeting]:
        """Return recent TDoc records joined with their parent meeting name.

        Convenience for CLI / export paths that want to show ``meeting_name``
        next to TDoc fields without exposing the DTO composition to callers.

        The filter parameters accept the rich grammar described in
        :mod:`doc3gpp.cli_filters` — ``null`` / ``not-null`` match the
        column's nullability, a leading ``!`` flips the comparison to
        ``NOT LIKE`` (with the ``!`` consumed), and anything else is
        treated as a ``LIKE`` pattern. ``uploaded_date`` additionally
        accepts a ``"<op> 'YYYY-MM-DD'"`` comparison.
        """
        return self._repository.list_with_meeting(
            limit=limit,
            tsg=tsg,
            meeting_like=meeting_like,
            meeting_id=meeting_id,
            year=year,
            status=status,
            cr_cat=cr_cat,
            spec=spec,
            wi=wi,
            revision_of=revision_of,
            revised_to=revised_to,
            title=title,
            ftp_url=ftp_url,
            source=source,
            tdoc_type=tdoc_type,
            uploaded_date=uploaded_date,
        )

    def sync_from_meeting_ftp(self, ftp_url: str, meeting_id: int | None = None) -> int:
        logger.info("Syncing TDocs from FTP %s for meeting_id %s", ftp_url, meeting_id)
        tdocs = fetch_tdocs_from_meeting_ftp(ftp_url=ftp_url, meeting_id=meeting_id)
        stored = self._repository.upsert_many(tdocs)
        logger.info("Stored %s TDoc records", stored)
        return stored
