"""Composition-root factories for the service layer.

These factories hide the concrete storage backend from CLI code and tests,
letting callers depend only on the Protocol-typed service interface.
"""

from __future__ import annotations

from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.tdoc_file_service import TDocFileService
from doc3gpp.services.tdoc_service import TDocService
from doc3gpp.services.tdoc_sync_coordinator import TDocSyncCoordinator
from doc3gpp.services.tsg_service import TsgService
from doc3gpp.services.wi_service import WiService
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
from doc3gpp.storage.repositories.tdoc_file_sql import SQLAlchemyTDocFileRepository
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository
from doc3gpp.storage.repositories.wi_sql import SQLAlchemyWiRepository


def build_meeting_service() -> MeetingService:
    """Construct a :class:`MeetingService` backed by the configured repo."""
    return MeetingService(SQLAlchemyMeetingRepository())


def build_tdoc_service() -> TDocService:
    """Construct a :class:`TDocService` backed by the configured repo."""
    return TDocService(SQLAlchemyTDocRepository())


def build_tdoc_file_service() -> TDocFileService:
    """Construct a :class:`TDocFileService` backed by the configured repo."""
    return TDocFileService(SQLAlchemyTDocFileRepository())


def build_tsg_service() -> TsgService:
    """Construct a :class:`TsgService` backed by the configured repo."""
    return TsgService(SQLAlchemyTsgRepository())


def build_wi_service() -> WiService:
    """Construct a :class:`WiService` backed by the configured repo."""
    return WiService(SQLAlchemyWiRepository())


def build_tdoc_sync_coordinator() -> TDocSyncCoordinator:
    """Construct a :class:`TDocSyncCoordinator` for the ``tdoc sync`` command.

    Encapsulates the cross-service orchestration (resolve meeting → fetch
    TDocs → fetch auxiliary TDoc files) so callers don't have to import
    meeting, TDoc and TDocFile repositories directly.
    """
    return TDocSyncCoordinator(
        SQLAlchemyMeetingRepository(),
        SQLAlchemyTDocRepository(),
        SQLAlchemyTDocFileRepository(),
    )