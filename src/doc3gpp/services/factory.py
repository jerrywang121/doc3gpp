"""Composition-root factories for the service layer.

These factories hide the concrete storage backend from CLI code and tests,
letting callers depend only on the Protocol-typed service interface.
"""

from __future__ import annotations

from doc3gpp.repository.protocols import (
    EmbeddingReranker,
    SearchIndexRepository,
    TDocCrChangeDetailsRepository,
    TDocCrTTCNDetailRepository,
)
from doc3gpp.models.search import SearchUnavailableError
from doc3gpp.scraping.cache import TDocCache
from doc3gpp.scraping.client import ScraperClient
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.search_service import SearchService
from doc3gpp.services.tdoc_cr_service import TDocCrService
from doc3gpp.services.tdoc_file_service import TDocFileService
from doc3gpp.services.tdoc_service import TDocService
from doc3gpp.services.tdoc_sync_coordinator import TDocSyncCoordinator
from doc3gpp.services.tsg_service import TsgService
from doc3gpp.services.wi_service import WiService
from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import Settings
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
    SQLAlchemyTDocCrChangeDetailsRepository,
)
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import SQLAlchemyTDocCrTtcnRepository
from doc3gpp.storage.repositories.tdoc_file_sql import SQLAlchemyTDocFileRepository
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository
from doc3gpp.storage.repositories.wi_sql import SQLAlchemyWiRepository


def build_meeting_service() -> MeetingService:
    """Construct a :class:`MeetingService` backed by the configured repos."""
    settings = get_settings()
    return MeetingService(
        SQLAlchemyMeetingRepository(),
        SQLAlchemyTsgRepository(),
        sync_interval=settings.sync.meeting_sync_interval,
    )


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
    previously extracted ``tdoc_cr_cover_page`` row next to its parent
    ``TDoc`` without going through the full extraction service.
    """
    return SQLAlchemyTDocCrRepository()


def build_tdoc_cr_ttcn_repository() -> SQLAlchemyTDocCrTtcnRepository:
    """Construct a :class:`SQLAlchemyTDocCrTtcnRepository` for direct lookups.

    Used by the Phase 7 ``tdoc show`` CLI command to surface a
    previously extracted ``tdoc_cr_ttcn_details`` row for TTCN CRs.
    """
    return SQLAlchemyTDocCrTtcnRepository()


def build_tdoc_cr_change_details_repository() -> SQLAlchemyTDocCrChangeDetailsRepository:
    """Construct a :class:`SQLAlchemyTDocCrChangeDetailsRepository` for direct lookups.

    Used by the ``tdoc show`` CLI command to surface body-derived
    change details next to the cover page and the TTCN sidecar.
    """
    return SQLAlchemyTDocCrChangeDetailsRepository()


def build_tdoc_file_repository() -> SQLAlchemyTDocFileRepository:
    """Construct a :class:`SQLAlchemyTDocFileRepository` for direct lookups.

    Used by the ``tdoc show`` CLI command to surface auxiliary
    ``tdoc_files`` rows next to their parent ``TDoc`` without going
    through the full sync-flow :class:`TDocFileService`. Mirrors the
    read-side factory pattern used by ``build_tdoc_cr_repository`` /
    ``build_tdoc_cr_ttcn_repository``.
    """
    return SQLAlchemyTDocFileRepository()


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
    settings = get_settings()
    return TDocSyncCoordinator(
        SQLAlchemyMeetingRepository(),
        SQLAlchemyTDocRepository(),
        SQLAlchemyTDocFileRepository(),
        tdoc_list_sync_interval=settings.sync.tdoc_list_sync_interval,
        tdoc_list_closed_window=settings.sync.tdoc_list_closed_window,
        tdoc_list_url_template=settings.sync.tdoc_list_url_template,
    )


def build_tdoc_cr_service(
    cr_ttcn_repository: TDocCrTTCNDetailRepository | None = None,
    cr_change_details_repository: TDocCrChangeDetailsRepository | None = None,
    *,
    max_tdoc_size_bytes: int | None = None,
) -> TDocCrService:
    """Construct a :class:`TDocCrService` for the ``tdoc parse`` command.

    Wires together:

    * :class:`~doc3gpp.scraping.cache.TDocCache` rooted at
      ``settings.cache.dir`` with the configured ``size_limit_mb``
      converted to bytes (``0`` → unlimited).
    * A fresh :class:`~doc3gpp.scraping.client.ScraperClient` (the
      client reads its own settings via :func:`get_settings`).
    * :class:`~doc3gpp.storage.repositories.tdoc_cr_sql.SQLAlchemyTDocCrRepository`
      for the cover-page detail table.
    * :class:`~doc3gpp.storage.repositories.tdoc_cr_ttcn_sql.SQLAlchemyTDocCrTtcnRepository`
      for the TTCN sidecar table.
    * :class:`~doc3gpp.storage.repositories.tdoc_cr_change_details_sql.SQLAlchemyTDocCrChangeDetailsRepository`
      for the body-derived change-details sidecar.
    * :class:`~doc3gpp.storage.repositories.tdoc_sql.SQLAlchemyTDocRepository`
      for read-only ``tdocs`` lookups (type guard) and for the FK
      probe that gates ``tdoc_extracts`` / ``tdoc_cr_cover_page`` writes
      in the ``--from-url`` direct-mode path.
    * :func:`build_search_service` — wired through to
      :class:`TDocCrService` so successful parses can keep the FTS5
      index in sync. Returns ``None`` when search is disabled, the
      dialect is non-sqlite, or FTS5 is missing; in those cases the
      service simply skips the auto-index hook.

    The factory is shared by both the filter-based batch path
    (existing ``tdoc parse --tdoc/--meeting-id`` flow) and the new
    direct-mode path (``tdoc parse --from-path/--from-url``). The
    service's two public entry points compose on the same wiring;
    the only caller-side difference is whether the dispatch goes
    through :meth:`TDocCrService.extract_many` or through
    :meth:`TDocCrService.extract_from_url` /
    :meth:`TDocCrService.extract_from_bytes`.

    Args:
        cr_ttcn_repository: Optional override for the TTCN sidecar
            repository (tests inject a stub here).
        cr_change_details_repository: Optional override for the
            body-derived change-details sidecar repository (tests
            inject a stub here).
        max_tdoc_size_bytes: Optional explicit cap (bytes). When
            ``None`` (default), the factory resolves from
            ``settings.tdoc_parse.max_tdoc_size_kb * 1024``. When an
            explicit value is supplied it overrides the setting —
            typically used by the CLI dispatcher to apply
            ``--max-tdoc-size-kb``. The value is forwarded to
            :class:`TDocCrService` as ``max_tdoc_size_bytes``;
            ``0`` disables the size guard.
    """
    settings = get_settings()
    if max_tdoc_size_bytes is None:
        max_tdoc_size_bytes = settings.tdoc_parse.max_tdoc_size_kb * 1024
    return TDocCrService(
        cache=TDocCache(
            root=settings.cache.dir,
            size_limit_bytes=settings.cache.size_limit_mb * 1024 * 1024,
        ),
        scraper_client=ScraperClient(),
        cr_repository=SQLAlchemyTDocCrRepository(),
        cr_ttcn_repository=cr_ttcn_repository or build_tdoc_cr_ttcn_repository(),  # type: ignore[call-arg]
        cr_change_details_repository=(
            cr_change_details_repository
            or build_tdoc_cr_change_details_repository()  # type: ignore[call-arg]
        ),
        tdoc_repository=SQLAlchemyTDocRepository(),
        max_tdoc_size_bytes=max_tdoc_size_bytes,
        search_service=build_search_service(),
    )


def build_search_service(
    settings: Settings | None = None,
    repo: SearchIndexRepository | None = None,
    reranker: EmbeddingReranker | None = None,
) -> SearchService | None:
    """Build a :class:`SearchService` or return ``None`` if unavailable.

    The factory is best-effort: any :class:`SearchUnavailableError`
    raised by the repo (wrong dialect, missing FTS5, missing extra)
    is caught here once at startup and returned as ``None``. The
    CLI and the :class:`TDocCrService` hook both treat ``None`` as
    "search is not available" and skip.

    Args:
        settings: Optional explicit settings (defaults to
            :func:`get_settings`). Tests inject a stub.
        repo: Optional explicit repo (tests inject a stub). When
            ``None``, the factory constructs the real
            :class:`SQLAlchemySearchIndexRepository`.
        reranker: Optional explicit reranker. When ``None``, the
            factory constructs the default
            :class:`PassthroughReranker`.
    """
    if settings is None:
        settings = get_settings()
    if not settings.search.enabled:
        return None
    try:
        resolved_engine = get_engine()
        if resolved_engine.dialect.name != "sqlite":
            raise SearchUnavailableError(
                f"search requires sqlite FTS5; current dialect is "
                f"{resolved_engine.dialect.name!r}"
            )
        if repo is None:
            from doc3gpp.storage.repositories.search_sql import (
                SQLAlchemySearchIndexRepository,
            )

            repo = SQLAlchemySearchIndexRepository()
        if reranker is None:
            from doc3gpp.services.search_service import (
                PassthroughReranker,
            )

            reranker = PassthroughReranker()
        return SearchService(repo=repo, reranker=reranker)
    except SearchUnavailableError:
        return None