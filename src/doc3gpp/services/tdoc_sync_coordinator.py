"""Cross-service orchestration for TDoc sync workflows.

The :class:`TDocSyncCoordinator` was extracted from the CLI to remove the
implicit dependency between ``MeetingService`` and ``TDocService`` that the
command-line entry point used to thread together by hand. The coordinator
accepts Protocol-typed repositories so it can be constructed with fakes in
tests and alternative storage backends in production without changing the
callers.
"""

from __future__ import annotations

import logging

from doc3gpp.models.meeting import Meeting
from doc3gpp.repository.protocols import MeetingRepository, TDocRepository
from doc3gpp.services.meetings_service import MeetingService
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
    """Coordinates TDoc sync by resolving a meeting record then fetching TDocs.

    The CLI calls ``sync_for_meeting_id`` or ``sync_for_meeting_name`` with
    the user-supplied selector; the coordinator looks up the meeting via the
    injected :class:`MeetingService`, validates its FTP URL, and dispatches
    the underlying TDoc sync through :class:`TDocService`.
    """

    def __init__(
        self,
        meeting_repository: MeetingRepository,
        tdoc_repository: TDocRepository,
    ) -> None:
        """Initialize the coordinator with Protocol-typed repositories."""
        self._meetings = MeetingService(meeting_repository)
        self._tdocs = TDocService(tdoc_repository)

    def sync_for_meeting_id(self, meeting_id: int) -> int:
        """Sync TDocs for the meeting with the given numeric ID."""
        logger.debug("Resolving meeting by id %s", meeting_id)
        meeting = self._meetings.get_by_id(meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(f"Meeting not found with id {meeting_id}")
        return self._sync_for_meeting(meeting)

    def sync_for_meeting_name(self, meeting_name: str) -> int:
        """Sync TDocs for the meeting with the given canonical name."""
        logger.debug("Resolving meeting by name %s", meeting_name)
        meeting = self._meetings.get_by_name(meeting_name)
        if meeting is None:
            raise MeetingNotFoundError(f"Meeting not found with name {meeting_name}")
        return self._sync_for_meeting(meeting)

    def _sync_for_meeting(self, meeting: Meeting) -> int:
        """Validate the resolved meeting then dispatch the TDoc sync."""
        if not meeting.ftp_url:
            raise MeetingMissingFtpUrlError(
                f"Meeting {meeting.meeting_id} ({meeting.name}) has no FTP URL stored"
            )
        logger.info(
            "Starting TDoc sync for meeting %s (id=%s, ftp=%s)",
            meeting.name,
            meeting.meeting_id,
            meeting.ftp_url,
        )
        return self._tdocs.sync_from_meeting_ftp(
            ftp_url=meeting.ftp_url,
            meeting_id=meeting.meeting_id,
        )