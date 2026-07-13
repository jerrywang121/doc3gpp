from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import logging

from doc3gpp.models.meeting import Meeting
from doc3gpp.models.sync import SyncOutcome
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
        sync_interval: timedelta = timedelta(hours=24),
    ) -> None:
        """Initialize the service with a repository backing the meeting storage.

        Args:
            repository: Repository used to persist and query meetings.
            tsg_repository: Optional repository for TSG reference records;
                required when ``sync`` needs to read or update
                ``tsgs.meeting_last_sync``.
            sync_interval: Minimum time between meeting calendar syncs
                for the same TSG. Syncs requested within this interval are
                skipped unless ``force=True`` is passed.
        """
        self._repository = repository
        self._tsg_repository = tsg_repository
        self._sync_interval = sync_interval

    def sync(
        self,
        meetings_url: str,
        tsg: str | None = None,
        force: bool = False,
    ) -> SyncOutcome:
        """Fetch meetings from the 3GPP calendar URL and persist all results.

        Args:
            meetings_url: DynaReport meeting calendar URL to fetch.
            tsg: Canonical TSG short name (e.g. ``R5``) owning the meeting
                page being scraped. When provided, stamped onto every
                parsed :class:`Meeting` so the persisted ``meetings.tsg``
                FK is populated and downstream ``meeting list --tsg``
                filters can scope by owning group. ``None`` leaves the
                field un-stamped (useful for tests / bulk imports).
            force: When ``True``, bypass the sync interval check.

        Returns:
            A :class:`SyncOutcome` describing whether the sync ran and how
            many rows were written.
        """
        canonical_tsg = tsg.upper() if tsg is not None else None
        if canonical_tsg is not None and not force and self._tsg_repository is not None:
            tsg_record = self._tsg_repository.get_by_short_name(canonical_tsg)
            last_sync = tsg_record.meeting_last_sync if tsg_record is not None else None
            now = datetime.now(timezone.utc)
            if last_sync is not None and (now - last_sync) < self._sync_interval:
                ago = now - last_sync
                return SyncOutcome(
                    status="skipped",
                    reason=(
                        f"Meeting sync skipped for TSG {canonical_tsg}: "
                        f"last sync {_format_duration(ago)} ago "
                        f"(sync interval {_format_duration(self._sync_interval)}). "
                        f"Use --force to override."
                    ),
                )

        logger.info("Syncing meetings from %s", meetings_url)
        meetings = fetch_calendar(meetings_url)
        logger.debug("Fetched %s meetings from calendar", len(meetings))
        if canonical_tsg is not None:
            meetings = [replace(m, tsg=canonical_tsg) for m in meetings]
        written = self._repository.upsert_many(meetings)

        if canonical_tsg is not None and self._tsg_repository is not None:
            self._tsg_repository.update_meeting_last_sync(
                canonical_tsg, datetime.now(timezone.utc)
            )
        return SyncOutcome(
            status="synced",
            reason=f"Meeting sync complete: {written} meeting rows stored",
            synced_count=written,
        )

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


def _format_duration(delta: timedelta) -> str:
    """Return a concise human-readable representation of a timedelta."""
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}s"
    if total_seconds < 3600:
        return f"{total_seconds // 60}m"
    if total_seconds < 86400:
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        if minutes:
            return f"{hours}h {minutes}m"
        return f"{hours}h"
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    if hours:
        return f"{days}d {hours}h"
    return f"{days}d"


