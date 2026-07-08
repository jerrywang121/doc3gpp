from __future__ import annotations

from dataclasses import replace
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
        today: date | None = None,
        tsg: str | None = None,
    ) -> int:
        """Fetch meetings from the 3GPP calendar URL and persist filtered results.

        Args:
            meetings_url: DynaReport meeting calendar URL to fetch.
            max_year_closed: Maximum years of *closed* meetings to keep
                (filters by ``end_date`` >= today minus N calendar years).
            max_year_future: Maximum years of *future* meetings to keep
                (filters by ``start_date`` <= today plus N calendar years).
            today: Optional override for ``date.today()``. Used by tests to
                pin the window to a deterministic date.
            tsg: Canonical TSG short name (e.g. ``R5``) owning the meeting
                page being scraped. When provided, stamped onto every
                parsed :class:`Meeting` so the persisted ``meetings.tsg``
                FK is populated and downstream ``meeting list --tsg``
                filters can scope by owning group. ``None`` leaves the
                field un-stamped (useful for tests / bulk imports).

        ``sync`` also trims out-of-window rows after the upsert so a later
        re-sync with a narrower ``--closed-years`` does not leave stale rows
        from the previous wider window in the database. See
        :func:`filter_by_year_window` for the exact predicate.
        """
        logger.info("Syncing meetings from %s", meetings_url)
        meetings = fetch_calendar(meetings_url)
        logger.debug("Fetched %s meetings from calendar", len(meetings))
        anchor = today if today is not None else date.today()
        meetings = filter_by_year_window(meetings, max_year_closed, max_year_future, anchor)
        logger.debug("Filtered meetings to %s within year window", len(meetings))
        if tsg is not None:
            canonical_tsg = tsg.upper()
            meetings = [replace(m, tsg=canonical_tsg) for m in meetings]
        written = self._repository.upsert_many(meetings)

        start_cutoff = years_ago(anchor, max_year_closed)
        deleted = self._repository.delete_with_end_before(start_cutoff)
        if deleted:
            logger.info(
                "Trimmed %s meeting rows older than %s (closed_years=%s)",
                deleted,
                start_cutoff.isoformat(),
                max_year_closed,
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
    ) -> list[Meeting]:
        """Return recent meetings from storage, optionally filtered and paginated.

        Filters and pagination are passed down to the repository for efficient
        SQL execution. ``offset`` is applied first, then ``limit`` caps the
        returned rows; use it to page past earlier rows in CLI listings.
        """
        return self._repository.list(
            limit=limit,
            offset=offset,
            tsg=tsg,
            name_like=name_like,
            location_like=location_like,
            year=year,
        )

    def get_by_id(self, meeting_id: int) -> Meeting | None:
        """Return a stored meeting record by its numeric meeting ID."""
        logger.debug("Retrieving meeting by id %s", meeting_id)
        return self._repository.get_by_id(meeting_id)

    def get_by_name(self, meeting_name: str) -> Meeting | None:
        """Return a stored meeting record by its exact meeting name."""
        logger.debug("Retrieving meeting by name %s", meeting_name)
        return self._repository.get_by_name(meeting_name)


def years_ago(today: date, years: int) -> date:
    """Return ``today`` minus ``years`` calendar years.

    Uses stdlib ``date`` arithmetic only: ``date.replace(year=today.year - years)``
    with a Feb 29 clamp when today is a leap day and the target year is not.

    This avoids the drift present in ``timedelta(days=356 * N)`` (which
    over- or under-shoots by ~9 days per calendar year) and does not
    require a dateutil dependency.
    """
    target_year = today.year - years
    try:
        return today.replace(year=target_year)
    except ValueError as exc:
        if today.month == 2 and today.day == 29:
            return date(target_year, 2, 28)
        raise exc


def filter_by_year_window(
    meetings: list[Meeting],
    max_year_closed: int,
    max_year_future: int,
    today: date | None = None,
) -> list[Meeting]:
    """Filter meetings to the configured closure and future year window.

    A meeting is kept when:
      - ``end_date`` >= today - ``max_year_closed`` calendar years, AND
      - ``start_date`` <= today + ``max_year_future`` calendar years.

    The cutoffs use calendar-aware arithmetic (:func:`years_ago`) so the
    window matches the user's mental model across leap-year boundaries.
    With non-negative inputs (CLI enforced) the window is non-empty by
    construction.
    """
    anchor = today if today is not None else date.today()
    start_cutoff = years_ago(anchor, max_year_closed)
    end_cutoff = anchor + timedelta(days=365 * max_year_future)
    return [m for m in meetings if start_cutoff <= m.end_date and m.start_date <= end_cutoff]  # noqa: E501
