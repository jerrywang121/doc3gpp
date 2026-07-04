"""Resolve a 3GPP TDoc identifier to its canonical zip URL and download it.

Pure network + cache layer — no parsing. The URL templates are derived from
``docs/ttcn_cr_cli_example.py:build_tdoc_zip_url`` and locked in by the
TDoc Extraction Pipeline design (see ``docs/implementation-status.md``
§Scraping and Parsing).

The ``R5-`` and ``C6-`` URL templates are intentionally unresolved (return
``None``) until exercised against the live site; callers should
treat ``None`` as "not yet supported" rather than an error. The
known-constraints list in ``docs/implementation-status.md`` tracks
which URL branches are still pending verification.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

import httpx

if TYPE_CHECKING:
    from doc3gpp.scraping.client import ScraperClient

logger = logging.getLogger(__name__)


# Pattern source of truth: ``docs/ttcn_cr_cli_example.py:29``.
# Matches ``R5s260009``, ``R5w260009``, ``R5-227476``, ``C6-250028`` (and
# the lower-case variants). The first char is the meeting family (``R``,
# ``S``, ``C``), the second is the working group digit ``[1-9]``,
# position 2 is the subtype separator (``-``, ``s``, or ``w``), and the
# last six chars are the sequence number.
_CR_ID_RE = re.compile(r"[RSC][1-9][-sw]\d{6}", re.IGNORECASE)

# Subdir names accepted by the Phase 1 ``TDocCache`` interface. Mirrored
# here so the Protocol stays narrow and the constant has a single owner.
CacheSubdir = Literal["zips", "markdown"]


class TDocZipDownloadError(Exception):
    """Raised when a TDoc zip cannot be downloaded.

    Wraps the underlying ``httpx.HTTPError`` so callers can catch a single
    type and decide whether to skip the TDoc or surface the failure.
    """

    def __init__(self, url: str, original: Exception) -> None:
        super().__init__(f"Failed to download TDoc zip from {url}: {original}")
        self.url = url
        self.original = original


class TDocCacheLike(Protocol):
    """Minimal cache interface consumed by ``download_tdoc_zip``.

    Defined as a Protocol so Phase 1's ``TDocCache`` implementation can
    slot in without import gymnastics. Keep this in sync with
    ``src/doc3gpp/scraping/cache.py`` once Phase 1 lands.
    """

    def put_bytes(self, key: str, payload: bytes, subdir: CacheSubdir) -> Path: ...

    def get_bytes(self, key: str, subdir: CacheSubdir) -> bytes | None: ...

    def path_for(self, key: str, subdir: CacheSubdir) -> Path: ...


def tsg_meeting_year_for(tdoc: str) -> tuple[str, int | None]:
    """Return the (tsg_short_name, four-digit-year) derived from a TDoc id.

    The shape comes from ``docs/ttcn_cr_cli_example.py:build_tdoc_zip_url``:
    positions 0-1 = TSG short name (uppercased), 3-4 = two-digit year
    (``20xx``).

    Examples:
        ``R5s260009`` -> ``('R5', 2026)``
        ``R5w260009`` -> ``('R5', 2026)``
        ``R5-227476`` -> ``('R5', 2022)``
        ``C6-250028`` -> ``('C6', 2025)``
        ``bogus``     -> ``('', None)``
    """
    if not tdoc:
        return ("", None)
    match = _CR_ID_RE.fullmatch(tdoc.strip())
    if match is None:
        return ("", None)
    canonical = match.group(0)
    tsg = canonical[:2].upper()
    try:
        year = 2000 + int(canonical[3:5])
    except ValueError:
        return (tsg, None)
    return (tsg, year)


def _build_tdoc_zip_url(canonical_tdoc: str) -> str | None:
    """Return the canonical 3GPP URL for a TDoc id, or ``None`` if unsupported.

    The input MUST be in the canonical ``Ts260009`` form (TSG short name
    upper-cased, subtype separator lowercase). Use ``tsg_meeting_year_for``
    to derive the components; this helper is the pure template builder.
    """
    tsg = canonical_tdoc[:2]
    sub = canonical_tdoc[2:3]
    year = "20" + canonical_tdoc[3:5]
    if tsg == "R5" and sub == "s":
        return (
            f"https://www.3gpp.org/ftp/tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/"
            f"{year}/Docs/{canonical_tdoc}.zip"
        )
    if tsg == "R5" and sub == "w":
        return (
            f"https://www.3gpp.org/ftp/tsg_ran/WG5_Test_ex-T1/Workshop/"
            f"TSGR5_Workshop_{year}/Docs/{canonical_tdoc}.zip"
        )
    # R5- and C6- templates deferred to Phase 8 per the plan.
    return None


def get_tdoc_zip_url(tdoc: str) -> str | None:
    """Return the canonical 3GPP URL for a TDoc zip, or ``None`` if unrecognised.

    Strategy: derive the canonical TDoc id from the input, then build the
    URL from the locked-in template. Per the plan, a DB lookup against
    ``tdocs.url`` is the "fast path"; that lookup is owned by Phase 5/6
    (``TDocRepository``), so we deliberately skip it here rather than
    introduce a Protocol dependency that Phase 6 will own.

    # TODO(phase-6): also check the tdocs table for an explicit URL stored
    # from a prior ``tdoc sync`` run; fall back to the template on miss.
    """
    if not tdoc:
        return None
    canonical = _canonicalise_tdoc_id(tdoc)
    if canonical is None:
        return None
    return _build_tdoc_zip_url(canonical)


def _canonicalise_tdoc_id(tdoc: str) -> str | None:
    """Normalise a TDoc id to the canonical ``Ts260009`` form.

    Strips surrounding whitespace, lowercases the input, and matches it
    against ``_CR_ID_RE``. Returns the canonical form (TSG short name
    upper-cased) on match, ``None`` otherwise.
    """
    match = _CR_ID_RE.fullmatch(tdoc.strip().lower())
    if match is None:
        return None
    lowered = match.group(0)
    return lowered[:2].upper() + lowered[2:]


def download_tdoc_zip(
    tdoc: str,
    client: "ScraperClient",
    cache: TDocCacheLike,
) -> Path:
    """Return the cache ``Path`` to the TDoc zip, downloading on cache miss.

    Cache key is ``tdoc.lower()``; subdir is ``"zips"``. On cache hit the
    cached path is returned without touching the network. On miss the URL
    is resolved via :func:`get_tdoc_zip_url`, fetched through ``client``,
    and written through the cache. Non-retryable ``httpx.HTTPError`` (and
    missing URL templates) are wrapped in :class:`TDocZipDownloadError`
    so the caller can catch a single type.

    Raises:
        ValueError: ``tdoc`` does not match the CR pattern (the cache and
            network are left untouched in that case — a bad id should
            fail fast, not produce a half-written cache entry).
        TDocZipDownloadError: the URL template is unknown for this TDoc
            shape, or the HTTP fetch raised a terminal ``httpx.HTTPError``.
    """
    if not tdoc:
        raise ValueError("TDoc id is empty")

    canonical = _canonicalise_tdoc_id(tdoc)
    if canonical is None:
        raise ValueError(f"Invalid TDoc id shape: {tdoc!r}")

    cache_key = canonical.lower()

    cached_bytes = cache.get_bytes(cache_key, "zips")
    if cached_bytes is not None:
        logger.debug("Cache hit for TDoc zip %s", cache_key)
        return cache.path_for(cache_key, "zips")

    url = get_tdoc_zip_url(canonical)
    if url is None:
        raise TDocZipDownloadError(url="", original=ValueError("no URL template"))

    try:
        payload = client.get_bytes(url)
    except httpx.HTTPError as exc:
        logger.error("HTTP error downloading TDoc zip %s from %s: %s", cache_key, url, exc)
        raise TDocZipDownloadError(url=url, original=exc) from exc

    cached_path = cache.put_bytes(cache_key, payload, "zips")
    logger.info("Cached TDoc zip %s at %s (%d bytes)", cache_key, cached_path, len(payload))
    return cached_path