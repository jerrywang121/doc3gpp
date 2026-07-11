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
   in :mod:`doc3gpp.parsers.docx_converter`. Key the markdown cache by
   the sha256 of the **docx bytes** so a re-downloaded zip with a
   tweaked docx invalidates the rendered markdown cleanly.
6. Parse the markdown into a :class:`TDocCRDetails` value object via
   :func:`doc3gpp.parsers.cr_parser.parse_cr_details`.
7. Persist both the details row and the cache-extract metadata sidecar
   in a single transaction via
   :class:`doc3gpp.storage.repositories.tdoc_cr_sql.SQLAlchemyTDocCrRepository`.

The service owns three caching layers:

* **Zip cache** (``<root>/zips/<tdoc>.bin``) — keyed by the lower-cased
  TDoc id; bypassed on network miss by
  :func:`doc3gpp.scraping.tdoc_zip_source.download_tdoc_zip`.
* **Markdown cache** (``<root>/markdown/<sha256>.bin``) — keyed by the
  sha256 of the docx bytes; skipped when the zip cache is already
  populated (i.e. we have the docx on disk, so we can re-derive the
  cache key cheaply).
* **Database cache** — the ``tdoc_cr_details`` / ``tdoc_extracts``
  rows; a hit short-circuits the entire pipeline and returns the
  persisted ``TDocCRDetails`` with ``from_cache=True``.

The ``from_cache`` flag on :class:`ExtractResult` refers to the DB
cache — it does NOT fire when only the markdown cache is hot. A hot
markdown cache still means we re-parsed and re-validated the document
against the latest parser regexes.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_cr import DirectParseResult, TDocCRDetails, TDocExtractMeta
from doc3gpp.parsers.cr_parser import (
    CRHeaderMissingError,
    extract_docx_from_zip,
    parse_cr_details,
)
from doc3gpp.parsers.direct_extractor import (
    derive_zip_cache_key,
    direct_parse_bytes,
    extract_tdoc_id_from_filename,
    is_3gpp_ftp_url,
)
from doc3gpp.parsers.normalizers import build_ftp_url, normalize_ftp_path
from doc3gpp.repository.protocols import (
    TDocCrDetailRepository,
    TDocRepository,
)
from doc3gpp.scraping.tdoc_zip_source import (
    TDocCacheLike,
    TDocZipDownloadError,
    download_tdoc_zip,
    resolve_download_url,
)

if TYPE_CHECKING:
    from doc3gpp.scraping.client import ScraperClient

logger = logging.getLogger(__name__)


# Public type alias for the type-check precondition. ``"CR"`` is the
# only TDoc type whose zip layout we currently parse; the value is
# normalised to upper-case so the comparison stays case-insensitive.
_CR_TDOC_TYPE = "CR"

# Conservative shape guard for ``tdoc_id``. The CR/zip parser already
# enforces a stricter regex internally; this is a *fast-fail* so a
# garbage id never touches the cache root, the network, or the DB.
_TDOC_ID_RE = re.compile(r"[A-Za-z0-9-]{1,32}")


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
            (zip + markdown paths and the inner docx filename). On a
            DB-cache hit the paths reflect what the previous successful
            extract wrote.
        from_cache: ``True`` iff the result was returned from the
            ``tdoc_cr_details`` / ``tdoc_extracts`` rows **without**
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
    normalised tdoc_id, and the remaining ids continue.

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
    """

    successes: dict[str, ExtractResult]
    failures: dict[str, str]


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
        tdoc_repository: TDocRepository,
    ) -> None:
        self._cache = cache
        self._scraper = scraper_client
        self._repo = cr_repository
        self._tdoc_repo = tdoc_repository

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(self, tdoc_id: str, *, force: bool = False) -> ExtractResult:
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
        4. Download the zip (cache or network) — preferring the
           per-TDoc URL stored in ``tdocs.ftp_url`` (rebuilt to a full
           URL via :func:`build_ftp_url`) and falling back to the
           template-based URL on failure.
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
        """
        normalised = self._validate_tdoc_id(tdoc_id)
        tdoc = self._load_tdoc(normalised)

        # Stored ``ftp_url`` is relative to the 3GPP FTP root; rebuild
        # the absolute URL the network layer expects.
        primary_url = (
            build_ftp_url(tdoc.ftp_url) if tdoc.ftp_url else None
        )

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
        doc_hash = hashlib.sha256(docx_bytes).hexdigest()

        markdown = self._load_or_render_markdown(
            doc_hash=doc_hash,
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

        details = replace(
            parse_cr_details(markdown, tdoc_id=normalised),
            ftp_url=stored_ftp_url,
        )
        meta = TDocExtractMeta(
            ftp_url=stored_ftp_url or "",
            tdoc_id=normalised,
            zip_path=str(downloaded.path),
            markdown_path=str(self._cache.path_for(doc_hash, "markdown")),
            doc_filename=doc_filename,
        )
        self._repo.upsert(details, meta)
        logger.info(
            "Persisted CR details for TDoc %s at ftp_url %s (spec=%s cr_num=%s)",
            normalised,
            stored_ftp_url,
            details.spec,
            details.cr_num,
        )
        return ExtractResult(
            details=details,
            extract_meta=meta,
            from_cache=False,
        )

    def extract_many(
        self,
        tdoc_ids: Iterable[str],
        *,
        force: bool = False,
    ) -> BatchExtractResult:
        """Extract a batch of TDocs and bundle successes with per-id failure reasons.

        Failures are logged at ``WARNING`` level (with the full
        traceback) and recorded in :attr:`BatchExtractResult.failures`
        keyed by the normalised tdoc_id, so a single broken id doesn't
        abort the rest of the batch. The CLI surfaces the short reason
        inline so the operator can tell which step failed without
        tailing the log file.

        Args:
            tdoc_ids: Iterable of TDoc ids to extract. Strings that
                fail the shape guard, are missing from the ``tdocs``
                table, or aren't CR type are logged and skipped.
            force: Forwarded to :meth:`extract`. When ``True`` every
                TDoc is re-fetched and re-parsed from scratch.

        Returns:
            A :class:`BatchExtractResult` whose ``successes`` dict maps
            the canonical tdoc_id to its :class:`ExtractResult` and
            whose ``failures`` dict maps the normalised tdoc_id to a
            short reason string (``"{ExceptionClassName}: {exc}"``).
        """
        successes: dict[str, ExtractResult] = {}
        failures: dict[str, str] = {}
        for raw_id in tdoc_ids:
            try:
                result = self.extract(raw_id, force=force)
            except (ValueError, LookupError, TDocZipDownloadError) as exc:
                logger.warning(
                    "Failed to extract TDoc %r: %s",
                    raw_id,
                    exc,
                    exc_info=True,
                )
                failures[raw_id.strip()] = f"{type(exc).__name__}: {exc}"
                continue
            successes[result.details.tdoc_id] = result
        return BatchExtractResult(successes=successes, failures=failures)

    # ------------------------------------------------------------------
    # Direct-parse path: ``tdoc parse --from-path/--from-url``
    # ------------------------------------------------------------------

    def extract_from_url(
        self,
        url: str,
        *,
        force: bool = False,
        full: bool = False,
    ) -> DirectParseResult:
        """Download ``url`` and return a :class:`DirectParseResult`.

        Branches on :func:`is_3gpp_ftp_url`:

        - **3GPP-URL path**: behaves like the regular
          :meth:`extract` happy path (cache + DB writes) but uses
          the URL-derived cache key (via
          :func:`derive_zip_cache_key`) and a FK probe against
          ``tdocs`` so the FK on ``tdoc_extracts`` / ``tdoc_cr_details``
          is never violated. The ``force`` flag is forwarded to
          :func:`download_tdoc_zip`; the per-TDoc id is auto-extracted
          from the URL's basename. The ``--format raw`` branch
          (downstream of this method) writes the ``tdoc_extracts``
          row but skips the ``tdoc_cr_details`` row.
        - **Other URL path**: in-memory parse only; the cache and the
          database are never touched.

        Args:
            url: HTTP or HTTPS URL (other schemes raise ``ValueError``
                — operators should use ``--from-path`` for
                ``ftp://`` / ``file://`` sources).
            force: When ``True``, bypass the on-disk zip cache (and
                the markdown cache, in the 3GPP-URL path). The
                ``tdoc_cr_details`` / ``tdoc_extracts`` rows are
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
            markdown, docx_filename, details = direct_parse_bytes(
                payload, filename=url, full=full,
            )
            return DirectParseResult(
                source_kind="url-other",
                markdown=markdown,
                details=details,
                extract_meta=None,
                from_cache=False,
                persisted=False,
                tdoc_id=details.tdoc_id,
                tdoc_id_in_tdocs=False,
            )

        cache_key = derive_zip_cache_key(url)
        stored_ftp_url = normalize_ftp_path(url)
        extracted_id = extract_tdoc_id_from_filename(url)

        if extracted_id is None:
            payload = self._scraper.get_bytes(url)
            markdown, docx_filename, details = direct_parse_bytes(
                payload, filename=url, full=full,
            )
            logger.warning(
                "Direct-parse URL %s has no TDoc id pattern; "
                "skipping cache and DB writes",
                url,
            )
            return DirectParseResult(
                source_kind="url-3gpp",
                markdown=markdown,
                details=details,
                extract_meta=None,
                from_cache=False,
                persisted=False,
                tdoc_id=None,
                tdoc_id_in_tdocs=False,
            )

        if not self._tdoc_in_tdocs(extracted_id):
            payload = self._scraper.get_bytes(url)
            markdown, docx_filename, details = direct_parse_bytes(
                payload, filename=url, full=full,
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
                details=details,
                extract_meta=None,
                from_cache=False,
                persisted=False,
                tdoc_id=extracted_id,
                tdoc_id_in_tdocs=False,
            )

        # 3GPP URL + tdoc_id ∈ tdocs: full happy path.
        return self._extract_from_3gpp_url(
            url=url,
            cache_key=cache_key,
            stored_ftp_url=stored_ftp_url,
            tdoc_id=extracted_id,
            force=force,
            full=full,
        )

    def extract_from_bytes(
        self,
        docx_bytes: bytes,
        filename: str,
        *,
        force: bool = False,
        full: bool = True,
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
        markdown, docx_filename, details = direct_parse_bytes(
            docx_bytes, filename=filename, full=full,
        )
        tdoc_id = extract_tdoc_id_from_filename(filename)
        return DirectParseResult(
            source_kind="local",
            markdown=markdown,
            details=details,
            extract_meta=None,
            from_cache=False,
            persisted=False,
            tdoc_id=tdoc_id,
            tdoc_id_in_tdocs=False,
        )

    def _tdoc_in_tdocs(self, tdoc_id: str) -> bool:
        """Return ``True`` when ``tdoc_id`` has a matching row in ``tdocs``.

        Cheap single-row probe against the TDoc repository; the FK on
        ``tdoc_extracts`` / ``tdoc_cr_details`` is non-nullable, so
        the service layer must consult this before either
        :meth:`_repo.upsert` is called from the direct path.
        """
        return self._tdoc_repo.get_by_id(tdoc_id) is not None

    def _extract_from_3gpp_url(
        self,
        *,
        url: str,
        cache_key: str,
        stored_ftp_url: str,
        tdoc_id: str,
        force: bool,
        full: bool,
    ) -> DirectParseResult:
        """Run the full extract pipeline for a 3GPP URL whose FK target exists.

        The 3GPP branch of :meth:`extract_from_url` delegates to this
        helper so the in-memory parse path stays readable. The
        helper reuses the on-disk zip + markdown caches (with
        ``cache_key`` so distinct revisions of the same ``tdoc_id``
        get distinct cache slots — the D10 fix) and writes both
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
                return DirectParseResult(
                    source_kind="url-3gpp",
                    markdown="",
                    details=cached_details,
                    extract_meta=cached_meta,
                    from_cache=True,
                    persisted=False,
                    tdoc_id=tdoc_id,
                    tdoc_id_in_tdocs=True,
                )

        zip_payload = self._scraper.get_bytes(url)
        self._cache.put_bytes(cache_key, zip_payload, "zips")
        zip_path = self._cache.path_for(cache_key, "zips")

        doc_filename, docx_bytes = extract_docx_from_zip(zip_payload)
        doc_hash = hashlib.sha256(docx_bytes).hexdigest()
        markdown = self._load_or_render_markdown(
            doc_hash=doc_hash,
            docx_bytes=docx_bytes,
            doc_filename=doc_filename,
            force=force,
        )
        details = parse_cr_details(markdown, tdoc_id=tdoc_id, full=full)
        details = replace(details, ftp_url=stored_ftp_url)
        meta = TDocExtractMeta(
            ftp_url=stored_ftp_url,
            tdoc_id=tdoc_id,
            zip_path=str(zip_path),
            markdown_path=str(self._cache.path_for(doc_hash, "markdown")),
            doc_filename=doc_filename,
        )
        self._repo.upsert(details, meta)
        logger.info(
            "Persisted direct-parse CR details for tdoc_id %s at %s",
            tdoc_id,
            stored_ftp_url,
        )
        return DirectParseResult(
            source_kind="url-3gpp",
            markdown=markdown,
            details=details,
            extract_meta=meta,
            from_cache=False,
            persisted=True,
            tdoc_id=tdoc_id,
            tdoc_id_in_tdocs=True,
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
        doc_hash: str,
        docx_bytes: bytes,
        doc_filename: str,
        force: bool,
    ) -> str:
        """Return markdown for ``docx_bytes``, hitting the cache when possible.

        Cache key is the sha256 of the docx bytes — a tweaked upstream
        document invalidates cleanly. ``force=True`` bypasses the
        markdown cache too (the zip is also re-downloaded upstream
        via :func:`download_tdoc_zip`).
        """
        if not force:
            cached = self._cache.get_bytes(doc_hash, "markdown")
            if cached is not None:
                logger.debug(
                    "Markdown cache hit for %s (sha256=%s)",
                    doc_filename,
                    doc_hash,
                )
                return cached.decode("utf-8")

        from doc3gpp.parsers.docx_converter import (
            convert_document_to_markdown,
        )

        markdown = convert_document_to_markdown(docx_bytes, doc_filename)
        self._cache.put_bytes(
            doc_hash,
            markdown.encode("utf-8"),
            "markdown",
        )
        return markdown


# Re-export the parser-side exception so callers can catch a single
# type if they want to treat "not a CR markdown" as a soft failure in
# batch flows. Not part of the Plan but convenient.
__all__ = [
    "BatchExtractResult",
    "CRHeaderMissingError",
    "ExtractResult",
    "TDocCrService",
    "TDocNotFoundError",
    "TDocTypeUnsupportedError",
    "TDocZipDownloadError",
]