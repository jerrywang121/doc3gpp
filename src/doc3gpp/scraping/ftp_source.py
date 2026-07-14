from __future__ import annotations

import logging
from collections.abc import Iterable
from urllib.parse import urljoin

import httpx

from doc3gpp.models.tdoc_file import TDocFile
from doc3gpp.parsers.normalizers import (
    FTP_BASE_URL,
    normalize_ftp_path,
)
from doc3gpp.parsers.tdoc_file_parser import parse_tdoc_files_from_listing
from doc3gpp.scraping.client import ScraperClient

logger = logging.getLogger(__name__)


#: Nested ``inbox/intermediate_crs/`` entry covers R5 meetings whose
#: Intermediate CRs live under a dedicated subdirectory of ``Inbox/``.
TDOC_FILE_SUBDIRS: tuple[str, ...] = (
    "inbox/",
    "inbox/intermediate_crs/",
    "docs/",
    "tdocs/",
    "review/",
)


def _normalize_optional_url(value: object) -> str | None:
    """Normalize an optional URL harvested from the XLSX parser.

    The XLSX hyperlink targets are absolute ``https://...`` URLs; the
    database stores the relative path instead. ``None`` and empty
    strings pass through untouched so callers can still distinguish
    "no hyperlink on this row" from "some path we couldn't resolve".
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return normalize_ftp_path(text)


def _strip_terminal_subdir(base_root: str) -> str:
    for suffix in TDOC_FILE_SUBDIRS + ("tdoc/",):
        if base_root.lower().endswith(suffix):
            base_root = base_root[: -len(suffix)]
            if not base_root.endswith("/"):
                base_root += "/"
            break
    return base_root


def fetch_tdoc_files_from_meeting_ftp(
    ftp_url: str,
    tdoc_ids: Iterable[str],
) -> list[TDocFile]:
    """Scan a meeting's FTP subfolders for auxiliary TDoc files.

    Visits each subfolder in :data:`TDOC_FILE_SUBDIRS` under
    ``ftp_url`` and returns the union of files that match the
    supplied ``tdoc_ids`` and the revision / review / support
    naming conventions. Subfolders that 404 are silently skipped
    (most meetings only have one of ``docs/``/``tdocs/``, only R5
    TTCN email meetings have a ``review/`` folder, and only R5
    meetings route Intermediate CRs through
    ``inbox/intermediate_crs/``); any other HTTP error propagates so
    transient failures surface.

    Files that appear in more than one subfolder are deduplicated by
    URL — the same upstream location can be reached through both
    ``docs/`` and the meeting root in some legacy layouts.
    """
    tdoc_id_set = {tid for tid in tdoc_ids if tid}
    if not tdoc_id_set:
        logger.debug("No TDoc IDs supplied; skipping TDoc file scan for %s", ftp_url)
        return []

    base_url = normalize_ftp_path(ftp_url)
    if not base_url.endswith("/"):
        base_url += "/"
    base_root = _strip_terminal_subdir(base_url)

    results: list[TDocFile] = []
    seen_urls: set[str] = set()

    with ScraperClient() as client:
        for subfolder in TDOC_FILE_SUBDIRS:
            directory_url = urljoin(FTP_BASE_URL, base_root + subfolder)
            logger.debug("Scanning FTP directory for TDoc files: %s", directory_url)
            try:
                html = client.get_text(directory_url)
            except httpx.HTTPError as exc:
                logger.debug(
                    "Skipping FTP directory %s: %s", directory_url, exc
                )
                continue

            for file in parse_tdoc_files_from_listing(
                html, base_url=directory_url, tdoc_ids=tdoc_id_set
            ):
                if file.ftp_url in seen_urls:
                    continue
                seen_urls.add(file.ftp_url)
                results.append(file)

    logger.info(
        "Found %s auxiliary TDoc file(s) under %s across %s subfolder(s)",
        len(results),
        ftp_url,
        len(TDOC_FILE_SUBDIRS),
    )
    return results
