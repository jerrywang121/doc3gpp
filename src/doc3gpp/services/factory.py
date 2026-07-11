"""Composition-root factories for the service layer.

These factories hide the concrete storage backend from CLI code and tests,
letting callers depend only on the Protocol-typed service interface.
"""

from __future__ import annotations

from doc3gpp.scraping.cache import TDocCache
from doc3gpp.scraping.client import ScraperClient
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.tdoc_cr_service import TDocCrService
from doc3gpp.services.tdoc_file_service import TDocFileService
from doc3gpp.services.tdoc_service import TDocService
from doc3gpp.services.tdoc_sync_coordinator import TDocSyncCoordinator
from doc3gpp.services.tsg_service import TsgService
from doc3gpp.services.wi_service import WiService
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
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


def build_tdoc_repository() -> SQLAlchemyTDocRepository:
    """Construct a :class:`SQLAlchemyTDocRepository` for direct lookups.

    Used by the Phase 7 ``tdoc show`` and ``tdoc parse`` CLI commands
    when a single TDoc needs to be resolved by its canonical
    ``tdoc_id`` without going through a service-layer wrapper. Keeps
    the existing :func:`build_tdoc_service` factory untouched.
    """
    return SQLAlchemyTDocRepository()


def build_tdoc_cr_repository() -> SQLAlchemyTDocCrRepository:
    """Construct a :class:`SQLAlchemyTDocCrRepository` for direct lookups.

    Used by the Phase 7 ``tdoc show`` CLI command to surface a
    previously extracted ``tdoc_cr_details`` row next to its parent
    ``TDoc`` without going through the full extraction service. Keeps
    the existing :func:`build_tdoc_cr_service` factory untouched.
    """
    return SQLAlchemyTDocCrRepository()


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


def build_tdoc_cr_service() -> TDocCrService:
    """Construct a :class:`TDocCrService` for the ``tdoc parse`` command.

    Wires together:

    * :class:`~doc3gpp.scraping.cache.TDocCache` rooted at
      ``settings.cache.dir`` with the configured ``size_limit_mb``
      converted to bytes (``0`` → unlimited).
    * A fresh :class:`~doc3gpp.scraping.client.ScraperClient` (the
      client reads its own settings via :func:`get_settings`).
    * :class:`~doc3gpp.storage.repositories.tdoc_cr_sql.SQLAlchemyTDocCrRepository`
      for the detail + extract-metadata tables.
    * :class:`~doc3gpp.storage.repositories.tdoc_sql.SQLAlchemyTDocRepository`
      for read-only ``tdocs`` lookups (type guard) and for the FK
      probe that gates ``tdoc_extracts`` / ``tdoc_cr_details`` writes
      in the ``--from-url`` direct-mode path.

    The factory is shared by both the filter-based batch path
    (existing ``tdoc parse --tdoc/--meeting-id`` flow) and the new
    direct-mode path (``tdoc parse --from-path/--from-url``). The
    service's two public entry points compose on the same wiring;
    the only caller-side difference is whether the dispatch goes
    through :meth:`TDocCrService.extract_many` or through
    :meth:`TDocCrService.extract_from_url` /
    :meth:`TDocCrService.extract_from_bytes`.
    """
    settings = get_settings()
    return TDocCrService(
        cache=TDocCache(
            root=settings.cache.dir,
            size_limit_bytes=settings.cache.size_limit_mb * 1024 * 1024,
        ),
        scraper_client=ScraperClient(),
        cr_repository=SQLAlchemyTDocCrRepository(),
        tdoc_repository=SQLAlchemyTDocRepository(),
    )