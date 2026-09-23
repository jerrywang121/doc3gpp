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
    pool = [
        v
        for v in versions
        if (release is None or v.release == release) and (version is None or v.version == version)
    ]
    if not pool:
        avail = sorted(versions, key=_version_key, reverse=True)[:5]
        raise SpecDocUnknownVersionError(
            f"unknown version release={release!r} version={version!r}; available: "
            + ", ".join(f"{v.version} ({v.release})" for v in avail),
            available=[v.version for v in avail],
        )
    if version is not None:
        exact = [v for v in pool if v.version == version]
        if exact:
            return exact[0]
    return sorted(pool, key=_version_key, reverse=True)[0]


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
