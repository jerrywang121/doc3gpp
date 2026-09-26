"""Version resolution + zip download for spec documents. Network only, no parsing."""
from __future__ import annotations

import logging

from doc3gpp.models.spec import SpecVersion
from doc3gpp.models.spec_doc import SpecDocUnknownVersionError

logger = logging.getLogger(__name__)


def _version_key(v: SpecVersion) -> tuple[int, ...]:
    parts: list[int] = []
    for seg in v.version.split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def resolve_spec_doc_version(
    versions: list[SpecVersion], release: str | None = None, version: str | None = None
) -> SpecVersion:
    if not versions:
        raise SpecDocUnknownVersionError("no versions stored for this spec", available=[])

    pool = [v for v in versions if release is None or v.release == release]
    if version is not None:
        exact = [v for v in pool if v.version == version]
        if not exact:
            avail = sorted(versions, key=_version_key, reverse=True)[:5]
            raise SpecDocUnknownVersionError(
                f"unknown version release={release!r} version={version!r}; available: "
                + ", ".join(f"{v.version} ({v.release})" for v in avail),
                available=[v.version for v in avail],
            )
        selected = exact[0]
        if not _has_zip_url(selected):
            raise SpecDocUnknownVersionError(
                f"version {selected.version} has no downloadable .zip link; "
                "the archive may not have been published yet",
                available=[selected.version],
            )
        return selected

    downloadable = [v for v in pool if _has_zip_url(v)]
    if not downloadable:
        release_label = f" for release {release!r}" if release is not None else ""
        raise SpecDocUnknownVersionError(
            f"no downloadable .zip links found{release_label}; "
            "the archive may not have been published yet",
            available=[v.version for v in sorted(pool, key=_version_key, reverse=True)[:5]],
        )
    return max(downloadable, key=_version_key)


def _has_zip_url(version: SpecVersion) -> bool:
    """Return whether a version row carries a non-empty ZIP download link."""
    return version.ftp_url.strip().lower().split("?", 1)[0].split("#", 1)[0].endswith(".zip")


def fetch_spec_doc_zip(ftp_url: str, client=None) -> bytes:
    from doc3gpp.scraping.client import ScraperClient

    own = client is None
    c = client or ScraperClient()
    try:
        logger.debug("Fetching spec zip: %s", ftp_url)
        return c.get_bytes(ftp_url)
    finally:
        if own:
            c.close()
