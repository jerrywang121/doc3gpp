from __future__ import annotations

from datetime import date
from datetime import timedelta

from doc3gpp.models.meeting import Meeting
from doc3gpp.repository.protocols import MeetingRepository
from doc3gpp.scraping.calendar_source import fetch_calendar


class CalendarService:
    """Sync and query 3GPP meeting calendar records."""

    def __init__(self, repository: MeetingRepository) -> None:
        self._repository = repository

    def sync(
        self,
        calendar_url: str,
        max_year_closed: int = 2,
        max_year_future: int = 1,
    ) -> int:
        meetings = fetch_calendar(calendar_url)
        meetings = self._filter_by_year_window(meetings, max_year_closed, max_year_future)
        return self._repository.upsert_many(meetings)

    def list_recent(self, limit: int = 50) -> list[Meeting]:
        return self._repository.list(limit=limit)

    @staticmethod
    def _filter_by_year_window(
        meetings: list[Meeting], max_year_closed: int, max_year_future: int
    ) -> list[Meeting]:
        today = date.today()
        start_cutoff = today - timedelta(days=356 * max_year_closed)
        end_cutoff = today + timedelta(days=356 * max_year_future)
        return [m for m in meetings if start_cutoff < m.end_date and m.start_date < end_cutoff]
