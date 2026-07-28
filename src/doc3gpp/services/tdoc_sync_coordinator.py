"""Cross-service orchestration for TDoc sync workflows.

The :class:`TDocSyncCoordinator` was extracted from the CLI to remove the
implicit dependency between ``MeetingService`` and ``TDocService`` that the
command-line entry point used to thread together by hand. The coordinator
accepts Protocol-typed repositories so it can be constructed with fakes in
tests and alternative storage backends in production without changing the
callers.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import logging

from doc3gpp.models.meeting import Meeting
from doc3gpp.models.sync import BulkSyncFailure
from doc3gpp.models.sync import BulkSyncOutcome
from doc3gpp.models.sync import SyncOutcome
from doc3gpp.repository.protocols import (
    MeetingRepository,
    TDocFileRepository,
    TDocRepository,
)
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.tdoc_file_service import TDocFileService
from doc3gpp.services.tdoc_service import TDocService

logger = logging.getLogger(__name__)


class MeetingNotFoundError(LookupError):
    """Raised when the requested meeting record cannot be located."""


class MeetingMissingFtpUrlError(ValueError):
    """Raised when a resolved meeting has no FTP URL stored.

    Without a stored ``ftp_url`` there is no upstream location to scrape
    TDocs from, so the sync cannot proceed.
    """


class TDocSyncCoordinator:
    """Coordinates the full TDoc sync flow for one or many meetings.

    The CLI calls ``sync_for_meeting_id`` or ``sync_for_meeting_name``
    with the user-supplied selector; the coordinator looks up the
    meeting via :class:`MeetingService`, validates its FTP URL, and
    dispatches the TDoc and auxiliary-file syncs through
    :class:`TDocService` and :class:`TDocFileService` respectively.
    After a successful TDoc list sync, the meeting's
    ``tdoc_list_last_sync`` timestamp is updated.

    ``sync_all_tracked_meetings`` discovers every distinct meeting ID
    stored in the ``tdocs`` table and runs the same per-meeting sync
    path for each one, collecting skipped, synced, and failed results
    without aborting the sweep.
    """

    def __init__(
        self,
        meeting_repository: MeetingRepository,
        tdoc_repository: TDocRepository,
        tdoc_file_repository: TDocFileRepository,
        tdoc_list_sync_interval: timedelta = timedelta(minutes=30),
        tdoc_list_closed_window: timedelta = timedelta(days=90),
        tdoc_list_url_template: str = "https://portal.3gpp.org/ngppapp/GenerateDocumentList.aspx?meetingId={meeting_id}",
    ) -> None:
        self._meeting_repository = meeting_repository
        self._meetings = MeetingService(meeting_repository)
        self._repository = tdoc_repository
        self._tdocs = TDocService(tdoc_repository)
        self._tdoc_files = TDocFileService(tdoc_file_repository)
        self._tdoc_list_sync_interval = tdoc_list_sync_interval
        self._tdoc_list_closed_window = tdoc_list_closed_window
        self._tdoc_list_url_template = tdoc_list_url_template

    def sync_for_meeting_id(self, meeting_id: int, force: bool = False) -> SyncOutcome:
        """Sync TDocs and auxiliary TDoc files for the meeting with the given numeric ID.

        Returns a :class:`SyncOutcome` because the coordinator may skip the
        sync based on the meeting's age or the local last-sync timestamp.
        """
        logger.debug("Resolving meeting by id %s", meeting_id)
        meeting = self._meetings.get_by_id(meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(f"Meeting not found with id {meeting_id}")
        return self._sync_for_meeting(meeting, force=force)

    def sync_for_meeting_name(self, meeting_name: str, force: bool = False) -> SyncOutcome:
        """Sync TDocs and auxiliary TDoc files for the meeting with the given canonical name.

        Returns a :class:`SyncOutcome` (see :meth:`sync_for_meeting_id`).
        """
        logger.debug("Resolving meeting by name %s", meeting_name)
        meeting = self._meetings.get_by_name(meeting_name)
        if meeting is None:
            raise MeetingNotFoundError(f"Meeting not found with name {meeting_name}")
        return self._sync_for_meeting(meeting, force=force)

    def sync_all_tracked_meetings(self, force: bool = False) -> BulkSyncOutcome:
        """Sync TDocs and auxiliary TDoc files for every tracked meeting.

        Discovers all distinct ``meeting_id`` values currently stored in
        the ``tdocs`` table and runs the per-meeting sync path for each
        one.         The existing closed-window and sync-interval skip rules still
        apply per meeting; ``force`` bypasses both for every meeting in
        the run.

        A single meeting failure is recorded in the returned
        :class:`BulkSyncOutcome` and does not abort the sweep.
        """
        meeting_ids = self._repository.list_distinct_meeting_ids()
        outcome = BulkSyncOutcome()

        for meeting_id in meeting_ids:
            logger.info("Bulk TDoc sync: processing meeting %s", meeting_id)
            try:
                meeting = self._meetings.get_by_id(meeting_id)
                if meeting is None:
                    raise MeetingNotFoundError(
                        f"Meeting not found with id {meeting_id}"
                    )
                result = self._sync_for_meeting(meeting, force=force)
            except (MeetingNotFoundError, MeetingMissingFtpUrlError) as exc:
                outcome = outcome.add_failure(
                    BulkSyncFailure(
                        meeting_id=meeting_id,
                        error=exc.__class__.__name__,
                        reason=str(exc),
                    )
                )
            else:
                outcome = outcome.add_outcome(result)

        return outcome

    def _sync_for_meeting(self, meeting: Meeting, force: bool) -> SyncOutcome:
        if not meeting.ftp_url:
            raise MeetingMissingFtpUrlError(
                f"Meeting {meeting.meeting_id} ({meeting.name}) has no FTP URL stored"
            )

        now = datetime.now(timezone.utc)
        if not force and meeting.tdoc_list_last_sync is not None:
            # The closed-window rule only applies to meetings that have already
            # been synced at least once — a never-synced meeting must still
            # get a chance to populate the tdocs table even when its
            # ``end_date`` is older than the configured cutoff.
            if meeting.end_date is not None:
                closed_cutoff = now - self._tdoc_list_closed_window
                if meeting.end_date < closed_cutoff.date():
                    return SyncOutcome(
                        status="skipped",
                        reason=(
                            f"TDoc sync skipped for meeting {meeting.meeting_id} "
                            f"({meeting.name}): end_date {meeting.end_date} is older "
                            f"than the {self._tdoc_list_closed_window.days}-day closed window. "
                            f"Use --force to override."
                        ),
                    )

            age = now - meeting.tdoc_list_last_sync
            if age < self._tdoc_list_sync_interval:
                return SyncOutcome(
                    status="skipped",
                    reason=(
                        f"TDoc sync skipped for meeting {meeting.meeting_id} "
                        f"({meeting.name}): last sync {self._format_duration(age)} ago "
                        f"(sync interval {self._format_duration(self._tdoc_list_sync_interval)}). "
                        f"Use --force to override."
                    ),
                )

        logger.info(
            "Starting TDoc sync for meeting %s (id=%s, ftp=%s)",
            meeting.name,
            meeting.meeting_id,
            meeting.ftp_url,
        )
        tdoc_count = self._tdocs.sync_tdoc_list(
            meeting_id=meeting.meeting_id,
            url_template=self._tdoc_list_url_template,
        )
        tdoc_ids = self._repository.list_tdoc_ids_for_meeting(meeting.meeting_id)
        file_count = self._tdoc_files.sync_from_meeting_ftp(
            ftp_url=meeting.ftp_url,
            tdoc_ids=tdoc_ids,
        )
        self._meeting_repository.update_tdoc_list_last_sync(
            meeting.meeting_id, datetime.now(timezone.utc)
        )
        return SyncOutcome(
            status="synced",
            reason=(
                f"TDoc sync complete: {tdoc_count} TDoc row(s) and "
                f"{file_count} auxiliary TDoc file(s) stored"
            ),
            synced_count=tdoc_count,
            file_count=file_count,
        )

    @staticmethod
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
