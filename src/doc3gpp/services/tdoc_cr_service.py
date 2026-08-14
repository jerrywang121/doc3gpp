"""End-to-end TDoc CR (Change Request) extraction pipeline.

Glues the per-stage building blocks together:

1. Validate the requested ``tdoc_id``.
2. Confirm the TDoc exists in the ``tdocs`` table and is of type
   ``"CR"`` (the ``tdocs.ftp_url`` row is the source of truth — the TDoc
   list XLSX classifies each id by document type).
3. Download (or cache-hit) the TDoc zip from the 3GPP FTP via
   :mod:`doc3gpp.scraping.tdoc_zip_source`.
4. Extract the ``.docx`` body from the zip via
   :func:`doc3gpp.parsers.cr_parser.extract_docx_from_zip`.
5. Convert the docx to markdown via the python-docx-based converter
   in :mod:`doc3gpp.parsers.docx_converter`. The markdown cache shares
   the zip cache's ``cache_file`` basename (URL-derived via
   :func:`doc3gpp.scraping.cache_keys.derive_cache_file`) so a fresh
   zip download lands on the same cache row as the prior run.
6. Parse the markdown into a :class:`TDocCRDetails` value object via
   :func:`doc3gpp.parsers.cr_parser.parse_cr_details`.
7. Persist both the details row and the cache-extract metadata sidecar
   in a single transaction via
   :class:`doc3gpp.storage.repositories.tdoc_cr_sql.SQLAlchemyTDocCrRepository`.

The service owns three caching layers:

* **Zip cache** (``<root>/zips/<cache_file>``) — keyed by the
  URL-derived ``cache_file`` basename (via
  :func:`doc3gpp.scraping.cache_keys.derive_cache_file`); bypassed on
  network miss by
  :func:`doc3gpp.scraping.tdoc_zip_source.download_tdoc_zip`.
* **Markdown cache** (``<root>/markdown/<cache_file>``) — shares the
  zip cache's ``cache_file`` key; on-disk bytes are a real ``ZIP``
  archive (single entry named ``<docx stem>.md``), so operators can
  open / extract the cached markdown with standard archival tooling
  (``unzip`` / 7z / WinZip). Legacy plain-UTF-8 and legacy gzip blobs
  are still decoded transparently via magic-byte sniffing. Skipped when
  the zip cache is already populated.
* **Database cache** — the ``tdoc_cr_cover_page`` / ``tdoc_extracts``
  rows; a hit short-circuits the entire pipeline and returns the
  persisted ``TDocCRDetails`` with ``from_cache=True``.

The ``from_cache`` flag on :class:`ExtractResult` refers to the DB
cache — it does NOT fire when only the markdown cache is hot. A hot
markdown cache still means we re-parsed and re-validated the document
against the latest parser regexes.

The parsed :class:`TDocCRParseResult` is fanned out across three
independent writes: the slim ``tdoc_cr_cover_page`` cover row, an
optional ``tdoc_cr_ttcn_details`` sidecar (only present for TTCN
CRs), and the ``tdoc_extracts`` cache-metadata row. Each write goes
through its own repository (cover via the slim CR repo, TTCN
sidecar via the dedicated TTCN repo, extract metadata via
``upsert_extract_meta``) so the storage surface mirrors the
slimmed cover dataclass and the new sidecar table without leaking
either parser payload back into the in-memory ``ExtractResult``.
"""

from __future__ import annotations

import gzip
import io
import logging
import re
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_cr import (
    DirectParseBatchResult,
    DirectParseResult,
    TDocCRDetails,
    TDocCRParseResult,
    TDocCRTTCNDetails,
    TDocExtractMeta,
)
from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
from doc3gpp.parsers.cr_parser import (
    CRHeaderMissingError,
    extract_docx_from_zip,
)
from doc3gpp.parsers.direct_extractor import (
    NotAFolderError,
    direct_parse_bytes,
    extract_tdoc_id_from_filename,
    is_3gpp_ftp_url,
    list_3gpp_directory,
)
from doc3gpp.parsers.normalizers import build_ftp_url, normalize_ftp_path
from doc3gpp.parsers.tdoc_parsers import (
    TDocParser,
    TDocParserRegistry,
    build_default_registry,
)
from doc3gpp.repository.protocols import (
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

# Re-exported so callers can catch ``TDocTooLargeError`` from a single
# import path (the service layer) without depending on the scraping
# module directly. The canonical home of the class is
# :mod:`doc3gpp.scraping.tdoc_zip_source` to avoid a service ↔ scraping
# circular import (the service already imports from scraping).
__all__ = [
    "BatchExtractResult",
    "CRHeaderMissingError",
    "ExtractResult",
    "TDocCrService",
    "TDocNotFoundError",
    "TDocNotYetOnFTPError",
    "TDocTooLargeError",
    "TDocTypeUnsupportedError",
    "TDocZipDownloadError",
]

if TYPE_CHECKING:
    from doc3gpp.scraping.client import ScraperClient
    from doc3gpp.services.search_service import SearchService
    from doc3gpp.services.semantic_search_service import SemanticSearchService

logger = logging.getLogger(__name__)


# Public type alias for the type-check precondition. ``"CR"`` is the
# only TDoc type whose zip layout we currently parse; the value is
# normalised to upper-case so the comparison stays case-insensitive.
_CR_TDOC_TYPE = "CR"

# Conservative shape guard for ``tdoc_id``. The CR/zip parser already
# enforces a stricter regex internally; this is a *fast-fail* so a
# garbage id never touches the cache root, the network, or the DB.
_TDOC_ID_RE = re.compile(r"[A-Za-z0-9-]{1,32}")


_GZIP_MAGIC = b"\x1f\x8b"
_ZIP_MAGIC = b"PK\x03\x04"


def _wrap_markdown_zip(text: str, *, inner_name: str) -> bytes:
    """Wrap ``text`` in a real ``zipfile.ZipFile`` archive for on-disk storage.

    The ``.zip`` extension on the cache key matches the on-disk format, so
    operators can ``unzip`` / 7z / WinZip-open the cached markdown straight
    from disk — and ``7z -t zip cache/markdown/...zip`` lists the inner
    ``<inner_name>`` entry without any custom decoder.

    The single entry is named ``inner_name`` (a ``.md`` basename derived
    from the source docx filename) so an extracted copy from the archive
    has a recognisable filename.

    Args:
        text: UTF-8 markdown text to wrap.
        inner_name: Entry name inside the archive (e.g.
            ``"R5s260009.md"``).

    Returns:
        The ZIP archive bytes ready to be handed to ``cache.put_bytes``.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(inner_name, text.encode("utf-8"))
    return buf.getvalue()


def _decompress_markdown(raw: bytes) -> str:
    """Decode cached markdown bytes written by :func:`_wrap_markdown_zip`.

    Three layouts are accepted on read:

    1. **Real ZIP archive** (``PK\\x03\\x04`` magic) — the post-this-change
       format. The first entry whose payload is valid UTF-8 wins; this
       matches the legacy entry shape (``<docx stem>.md``) the writer
       produces.
    2. **Legacy gzip blob** (``\\x1f\\x8b`` magic) — the prior
       pre-real-zip cache format. Decompressed via :mod:`gzip`.
    3. **Legacy plain UTF-8** — the pre-gzip cache format (no compression
       at all). Decoded directly.

    Magic-byte sniffing keeps read tolerant: a cache populated by either
    legacy write path stays readable while new writes adopt the real-ZIP
    layout.

    Raises:
        OSError: zip / gzip decompression failed (corrupt cache file).
        UnicodeDecodeError: payload bytes are not valid UTF-8.
    """
    if not raw:
        return ""
    if raw[:4] == _ZIP_MAGIC:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in zf.namelist():
                try:
                    return zf.read(name).decode("utf-8")
                except UnicodeDecodeError:
                    continue
            return ""
    if raw[:2] == _GZIP_MAGIC:
        return gzip.decompress(raw).decode("utf-8")
    return raw.decode("utf-8")


def _read_cached_markdown_path(cache_file: str, cache_root: Path) -> str:
    """Load markdown from ``cache_root/markdown/<cache_file>``.

    Returns empty string on any read/decode error so callers degrade
    safely. The cache_file is trusted to be a previously persisted
    basename from a :class:`TDocExtractMeta` row; the on-disk path is
    reconstructed as ``cache_root / "markdown" / cache_file``. If the
    file has been purged, is corrupt, or fails to decode, the failure
    is logged at WARNING and an empty string is returned so callers
    (including ``--format raw``) degrade safely while still benefiting
    from the DB cache hit for other formats.
    """
    path = cache_root / "markdown" / cache_file
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        logger.warning("Failed to read cached markdown at %s: %s", path, exc)
        return ""
    try:
        return _decompress_markdown(raw)
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Failed to decode cached markdown at %s: %s", path, exc)
        return ""


# ---------------------------------------------------------------------------
# Public exceptions raised by the service.
# ---------------------------------------------------------------------------


class TDocTypeUnsupportedError(ValueError):
    """The given tdoc_id is known but its type is not ``"CR"``.

    Inherits from :class:`ValueError` so callers that catch the broad
    class still handle it correctly. The CLI translates this into a
    friendly ``typer.BadParameter`` with the observed ``type`` value
    surfaced for the operator.
    """

    def __init__(self, tdoc_id: str, observed_type: str | None) -> None:
        self.tdoc_id = tdoc_id
        self.observed_type = observed_type
        super().__init__(
            f"TDoc {tdoc_id!r} has type {observed_type!r}; "
            "only CR-type TDocs are extractable"
        )


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


class TDocNotYetOnFTPError(ValueError):
    """The row is stored but its ``ftp_url`` is still NULL.

    Surfaced by :meth:`TDocCrService.extract` when
    :attr:`doc3gpp.models.tdoc.TDoc.ftp_url` is ``None`` — meaning
    the 3GPP FTP pipeline has not propagated the upload yet, so there
    is nothing to download. Distinct from :class:`TDocNotFoundError`
    (the row exists) and :class:`TDocZipDownloadError` (we tried but
    failed); the upstream pipeline state is the actionable cause.
    """

    def __init__(self, tdoc_id: str) -> None:
        self.tdoc_id = tdoc_id
        super().__init__(
            f"TDoc {tdoc_id!r} has no ftp_url yet — the 3GPP upload "
            "pipeline has not propagated a final URL; try again later "
            "or run `doc3gpp tdoc sync` to refresh the tdocs table"
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
            side) and the CLI summary table surfaces it under its
            own "Skipped (not yet on FTP)" line.
    """

    successes: dict[str, ExtractResult]
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
        scraper_client: "ScraperClient",
        cr_repository: TDocCrDetailRepository,
        cr_ttcn_repository: TDocCrTTCNDetailRepository,
        cr_change_details_repository: TDocCrChangeDetailsRepository,
        tdoc_repository: TDocRepository,
        parser: TDocParser | None = None,
        parser_registry: TDocParserRegistry | None = None,
        max_tdoc_size_bytes: int = 0,
        search_service: "SearchService | None" = None,
        semantic_service: "SemanticSearchService | None" = None,
    ) -> None:
        self._cache = cache
        self._scraper = scraper_client
        self._repo = cr_repository
        self._cr_ttcn_repo = cr_ttcn_repository
        self._change_details_repo = cr_change_details_repository
        self._tdoc_repo = tdoc_repository
        self._parser = parser
        self._parser_registry = parser_registry
        self._max_tdoc_size_bytes = max_tdoc_size_bytes
        self._search_service = search_service
        self._semantic_service = semantic_service
        from doc3gpp.settings.loader import get_settings as _gs
        self._settings = _gs()

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
        except Exception as exc:
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(
        self,
        tdoc_id: str,
        *,
        force: bool = False,
        full: bool = False,
    ) -> ExtractResult:
        """Return parsed CR details + extract metadata for ``tdoc_id``.

        Sequence:

        1. Validate ``tdoc_id`` shape (``^[A-Za-z0-9-]{1,32}$``).
        2. Look up the TDoc in the ``tdocs`` table via
           :meth:`TDocRepository.get_by_id` — raise
           :class:`TDocNotFoundError` on miss and
           :class:`TDocTypeUnsupportedError` if ``type != "CR"``.
        3. Pre-resolve the candidate download URL(s) via
           :func:`resolve_download_url` and probe the DB cache by URL
           before any network I/O. A hit short-circuits with
           ``from_cache=True`` (and ``force=True`` skips this).
        4. :func:`download_tdoc_zip` checks the on-disk zip cache first
           via the ``ftp_url``-derived cache key (regardless of
           ``force``) and returns the cached path on a hit. On a miss,
           it downloads from the per-TDoc URL stored in
           ``tdocs.ftp_url`` (rebuilt to a full URL via
           :func:`build_ftp_url`), falling back to the template-based
           URL on failure.
        5. Once the actual serving URL is known, probe the DB cache
           again by that URL — covers the case where ``primary_url``
           was unset at step 3 but the template URL was usable.
        6. Otherwise: extract the docx → compute sha256 of the
           docx bytes → look up the markdown cache (skip on
           ``force``) → render markdown if needed → parse → persist
           under the resolved URL (normalised back to the relative
           ``ftp_url`` form via :func:`normalize_ftp_path`) → return
           ``from_cache=False``. When the zip was served from the
           local cache, ``downloaded.url`` is ``None`` (the URL that
           originally populated the cache is not tracked); fall back
           to the first candidate the resolver would currently try
           so persistence still has a non-empty identity URL.

        Args:
            tdoc_id: Canonical TDoc identifier.
            force: Bypass the DB short-circuit probe (``tdoc_cr_cover_page``
                / ``tdoc_extracts``) and the markdown cache on a fresh
                parse. The on-disk zip cache is **always** consulted
                first regardless of ``force`` — :func:`download_tdoc_zip`
                keys the cache on ``tdocs.ftp_url`` (via
                :func:`derive_cache_file`), and a hit returns the cached
                path without re-downloading, even when ``force=True``.
                The ``tdoc_cr_cover_page`` / ``tdoc_extracts`` rows are
                always re-upserted on the parse path that runs.
            full: Forwarded to the parser as ``full=True``. For TTCN
                CRs this enables extraction of the per-correction
                ``before_change`` / ``after_change`` / ``new_change``
                content alongside the metadata fields; without it, only
                the metadata (``function_name``, ``reason_for_change``,
                ``summary_of_change``, ``ttcn_module``, ``mcc160_comment``)
                is captured. Safe to flip on for non-TTCN CRs — the
                generic :class:`CRParser` ignores the flag.
        """
        normalised = self._validate_tdoc_id(tdoc_id)
        tdoc = self._load_tdoc(normalised)

        if not tdoc.ftp_url:
            # Stored row but the upload pipeline hasn't propagated a
            # final URL yet — there is nothing on the 3GPP FTP to
            # download. Surface this as a distinct exception so
            # ``extract_many`` can route it into the skip bucket
            # instead of treating it as a download failure.
            raise TDocNotYetOnFTPError(normalised)

        # Stored ``ftp_url`` is relative to the 3GPP FTP root; rebuild
        # the absolute URL the network layer expects.
        primary_url = build_ftp_url(tdoc.ftp_url)

        # Derive the cache key from ``tdoc.ftp_url`` so it matches
        # what :func:`download_tdoc_zip` writes to the zips cache
        # (keyed on the same relative url). Keeping the two in sync
        # is required for a cache hit to feed the same key into the
        # markdown cache.
        cache_file = derive_cache_file(tdoc.ftp_url)

        # Hoist the URL candidates so the persistence fallback below
        # (used when the zip cache hits and ``downloaded.url`` is
        # None) reuses the same ordering the cache probe iterates
        # over — a freshly-persisted row must be findable by the
        # next probe without re-resolving.
        candidates = resolve_download_url(normalised, primary_url)

        # Pre-download cache probe: known candidate URLs can be
        # resolved without touching the network, so the DB cache
        # short-circuits the zip fetch.
        if not force:
            for candidate in candidates:
                cached_details = self._repo.get_by_url(
                    normalize_ftp_path(candidate)
                )
                cached_meta = self._repo.get_extract_meta_by_url(
                    normalize_ftp_path(candidate)
                )
                if cached_details is not None and cached_meta is not None:
                    logger.debug(
                        "DB cache hit for TDoc %s at URL %s", normalised, candidate
                    )
                    return ExtractResult(
                        details=cached_details,
                        extract_meta=cached_meta,
                        from_cache=True,
                    )

        downloaded = download_tdoc_zip(
            normalised,
            self._scraper,
            self._cache,
            primary_url=primary_url,
            ftp_url=tdoc.ftp_url,
            max_bytes=self._max_tdoc_size_bytes,
        )

        # Post-download probe: ``tdoc.ftp_url`` was None so step 3 had
        # no candidates, but the download resolved a URL via the
        # template. The cache contract is per-URL, so we check again.
        resolved_url = downloaded.url
        if not force and resolved_url:
            cached_details = self._repo.get_by_url(
                normalize_ftp_path(resolved_url)
            )
            cached_meta = self._repo.get_extract_meta_by_url(
                normalize_ftp_path(resolved_url)
            )
            if cached_details is not None and cached_meta is not None:
                logger.debug(
                    "DB cache hit for TDoc %s at URL %s", normalised, resolved_url
                )
                return ExtractResult(
                    details=cached_details,
                    extract_meta=cached_meta,
                    from_cache=True,
                )

        doc_filename, docx_bytes = extract_docx_from_zip(downloaded.path.read_bytes())

        markdown = self._load_or_render_markdown(
            cache_file=cache_file,
            docx_bytes=docx_bytes,
            doc_filename=doc_filename,
            force=force,
        )

        # Persist under the relative ``ftp_url`` form so the database
        # stores the same shape as ``meetings.ftp_url``. The download
        # layer hands us an absolute URL on a fresh fetch; on a zip
        # cache hit it returns ``url=None`` (the URL that originally
        # populated the cache is unknown), so fall back to the first
        # candidate the resolver would try — the same URL the cache
        # probe above iterates over, which keeps the DB-cache contract
        # consistent for the next call. ``download_tdoc_zip`` raises
        # before returning when no candidate exists, so ``candidates``
        # is non-empty here.
        if resolved_url:
            stored_ftp_url = normalize_ftp_path(resolved_url)
        else:
            stored_ftp_url = normalize_ftp_path(candidates[0])

        parsed: TDocCRParseResult = self._resolve_parser(normalised).parse(
            markdown, tdoc_id=normalised, full=full,
        )
        cover = replace(parsed.cover, ftp_url=stored_ftp_url)
        ttcn: TDocCRTTCNDetails | None = (
            replace(
                parsed.ttcn,
                ftp_url=stored_ftp_url,
                tdoc_id=normalised,
            )
            if parsed.ttcn is not None
            else None
        )
        changes: TDocCRChangeDetails | None = (
            replace(
                parsed.changes,
                ftp_url=stored_ftp_url,
                tdoc_id=normalised,
            )
            if parsed.changes is not None
            else None
        )
        meta = TDocExtractMeta(
            ftp_url=stored_ftp_url or "",
            tdoc_id=normalised,
            cache_file=cache_file,
            doc_filename=doc_filename,
        )
        self._repo.upsert(cover)
        if ttcn is not None:
            self._cr_ttcn_repo.upsert(ttcn)
        if changes is not None:
            self._change_details_repo.upsert(changes)
        self._repo.upsert_extract_meta(meta)
        self._index_after_parse(normalised)
        self._embed_after_parse(normalised)
        logger.info(
            "Persisted CR details for TDoc %s at ftp_url %s (spec=%s cr_num=%s)",
            normalised,
            stored_ftp_url,
            cover.spec,
            cover.cr_num,
        )
        return ExtractResult(
            details=cover,
            extract_meta=meta,
            from_cache=False,
        )

    def extract_many(
        self,
        tdoc_ids: Iterable[str],
        *,
        force: bool = False,
        full: bool = False,
        on_progress: Callable[[str], None] | None = None,
    ) -> BatchExtractResult:
        """Extract a batch of TDocs and bundle successes with per-id failure reasons.

        Failures are logged at ``WARNING`` level (with the full
        traceback) and recorded in :attr:`BatchExtractResult.failures`
        keyed by the normalised tdoc_id, so a single broken id doesn't
        abort the rest of the batch. The CLI surfaces the short reason
        inline so the operator can tell which step failed without
        tailing the log file.

        Rows whose ``ftp_url`` is NULL (the 3GPP upload pipeline
        hasn't propagated a final URL yet) are routed to the skip
        bucket (:attr:`BatchExtractResult.skipped`) instead of
        :attr:`failures` — "FTP is lagging" is not an error from our
        side, and these IDs deserve a separate CLI summary line so
        the operator doesn't see them as failures.

        Args:
            tdoc_ids: Iterable of TDoc ids to extract. Strings that
                fail the shape guard, are missing from the ``tdocs``
                table, or aren't CR type are logged and skipped.
            force: Forwarded to :meth:`extract`. When ``True`` every
                TDoc is re-fetched and re-parsed from scratch.
            full: Forwarded to :meth:`extract` for every id in the
                batch. See :meth:`extract` for the TTCN
                ``before_change`` / ``after_change`` / ``new_change``
                semantics.

        Returns:
            A :class:`BatchExtractResult` whose ``successes`` dict maps
            the canonical tdoc_id to its :class:`ExtractResult`,
            whose ``failures`` dict maps the normalised tdoc_id to a
            short reason string (``"{ExceptionClassName}: {exc}"``),
            and whose ``skipped`` dict maps the normalised tdoc_id to
            a short reason string for ids whose ``ftp_url`` is NULL
            (the 3GPP upload pipeline hasn't propagated yet). The
            skip bucket is surfaced separately by the CLI so the
            operator can tell "FTP is lagging" apart from "this
            batch had real failures".
        """
        successes: dict[str, ExtractResult] = {}
        failures: dict[str, str] = {}
        skipped: dict[str, str] = {}
        tdoc_ids = list(tdoc_ids)
        total = len(tdoc_ids)
        for i, raw_id in enumerate(tdoc_ids, start=1):
            if on_progress is not None:
                on_progress(f"parsed {i}/{total} TDocs")
            try:
                result = self.extract(raw_id, force=force, full=full)
            except TDocNotYetOnFTPError as exc:
                logger.info("Skipping TDoc %r: %s", raw_id, exc)
                skipped[raw_id.strip()] = f"{type(exc).__name__}: {exc}"
                continue
            except TDocTooLargeError as exc:
                logger.info(
                    "Skipping TDoc %r: %s bytes exceeds max_tdoc_size_kb limit (%s bytes)",
                    raw_id, exc.size, exc.limit,
                )
                skipped[raw_id.strip()] = f"{type(exc).__name__}: {exc}"
                continue
            except (ValueError, LookupError, TDocZipDownloadError, TypeError) as exc:
                logger.warning(
                    "Failed to extract TDoc %r: %s",
                    raw_id,
                    exc,
                    exc_info=True,
                )
                failures[raw_id.strip()] = f"{type(exc).__name__}: {exc}"
                continue
            successes[result.details.tdoc_id] = result
        return BatchExtractResult(successes=successes, failures=failures, skipped=skipped)

    # ------------------------------------------------------------------
    # Direct-parse path: ``tdoc parse --from-path/--from-url``
    # ------------------------------------------------------------------

    def extract_from_url(
        self,
        url: str,
        *,
        force: bool = False,
        full: bool = False,
        max_tdoc_size_bytes: int | None = None,
    ) -> DirectParseResult:
        """Download ``url`` and return a :class:`DirectParseResult`.

        Branches on :func:`is_3gpp_ftp_url`:

        - **3GPP-URL path**: behaves like the regular
          :meth:`extract` happy path (cache + DB writes) but uses
          the URL-derived cache key (via
          :func:`derive_cache_file`) and a FK probe against
          ``tdocs`` so the FK on ``tdoc_extracts`` / ``tdoc_cr_cover_page``
          is never violated. The ``force`` flag is forwarded to
          :func:`download_tdoc_zip`; the per-TDoc id is auto-extracted
          from the URL's basename. The ``--format raw`` branch
          (downstream of this method) writes the ``tdoc_extracts``
          row but skips the ``tdoc_cr_cover_page`` row.
        - **Other URL path**: in-memory parse only; the cache and the
          database are never touched.

        Args:
            url: HTTP or HTTPS URL (other schemes raise ``ValueError``
                — operators should use ``--from-path`` for
                ``ftp://`` / ``file://`` sources).
            force: When ``True`` on the 3GPP-URL path, skip the DB
                short-circuit probe and re-render markdown (bypassing
                the markdown cache). The on-disk zip is **always**
                re-downloaded from ``url`` in the 3GPP-URL path
                regardless of ``force`` — the direct-parse helper
                does not consult the zip cache, it overwrites the
                ``zips/<cache_file>`` slot on every call so the
                caller always sees fresh bytes. The
                ``tdoc_cr_cover_page`` / ``tdoc_extracts`` rows are
                always re-upserted on a 3GPP-URL call.
            full: Forwarded to :func:`parse_cr_details` as
                ``full=True`` for the TTCN corrections sub-parser.

        Returns:
            A :class:`DirectParseResult` describing which cache +
            DB writes landed. Never raises for FK misses — the
            caller is expected to print the warning and emit the
            parsed record to stdout.
        """
        if not is_3gpp_ftp_url(url):
            payload = self._scraper.get_bytes(url)
            markdown, docx_filename, parsed = direct_parse_bytes(
                payload, filename=url, full=full,
                max_bytes=self._max_tdoc_size_bytes,
            )
            return DirectParseResult(
                source_kind="url-other",
                markdown=markdown,
                details=parsed.cover,
                extract_meta=None,
                from_cache=False,
                persisted=False,
                tdoc_id=parsed.cover.tdoc_id,
                tdoc_id_in_tdocs=False,
                source_url=url,
            )

        cache_file = derive_cache_file(url)
        stored_ftp_url = normalize_ftp_path(url)
        extracted_id = extract_tdoc_id_from_filename(url)

        if extracted_id is None:
            payload = self._scraper.get_bytes(url)
            markdown, docx_filename, parsed = direct_parse_bytes(
                payload, filename=url, full=full,
                max_bytes=self._max_tdoc_size_bytes,
            )
            logger.warning(
                "Direct-parse URL %s has no TDoc id pattern; "
                "skipping cache and DB writes",
                url,
            )
            return DirectParseResult(
                source_kind="url-3gpp",
                markdown=markdown,
                details=parsed.cover,
                extract_meta=None,
                from_cache=False,
                persisted=False,
                tdoc_id=None,
                tdoc_id_in_tdocs=False,
                source_url=url,
            )

        if not self._tdoc_in_tdocs(extracted_id):
            payload = self._scraper.get_bytes(url)
            markdown, docx_filename, parsed = direct_parse_bytes(
                payload, filename=url, full=full,
                max_bytes=self._max_tdoc_size_bytes,
            )
            logger.warning(
                "Direct-parse URL %s has tdoc_id %s missing from tdocs; "
                "skipping cache and DB writes",
                url,
                extracted_id,
            )
            return DirectParseResult(
                source_kind="url-3gpp",
                markdown=markdown,
                details=parsed.cover,
                extract_meta=None,
                from_cache=False,
                persisted=False,
                tdoc_id=extracted_id,
                tdoc_id_in_tdocs=False,
                source_url=url,
            )

        # 3GPP URL + tdoc_id ∈ tdocs: full happy path.
        return self._extract_from_3gpp_url(
            url=url,
            cache_file=cache_file,
            stored_ftp_url=stored_ftp_url,
            tdoc_id=extracted_id,
            force=force,
            full=full,
            max_bytes=(
                max_tdoc_size_bytes
                if max_tdoc_size_bytes is not None
                else self._max_tdoc_size_bytes
            ),
        )

    def extract_from_url_batch(
        self,
        url: str,
        *,
        max_depth: int = 2,
        force: bool = False,
        full: bool = False,
        max_tdoc_size_bytes: int | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> DirectParseBatchResult:
        """Batch-parse every matching ``.docx``/``.zip`` under a 3GPP FTP folder.

        The URL is probed once. If it points to a single file,
        :class:`NotAFolderError` is raised and the caller should fall back
        to :meth:`extract_from_url`. If it is a folder, the listing is
        scanned breadth-first up to ``max_depth`` levels and each matching
        file URL is handed to :meth:`extract_from_url`, preserving the
        existing DB/cache behavior for FK hits and the in-memory warning
        path for FK misses.

        Args:
            url: A URL that passes :func:`is_3gpp_ftp_url`.
            max_depth: Maximum folder levels to descend. ``0`` means the
                root folder only.
            force: Forwarded to :meth:`extract_from_url` for every file.
            full: Forwarded to :meth:`extract_from_url` for every file.

        Returns:
            A :class:`DirectParseBatchResult` with per-file results and a
            failure map keyed by file URL.

        Raises:
            ValueError: ``url`` is not a 3GPP FTP URL.
            NotAFolderError: ``url`` resolves to a single file rather than
                a folder listing.
        """
        if not is_3gpp_ftp_url(url):
            raise ValueError(f"URL is not a 3GPP FTP URL: {url}")

        file_urls = self.collect_3gpp_file_urls(url, max_depth=max_depth)

        results: list[DirectParseResult] = []
        failures: dict[str, str] = {}
        skipped: dict[str, str] = {}
        for i, file_url in enumerate(file_urls, start=1):
            if on_progress is not None:
                on_progress(f"parsed {i}/{len(file_urls)} files")
            try:
                result = self.extract_from_url(
                    file_url, force=force, full=full,
                    max_tdoc_size_bytes=max_tdoc_size_bytes,
                )
            except TDocTooLargeError as exc:
                logger.info(
                    "Skipping %s: %s bytes exceeds max_tdoc_size_kb limit (%s bytes)",
                    file_url, exc.size, exc.limit,
                )
                skipped[file_url] = f"{type(exc).__name__}: {exc}"
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to parse %s: %s", file_url, exc, exc_info=True,
                )
                failures[file_url] = f"{type(exc).__name__}: {exc}"
                continue
            results.append(result)

        return DirectParseBatchResult(
            results=results, failures=failures, skipped=skipped,
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
            except Exception as exc:  # noqa: BLE001
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

    def extract_from_bytes(
        self,
        docx_bytes: bytes,
        filename: str,
        *,
        force: bool = False,
        full: bool = True,
        max_tdoc_size_bytes: int | None = None,
    ) -> DirectParseResult:
        """Parse ``docx_bytes`` from a local source — never touches cache or DB.

        Local files have no FK to satisfy, so the result is always
        emitted (the caller writes the parsed record to stdout or
        ``--output``) and the on-disk cache + database stay
        untouched. The dataclass fields that describe cache hits or
        persistence are therefore always ``False`` / ``None`` for
        this code path.

        Args:
            docx_bytes: Raw bytes of the document. The helper
                :func:`direct_parse_bytes` dispatches on the leading
                ``b"PK"`` to handle both bare ``.docx`` and zip-
                wrapped ``.docx`` inputs.
            filename: Source filename; used to drive the docx
                extension guard and to auto-extract the TDoc id.
            force: Accepted for signature parity with
                :meth:`extract_from_url`; local files always bypass
                the cache so the flag is a no-op.
            full: Forwarded to :func:`parse_cr_details` (the
                default ``True`` mirrors the CLI default for direct
                mode and surfaces TTCN ``before_change`` /
                ``after_change`` content).

        Returns:
            A :class:`DirectParseResult` whose ``source_kind`` is
            ``"local"`` and whose persistence fields are all false.
        """
        del force
        markdown, docx_filename, parsed = direct_parse_bytes(
            docx_bytes, filename=filename, full=full,
            max_bytes=(
                max_tdoc_size_bytes
                if max_tdoc_size_bytes is not None
                else self._max_tdoc_size_bytes
            ),
        )
        tdoc_id = extract_tdoc_id_from_filename(filename)
        return DirectParseResult(
            source_kind="local",
            markdown=markdown,
            details=parsed.cover,
            extract_meta=None,
            from_cache=False,
            persisted=False,
            tdoc_id=tdoc_id,
            tdoc_id_in_tdocs=False,
        )

    def _tdoc_in_tdocs(self, tdoc_id: str) -> bool:
        """Return ``True`` when ``tdoc_id`` has a matching row in ``tdocs``.

        Cheap single-row probe against the TDoc repository; the FK on
        ``tdoc_extracts`` / ``tdoc_cr_cover_page`` is non-nullable, so
        the service layer must consult this before either
        :meth:`_repo.upsert` is called from the direct path.
        """
        return self._tdoc_repo.get_by_id(tdoc_id) is not None

    def _extract_from_3gpp_url(
        self,
        *,
        url: str,
        cache_file: str,
        stored_ftp_url: str,
        tdoc_id: str,
        force: bool,
        full: bool,
        max_bytes: int = 0,
    ) -> DirectParseResult:
        """Run the full extract pipeline for a 3GPP URL whose FK target exists.

        The 3GPP branch of :meth:`extract_from_url` delegates to this
        helper so the in-memory parse path stays readable. The helper
        reuses the **markdown** cache (via :meth:`_load_or_render_markdown`
        — ``cache_file`` keeps distinct revisions of the same
        ``tdoc_id`` in distinct slots, the D10 fix) but **always**
        re-downloads the zip from ``url`` via
        :meth:`ScraperClient.get_bytes` and overwrites
        ``zips/<cache_file>`` on every call. The DB short-circuit
        probe runs only when ``force=False``; the markdown cache is
        consulted regardless of ``force``. Writes both
        :class:`TDocExtractMeta` + :class:`TDocCRDetails` rows on a
        fresh extract.
        """
        if not force:
            cached_details = self._repo.get_by_url(stored_ftp_url)
            cached_meta = self._repo.get_extract_meta_by_url(stored_ftp_url)
            if cached_details is not None and cached_meta is not None:
                logger.debug(
                    "DB cache hit for direct-parse URL %s", url,
                )
                cached_markdown = _read_cached_markdown_path(
                    cached_meta.cache_file, self._cache.root,
                )
                return DirectParseResult(
                    source_kind="url-3gpp",
                    markdown=cached_markdown,
                    details=cached_details,
                    extract_meta=cached_meta,
                    from_cache=True,
                    persisted=False,
                    tdoc_id=tdoc_id,
                    tdoc_id_in_tdocs=True,
                    source_url=url,
                )

        zip_payload = self._scraper.get_bytes(url)
        # Post-fetch size guard: defence-in-depth — the direct-parse
        # path also writes its own zip slot, and the cache's
        # enforce_size_limit may evict it differently from the DB-mode
        # path. The size check is cheap and keeps the per-file budget
        # honest end-to-end.
        if max_bytes > 0 and len(zip_payload) > max_bytes:
            self._cache.put_bytes(cache_file, zip_payload, "zips")
            raise TDocTooLargeError(
                source=f"download:{url}",
                size=len(zip_payload),
                limit=max_bytes,
            )
        self._cache.put_bytes(cache_file, zip_payload, "zips")

        doc_filename, docx_bytes = extract_docx_from_zip(zip_payload)
        markdown = self._load_or_render_markdown(
            cache_file=cache_file,
            docx_bytes=docx_bytes,
            doc_filename=doc_filename,
            force=force,
        )
        parsed: TDocCRParseResult = self._resolve_parser(tdoc_id).parse(
            markdown, tdoc_id=tdoc_id, full=full,
        )
        cover = replace(parsed.cover, ftp_url=stored_ftp_url)
        ttcn: TDocCRTTCNDetails | None = (
            replace(
                parsed.ttcn,
                ftp_url=stored_ftp_url,
                tdoc_id=tdoc_id,
            )
            if parsed.ttcn is not None
            else None
        )
        changes: TDocCRChangeDetails | None = (
            replace(
                parsed.changes,
                ftp_url=stored_ftp_url,
                tdoc_id=tdoc_id,
            )
            if parsed.changes is not None
            else None
        )
        meta = TDocExtractMeta(
            ftp_url=stored_ftp_url,
            tdoc_id=tdoc_id,
            cache_file=cache_file,
            doc_filename=doc_filename,
        )
        self._repo.upsert(cover)
        if ttcn is not None:
            self._cr_ttcn_repo.upsert(ttcn)
        if changes is not None:
            self._change_details_repo.upsert(changes)
        self._repo.upsert_extract_meta(meta)
        self._index_after_parse(tdoc_id)
        self._embed_after_parse(tdoc_id)
        logger.info(
            "Persisted direct-parse CR details for tdoc_id %s at %s",
            tdoc_id,
            stored_ftp_url,
        )
        return DirectParseResult(
            source_kind="url-3gpp",
            markdown=markdown,
            details=cover,
            extract_meta=meta,
            from_cache=False,
            persisted=True,
            tdoc_id=tdoc_id,
            tdoc_id_in_tdocs=True,
            source_url=url,
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
        ``[RSC][1-9][-sw]\d{6}`` because the ``tdocs`` table may also
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
            TDocTypeUnsupportedError: The row's ``type`` is not ``"CR"``
                (case-insensitive).
        """
        tdoc = self._tdoc_repo.get_by_id(tdoc_id)
        if tdoc is None:
            raise TDocNotFoundError(tdoc_id)
        observed = (tdoc.type or "").strip().upper()
        if observed != _CR_TDOC_TYPE:
            raise TDocTypeUnsupportedError(tdoc_id, tdoc.type)
        return tdoc

    def _load_or_render_markdown(
        self,
        *,
        cache_file: str,
        docx_bytes: bytes,
        doc_filename: str,
        force: bool,
    ) -> str:
        """Return markdown for ``docx_bytes``, hitting the cache when possible.

        Cache key is the ``cache_file`` (URL-derived basename) — shared
        with the zip cache. On-disk bytes are a real ``zipfile.ZipFile``
        archive (single ``<docx stem>.md`` entry), so the ``.zip``
        extension on disk matches a format that ``unzip`` / 7z / WinZip
        understand. Legacy plain-UTF-8 and legacy gzip cache files are
        still decoded transparently via
        :func:`_decompress_markdown`'s magic-byte sniff.
        ``force=True`` bypasses the markdown cache only — the upstream
        zip is reused from :func:`download_tdoc_zip` on a cache hit
        (the zip cache is keyed on ``ftp_url`` and consulted first
        regardless of ``force``).
        """
        if not force:
            cached = self._cache.get_bytes(cache_file, "markdown")
            if cached is not None:
                logger.debug(
                    "Markdown cache hit for %s (cache_file=%s)",
                    doc_filename,
                    cache_file,
                )
                return _decompress_markdown(cached)

        from doc3gpp.parsers.docx_converter import (
            convert_document_to_markdown,
        )

        markdown = convert_document_to_markdown(docx_bytes, doc_filename)
        docx_stem = Path(doc_filename).stem or "markdown"
        inner_name = f"{docx_stem}.md"
        self._cache.put_bytes(
            cache_file,
            _wrap_markdown_zip(markdown, inner_name=inner_name),
            "markdown",
        )
        return markdown


# Re-export the parser-side exception so callers can catch a single
# type if they want to treat "not a CR markdown" as a soft failure in
# batch flows. Not part of the Plan but convenient. 