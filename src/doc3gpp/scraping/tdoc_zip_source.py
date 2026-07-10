"""Resolve a 3GPP TDoc identifier to its canonical zip URL and download it.

Pure network + cache layer — no parsing. The URL templates are derived from
the 3GPP directory layout and verified against offline fixtures for the
``R5s`` (TTCN email CR) and ``R5w`` (TTCN workshop CR) branches.

The ``R5-`` and ``C6-`` URL templates are intentionally unresolved (return
``None``) until exercised against the live site; callers should
treat ``None`` as "not yet supported" rather than an error.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, Protocol

import httpx

if TYPE_CHECKING:
    from doc3gpp.scraping.client import ScraperClient

logger = logging.getLogger(__name__)


# CR-shape TDoc identifier. Shared with ``cli_filters.TDOC_ID_RE``; keep
# in sync. Matches ``R5s260009``, ``R5w260009``, ``R5-227476``, ``C6-250028`` (and
# the lower-case variants). The first char is the meeting family (``R``,
# ``S``, ``C``), the second is the working group digit ``[1-9]``,
# position 2 is the subtype separator (``-``, ``s``, or ``w``), and the
# last six chars are the sequence number.
_CR_ID_RE = re.compile(r"[RSC][1-9][-sw]\d{6}", re.IGNORECASE)

# Subdir names accepted by the Phase 1 ``TDocCache`` interface. Mirrored
# here so the Protocol stays narrow and the constant has a single owner.
CacheSubdir = Literal["zips", "markdown"]


class DownloadedZip(NamedTuple):
    """Result of a successful zip download.

    Attributes:
        path: Cache path to the zip bytes. Always set, whether the bytes
            came from a fresh network fetch or a prior cache hit.
        url: The exact URL the bytes were fetched from, when known.
            ``None`` on a cache hit (the URL that populated the cache
            in an earlier call is not tracked here) and on a fresh
            download only when every candidate URL was deduplicated to
            a single fetch — the service layer treats ``None`` as
            "no provenance available" rather than an error.
    """

    path: Path
    url: str | None


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

    The shape comes from ``_CR_ID_RE`` above: positions 0-1 = TSG short name
    (uppercased), 3-4 = two-digit year (``20xx``).

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
    URL from the locked-in template. Callers that have a stored
    ``tdocs.ftp_url`` (from a prior ``tdoc sync`` run) should pass it to
    :func:`download_tdoc_zip` as ``primary_url`` (after rebuilding the
    absolute URL via :func:`doc3gpp.parsers.normalizers.build_ftp_url`)
    so the per-TDoc URL takes precedence over the template-based guess.
    """
    if not tdoc:
        return None
    canonical = canonicalise_tdoc_id(tdoc)
    if canonical is None:
        return None
    return _build_tdoc_zip_url(canonical)


def resolve_download_url(
    tdoc: str,
    primary_url: str | None = None,
) -> list[str]:
    """Return the URL(s) ``download_tdoc_zip`` would try, in order.

    Pre-resolves the candidate URLs without touching the network so the
    caller can perform a ``get_by_url`` DB cache lookup before paying
    the cost of an HTTP fetch. The order matches
    :func:`download_tdoc_zip`: ``primary_url`` first (when provided and
    distinct from the template), then the template URL. ``tdoc`` is
    first canonicalised; an unrecognised id returns an empty list.
    """
    candidates: list[str] = []
    if primary_url:
        candidates.append(primary_url)
    template_url = get_tdoc_zip_url(tdoc)
    if template_url and template_url not in candidates:
        candidates.append(template_url)
    return candidates


def canonicalise_tdoc_id(tdoc: str) -> str | None:
    """Normalise a CR-shape TDoc id to its canonical ``R5s260009`` form.

    Returns the canonical form (TSG short name upper-cased, everything
    else as-is) on a match against ``_CR_ID_RE``, ``None`` otherwise.

    Examples:
        ``r5s260009`` -> ``R5s260009``
        ``R5S260009`` -> ``R5s260009``
        ``R5-227476`` -> ``R5-227476``
        ``bogus``     -> ``None``
        ``LS-260001`` -> ``None`` (non-CR shape)

    Intentionally narrow to CR shapes; the only ones whose download URL
    template we resolve. Non-CR rows (LS / DRAFT / etc.) live in the
    ``tdocs`` table with their own shapes; callers that accept arbitrary
    ids should fall back to the stripped input when this returns
    ``None`` rather than rejecting the id outright.
    """
    if not tdoc:
        return None
    match = _CR_ID_RE.fullmatch(tdoc.strip().lower())
    if match is None:
        return None
    lowered = match.group(0)
    return lowered[:2].upper() + lowered[2:]


def download_tdoc_zip(
    tdoc: str,
    client: "ScraperClient",
    cache: TDocCacheLike,
    primary_url: str | None = None,
) -> DownloadedZip:
    """Return a :class:`DownloadedZip` for the TDoc, downloading on cache miss.

    Cache key is ``tdoc.lower()``; subdir is ``"zips"``. On cache hit the
    cached path is returned (with ``url=None`` since the URL that
    populated the cache in an earlier call is not tracked here). On
    miss the function tries each candidate URL in order and caches the
    first successful download:

    1. ``primary_url`` (typically rebuilt from ``tdocs.ftp_url`` via
   :func:`doc3gpp.parsers.normalizers.build_ftp_url`).
    2. The template-based URL from :func:`get_tdoc_zip_url`.

    The two are deduplicated, so a ``primary_url`` that matches the
    template only triggers one fetch. A terminal ``httpx.HTTPError`` on
    ``primary_url`` is swallowed and the template is tried as a fallback;
    if the template also fails, the last error is raised as
    :class:`TDocZipDownloadError`. Programming errors (e.g.
    ``httpx.InvalidURL``) on ``primary_url`` propagate immediately so
    a malformed stored URL doesn't silently mask the real problem.

    The returned ``url`` records the exact candidate that succeeded on a
    fresh download, so the caller can persist the download provenance
    alongside the extracted CR fields.

    Raises:
        ValueError: ``tdoc`` does not match the CR pattern (the cache and
            network are left untouched in that case — a bad id should
            fail fast, not produce a half-written cache entry).
        TDocZipDownloadError: every candidate URL was tried and none
            succeeded (no ``primary_url`` and no template, or all
            fetches raised a terminal ``httpx.HTTPError``).
    """
    if not tdoc:
        raise ValueError("TDoc id is empty")

    canonical = canonicalise_tdoc_id(tdoc)
    if canonical is None:
        raise ValueError(f"Invalid TDoc id shape: {tdoc!r}")

    cache_key = canonical.lower()

    cached_bytes = cache.get_bytes(cache_key, "zips")
    if cached_bytes is not None:
        logger.debug("Cache hit for TDoc zip %s", cache_key)
        return DownloadedZip(path=cache.path_for(cache_key, "zips"), url=None)

    candidates: list[str] = []
    if primary_url:
        candidates.append(primary_url)
    template_url = get_tdoc_zip_url(canonical)
    if template_url and template_url not in candidates:
        candidates.append(template_url)

    if not candidates:
        raise TDocZipDownloadError(url="", original=ValueError("no URL template"))

    last_error: TDocZipDownloadError | None = None
    for url in candidates:
        try:
            payload = client.get_bytes(url)
        except httpx.HTTPError as exc:
            logger.error(
                "HTTP error downloading TDoc zip %s from %s: %s", cache_key, url, exc
            )
            last_error = TDocZipDownloadError(url=url, original=exc)
            continue
        cached_path = cache.put_bytes(cache_key, payload, "zips")
        logger.info(
            "Cached TDoc zip %s at %s (%d bytes) from %s",
            cache_key,
            cached_path,
            len(payload),
            url,
        )
        return DownloadedZip(path=cached_path, url=url)

    assert last_error is not None  # candidates is non-empty, so we must have set it
    raise last_error