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
    """Coordinates the full TDoc sync flow for a meeting.

    The CLI calls ``sync_for_meeting_id`` or ``sync_for_meeting_name``
    with the user-supplied selector; the coordinator looks up the
    meeting via :class:`MeetingService`, validates its FTP URL, and
    dispatches the TDoc and auxiliary-file syncs through
    :class:`TDocService` and :class:`TDocFileService` respectively.
    """

    def __init__(
        self,
        meeting_repository: MeetingRepository,
        tdoc_repository: TDocRepository,
        tdoc_file_repository: TDocFileRepository,
    ) -> None:
        self._meetings = MeetingService(meeting_repository)
        self._repository = tdoc_repository
        self._tdocs = TDocService(tdoc_repository)
        self._tdoc_files = TDocFileService(tdoc_file_repository)

    def sync_for_meeting_id(self, meeting_id: int) -> str:
        """Sync TDocs and auxiliary TDoc files for the meeting with the given numeric ID.

        Returns a human-readable summary string because the coordinator
        aggregates two distinct sync passes (TDocs and TDoc files) that
        report different units. The CLI surfaces this string directly to
        the operator.
        """
        logger.debug("Resolving meeting by id %s", meeting_id)
        meeting = self._meetings.get_by_id(meeting_id)
        if meeting is None:
            raise MeetingNotFoundError(f"Meeting not found with id {meeting_id}")
        return self._sync_for_meeting(meeting)

    def sync_for_meeting_name(self, meeting_name: str) -> str:
        """Sync TDocs and auxiliary TDoc files for the meeting with the given canonical name.

        Returns a human-readable summary string (see
        :meth:`sync_for_meeting_id`).
        """
        logger.debug("Resolving meeting by name %s", meeting_name)
        meeting = self._meetings.get_by_name(meeting_name)
        if meeting is None:
            raise MeetingNotFoundError(f"Meeting not found with name {meeting_name}")
        return self._sync_for_meeting(meeting)

    def _sync_for_meeting(self, meeting: Meeting) -> str:
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
        tdoc_count = self._tdocs.sync_from_meeting_ftp(
            ftp_url=meeting.ftp_url,
            meeting_id=meeting.meeting_id,
        )
        tdoc_ids = self._repository.list_tdoc_ids_for_meeting(meeting.meeting_id)
        file_count = self._tdoc_files.sync_from_meeting_ftp(
            ftp_url=meeting.ftp_url,
            tdoc_ids=tdoc_ids,
        )
        return (
            f"TDoc sync complete: {tdoc_count} TDoc row(s) and "
            f"{file_count} auxiliary TDoc file(s) stored"
        )
