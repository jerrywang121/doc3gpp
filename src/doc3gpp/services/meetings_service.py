from __future__ import annotations

from datetime import date
from datetime import timedelta
import logging

from doc3gpp.models.meeting import Meeting
from doc3gpp.repository.protocols import MeetingRepository
from doc3gpp.scraping.calendar_source import fetch_calendar

logger = logging.getLogger(__name__)


class MeetingService:
    """Sync and query 3GPP meeting records.

    This service is responsible for fetching meeting calendar data from the 3GPP
    site, filtering it by the configured year window, and persisting it through
    the configured repository implementation.
    """

    def __init__(self, repository: MeetingRepository) -> None:
        """Initialize the service with a repository backing the meeting storage."""
        self._repository = repository

    def sync(
        self,
        meetings_url: str,
        max_year_closed: int = 2,
        max_year_future: int = 1,
    ) -> int:
        """Fetch meetings from the 3GPP calendar URL and persist filtered results."""
        logger.info("Syncing meetings from %s", meetings_url)
        meetings = fetch_calendar(meetings_url)
        logger.debug("Fetched %s meetings from calendar", len(meetings))
        meetings = self._filter_by_year_window(meetings, max_year_closed, max_year_future)
        logger.debug("Filtered meetings to %s within year window", len(meetings))
        return self._repository.upsert_many(meetings)

    def list_recent(
        self,
        limit: int = 50,
        tsg: str | None = None,
        name_like: str | None = None,
        location_like: str | None = None,
        year: int | None = None,
    ) -> list[Meeting]:
        """Return recent meetings from storage, optionally filtered.

        Filters are passed down to the repository for efficient SQL execution.
        """
        return self._repository.list(
            limit=limit, tsg=tsg, name_like=name_like, location_like=location_like, year=year
        )

    def get_by_id(self, meeting_id: int) -> Meeting | None:
        """Return a stored meeting record by its numeric meeting ID."""
        logger.debug("Retrieving meeting by id %s", meeting_id)
        return self._repository.get_by_id(meeting_id)

    def get_by_name(self, meeting_name: str) -> Meeting | None:
        """Return a stored meeting record by its exact meeting name."""
        logger.debug("Retrieving meeting by name %s", meeting_name)
        return self._repository.get_by_name(meeting_name)

    @staticmethod
    def _filter_by_year_window(
        meetings: list[Meeting], max_year_closed: int, max_year_future: int
    ) -> list[Meeting]:
        """Filter meetings by configured closure and future year window."""
        today = date.today()
        start_cutoff = today - timedelta(days=356 * max_year_closed)
        end_cutoff = today + timedelta(days=356 * max_year_future)
        return [m for m in meetings if start_cutoff < m.end_date and m.start_date < end_cutoff]
