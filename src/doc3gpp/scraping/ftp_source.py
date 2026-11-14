from __future__ import annotations

import logging
import re
from collections import deque
from collections.abc import Iterable
from urllib.parse import unquote, urljoin

import httpx

from doc3gpp.models.tdoc_file import TDocFile
from doc3gpp.parsers.normalizers import (
    FTP_BASE_URL,
    normalize_ftp_path,
)
from doc3gpp.parsers.tdoc_file_parser import (
    parse_tdoc_files_from_listing,
    parse_tdoc_subfolders,
)
from doc3gpp.scraping.client import ScraperClient

logger = logging.getLogger(__name__)


TDOC_FILE_SUBDIRS: tuple[str, ...] = (
    "inbox/",
    "docs/",
    "tdoc/",
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
    for suffix in ("inbox/intermediate_crs/",) + TDOC_FILE_SUBDIRS + ("tdocs/",):
        if base_root.lower().endswith("/" + suffix) or base_root.lower() == suffix:
            base_root = base_root[: -len(suffix)]
            if not base_root.endswith("/"):
                base_root += "/"
            break
    return base_root


def _url_identity(url: str) -> str:
    value = normalize_ftp_path(url)
    while (decoded := unquote(value)) != value:
        value = decoded
    return value.rstrip("/").casefold()


def fetch_tdoc_files_from_meeting_ftp(
    ftp_url: str,
    tdoc_ids: Iterable[str],
    *,
    tsg: str | None = None,
    max_depth: int = 4,
) -> list[TDocFile]:
    """Scan selected meeting subtrees up to the meeting-relative depth limit."""
    tdoc_id_set = {tid for tid in tdoc_ids if tid}
    if not tdoc_id_set:
        logger.debug("No TDoc IDs supplied; skipping TDoc file scan for %s", ftp_url)
        return []

    def group(tid: str) -> str | None:
        match = re.match(r"^([a-z]+\d*)", tid, re.IGNORECASE)
        return match.group(1).casefold() if match else None

    if tsg is None:
        groups = {group(tid) for tid in tdoc_id_set}
        if len(groups) != 1 or None in groups:
            logger.warning("Cannot infer a unique TDoc group for %s; skipping file scan", ftp_url)
            return []
        meeting_group = next(iter(groups))
    else:
        meeting_group = tsg.strip().casefold()
    tdoc_id_set = {tid for tid in tdoc_id_set if group(tid) == meeting_group}
    if not tdoc_id_set or max_depth < 1:
        return []

    base_url = normalize_ftp_path(ftp_url)
    if not base_url.endswith("/"):
        base_url += "/"
    base_root = _strip_terminal_subdir(base_url)

    results: list[TDocFile] = []
    seen_urls: set[str] = set()
    seen_directories: set[str] = set()

    with ScraperClient() as client:
        queue = deque(
            (urljoin(FTP_BASE_URL, base_root + subfolder), 1, subfolder.rstrip("/"))
            for subfolder in TDOC_FILE_SUBDIRS
        )
        while queue:
            directory_url, depth, root_folder = queue.popleft()
            key = _url_identity(directory_url)
            if key in seen_directories:
                continue
            seen_directories.add(key)
            logger.debug("Scanning FTP directory for TDoc files: %s", directory_url)
            try:
                html = client.get_text(directory_url)
            except httpx.HTTPError as exc:
                logger.debug(
                    "Skipping FTP directory %s: %s", directory_url, exc
                )
                continue

            for file in parse_tdoc_files_from_listing(
                html, base_url=directory_url, tdoc_ids=tdoc_id_set,
                folder=root_folder, restrict_to_directory=True,
            ):
                key = _url_identity(file.ftp_url)
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                results.append(file)
            if depth < max_depth:
                queue.extend(
                    (child, depth + 1, root_folder)
                    for child in parse_tdoc_subfolders(html, directory_url)
                )

    logger.info(
        "Found %s auxiliary TDoc file(s) under %s across %s subfolder(s)",
        len(results),
        ftp_url,
        len(seen_directories),
    )
    return results
