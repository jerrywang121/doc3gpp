"""End-to-end TDoc CR (Change Request) extraction pipeline.

Glues the per-stage building blocks together:

1. Validate the requested ``tdoc_id``.
2. Confirm the TDoc exists in the ``tdocs`` table and is of type
   ``"CR"`` (the ``tdocs.ftp_url`` row is the source of truth — the TDoc
   list XLSX classifies each id by document type).
3. Download (or cache-hit) the TDoc zip from the 3GPP FTP via
   :mod:`doc3gpp.scraping.tdoc_zip_source`.
4. Enumerate every supported ``.docx`` member and convert members
   sequentially through the shared parser pipeline.
5. Select exactly one main document with the structural CR-cover detector;
   supporting documents remain content-only. Annexes that fail to convert are
   recorded in ``skipped_sources`` instead of failing the TDoc.
6. Build a versioned Markdown archive that holds every converted document.
7. Persist the cover, optional sidecars, extraction metadata, and the annex
   rows atomically through ``TDocContentRepository`` before indexing.

TDocs whose type has a family content store (WID, LS, discussion) are handed to
:class:`doc3gpp.services.tdoc_family_service.TDocFamilyService` instead.

The service owns three caching layers:

* **Zip cache** (``<root>/zips/<cache_file>``) — keyed by the
  URL-derived ``cache_file`` basename (via
  :func:`doc3gpp.scraping.cache_keys.derive_cache_file`); bypassed on
  network miss by
  :func:`doc3gpp.scraping.tdoc_zip_source.download_tdoc_zip`.
* **Markdown cache** (``<root>/markdown/<cache_file>``) — a real ZIP
  archive with one Markdown member per DOCX and a versioned manifest. The
  source path, document IDs, order, and content hashes are explicit in the
  manifest; legacy plain-UTF-8, gzip, and one-member ZIP artifacts remain
  readable for transitional cache consumers.
* **Database cache** — a version-2 parent row, the cover row, and the complete
  annex collection. The CR main is never stored in SQL. A hit also requires the
  Markdown archive file, so a cached archive is the only source of the main.

The ``from_cache`` flag on :class:`ExtractResult` refers to a complete SQL
bundle hit. A cache artifact alone is not evidence of extraction completeness
and is never used to skip reprocessing of an incomplete SQL parent.

The main document's :class:`TDocCRParseResult` owns the slim cover row and
optional TTCN/body-change sidecars. Supporting documents never enter the CR
parser. ``replace_cr_bundle`` is the single repository write boundary; search
and semantic indexing remain best-effort hooks that run only after the atomic
transaction commits.
"""

from __future__ import annotations

import asyncio
import logging
import re
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_content_families import (
    FAMILY_STORES,
    TDOC_CONTENT_STORES,
    is_complete_cr_extraction,
    is_eligible_type,
    store_for_type,
)
from doc3gpp.models.tdoc_cr import (
    DirectParseBatchResult,
    DirectParseResult,
    DirectSourceKind,
    TDocCRDetails,
    TDocCRParseResult,
    TDocCRTTCNDetails,
    TDocExtractMeta,
)
from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
from doc3gpp.models.tdoc_document import (
    TDocDocument,
    TDocParseBundle,
    markdown_member_name,
)
from doc3gpp.parsers.cr_parser import CRHeaderMissingError
from doc3gpp.parsers.direct_extractor import (
    NotAFolderError,
    _direct_parse_payload,
    direct_parse_bytes,
    extract_tdoc_id_from_filename,
    is_3gpp_ftp_candidate_url,
    list_3gpp_directory,
    validate_tdoc_ftp_url,
)
from doc3gpp.parsers.docx_converter import DocxMemberPool
from doc3gpp.parsers.normalizers import build_ftp_url, normalize_ftp_path
from doc3gpp.parsers.tdoc_family_main import TDocMainDocumentError
from doc3gpp.parsers.tdoc_parsers import (
    TDocParser,
    TDocParserRegistry,
    build_default_registry,
)
from doc3gpp.parsers.tdoc_zip import (
    build_markdown_archive,
    document_id_for,
    read_markdown_archive,
)
from doc3gpp.parsers.tdoc_zip_convert import (
    TDocZipConversion,
    convert_direct_main_markdown,
    convert_direct_payload,
)
from doc3gpp.repository.protocols import (
    TDocContentRepository,
    TDocCrChangeDetailsRepository,
    TDocCrDetailRepository,
    TDocCrTTCNDetailRepository,
    TDocRepository,
)
from doc3gpp.scraping.cache_keys import derive_cache_file
from doc3gpp.scraping.tdoc_zip_source import (
    TDocCacheLike,
    TDocTooLargeError,
    TDocZipDownloadError,
    download_tdoc_zip,
    resolve_download_url,
)
from doc3gpp.services.pipeline_runtime import PipelineRuntime
from doc3gpp.services.tdoc_errors import (
    TDocNotYetOnFTPError,
    TDocTypeSkippedError,
    TDocTypeUnsupportedError,
)
from doc3gpp.services.tdoc_family_service import (
    FamilyExtractResult,
    PreparedFamilyExtract,
    TDocFamilyService,
)
from doc3gpp.services.tdoc_pipeline import PipelineItem, PipelineProgress, TDocPipeline
from doc3gpp.services.tdoc_type_detection import (
    TYPE_UNDETERMINED,
    DirectTypeDecision,
    decide_direct_type,
    decide_known_type,
)

# Re-exported so callers can catch ``TDocTooLargeError`` from a single
# import path (the service layer) without depending on the scraping
# module directly. The canonical home of the class is
# :mod:`doc3gpp.scraping.tdoc_zip_source` to avoid a service ↔ scraping
# circular import (the service already imports from scraping).
__all__ = [
    "BatchExtractResult",
    "CRHeaderMissingError",
    "ExtractResult",
    "PreparedDirectFamily",
    "PreparedDirectTDoc",
    "PreparedTDocExtract",
    "TDocCrService",
    "TDocNotFoundError",
    "TDocNotYetOnFTPError",
    "TDocTooLargeError",
    "TDocTypeSkippedError",
    "TDocTypeUnsupportedError",
    "TDocZipDownloadError",
]

if TYPE_CHECKING:
    from doc3gpp.models.tdoc_content_families import TDocContentStore
    from doc3gpp.scraping.client import ScraperClient
    from doc3gpp.services.search_service import SearchService
    from doc3gpp.services.semantic_search_service import SemanticSearchService

logger = logging.getLogger(__name__)


_CR_STORE = next(store for store in TDOC_CONTENT_STORES if store.key == "cr")

# Conservative shape guard for ``tdoc_id``. The CR/zip parser already
# enforces a stricter regex internally; this is a *fast-fail* so a
# garbage id never touches the cache root, the network, or the DB.
_TDOC_ID_RE = re.compile(r"[A-Za-z0-9-]{1,32}")


# ---------------------------------------------------------------------------
# Public exceptions raised by the service.
# ---------------------------------------------------------------------------


class _DirectPipelineError(BaseException):
    """Preserve a direct one-item exception through the runner."""

    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__(str(cause))


class TDocNotFoundError(LookupError):
    """The given tdoc_id is not present in the ``tdocs`` table.

    Inherits from :class:`LookupError` so callers that catch
    :class:`KeyError` (a sibling class) also handle this correctly.
    """

    def __init__(self, tdoc_id: str) -> None:
        self.tdoc_id = tdoc_id
        super().__init__(
            f"TDoc {tdoc_id!r} is not stored in the tdocs table; "
            "run `doc3gpp tdoc sync` first"
        )


class TDocUrlNotFoundError(LookupError):
    """No row matches the requested FTP URL across any of the six URL-keyed tables.

    Raised by ``TDocShowRecordByUrl.from_ftp_url`` consumers (the web
    ``GET /tdocs/by-url`` route and the MCP ``get_tdoc(ftp_url=...)``
    tool) when the normalised URL resolves to no rows in ``tdocs``,
    ``tdoc_cr_cover_page``, ``tdoc_cr_ttcn_details``, ``tdoc_extracts``,
    ``tdoc_cr_change_details``, or ``tdoc_files``. Distinct from
    :class:`TDocNotFoundError` (which is raised for a missing
    ``tdoc_id`` lookup).
    """

    def __init__(self, ftp_url: str) -> None:
        self.ftp_url = ftp_url
        super().__init__(
            f"No stored rows match ftp_url {ftp_url!r}. The URL was looked "
            "up against tdocs, tdoc_cr_cover_page, tdoc_cr_ttcn_details, "
            "tdoc_extracts, tdoc_cr_change_details, and tdoc_files; "
            "none matched. The upstream document may not have been "
            "ingested yet — run 'doc3gpp tdoc sync' on the parent "
            f"meeting, or 'doc3gpp tdoc parse --from-url {ftp_url}' to "
            "populate the URL-keyed tables."
        )


# ---------------------------------------------------------------------------
# Service result DTO.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ExtractResult:
    """Outcome of a single :meth:`TDocCrService.extract` call.

    Attributes:
        details: The parsed CR fields. On a cache hit, the fields come
            straight from the persisted row; on a fresh extract, they
            come from the in-memory parser output that was just
            upserted.
        extract_meta: Metadata pointing at the on-disk cache artefacts
            (the ``cache_file`` basename and the inner docx filename).
            On a DB-cache hit the basename reflects what the previous
            successful extract wrote.
        from_cache: ``True`` iff the result was returned from the
            ``tdoc_cr_cover_page`` / ``tdoc_extracts`` rows **without**
            re-downloading the zip or re-rendering the markdown. A hot
            markdown cache alone does NOT set this flag — we still
            re-parse the markdown against the latest parser regexes
            before declaring the result "fresh".
    """

    details: TDocCRDetails
    extract_meta: TDocExtractMeta
    from_cache: bool

    @property
    def tdoc_id(self) -> str:
        return self.details.tdoc_id


def _annexes_of(
    documents: tuple[TDocDocument, ...],
    main_source_file: str,
) -> tuple[TDocDocument, ...]:
    """Return every converted document except the CR main, which SQL never stores."""
    return tuple(document for document in documents if document.source_file != main_source_file)


@dataclass(slots=True, frozen=True)
class PreparedTDocExtract:
    """CR metadata and all converted source documents for one TDoc ZIP."""

    details: TDocCRDetails
    documents: tuple[TDocDocument, ...]
    main_source_file: str
    ttcn: TDocCRTTCNDetails | None
    changes: TDocCRChangeDetails | None
    extract_meta: TDocExtractMeta

    @property
    def annexes(self) -> tuple[TDocDocument, ...]:
        return _annexes_of(self.documents, self.main_source_file)


@dataclass(slots=True, frozen=True)
class PreparedDirectTDoc:
    """Prepared direct-URL bundle awaiting the serialized SQL writer."""

    url: str
    documents: tuple[TDocDocument, ...]
    main_source_file: str
    details: TDocCRDetails
    ttcn: TDocCRTTCNDetails | None
    changes: TDocCRChangeDetails | None
    extract_meta: TDocExtractMeta
    tdoc_id: str
    cache_file: str

    @property
    def annexes(self) -> tuple[TDocDocument, ...]:
        return _annexes_of(self.documents, self.main_source_file)


@dataclass(slots=True, frozen=True)
class PreparedDirectFamily:
    """Prepared family bundle from a direct URL, awaiting the serialized SQL writer."""

    url: str
    prepared: PreparedFamilyExtract

    @property
    def tdoc_id(self) -> str:
        return self.prepared.tdoc_id


@dataclass(slots=True, frozen=True)
class _DirectSource:
    """One direct-mode source: its bytes, identity, and the TDoc id from its name."""

    payload: bytes
    filename: str
    kind: DirectSourceKind
    source_url: str | None
    identity: str
    tdoc_id: str | None


def _check_direct_size(payload: bytes, source: str, max_bytes: int) -> None:
    if max_bytes > 0 and len(payload) > max_bytes:
        raise TDocTooLargeError(source=source, size=len(payload), limit=max_bytes)


def _family_direct_result(
    *,
    source_kind: DirectSourceKind,
    source_url: str | None,
    tdoc_id: str | None,
    tdoc_id_in_tdocs: bool,
    store: TDocContentStore,
    documents: tuple[TDocDocument, ...],
    main_source_file: str,
    extract_meta: TDocExtractMeta | None,
    skipped_sources: tuple[str, ...],
    from_cache: bool,
    persisted: bool,
) -> DirectParseResult:
    return DirectParseResult(
        source_kind=source_kind,
        documents=documents,
        main_source_file=main_source_file,
        details=None,
        extract_meta=extract_meta,
        from_cache=from_cache,
        persisted=persisted,
        tdoc_id=tdoc_id,
        tdoc_id_in_tdocs=tdoc_id_in_tdocs,
        source_url=source_url,
        content_store=store,
        skipped_sources=skipped_sources,
    )


@dataclass(slots=True, frozen=True)
class BatchExtractResult:
    """Outcome of a batch :meth:`TDocCrService.extract_many` call.

    Bundles successful extracts with a per-id failure reason so the
    CLI can surface a short inline message (e.g.
    ``TDocNotFoundError: TDoc 'R5s260010' is not stored...``) instead
    of pointing the operator at the log file. A single broken id does
    not abort the batch — it lands in :attr:`failures` keyed by the
    normalised tdoc_id, and the remaining ids continue. Skipped ids
    (e.g. rows whose ``ftp_url`` is still NULL because the 3GPP upload
    pipeline hasn't propagated yet) land in :attr:`skipped` instead —
    the CLI counts them separately so they don't pollute the failure
    total.

    Attributes:
        successes: ``{tdoc_id: ExtractResult}`` for every id that
            produced a usable extract (cache hit or fresh). The keys are
            the canonical (normalised) ids as stored in the database.
        failures: ``{tdoc_id: short reason}`` for every id that the
            per-id ``try/except`` swallowed. The reason is formatted as
            ``"{ExceptionClassName}: {exc}"`` — the class name tells
            the operator *which* step failed (download, parse, type
            guard) without tailing logs, and the exception's own
            message carries the actionable detail (e.g. "run
            ``doc3gpp tdoc sync`` first" for a missing row).
        skipped: ``{tdoc_id: short reason}`` for every id that was
            routed to the skip bucket (currently
            :class:`TDocNotYetOnFTPError` only). Tracked separately
            from failures because the operator-facing meaning differs
            ("FTP hasn't published yet" is not an error from our
            side) and the CLI summary table counts it in its generic
            "Skipped" line.
    """

    successes: dict[str, ExtractResult | FamilyExtractResult]
    failures: dict[str, str]
    skipped: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


class TDocCrService:
    """End-to-end TDoc CR extraction orchestrator.

    Wires together the cache, the HTTP scraper client, the CR-detail
    repository, and the read-only TDoc repository (used purely to
    validate ``tdoc_id`` shape and ``type == "CR"``).
    """

    def __init__(
        self,
        *,
        cache: TDocCacheLike,
        scraper_client: ScraperClient,
        cr_repository: TDocCrDetailRepository,
        cr_ttcn_repository: TDocCrTTCNDetailRepository,
        cr_change_details_repository: TDocCrChangeDetailsRepository,
        tdoc_repository: TDocRepository,
        content_repo: TDocContentRepository,
        parser: TDocParser | None = None,
        parser_registry: TDocParserRegistry | None = None,
        max_tdoc_size_bytes: int = 0,
        max_documents_per_zip: int | None = None,
        max_uncompressed_size_bytes: int | None = None,
        search_service: SearchService | None = None,
        semantic_service: SemanticSearchService | None = None,
        family_service: TDocFamilyService | None = None,
        docx_pool: DocxMemberPool | None = None,
    ) -> None:
        from doc3gpp.settings.loader import get_settings

        self._settings = get_settings()
        self._cache = cache
        self._scraper = scraper_client
        self._repo = cr_repository
        self._cr_ttcn_repo = cr_ttcn_repository
        self._change_details_repo = cr_change_details_repository
        self._tdoc_repo = tdoc_repository
        self._content_repo = content_repo
        self._parser = parser
        self._parser_registry = parser_registry
        self._max_tdoc_size_bytes = max_tdoc_size_bytes
        self._max_documents_per_zip = (
            self._settings.tdoc_parse.max_documents_per_zip
            if max_documents_per_zip is None
            else max_documents_per_zip
        )
        self._max_uncompressed_size_bytes = (
            self._settings.tdoc_parse.max_uncompressed_size_kb * 1024
            if max_uncompressed_size_bytes is None
            else max_uncompressed_size_bytes
        )
        self._search_service = search_service
        self._semantic_service = semantic_service
        self._docx_pool = docx_pool
        if family_service is None:
            family_service = TDocFamilyService(
                scraper=scraper_client,
                cache=cache,
                content_repo=content_repo,
                max_tdoc_size_bytes=max_tdoc_size_bytes,
                max_documents_per_zip=self._max_documents_per_zip,
                max_uncompressed_size_bytes=self._max_uncompressed_size_bytes,
                docx_pool=docx_pool,
            )
        self._family_service = family_service

    def _resolve_parser(self, tdoc_id: str) -> TDocParser:
        """Return the parser to use for ``tdoc_id``.

        When a single parser was injected at construction, it is used
        for every call. Otherwise the registry resolves a parser per
        ``tdoc_id`` so TTCN ids route to :class:`TTCNCRParser` and
        other ids fall back to the generic :class:`CRParser`.
        """
        if self._parser is not None:
            return self._parser
        registry = self._parser_registry or build_default_registry()
        return registry.resolve(tdoc_id, tdoc_type="CR")


    def _parse_bundle(
        self,
        payload: bytes,
        *,
        filename: str,
        max_bytes: int | None = None,
        tdoc_id: str | None = None,
    ) -> TDocParseBundle:
        """Run shared ZIP enumeration, sequential conversion, and main parsing."""
        source_limit = (
            self._max_tdoc_size_bytes if max_bytes is None else max_bytes
        )
        if (
            self._parser is None
            and self._parser_registry is None
            and tdoc_id is None
        ):
            return direct_parse_bytes(
                payload,
                filename=filename,
                max_bytes=source_limit,
                max_documents=self._max_documents_per_zip,
                max_uncompressed_bytes=self._max_uncompressed_size_bytes,
            )

        def parse_main(markdown: str, parsed_tdoc_id: str) -> TDocCRParseResult:
            resolved_id = tdoc_id or parsed_tdoc_id
            return self._resolve_parser(resolved_id).parse(
                markdown,
                tdoc_id=resolved_id,
            )

        return _direct_parse_payload(
            payload,
            filename=filename,
            max_bytes=source_limit,
            max_documents=self._max_documents_per_zip,
            max_uncompressed_bytes=self._max_uncompressed_size_bytes,
            tdoc_id=tdoc_id,
            parse_main=parse_main,
        )

    @staticmethod
    def _bind_documents_to_ftp_url(
        documents: tuple[TDocDocument, ...],
        ftp_url: str,
    ) -> tuple[TDocDocument, ...]:
        """Replace ephemeral identities with immutable-URL document identities."""
        persistent: list[TDocDocument] = []
        for document in documents:
            document_id = document_id_for(ftp_url, document.source_file)
            persistent.append(
                replace(
                    document,
                    document_id=document_id,
                    markdown_file=markdown_member_name(document_id, document.source_file),
                )
            )
        return tuple(persistent)

    def _write_markdown_archive(
        self,
        *,
        cache_file: str,
        ftp_url: str,
        main_source_file: str,
        documents: tuple[TDocDocument, ...],
    ) -> None:
        archive = build_markdown_archive(
            documents,
            ftp_url=ftp_url,
            main_source_file=main_source_file,
        )
        self._cache.put_bytes(cache_file, archive, "markdown")

    def _index_after_parse(self, tdoc_id: str) -> None:
        """Best-effort FTS5 upsert after a successful parse.

        Called by both the DB-mode ``extract`` happy path and the
        direct-mode ``_extract_from_3gpp_url`` happy path — both
        produce the same final DB state, so both should keep the
        index in sync. Skipped when
        ``Settings.search.auto_index_on_parse`` is False or when
        the search extra is not installed (``_search_service`` is
        ``None``).

        Best-effort: every exception is caught and logged. A failing
        index write never aborts a successful parse.
        """
        if not self._settings.search.auto_index_on_parse:
            return
        if self._search_service is None:
            return
        try:
            self._search_service.upsert_for_tdoc(tdoc_id)
        except Exception as exc:  # noqa: BLE001 - index updates are best-effort
            logger.warning(
                "failed to update search index for tdoc_id=%s: %s",
                tdoc_id, exc,
            )

    def _embed_after_parse(self, tdoc_id: str) -> None:
        """Best-effort embedding upsert after a successful parse.

        Sibling of :meth:`_index_after_parse`; fires from the same two
        call sites so the vector index stays in sync with both the
        DB-mode ``extract`` happy path and the direct-mode
        ``_extract_from_3gpp_url`` happy path. Skipped when
        ``Settings.semantic_search.auto_embed_on_parse`` is False or
        when the semantic extra is not installed
        (``_semantic_service`` is ``None``).

        Best-effort: every exception is caught and logged. A failing
        embed never aborts a successful parse — a stale FTS5/vector
        index can always be rebuilt via :meth:`SearchService.rebuild`
        / :meth:`SemanticSearchService.rebuild_embeddings`.
        """
        if not self._settings.semantic_search.auto_embed_on_parse:
            return
        if self._semantic_service is None:
            return
        try:
            self._semantic_service.index_for_tdoc(tdoc_id)
        except Exception as exc:  # noqa: BLE001 - best-effort hook must not abort a successful parse
            logger.warning(
                "failed to update embedding index for tdoc_id=%s: %s",
                tdoc_id, exc,
            )

    def _cached_cr_parts(
        self,
        stored_url: str,
        tdoc_id: str,
    ) -> tuple[TDocCRDetails, TDocExtractMeta, tuple[TDocDocument, ...]] | None:
        """Return the stored CR cover, parent, and annexes when a complete bundle exists."""
        cached_details = self._repo.get_by_url(stored_url)
        cached_meta = self._content_repo.get_extract_meta(stored_url)
        if (
            cached_details is None
            or cached_meta is None
            or cached_meta.tdoc_id != tdoc_id
            or normalize_ftp_path(cached_meta.ftp_url) != stored_url
        ):
            return None
        annexes = self._content_repo.list_documents(_CR_STORE, stored_url)
        if not is_complete_cr_extraction(cached_meta, annexes, has_cover=True):
            return None
        if not self._cache.path_for(cached_meta.cache_file, "markdown").is_file():
            return None
        return cached_details, cached_meta, annexes

    def _cached_extract(self, stored_url: str, tdoc_id: str) -> ExtractResult | None:
        cached = self._cached_cr_parts(stored_url, tdoc_id)
        if cached is None:
            return None
        details, meta, _annexes = cached
        logger.debug(
            "Complete SQL extraction cache hit for TDoc %s at %s",
            tdoc_id,
            stored_url,
        )
        return ExtractResult(details=details, extract_meta=meta, from_cache=True)

    def _cached_main_document(self, meta: TDocExtractMeta) -> TDocDocument | None:
        """Return the CR main from its archive; SQL never stores it."""
        raw = self._cache.get_bytes(meta.cache_file, "markdown")
        if raw is None:
            return None
        try:
            archive = read_markdown_archive(raw)
        except (OSError, ValueError, zipfile.BadZipFile):
            return None
        return next(
            (
                document
                for document in archive.documents
                if document.source_file == meta.doc_filename
            ),
            None,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def prepare_extract(
        self,
        tdoc_id: str,
        *,
        force: bool = False,
    ) -> PreparedTDocExtract | PreparedFamilyExtract | ExtractResult | FamilyExtractResult:
        """Prepare every supported DOCX in one TDoc ZIP without SQL writes."""
        normalised = self._validate_tdoc_id(tdoc_id)
        tdoc = self._load_tdoc(normalised)
        if not tdoc.ftp_url:
            raise TDocNotYetOnFTPError(normalised)
        store = store_for_type(tdoc.type)
        if store is not None and store.includes_main:
            return self._family_service.prepare_extract(tdoc, store, force=force)

        primary_url = build_ftp_url(tdoc.ftp_url)
        cache_file = derive_cache_file(tdoc.ftp_url)
        candidates = resolve_download_url(normalised, primary_url)
        canonical_url = normalize_ftp_path(primary_url)

        if not force:
            for candidate in candidates:
                stored_url = normalize_ftp_path(candidate)
                if stored_url != canonical_url:
                    continue
                cached = self._cached_extract(stored_url, normalised)
                if cached is not None:
                    return cached

        downloaded = download_tdoc_zip(
            normalised,
            self._scraper,
            self._cache,
            primary_url=primary_url,
            ftp_url=tdoc.ftp_url,
            max_bytes=self._max_tdoc_size_bytes,
        )
        resolved_url = downloaded.url
        if (
            not force
            and resolved_url
            and normalize_ftp_path(resolved_url) == canonical_url
        ):
            cached = self._cached_extract(normalize_ftp_path(resolved_url), normalised)
            if cached is not None:
                return cached

        stored_url = (
            normalize_ftp_path(resolved_url)
            if resolved_url
            else normalize_ftp_path(candidates[0])
        )
        bundle = self._parse_bundle(
            downloaded.path.read_bytes(),
            filename=cache_file,
            max_bytes=self._max_tdoc_size_bytes,
            tdoc_id=normalised,
        )
        documents = self._bind_documents_to_ftp_url(bundle.documents, stored_url)
        parsed = bundle.main_parse_result
        cover = replace(
            parsed.cover,
            tdoc_id=normalised,
            ftp_url=stored_url,
        )
        ttcn = (
            replace(parsed.ttcn, ftp_url=stored_url, tdoc_id=normalised)
            if parsed.ttcn is not None
            else None
        )
        changes = (
            replace(parsed.changes, ftp_url=stored_url, tdoc_id=normalised)
            if parsed.changes is not None
            else None
        )
        meta = TDocExtractMeta(
            ftp_url=stored_url,
            tdoc_id=normalised,
            cache_file=cache_file,
            doc_filename=bundle.main_source_file,
            extraction_version=2,
            document_count=len(_annexes_of(documents, bundle.main_source_file)),
            skipped_sources=bundle.skipped_sources,
        )
        self._write_markdown_archive(
            cache_file=cache_file,
            ftp_url=stored_url,
            main_source_file=bundle.main_source_file,
            documents=documents,
        )
        return PreparedTDocExtract(
            details=cover,
            documents=documents,
            main_source_file=bundle.main_source_file,
            ttcn=ttcn,
            changes=changes,
            extract_meta=meta,
        )

    def persist_prepared_extract(
        self,
        prepared: PreparedTDocExtract | PreparedFamilyExtract,
    ) -> ExtractResult | FamilyExtractResult:
        """Commit the complete bundle before running best-effort index hooks."""
        if isinstance(prepared, PreparedFamilyExtract):
            family_result = self._family_service.persist_prepared_extract(prepared)
            self._index_after_parse(family_result.tdoc_id)
            self._embed_after_parse(family_result.tdoc_id)
            return family_result
        if not isinstance(prepared, PreparedTDocExtract):
            raise TypeError(f"cannot persist {type(prepared).__name__}")
        self._content_repo.replace_cr_bundle(
            prepared.details,
            prepared.ttcn,
            prepared.changes,
            prepared.extract_meta,
            prepared.annexes,
        )
        self._index_after_parse(prepared.details.tdoc_id)
        self._embed_after_parse(prepared.details.tdoc_id)
        logger.info(
            "Persisted %d CR annexes for TDoc %s at ftp_url %s",
            len(prepared.annexes),
            prepared.details.tdoc_id,
            prepared.extract_meta.ftp_url,
        )
        return ExtractResult(
            details=prepared.details,
            extract_meta=prepared.extract_meta,
            from_cache=False,
        )

    def extract(
        self,
        tdoc_id: str,
        *,
        force: bool = False,
    ) -> ExtractResult | FamilyExtractResult:
        """Prepare and persist one TDoc synchronously for CLI callers."""
        prepared = self.prepare_extract(tdoc_id, force=force)
        if isinstance(prepared, (ExtractResult, FamilyExtractResult)):
            return prepared
        return self.persist_prepared_extract(prepared)

    def extract_many(
        self,
        tdoc_ids: Iterable[str],
        *,
        force: bool = False,
        on_progress: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        runner: TDocPipeline | None = None,
    ) -> BatchExtractResult:
        """Extract a batch through the ordered shared preparation pipeline.

        Preparation runs in the runner's bounded pool and the prepared
        repository writes run on its single persistence lane. When no runner
        is supplied, a one-worker compatibility runtime is created and
        drained before returning so existing synchronous callers retain the
        same lifecycle and result contract.
        """
        source_ids = list(tdoc_ids)
        items: list[PipelineItem] = []
        repeats: list[PipelineItem] = []
        item_positions: list[int] = []
        repeat_positions: list[int] = []
        seen_keys: set[str] = set()
        for position, raw_id in enumerate(source_ids):
            item = PipelineItem(key=raw_id.strip(), value=raw_id)
            if item.key in seen_keys:
                repeats.append(item)
                repeat_positions.append(position)
            else:
                seen_keys.add(item.key)
                items.append(item)
                item_positions.append(position)
        owns_runner = runner is None
        runtime: PipelineRuntime | None = None
        if runner is None:
            runtime = PipelineRuntime(prepare_workers=1)
            runner = TDocPipeline(runtime)

        progress_offset = 0
        progress_total = len(items) + len(repeats)

        def report(progress: PipelineProgress) -> None:
            if on_progress is not None:
                on_progress(
                    f"parsed {progress_offset + progress.completed}"
                    f"/{progress_total} TDocs"
                )

        try:
            def run(batch: list[PipelineItem], *, force: bool):
                return runner.run_sync(
                    batch,
                    lambda raw_id: self.prepare_extract(
                        raw_id,
                        force=force,
                    ),
                    persist=cast(
                        Callable[[object], object],
                        self.persist_prepared_extract,
                    ),
                    is_complete=lambda value: isinstance(
                        value, (ExtractResult, FamilyExtractResult)
                    ),
                    is_cancelled=is_cancelled,
                    on_progress=report,
                )

            ordered: dict[int, Any] = dict(
                zip(item_positions, run(items, force=force), strict=True)
            )
            if repeats:
                # Repeated ids run only after every first occurrence has been
                # persisted, so they observe the stored state exactly as
                # sequential execution would (same ``force`` as the batch).
                progress_offset = len(items)
                ordered.update(
                    zip(repeat_positions, run(repeats, force=force), strict=True)
                )
            outcomes = [ordered[i] for i in sorted(ordered)]
        finally:
            if owns_runner and runtime is not None:
                runtime.shutdown()

        successes: dict[str, ExtractResult | FamilyExtractResult] = {}
        failures: dict[str, str] = {}
        skipped: dict[str, str] = {}
        for outcome in outcomes:
            reason = outcome.reason or ""
            if outcome.status == "success":
                result = cast(ExtractResult | FamilyExtractResult, outcome.value)
                successes[result.tdoc_id] = result
            elif outcome.status == "skipped":
                logger.info("Skipping TDoc %r: %s", outcome.key, reason)
                skipped[outcome.key] = reason
            else:
                logger.warning("Failed to extract TDoc %r: %s", outcome.key, reason)
                failures[outcome.key] = reason
        return BatchExtractResult(
            successes=successes,
            failures=failures,
            skipped=skipped,
        )

    # ------------------------------------------------------------------
    # Direct-parse path: ``tdoc parse --from-path/--from-url``
    # ------------------------------------------------------------------

    def extract_from_url(
        self,
        url: str,
        *,
        force: bool = False,
        max_tdoc_size_bytes: int | None = None,
        runner: TDocPipeline | None = None,
    ) -> DirectParseResult:
        """Download and parse one URL through the shared one-item pipeline."""
        owns_runner = runner is None
        runtime: PipelineRuntime | None = None
        if runner is None:
            runtime = PipelineRuntime(prepare_workers=1)
            runner = TDocPipeline(runtime)

        def prepare_one(
            value: str,
        ) -> PreparedDirectTDoc | PreparedDirectFamily | DirectParseResult:
            try:
                return self.prepare_from_url(
                    value,
                    force=force,
                    max_tdoc_size_bytes=max_tdoc_size_bytes,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The batch runner intentionally classifies exceptions into
                # result buckets. A direct one-item API must retain its
                # established exception contract, so carry the original
                # exception through the runner's fatal path.
                raise _DirectPipelineError(exc) from exc

        def persist_one(prepared: PreparedDirectTDoc) -> DirectParseResult:
            try:
                return self.persist_prepared_url(prepared)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _DirectPipelineError(exc) from exc

        try:
            try:
                outcomes = runner.run_sync(
                    [PipelineItem(key=url, value=url)],
                    prepare_one,
                    persist=cast(Callable[[object], object], persist_one),
                    is_complete=lambda value: isinstance(value, DirectParseResult),
                )
            except _DirectPipelineError as exc:
                raise exc.cause
        finally:
            if owns_runner and runtime is not None:
                runtime.shutdown()

        outcome = outcomes[0]
        if outcome.status != "success":
            raise RuntimeError(outcome.reason or "direct TDoc parse failed")
        return cast(DirectParseResult, outcome.value)

    def prepare_from_url(
        self,
        url: str,
        *,
        force: bool = False,
        max_tdoc_size_bytes: int | None = None,
    ) -> PreparedDirectTDoc | PreparedDirectFamily | DirectParseResult:
        """Prepare one direct URL bundle without mutating SQL repositories."""
        max_bytes = (
            self._max_tdoc_size_bytes
            if max_tdoc_size_bytes is None
            else max_tdoc_size_bytes
        )
        if is_3gpp_ftp_candidate_url(url):
            url = validate_tdoc_ftp_url(url)
        else:
            payload = self._scraper.get_bytes(url)
            return self._direct_in_memory(
                _DirectSource(
                    payload=payload,
                    filename=url,
                    kind="url-other",
                    source_url=url,
                    identity=url,
                    tdoc_id=extract_tdoc_id_from_filename(url),
                ),
                max_bytes=max_bytes,
            )

        cache_file = derive_cache_file(url)
        stored_ftp_url = normalize_ftp_path(url)
        extracted_id = extract_tdoc_id_from_filename(url)

        if extracted_id is None:
            logger.warning(
                "Direct-parse URL %s has no TDoc id pattern; skipping cache and DB writes",
                url,
            )
            payload = self._scraper.get_bytes(url)
            return self._direct_in_memory(
                _DirectSource(
                    payload=payload,
                    filename=url,
                    kind="url-3gpp",
                    source_url=url,
                    identity=stored_ftp_url,
                    tdoc_id=None,
                ),
                max_bytes=max_bytes,
            )

        tdoc = self._tdoc_repo.get_by_id(extracted_id)
        if tdoc is None:
            logger.warning(
                "Direct-parse URL %s has tdoc_id %s missing from tdocs; "
                "skipping cache and DB writes",
                url,
                extracted_id,
            )
            payload = self._scraper.get_bytes(url)
            return self._direct_in_memory(
                _DirectSource(
                    payload=payload,
                    filename=url,
                    kind="url-3gpp",
                    source_url=url,
                    identity=stored_ftp_url,
                    tdoc_id=extracted_id,
                ),
                max_bytes=max_bytes,
            )

        return self._prepare_from_3gpp_url(
            url=url,
            cache_file=cache_file,
            stored_ftp_url=stored_ftp_url,
            tdoc=tdoc,
            force=force,
            max_bytes=max_bytes,
        )

    def persist_prepared_url(
        self,
        prepared: PreparedDirectTDoc | PreparedDirectFamily,
    ) -> DirectParseResult:
        """Commit a URL-owned bundle once on the serialized writer lane."""
        if isinstance(prepared, PreparedDirectFamily):
            family_result = self._family_service.persist_prepared_extract(prepared.prepared)
            self._index_after_parse(family_result.tdoc_id)
            self._embed_after_parse(family_result.tdoc_id)
            logger.info(
                "Persisted %d family documents for TDoc %s at ftp_url %s",
                len(family_result.documents),
                family_result.tdoc_id,
                prepared.prepared.extract_meta.ftp_url,
            )
            return _family_direct_result(
                source_kind="url-3gpp",
                source_url=prepared.url,
                tdoc_id=family_result.tdoc_id,
                tdoc_id_in_tdocs=True,
                store=family_result.store,
                documents=family_result.documents,
                main_source_file=prepared.prepared.extract_meta.doc_filename,
                extract_meta=family_result.extract_meta,
                skipped_sources=family_result.skipped_sources,
                from_cache=False,
                persisted=True,
            )
        self._content_repo.replace_cr_bundle(
            prepared.details,
            prepared.ttcn,
            prepared.changes,
            prepared.extract_meta,
            prepared.annexes,
        )
        self._index_after_parse(prepared.tdoc_id)
        self._embed_after_parse(prepared.tdoc_id)
        logger.info(
            "Persisted %d direct-parse annexes for TDoc %s at %s",
            len(prepared.annexes),
            prepared.tdoc_id,
            prepared.extract_meta.ftp_url,
        )
        return DirectParseResult(
            source_kind="url-3gpp",
            documents=prepared.documents,
            main_source_file=prepared.main_source_file,
            details=prepared.details,
            extract_meta=prepared.extract_meta,
            from_cache=False,
            persisted=True,
            tdoc_id=prepared.tdoc_id,
            tdoc_id_in_tdocs=True,
            source_url=prepared.url,
        )

    def extract_from_url_batch(
        self,
        url: str,
        *,
        max_depth: int = 2,
        force: bool = False,
        max_tdoc_size_bytes: int | None = None,
        on_progress: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        runner: TDocPipeline | None = None,
    ) -> DirectParseBatchResult:
        """Discover a folder synchronously, then run its files through the pipeline."""
        try:
            url = validate_tdoc_ftp_url(url)
        except ValueError as exc:
            raise ValueError(f"URL is not a 3GPP FTP URL: {url}") from exc

        # Discovery intentionally stays outside the runner: the BFS listing
        # order is part of the direct-folder contract and must not depend on
        # preparation completion order.
        file_urls = self.collect_3gpp_file_urls(url, max_depth=max_depth)
        items = [PipelineItem(key=file_url, value=file_url) for file_url in file_urls]

        owns_runner = runner is None
        runtime: PipelineRuntime | None = None
        if runner is None:
            runtime = PipelineRuntime(prepare_workers=1)
            runner = TDocPipeline(runtime)

        def report(progress: PipelineProgress) -> None:
            if on_progress is not None:
                on_progress(
                    f"parsed {progress.completed}/{progress.total} files"
                )

        try:
            outcomes = runner.run_sync(
                items,
                lambda file_url: self.prepare_from_url(
                    file_url,
                    force=force,
                    max_tdoc_size_bytes=max_tdoc_size_bytes,
                ),
                persist=cast(
                    Callable[[object], object],
                    self.persist_prepared_url,
                ),
                is_complete=lambda value: isinstance(value, DirectParseResult),
                is_cancelled=is_cancelled,
                on_progress=report,
            )
        finally:
            if owns_runner and runtime is not None:
                runtime.shutdown()

        results: list[DirectParseResult] = []
        failures: dict[str, str] = {}
        skipped: dict[str, str] = {}
        for outcome in outcomes:
            reason = outcome.reason or ""
            if outcome.status == "success":
                results.append(cast(DirectParseResult, outcome.value))
            elif outcome.status == "skipped":
                logger.info("Skipping %s: %s", outcome.key, reason)
                skipped[outcome.key] = reason
            else:
                logger.warning("Failed to parse %s: %s", outcome.key, reason)
                failures[outcome.key] = reason

        return DirectParseBatchResult(
            results=results,
            failures=failures,
            skipped=skipped,
        )

    def _collect_3gpp_file_urls(
        self,
        root_url: str,
        *,
        max_depth: int,
    ) -> list[str]:
        """BFS over ``root_url`` and return matching file URLs in visit order."""
        visited: set[str] = set()
        file_urls: list[str] = []
        queue: list[tuple[str, int]] = [(root_url, 0)]

        while queue:
            folder_url, depth = queue.pop(0)
            if folder_url in visited:
                continue
            visited.add(folder_url)

            try:
                listing = list_3gpp_directory(folder_url, client=self._scraper)
            except NotAFolderError:
                if depth == 0:
                    raise
                logger.warning("Skipping non-folder URL %s", folder_url)
                continue
            except Exception as exc:
                logger.warning(
                    "Failed to list %s: %s", folder_url, exc, exc_info=True,
                )
                continue

            file_urls.extend(listing.file_urls)

            if depth < max_depth:
                for subfolder_url in listing.subfolder_urls:
                    if subfolder_url not in visited:
                        queue.append((subfolder_url, depth + 1))

        return file_urls

    collect_3gpp_file_urls = _collect_3gpp_file_urls

    def prepare_from_bytes(
        self,
        docx_bytes: bytes,
        filename: str,
        *,
        max_tdoc_size_bytes: int | None = None,
    ) -> DirectParseResult:
        """Convert local DOCX/ZIP bytes in memory without cache or SQL writes."""
        max_bytes = (
            self._max_tdoc_size_bytes
            if max_tdoc_size_bytes is None
            else max_tdoc_size_bytes
        )
        tdoc_id = extract_tdoc_id_from_filename(filename)
        source = _DirectSource(
            payload=docx_bytes,
            filename=filename,
            kind="local",
            source_url=None,
            identity=f"local:{tdoc_id}:{filename}",
            tdoc_id=tdoc_id,
        )
        return self._direct_in_memory(source, max_bytes=max_bytes)

    def extract_from_bytes(
        self,
        docx_bytes: bytes,
        filename: str,
        *,
        force: bool = False,
        max_tdoc_size_bytes: int | None = None,
        runner: TDocPipeline | None = None,
    ) -> DirectParseResult:
        """Parse local bytes through the shared one-item preparation lane."""
        del force
        max_bytes = (
            max_tdoc_size_bytes
            if max_tdoc_size_bytes is not None
            else self._max_tdoc_size_bytes
        )
        owns_runner = runner is None
        runtime: PipelineRuntime | None = None
        if runner is None:
            runtime = PipelineRuntime(prepare_workers=1)
            runner = TDocPipeline(runtime)

        def prepare_one(payload: bytes) -> DirectParseResult:
            try:
                return self.prepare_from_bytes(
                    payload,
                    filename,
                    max_tdoc_size_bytes=max_bytes,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _DirectPipelineError(exc) from exc

        try:
            try:
                outcomes = runner.run_sync(
                    [PipelineItem(key=filename, value=docx_bytes)],
                    prepare_one,
                )
            except _DirectPipelineError as exc:
                raise exc.cause
        finally:
            if owns_runner and runtime is not None:
                runtime.shutdown()

        outcome = outcomes[0]
        if outcome.status != "success":
            raise RuntimeError(outcome.reason or "direct TDoc parse failed")
        return cast(DirectParseResult, outcome.value)

    def _direct_tdoc_type(self, tdoc_id: str | None) -> str | None:
        if tdoc_id is None:
            return None
        tdoc = self._tdoc_repo.get_by_id(tdoc_id)
        return None if tdoc is None else tdoc.type

    def _convert_direct(
        self,
        payload: bytes,
        *,
        filename: str,
        tdoc_id: str,
        identity: str,
    ) -> TDocZipConversion:
        return convert_direct_payload(
            payload,
            filename=filename,
            tdoc_id=tdoc_id,
            identity=identity,
            max_documents=self._max_documents_per_zip,
            max_uncompressed_bytes=self._max_uncompressed_size_bytes,
            docx_pool=self._docx_pool,
        )

    def _decide_direct(
        self,
        source: _DirectSource,
        *,
        tdoc_type: str | None,
    ) -> DirectTypeDecision:
        """Route a source by its TDoc type; sniff only the main when the type is unknown.

        With no name-matched main, only the structural CR check can identify the
        document, so the decision falls back to CR.
        """
        tdoc_id = source.tdoc_id
        if tdoc_id is None:
            return DirectTypeDecision("cr", _CR_STORE)
        try:
            return decide_direct_type(
                tdoc_id,
                tdoc_type=tdoc_type,
                main_markdown=lambda: convert_direct_main_markdown(
                    source.payload,
                    filename=source.filename,
                    tdoc_id=tdoc_id,
                    max_documents=self._max_documents_per_zip,
                    max_uncompressed_bytes=self._max_uncompressed_size_bytes,
                    docx_pool=self._docx_pool,
                ),
            )
        except TDocMainDocumentError:
            return DirectTypeDecision("cr", _CR_STORE)

    def _parse_cr_bundle(
        self,
        payload: bytes,
        *,
        filename: str,
        max_bytes: int,
        tdoc_id: str | None,
        sniffed: bool,
    ) -> TDocParseBundle:
        """Run the CR parse; a sniffed source with no CR cover is skipped, not failed."""
        try:
            return self._parse_bundle(
                payload,
                filename=filename,
                max_bytes=max_bytes,
                tdoc_id=tdoc_id,
            )
        except CRHeaderMissingError as exc:
            if sniffed:
                raise TDocTypeSkippedError(tdoc_id, TYPE_UNDETERMINED) from exc
            raise

    def _direct_in_memory(
        self,
        source: _DirectSource,
        *,
        max_bytes: int,
    ) -> DirectParseResult:
        """Route an in-memory source to the family or CR parse with no SQL writes."""
        _check_direct_size(source.payload, source.filename, max_bytes)
        tdoc_type = self._direct_tdoc_type(source.tdoc_id)
        decision = self._decide_direct(source, tdoc_type=tdoc_type)
        if decision.kind == "skipped":
            raise TDocTypeSkippedError(source.tdoc_id, decision.reason or TYPE_UNDETERMINED)
        if decision.kind == "family":
            assert decision.store is not None and source.tdoc_id is not None
            conversion = self._convert_direct(
                source.payload,
                filename=source.filename,
                tdoc_id=source.tdoc_id,
                identity=source.identity,
            )
            return _family_direct_result(
                source_kind=source.kind,
                source_url=source.source_url,
                tdoc_id=source.tdoc_id,
                tdoc_id_in_tdocs=False,
                store=decision.store,
                documents=conversion.documents,
                main_source_file=conversion.main_source_file,
                extract_meta=None,
                skipped_sources=conversion.skipped_sources,
                from_cache=False,
                persisted=False,
            )
        bundle = self._parse_cr_bundle(
            source.payload,
            filename=source.filename,
            max_bytes=max_bytes,
            tdoc_id=source.tdoc_id,
            sniffed=tdoc_type is None,
        )
        cover = bundle.main_parse_result.cover
        tdoc_id = source.tdoc_id
        if tdoc_id is None and source.kind != "url-3gpp":
            tdoc_id = extract_tdoc_id_from_filename(cover.extracted_tdoc_id or "") or cover.tdoc_id
            cover = replace(cover, tdoc_id=tdoc_id)
        return DirectParseResult(
            source_kind=source.kind,
            documents=bundle.documents,
            main_source_file=bundle.main_source_file,
            details=cover,
            extract_meta=None,
            from_cache=False,
            persisted=False,
            tdoc_id=tdoc_id,
            tdoc_id_in_tdocs=False,
            source_url=source.source_url,
        )

    def _fetch_direct_zip(self, url: str, *, cache_file: str, max_bytes: int) -> bytes:
        payload = self._scraper.get_bytes(url)
        self._cache.put_bytes(cache_file, payload, "zips")
        if max_bytes > 0 and len(payload) > max_bytes:
            raise TDocTooLargeError(
                source=f"download:{url}",
                size=len(payload),
                limit=max_bytes,
            )
        return payload

    def _family_prepared(
        self,
        *,
        url: str,
        cache_file: str,
        stored_ftp_url: str,
        tdoc_id: str,
        store: TDocContentStore,
        conversion: TDocZipConversion,
    ) -> PreparedDirectFamily:
        prepared = self._family_service.prepare_conversion(
            tdoc_id=tdoc_id,
            store=store,
            ftp_url=stored_ftp_url,
            cache_file=cache_file,
            conversion=conversion,
        )
        return PreparedDirectFamily(url=url, prepared=prepared)

    def _cached_family_result(
        self,
        *,
        url: str,
        stored_ftp_url: str,
        tdoc_id: str,
        store: TDocContentStore,
    ) -> DirectParseResult | None:
        cached = self._family_service.cached_extract(tdoc_id, store, stored_ftp_url)
        if cached is None or cached.extract_meta is None:
            return None
        return _family_direct_result(
            source_kind="url-3gpp",
            source_url=url,
            tdoc_id=tdoc_id,
            tdoc_id_in_tdocs=True,
            store=store,
            documents=cached.documents,
            main_source_file=cached.extract_meta.doc_filename,
            extract_meta=cached.extract_meta,
            skipped_sources=cached.skipped_sources,
            from_cache=True,
            persisted=False,
        )

    def _cached_cr_result(
        self,
        *,
        url: str,
        stored_ftp_url: str,
        tdoc_id: str,
    ) -> DirectParseResult | None:
        cached = self._cached_cr_parts(stored_ftp_url, tdoc_id)
        if cached is None:
            return None
        cached_details, cached_meta, cached_annexes = cached
        cached_main = self._cached_main_document(cached_meta)
        if cached_main is None:
            return None
        logger.debug("Complete SQL extraction cache hit for direct URL %s", url)
        return DirectParseResult(
            source_kind="url-3gpp",
            documents=(cached_main, *cached_annexes),
            main_source_file=cached_meta.doc_filename,
            details=cached_details,
            extract_meta=cached_meta,
            from_cache=True,
            persisted=False,
            tdoc_id=tdoc_id,
            tdoc_id_in_tdocs=True,
            source_url=url,
        )

    def _cached_unknown_type_result(
        self,
        *,
        url: str,
        stored_ftp_url: str,
        tdoc_id: str,
    ) -> DirectParseResult | None:
        """Return a complete stored bundle of any type, so a hit never downloads."""
        for store in FAMILY_STORES:
            cached = self._cached_family_result(
                url=url, stored_ftp_url=stored_ftp_url, tdoc_id=tdoc_id, store=store,
            )
            if cached is not None:
                return cached
        return self._cached_cr_result(url=url, stored_ftp_url=stored_ftp_url, tdoc_id=tdoc_id)

    def _prepare_family_from_url(
        self,
        *,
        url: str,
        cache_file: str,
        stored_ftp_url: str,
        tdoc_id: str,
        store: TDocContentStore,
        force: bool,
        max_bytes: int,
    ) -> PreparedDirectFamily | DirectParseResult:
        if not force:
            cached = self._cached_family_result(
                url=url, stored_ftp_url=stored_ftp_url, tdoc_id=tdoc_id, store=store,
            )
            if cached is not None:
                return cached
        payload = self._fetch_direct_zip(url, cache_file=cache_file, max_bytes=max_bytes)
        conversion = self._convert_direct(
            payload,
            filename=url,
            tdoc_id=tdoc_id,
            identity=stored_ftp_url,
        )
        return self._family_prepared(
            url=url,
            cache_file=cache_file,
            stored_ftp_url=stored_ftp_url,
            tdoc_id=tdoc_id,
            store=store,
            conversion=conversion,
        )

    def _prepare_cr_from_url(
        self,
        *,
        url: str,
        cache_file: str,
        stored_ftp_url: str,
        tdoc_id: str,
        max_bytes: int,
        force: bool,
        payload: bytes | None,
        sniffed: bool,
    ) -> PreparedDirectTDoc | DirectParseResult:
        """Prepare a URL-keyed CR bundle; ``payload`` is set once the type was sniffed."""
        if payload is None:
            if not force:
                cached = self._cached_cr_result(
                    url=url, stored_ftp_url=stored_ftp_url, tdoc_id=tdoc_id,
                )
                if cached is not None:
                    return cached
            payload = self._fetch_direct_zip(url, cache_file=cache_file, max_bytes=max_bytes)

        bundle = self._parse_cr_bundle(
            payload,
            filename=url,
            max_bytes=max_bytes,
            tdoc_id=tdoc_id,
            sniffed=sniffed,
        )
        documents = self._bind_documents_to_ftp_url(
            bundle.documents, stored_ftp_url
        )
        parsed = bundle.main_parse_result
        cover = replace(
            parsed.cover,
            tdoc_id=tdoc_id,
            ftp_url=stored_ftp_url,
        )
        ttcn = (
            replace(parsed.ttcn, ftp_url=stored_ftp_url, tdoc_id=tdoc_id)
            if parsed.ttcn is not None
            else None
        )
        changes = (
            replace(parsed.changes, ftp_url=stored_ftp_url, tdoc_id=tdoc_id)
            if parsed.changes is not None
            else None
        )
        meta = TDocExtractMeta(
            ftp_url=stored_ftp_url,
            tdoc_id=tdoc_id,
            cache_file=cache_file,
            doc_filename=bundle.main_source_file,
            extraction_version=2,
            document_count=len(_annexes_of(documents, bundle.main_source_file)),
            skipped_sources=bundle.skipped_sources,
        )
        self._write_markdown_archive(
            cache_file=cache_file,
            ftp_url=stored_ftp_url,
            main_source_file=bundle.main_source_file,
            documents=documents,
        )
        return PreparedDirectTDoc(
            url=url,
            documents=documents,
            main_source_file=bundle.main_source_file,
            details=cover,
            ttcn=ttcn,
            changes=changes,
            extract_meta=meta,
            tdoc_id=tdoc_id,
            cache_file=cache_file,
        )

    def _prepare_from_3gpp_url(
        self,
        *,
        url: str,
        cache_file: str,
        stored_ftp_url: str,
        tdoc: TDoc,
        force: bool,
        max_bytes: int,
    ) -> PreparedDirectTDoc | PreparedDirectFamily | DirectParseResult:
        """Prepare a URL-keyed bundle for a TDoc whose ``tdocs`` row exists."""
        tdoc_id = tdoc.tdoc_id
        if tdoc.type is not None and tdoc.type.strip():
            decision = decide_known_type(tdoc_id, tdoc.type)
            if decision.kind == "skipped":
                raise TDocTypeSkippedError(tdoc_id, decision.reason or TYPE_UNDETERMINED)
            if decision.kind == "family":
                assert decision.store is not None
                return self._prepare_family_from_url(
                    url=url,
                    cache_file=cache_file,
                    stored_ftp_url=stored_ftp_url,
                    tdoc_id=tdoc_id,
                    store=decision.store,
                    force=force,
                    max_bytes=max_bytes,
                )
            return self._prepare_cr_from_url(
                url=url,
                cache_file=cache_file,
                stored_ftp_url=stored_ftp_url,
                tdoc_id=tdoc_id,
                max_bytes=max_bytes,
                force=force,
                payload=None,
                sniffed=False,
            )

        if not force:
            cached = self._cached_unknown_type_result(
                url=url, stored_ftp_url=stored_ftp_url, tdoc_id=tdoc_id,
            )
            if cached is not None:
                return cached
        payload = self._fetch_direct_zip(url, cache_file=cache_file, max_bytes=max_bytes)
        source = _DirectSource(
            payload=payload,
            filename=url,
            kind="url-3gpp",
            source_url=url,
            identity=stored_ftp_url,
            tdoc_id=tdoc_id,
        )
        decision = self._decide_direct(source, tdoc_type=None)
        if decision.kind == "skipped":
            raise TDocTypeSkippedError(tdoc_id, decision.reason or TYPE_UNDETERMINED)
        if decision.kind == "family":
            assert decision.store is not None
            conversion = self._convert_direct(
                payload,
                filename=url,
                tdoc_id=tdoc_id,
                identity=stored_ftp_url,
            )
            return self._family_prepared(
                url=url,
                cache_file=cache_file,
                stored_ftp_url=stored_ftp_url,
                tdoc_id=tdoc_id,
                store=decision.store,
                conversion=conversion,
            )
        return self._prepare_cr_from_url(
            url=url,
            cache_file=cache_file,
            stored_ftp_url=stored_ftp_url,
            tdoc_id=tdoc_id,
            max_bytes=max_bytes,
            force=force,
            payload=payload,
            sniffed=True,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _validate_tdoc_id(self, tdoc_id: str) -> str:
        r"""Normalise and shape-validate ``tdoc_id``.

        Returns the stripped input; raises :class:`ValueError` for an
        empty value or one that does not match the conservative
        ``[A-Za-z0-9-]{1,32}`` regex. The regex is deliberately
        broader than the CR parser's stricter
        ``[RSC][1-9P][-sw]\d{6}`` because the ``tdocs`` table may also
        hold LS / DRAFT / other non-CR rows whose ids use a different
        shape — the type check downstream is what makes those a
        hard error rather than this regex.
        """
        stripped = tdoc_id.strip()
        if not stripped:
            raise ValueError("tdoc_id is required and cannot be empty")
        if not _TDOC_ID_RE.fullmatch(stripped):
            raise ValueError(
                f"Invalid tdoc_id shape: {tdoc_id!r} "
                f"(must match {_TDOC_ID_RE.pattern})"
            )
        return stripped

    def _load_tdoc(self, tdoc_id: str) -> TDoc:
        """Resolve ``tdoc_id`` against the ``tdocs`` table.

        Raises:
            TDocNotFoundError: The row is absent.
            TDocTypeUnsupportedError: The row's ``type`` has no content store.
        """
        tdoc = self._tdoc_repo.get_by_id(tdoc_id)
        if tdoc is None:
            raise TDocNotFoundError(tdoc_id)
        if not is_eligible_type(tdoc.type):
            raise TDocTypeUnsupportedError(tdoc_id, tdoc.type)
        return tdoc

# Re-export the parser-side exception so callers can catch a single
# type if they want to treat "not a CR markdown" as a soft failure in
# batch flows. Not part of the Plan but convenient.
