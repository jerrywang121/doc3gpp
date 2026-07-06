"""End-to-end TDoc CR (Change Request) extraction pipeline.

Glues the per-stage building blocks together:

1. Validate the requested ``tdoc_id``.
2. Confirm the TDoc exists in the ``tdocs`` table and is of type
   ``"CR"`` (the ``tdocs.url`` row is the source of truth — the TDoc
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
from dataclasses import dataclass
from typing import TYPE_CHECKING

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
from doc3gpp.parsers.cr_parser import (
    CRHeaderMissingError,
    extract_docx_from_zip,
    parse_cr_details,
)
from doc3gpp.repository.protocols import (
    TDocCrDetailRepository,
    TDocRepository,
)
from doc3gpp.scraping.tdoc_zip_source import (
    TDocCacheLike,
    TDocZipDownloadError,
    download_tdoc_zip,
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
        3. If a detail row exists and ``not force``, return
           :class:`ExtractResult` with ``from_cache=True`` without
           touching the network or the python-docx renderer.
        4. Download the zip (cache or network) — preferring the
           per-TDoc URL stored in ``tdocs.url`` (extracted from the
           XLSX hyperlink during ``tdoc sync``) and falling back to
           the template-based URL on failure → extract the docx →
           compute sha256 of the docx bytes → look up the markdown
           cache (skip on ``force``) → render markdown if needed →
           parse → persist → return ``from_cache=False``.
        """
        normalised = self._validate_tdoc_id(tdoc_id)
        tdoc = self._load_tdoc(normalised)

        # DB-cache hit: short-circuit everything. A force=True request
        # always bypasses this branch.
        if not force:
            cached_details = self._repo.get(normalised)
            cached_meta = self._repo.get_extract_meta(normalised)
            if cached_details is not None and cached_meta is not None:
                logger.debug("DB cache hit for TDoc %s", normalised)
                return ExtractResult(
                    details=cached_details,
                    extract_meta=cached_meta,
                    from_cache=True,
                )

        zip_path = download_tdoc_zip(
            normalised,
            self._scraper,
            self._cache,
            primary_url=tdoc.url,
        )
        doc_filename, docx_bytes = extract_docx_from_zip(zip_path.read_bytes())
        doc_hash = hashlib.sha256(docx_bytes).hexdigest()

        markdown = self._load_or_render_markdown(
            doc_hash=doc_hash,
            docx_bytes=docx_bytes,
            doc_filename=doc_filename,
            force=force,
        )

        details = parse_cr_details(markdown, tdoc_id=normalised)
        meta = TDocExtractMeta(
            tdoc_id=normalised,
            zip_path=str(zip_path),
            markdown_path=str(self._cache.path_for(doc_hash, "markdown")),
            doc_filename=doc_filename,
        )
        self._repo.upsert(details, meta)
        logger.info(
            "Persisted CR details for TDoc %s (spec=%s cr_num=%s)",
            normalised,
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
    ) -> dict[str, ExtractResult]:
        """Extract a batch of TDocs and return only the successes.

        Failures are logged at ``WARNING`` level via the module logger
        (not raised) so a single broken id doesn't abort the rest of
        the batch. The CLI surfaces a summary line built from the
        difference between the input set and the returned dict's keys.

        Args:
            tdoc_ids: Iterable of TDoc ids to extract. Strings that
                fail the shape guard, are missing from the ``tdocs``
                table, or aren't CR type are logged and skipped.
            force: Forwarded to :meth:`extract`. When ``True`` every
                TDoc is re-fetched and re-parsed from scratch.

        Returns:
            ``{tdoc_id: ExtractResult}`` for every successful extract.
            Cache hits and fresh extracts both land in the dict — the
            ``from_cache`` flag on each :class:`ExtractResult` tells
            the caller which it was.
        """
        results: dict[str, ExtractResult] = {}
        for raw_id in tdoc_ids:
            try:
                result = self.extract(raw_id, force=force)
            except (ValueError, LookupError, TDocZipDownloadError) as exc:
                logger.warning(
                    "Failed to extract TDoc %r: %s",
                    raw_id,
                    exc,
                )
                continue
            results[result.details.tdoc_id] = result
        return results

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
    "CRHeaderMissingError",
    "ExtractResult",
    "TDocCrService",
    "TDocNotFoundError",
    "TDocTypeUnsupportedError",
    "TDocZipDownloadError",
]