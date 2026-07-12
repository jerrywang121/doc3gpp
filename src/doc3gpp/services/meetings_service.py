from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from datetime import timezone
import logging

from doc3gpp.models.meeting import Meeting
from doc3gpp.repository.protocols import MeetingRepository
from doc3gpp.repository.protocols import TsgRepository
from doc3gpp.scraping.calendar_source import fetch_calendar

logger = logging.getLogger(__name__)


class MeetingService:
    """Sync and query 3GPP meeting records.

    This service is responsible for fetching meeting calendar data from the 3GPP
    site and persisting it through the configured repository implementation.
    """

    def __init__(
        self,
        repository: MeetingRepository,
        tsg_repository: TsgRepository | None = None,
    ) -> None:
        """Initialize the service with a repository backing the meeting storage."""
        self._repository = repository
        self._tsg_repository = tsg_repository

    def sync(
        self,
        meetings_url: str,
        tsg: str | None = None,
    ) -> int:
        """Fetch meetings from the 3GPP calendar URL and persist all results.

        Args:
            meetings_url: DynaReport meeting calendar URL to fetch.
            tsg: Canonical TSG short name (e.g. ``R5``) owning the meeting
                page being scraped. When provided, stamped onto every
                parsed :class:`Meeting` so the persisted ``meetings.tsg``
                FK is populated and downstream ``meeting list --tsg``
                filters can scope by owning group. ``None`` leaves the
                field un-stamped (useful for tests / bulk imports).

        Returns:
            The number of meeting rows written (insert or update).
        """
        logger.info("Syncing meetings from %s", meetings_url)
        meetings = fetch_calendar(meetings_url)
        logger.debug("Fetched %s meetings from calendar", len(meetings))
        if tsg is not None:
            canonical_tsg = tsg.upper()
            meetings = [replace(m, tsg=canonical_tsg) for m in meetings]
        written = self._repository.upsert_many(meetings)

        if tsg is not None and self._tsg_repository is not None:
            self._tsg_repository.update_meeting_last_sync(
                tsg.upper(), datetime.now(timezone.utc)
            )
        return written

    def list_recent(
        self,
        limit: int = 50,
        offset: int = 0,
        tsg: str | None = None,
        name_like: str | None = None,
        location_like: str | None = None,
        year: int | None = None,
        tdoc_id: tuple[str, int] | None = None,
    ) -> list[Meeting]:
        """Return recent meetings from storage, optionally filtered and paginated.

        Filters and pagination are passed down to the repository for efficient
        SQL execution. ``offset`` is applied first, then ``limit`` caps the
        returned rows; use it to page past earlier rows in CLI listings.
        ``tdoc_id`` is the ``(prefix, number)`` tuple produced by
        :func:`doc3gpp.cli_filters.parse_tdoc_id` and narrows the result to
        meetings whose ``start_doc`` / ``end_doc`` range brackets the TDoc.
        """
        return self._repository.list(
            limit=limit,
            offset=offset,
            tsg=tsg,
            name_like=name_like,
            location_like=location_like,
            year=year,
            tdoc_id=tdoc_id,
        )

    def get_by_id(self, meeting_id: int) -> Meeting | None:
        """Return a stored meeting record by its numeric meeting ID."""
        logger.debug("Retrieving meeting by id %s", meeting_id)
        return self._repository.get_by_id(meeting_id)

    def get_by_name(self, meeting_name: str) -> Meeting | None:
        """Return a stored meeting record by its exact meeting name."""
        logger.debug("Retrieving meeting by name %s", meeting_name)
        return self._repository.get_by_name(meeting_name)



