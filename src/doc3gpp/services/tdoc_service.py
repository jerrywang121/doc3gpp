from __future__ import annotations

import logging

from doc3gpp.models.tdoc import TDoc, TDocWithMeeting
from doc3gpp.repository.protocols import TDocRepository
from doc3gpp.scraping.ftp_source import fetch_tdocs_from_meeting_ftp

logger = logging.getLogger(__name__)


class TDocService:
    """Service methods for persisting and retrieving TDoc records."""

    def __init__(self, repository: TDocRepository) -> None:
        """Initialize the TDoc service with a repository backing the TDoc storage."""
        self._repository = repository

    def list_recent(
        self,
        limit: int = 20,
        tsg: str | None = None,
        meeting_like: str | None = None,
        meeting_id: int | None = None,
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
            "Listing %s recent TDocs with filters tsg=%s meeting_like=%s meeting_id=%s year=%s source_like=%s spec_like=%s wi_like=%s title_like=%s cat_like=%s status_like=%s type_like=%s",
            limit,
            tsg,
            meeting_like,
            meeting_id,
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
            meeting_id=meeting_id,
            year=year,
            source_like=source_like,
            spec_like=spec_like,
            wi_like=wi_like,
            title_like=title_like,
            cat_like=cat_like,
            status_like=status_like,
            type_like=type_like,
        )

    def list_recent_with_meeting(
        self,
        limit: int = 20,
        tsg: str | None = None,
        meeting_like: str | None = None,
        meeting_id: int | None = None,
        year: int | None = None,
        source_like: str | None = None,
        spec_like: str | None = None,
        wi_like: str | None = None,
        title_like: str | None = None,
        cat_like: str | None = None,
        status_like: str | None = None,
        type_like: str | None = None,
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
        """Like :meth:`list_recent` but each row carries its meeting display name.

        Convenience for CLI / export paths that want to show ``meeting_name``
        next to TDoc fields without exposing the DTO composition to callers.

        The un-suffixed parameters (``status``, ``cr_cat``, ``spec``, ``wi``,
        ``revision_of``, ``revised_to``, ``title``, ``ftp_url``, ``source``,
        ``tdoc_type``, ``uploaded_date``) accept the rich filter grammar
        described in :mod:`doc3gpp.cli_filters` (``null`` / ``not-null`` /
        SQL ``LIKE`` for text columns; date comparison for ``uploaded_date``).
        When both the ``*_like`` and un-suffixed forms of the same column are
        passed the predicates are combined with ``AND`` (narrowing, not
        overriding).
        """
        return self._repository.list_with_meeting(
            limit=limit,
            tsg=tsg,
            meeting_like=meeting_like,
            meeting_id=meeting_id,
            year=year,
            source_like=source_like,
            spec_like=spec_like,
            wi_like=wi_like,
            title_like=title_like,
            cat_like=cat_like,
            status_like=status_like,
            type_like=type_like,
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
