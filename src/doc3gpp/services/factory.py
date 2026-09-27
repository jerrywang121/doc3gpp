"""Composition-root factories for the service layer.

These factories hide the concrete storage backend from CLI code and tests,
letting callers depend only on the Protocol-typed service interface.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from doc3gpp.models.search import SearchUnavailableError
from doc3gpp.models.semantic_search import (
    SemanticSearchUnavailableError,
    VectorIndexUnavailableError,
)
from doc3gpp.repository.protocols import (
    Embedder,
    EmbeddingReranker,
    SearchIndexRepository,
    SpecDocRepository,
    SpecDocSearchRepository,
    SpecDocVectorRepository,
    TDocCrChangeDetailsRepository,
    TDocCrTTCNDetailRepository,
    TDocRepository,
)
from doc3gpp.scraping.cache import TDocCache
from doc3gpp.scraping.client import ScraperClient
from doc3gpp.services.embedding.remote_embedder import OpenAICompatibleEmbedder
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.search_service import SearchService
from doc3gpp.services.semantic_search_service import SemanticSearchService
from doc3gpp.services.spec_doc_index_service import SpecDocIndexService
from doc3gpp.services.spec_doc_search_facade import SpecDocSearchFacade
from doc3gpp.services.spec_service import SpecService
from doc3gpp.services.tdoc_cr_service import TDocCrService
from doc3gpp.services.tdoc_file_service import TDocFileService
from doc3gpp.services.tdoc_index_service import TDocIndexService
from doc3gpp.services.tdoc_search_facade import TDocSearchFacade
from doc3gpp.services.tdoc_service import TDocService
from doc3gpp.services.tdoc_sync_coordinator import TDocSyncCoordinator
from doc3gpp.services.testcase_service import TestCaseService
from doc3gpp.services.tsg_service import TsgService
from doc3gpp.services.wi_service import WiService
from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import Settings

if TYPE_CHECKING:
    from doc3gpp.services.spec_doc_service import SpecDocService
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
from doc3gpp.storage.repositories.search_sql import SQLAlchemySearchIndexRepository
from doc3gpp.storage.repositories.spec_doc_search_sql import (
    SQLAlchemySpecDocSearchRepository,
)
from doc3gpp.storage.repositories.spec_doc_sql import SQLAlchemySpecDocRepository
from doc3gpp.storage.repositories.spec_doc_vector_sql import (
    SQLAlchemySpecDocVectorRepository,
)
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

_UNSET = object()


class _LazyService:
    """Load an optional service only when one of its methods is used."""

    def __init__(
        self,
        builder: Callable[[], object | None],
        error_factory: Callable[[], Exception] | None = None,
    ) -> None:
        self._builder = builder
        self._error_factory = error_factory
        self._loaded = False
        self._service: object | None = None

    def _get(self) -> object | None:
        if not self._loaded:
            self._service = self._builder()
            self._loaded = True
        return self._service

    def __getattr__(self, name: str) -> object:
        service = self._get()
        if service is None:
            if self._error_factory is not None:
                raise self._error_factory()
            raise AttributeError(name)
        return getattr(service, name)


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


def build_tdoc_search_facade(
    settings: Settings | None = None,
    *,
    fts5_service: SearchService | None | object = _UNSET,
    semantic_service: SemanticSearchService | None | object = _UNSET,
    source_repo: TDocRepository | None | object = _UNSET,
    embedder: Embedder | None = None,
    embedder_factory: Callable[[], Embedder | None] | None = None,
) -> TDocSearchFacade:
    """Construct the unified TDoc search facade from live collaborators."""
    if settings is None:
        settings = get_settings()
    embedder_value = embedder
    embedder_loaded = embedder is not None

    def shared_embedder() -> Embedder | None:
        nonlocal embedder_value, embedder_loaded
        if not embedder_loaded:
            embedder_value = (
                embedder_factory() if embedder_factory is not None
                else build_embedder(settings)
            )
            embedder_loaded = True
        return embedder_value

    if fts5_service is _UNSET:
        fts5_service = _LazyService(
            lambda: build_search_service(settings, embedder=shared_embedder()),
            lambda: SearchUnavailableError("FTS5 search is not available"),
        )
    if semantic_service is _UNSET:
        semantic_service = _LazyService(
            lambda: build_semantic_search_service(
                settings,
                fts5_service=fts5_service,  # type: ignore[arg-type]
                embedder=shared_embedder(),
            ),
            lambda: SemanticSearchUnavailableError(
                "semantic search is not available"
            ),
        )
    if source_repo is _UNSET:
        source_repo = SQLAlchemyTDocRepository()
    return TDocSearchFacade(
        fts5_service=fts5_service,
        semantic_service=semantic_service,
        source_repo=source_repo,
        settings=settings,
    )


def build_tdoc_index_service(
    settings: Settings | None = None,
    *,
    fts5_service: SearchService | None | object = _UNSET,
    semantic_service: SemanticSearchService | None | object = _UNSET,
    embedder: Embedder | None = None,
) -> TDocIndexService:
    """Construct the TDoc index coordinator from live low-level services."""
    if settings is None:
        settings = get_settings()
    if fts5_service is _UNSET:
        fts5_service = build_search_service(settings, embedder=embedder)
    if semantic_service is _UNSET:
        semantic_service = build_semantic_search_service(
            settings,
            fts5_service=fts5_service,  # type: ignore[arg-type]
            embedder=embedder,
        )
    return TDocIndexService(
        fts5_service=fts5_service,
        semantic_service=semantic_service,
        settings=settings,
    )


def build_spec_doc_search_facade(
    settings: Settings | None = None,
    *,
    fts5_service: object = _UNSET,
    semantic_service: object = _UNSET,
    source_repo: SpecDocRepository | None | object = _UNSET,
    embedder: Embedder | None = None,
    embedder_factory: Callable[[], Embedder | None] | None = None,
) -> SpecDocSearchFacade:
    """Construct the unified spec-document search facade."""
    if settings is None:
        settings = get_settings()
    embedder_value = embedder
    embedder_loaded = embedder is not None

    def shared_embedder() -> Embedder | None:
        nonlocal embedder_value, embedder_loaded
        if not embedder_loaded:
            embedder_value = (
                embedder_factory() if embedder_factory is not None
                else build_embedder(settings)
            )
            embedder_loaded = True
        return embedder_value

    if fts5_service is _UNSET:
        fts5_service = _LazyService(
            lambda: build_spec_doc_search_service(settings),
            lambda: SearchUnavailableError("FTS5 search is not available"),
        )
    if semantic_service is _UNSET:
        semantic_service = _LazyService(
            lambda: build_spec_doc_semantic_service(
                settings,
                embedder=shared_embedder(),
                fts5_service=fts5_service,
            ),
            lambda: SemanticSearchUnavailableError(
                "semantic search is not available"
            ),
        )
    if source_repo is _UNSET:
        source_repo = build_spec_doc_repository()
    return SpecDocSearchFacade(
        fts5_service=fts5_service,
        semantic_service=semantic_service,
        source_repo=source_repo,
        settings=settings,
    )


def build_spec_doc_index_service(
    settings: Settings | None = None,
    *,
    fts5_service: object = _UNSET,
    semantic_service: object = _UNSET,
    embedder: Embedder | None = None,
) -> SpecDocIndexService:
    """Construct the spec-document index coordinator."""
    if settings is None:
        settings = get_settings()
    if fts5_service is _UNSET:
        fts5_service = build_spec_doc_search_service(settings)
    if semantic_service is _UNSET:
        semantic_service = build_spec_doc_semantic_service(
            settings, embedder=embedder, fts5_service=fts5_service
        )
    return SpecDocIndexService(
        fts5_service=fts5_service,
        semantic_service=semantic_service,
        settings=settings,
    )


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
        parsed_status_repository=SQLAlchemySpecDocRepository(),
    )


def build_spec_doc_repository() -> SQLAlchemySpecDocRepository:
    """Construct the spec-document source repository."""
    return SQLAlchemySpecDocRepository()


def build_spec_doc_search_service(
    settings: Settings | None = None,
    repo: SpecDocSearchRepository | None = None,
) -> object | None:
    """Build a spec-doc FTS5 search hook or return ``None`` if unavailable.

    Best-effort: any :class:`SearchUnavailableError` raised by the
    repo (missing FTS5, missing extra) is caught here once at startup
    and returned as ``None``. The CLI and the
    :class:`SpecDocService` hook both treat ``None`` as "search is
    not available" and skip. Disabled via
    ``Settings.spec_doc.auto_index_on_parse`` being ``False`` only
    at the hook site — the builder still returns the live repo so
    CLI ``search`` commands keep working until the flag is read.
    """
    if settings is None:
        settings = get_settings()
    if not settings.search.enabled:
        return None
    try:
        if repo is None:
            repo = SQLAlchemySpecDocSearchRepository()
        from doc3gpp.services.spec_doc_search_service import SpecDocSearchService

        return SpecDocSearchService(repo=repo)
    except SearchUnavailableError:
        return None


def build_lazy_search_service(
    settings: Settings,
    *,
    embedder_factory: Callable[[], Embedder | None],
) -> _LazyService:
    return _LazyService(
        lambda: build_search_service(settings, embedder=embedder_factory()),
        lambda: SearchUnavailableError("FTS5 search is not available"),
    )


def build_lazy_semantic_search_service(
    settings: Settings,
    *,
    fts5_service: object,
    embedder_factory: Callable[[], Embedder | None],
) -> _LazyService:
    return _LazyService(
        lambda: build_semantic_search_service(
            settings,
            fts5_service=fts5_service,  # type: ignore[arg-type]
            embedder=embedder_factory(),
        ),
        lambda: SemanticSearchUnavailableError(
            "semantic search is not available"
        ),
    )


def build_lazy_spec_doc_search_service(
    settings: Settings,
) -> _LazyService:
    return _LazyService(
        lambda: build_spec_doc_search_service(settings),
        lambda: SearchUnavailableError("FTS5 search is not available"),
    )


def build_lazy_spec_doc_semantic_service(
    settings: Settings,
    *,
    fts5_service: object,
    embedder_factory: Callable[[], Embedder | None],
) -> _LazyService:
    return _LazyService(
        lambda: build_spec_doc_semantic_service(
            settings,
            fts5_service=fts5_service,
            embedder=embedder_factory(),
        ),
        lambda: SemanticSearchUnavailableError(
            "semantic search is not available"
        ),
    )


def build_spec_doc_semantic_service(
    settings: Settings | None = None,
    vector_repo: SpecDocVectorRepository | None = None,
    embedder: Embedder | None = None,
    fts5_service: object | None = None,
) -> object | None:
    """Build a spec-doc hybrid semantic hook or return ``None`` if unavailable."""
    from sqlalchemy.exc import OperationalError as SAOperationalError

    from doc3gpp.models.semantic_search import EmbedderUnavailableError

    if settings is None:
        settings = get_settings()
    if not settings.semantic_search.enabled:
        return None
    try:
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
                vector_repo = SQLAlchemySpecDocVectorRepository(
                    expected_model=getattr(embedder, "model_name", None),
                    expected_dim=live_dim,
                )
            except VectorIndexUnavailableError:
                return None
        from doc3gpp.services.spec_doc_semantic_service import SpecDocSemanticService

        return SpecDocSemanticService(
            fts5_service=fts5_service,
            embedder=embedder,
            vector_repo=vector_repo,
            settings=settings,
            doc_repo=SQLAlchemySpecDocRepository(),
        )
    except (
        VectorIndexUnavailableError,
        EmbedderUnavailableError,
        SAOperationalError,
    ):
        return None


def build_spec_doc_service(
    embedder: Embedder | None = None,
    *,
    settings: Settings | None = None,
    search_service: object | None = None,
    semantic_service: object | None = None,
) -> SpecDocService:
    """Construct a :class:`SpecDocService` for the ``spec doc`` commands.

    Wires the main-DB :class:`SQLAlchemySpecRepository` (version
    resolution), the specdata :class:`SQLAlchemySpecDocRepository`,
    the FTS5 search hook (:func:`build_spec_doc_search_service`),
    and the vector hook
    (:func:`build_spec_doc_semantic_service`). Both hooks are
    best-effort — ``None`` when the subsystem is disabled or its
    extra is not installed; the service skips the hook in that
    case. A single shared embedder (built once here when the caller
    does not inject one) feeds the semantic hook so one httpx
    client is shared per process.
    """
    from doc3gpp.services.spec_doc_service import SpecDocService

    if settings is None:
        settings = get_settings()
    if embedder is None and semantic_service is None:
        embedder = build_embedder(settings)
    return SpecDocService(
        spec_repo=SQLAlchemySpecRepository(),
        settings=settings,
        search_service=(
            search_service
            if search_service is not None
            else build_spec_doc_search_service(settings)
        ),
        semantic_service=(
            semantic_service
            if semantic_service is not None
            else build_spec_doc_semantic_service(settings, embedder=embedder)
        ),
        embedder=embedder,
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
    embedder: Embedder | None = None,
    search_service: object | None = None,
    semantic_service: object | None = None,
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
    if embedder is None and (
        search_service is None or semantic_service is None
    ):
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
        search_service=(
            search_service
            if search_service is not None
            else build_search_service(embedder=embedder)
        ),
        semantic_service=(
            semantic_service
            if semantic_service is not None
            else build_semantic_search_service(embedder=embedder)
        ),
    )


def build_semantic_search_service(
    settings: Settings | None = None,
    fts5_service: SearchService | None = None,
    embedder: Embedder | None = None,
    vector_repo: VectorIndexRepository | None = None,  # noqa: F821
) -> SemanticSearchService | None:
    """Build a :class:`SemanticSearchService` or return ``None`` if unavailable.

    Best-effort: catches :class:`VectorIndexUnavailableError`,
    :class:`EmbedderUnavailableError`
    raised by the collaborators and returns ``None``. FTS5 is the
    foundation — if :func:`build_search_service` returns ``None`` this
    returns ``None`` too.
    """
    from sqlalchemy.exc import OperationalError as SAOperationalError

    from doc3gpp.models.semantic_search import (
        EmbedderUnavailableError,
        VectorIndexUnavailableError,
    )
    from doc3gpp.storage.repositories.vector_sql import (
        SQLAlchemyVectorIndexRepository,
    )

    if settings is None:
        settings = get_settings()
    if not settings.semantic_search.enabled:
        return None
    try:
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
    embedder: Embedder | None = None,
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
