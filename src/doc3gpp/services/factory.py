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
from doc3gpp.services.embedding.remote_embedder import OpenAICompatibleEmbedder
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.search_service import SearchService
from doc3gpp.services.semantic_search_service import SemanticSearchService
from doc3gpp.services.spec_service import SpecService
from doc3gpp.services.tdoc_cr_service import TDocCrService
from doc3gpp.services.tdoc_file_service import TDocFileService
from doc3gpp.services.tdoc_service import TDocService
from doc3gpp.services.tdoc_sync_coordinator import TDocSyncCoordinator
from doc3gpp.services.testcase_service import TestCaseService
from doc3gpp.services.tsg_service import TsgService
from doc3gpp.services.wi_service import WiService
from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import Settings
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
from doc3gpp.storage.repositories.search_sql import SQLAlchemySearchIndexRepository
from doc3gpp.storage.repositories.spec_sql import SQLAlchemySpecRepository
from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
    SQLAlchemyTDocCrChangeDetailsRepository,
)
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import SQLAlchemyTDocCrTtcnRepository
from doc3gpp.storage.repositories.tdoc_file_sql import SQLAlchemyTDocFileRepository
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
from doc3gpp.storage.repositories.testcase_sql import SQLAlchemyTestCaseRepository
from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository
from doc3gpp.storage.repositories.vector_sql import SQLAlchemyVectorIndexRepository
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


def build_spec_service() -> SpecService:
    """Construct a :class:`SpecService` backed by the configured repo."""
    settings = get_settings()
    return SpecService(
        SQLAlchemySpecRepository(),
        sync_interval=settings.sync.spec_sync_interval,
    )


def build_testcase_service() -> TestCaseService:
    """Construct a :class:`TestCaseService` backed by the configured repo."""
    return TestCaseService(SQLAlchemyTestCaseRepository())


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


def build_embedder(settings: Settings | None = None) -> OpenAICompatibleEmbedder | None:
    """Construct the shared remote embedder, or ``None`` when unconfigured.

    ``None`` (``embedding_base_url`` unset/empty) disables the semantic
    stack via the existing ``None``-service paths. The web app builds ONE
    instance and injects it into every service that embeds so a single
    server process shares one httpx client.
    """
    if settings is None:
        settings = get_settings()
    sem = settings.semantic_search
    base_url = (sem.embedding_base_url or "").strip()
    if not base_url:
        return None
    return OpenAICompatibleEmbedder(
        base_url=base_url,
        model=sem.embedding_model,
        api_key=sem.embedding_api_key,
        timeout_s=sem.embedding_timeout_s,
        batch_size=sem.embedding_batch_size,
    )


def build_tdoc_cr_service(
    cr_ttcn_repository: TDocCrTTCNDetailRepository | None = None,
    cr_change_details_repository: TDocCrChangeDetailsRepository | None = None,
    *,
    max_tdoc_size_bytes: int | None = None,
    embedder: Embedder | None = None,  # noqa: F821
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
      index in sync. Returns ``None`` when search is disabled or
      FTS5 is missing; in those cases the service simply skips the
      auto-index hook.

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
    # Build the embedder once and share it: build_search_service and
    # build_semantic_search_service each build+probe their own when
    # passed None, which would cost two HTTP probe round-trips per
    # parse. The web app already shares one instance per process.
    if embedder is None:
        embedder = build_embedder(settings)
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
        search_service=build_search_service(embedder=embedder),
        semantic_service=build_semantic_search_service(embedder=embedder),
    )


def build_semantic_search_service(
    settings: Settings | None = None,
    fts5_service: SearchService | None = None,
    embedder: Embedder | None = None,  # noqa: F821
    vector_repo: VectorIndexRepository | None = None,  # noqa: F821
) -> SemanticSearchService | None:
    """Build a :class:`SemanticSearchService` or return ``None`` if unavailable.

    Best-effort: catches :class:`VectorIndexUnavailableError`,
    :class:`EmbedderUnavailableError`
    raised by the collaborators and returns ``None``. FTS5 is the
    foundation — if :func:`build_search_service` returns ``None`` this
    returns ``None`` too.
    """
    from doc3gpp.models.semantic_search import (
        EmbedderUnavailableError,
        VectorIndexUnavailableError,
    )
    from sqlalchemy.exc import OperationalError as SAOperationalError
    from doc3gpp.storage.repositories.vector_sql import (
        SQLAlchemyVectorIndexRepository,
    )

    if settings is None:
        settings = get_settings()
    if not settings.semantic_search.enabled:
        return None
    try:
        if fts5_service is None:
            fts5_service = build_search_service(settings)
        if fts5_service is None:
            return None
        if embedder is None:
            embedder = build_embedder(settings)
            if embedder is None:
                return None
        if vector_repo is None:
            try:
                live_dim = getattr(embedder, "dim", None)
            except EmbedderUnavailableError:
                return None
            if live_dim is None:
                return None
            try:
                vector_repo = SQLAlchemyVectorIndexRepository(
                    expected_model=getattr(embedder, "model_name", None),
                    expected_dim=live_dim,
                )
            except VectorIndexUnavailableError:
                # Construction failures (missing schema, no sqlite-vec)
                # degrade to None via the outer handler. A model/dim
                # mismatch ALSO lands here — the status panel reads
                # vec_meta directly in that case (see cli.index_command)
                # so the pending mismatch stays visible.
                return None
        return SemanticSearchService(
            fts5_service=fts5_service, embedder=embedder,
            vector_repo=vector_repo, settings=settings,
        )
    except (
        VectorIndexUnavailableError,
        EmbedderUnavailableError,
        SAOperationalError,
    ):
        return None


def build_search_service(
    settings: Settings | None = None,
    repo: SearchIndexRepository | None = None,
    reranker: EmbeddingReranker | None = None,
    *,
    quiet: bool = False,
    embedder: Embedder | None = None,  # noqa: F821
) -> SearchService | None:
    """Build a :class:`SearchService` or return ``None`` if unavailable.

    The factory is best-effort: any :class:`SearchUnavailableError`
    raised by the repo (missing FTS5, missing extra) is caught here
    once at startup and returned as ``None``. The
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
        quiet: Forwarded to :class:`SearchService` so the
            :class:`SemanticReranker`'s one-shot empty-vector
            ``logger.warning`` is suppressed under ``--quiet``.
            Default ``False`` preserves every existing caller.
        embedder: Optional shared embedder. When ``None`` (default),
            the factory builds one via :func:`build_embedder` (which
            returns ``None`` when ``embedding_base_url`` is unset —
            the reranker then stays a passthrough). The web app passes
            the single shared instance so one httpx client is shared
            per process.
    """
    if settings is None:
        settings = get_settings()
    if not settings.search.enabled:
        return None
    try:
        if repo is None:
            repo = SQLAlchemySearchIndexRepository()
        if reranker is None:
            from doc3gpp.models.semantic_search import (
                EmbedderUnavailableError,
                VectorIndexUnavailableError,
            )
            from doc3gpp.services.search_service import PassthroughReranker
            from doc3gpp.services.semantic_reranker import SemanticReranker

            if (
                settings.search.enabled
                and settings.semantic_search.enabled
            ):
                try:
                    if embedder is None:
                        embedder = build_embedder(settings)
                    if embedder is None:
                        reranker = PassthroughReranker()
                    else:
                        try:
                            live_dim = getattr(embedder, "dim", None)
                        except EmbedderUnavailableError:
                            live_dim = None
                        if live_dim is None:
                            reranker = PassthroughReranker()
                        else:
                            vector_repo = SQLAlchemyVectorIndexRepository(
                                expected_model=getattr(embedder, "model_name", None),
                                expected_dim=live_dim,
                            )
                            reranker = SemanticReranker(
                                embedder=embedder, vector_repo=vector_repo,
                                settings=settings,
                            )
                except (
                    VectorIndexUnavailableError,
                    EmbedderUnavailableError,
                ):
                    reranker = PassthroughReranker()
            else:
                reranker = PassthroughReranker()
        return SearchService(repo=repo, reranker=reranker, quiet=quiet)
    except SearchUnavailableError:
        return None