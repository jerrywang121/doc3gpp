from __future__ import annotations

import logging
from collections.abc import Iterable
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_file import TDocFile
from doc3gpp.parsers.normalizers import (
    FTP_BASE_URL,
    normalize_ftp_path,
)
from doc3gpp.parsers.tdoc_file_parser import parse_tdoc_files_from_listing
from doc3gpp.parsers.tdoc_parser import read_tdoc_sheet
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


# Backwards-compatible alias for the historical private name. New code
# should call :func:`doc3gpp.parsers.normalizers.normalize_ftp_path`.
_normalize_ftp_path = normalize_ftp_path

def _find_tdoc_list_filename(hrefs: list[str]) -> str | None:
    """Select a TDoc list XLSX filename from directory listing links.

    Hrefs from the 3GPP FTP directory listing are full URLs (e.g.
    ``https://www.3gpp.org/ftp/.../TDoc_List_Meeting_*.xlsx``). Match by the
    basename rather than requiring the href to start with the bare filename.
    """
    for href in hrefs:
        lower = href.lower()
        basename = lower.rsplit("/", 1)[-1]
        if basename.startswith("tdoc_list_meeting_") and basename.endswith(".xlsx"):
            return href
    return None


def _extract_hrefs(html: str) -> list[str]:
    """Extract all anchor href values from an HTML directory listing."""
    soup = BeautifulSoup(html, "lxml")
    return [a["href"] for a in soup.find_all("a", href=True)]


def fetch_tdocs_from_meeting_ftp(ftp_url: str, meeting_id: int | None = None) -> list[TDoc]:
    """Discover a meeting TDoc list XLSX from the FTP path and parse its rows.

    `meeting_id` is an optional integer referring to `meetings.meeting_id`.
    """
    base_url = normalize_ftp_path(ftp_url)
    if not base_url.endswith("/"):
        base_url += "/"

    logger.debug("Normalized FTP base path: %s", base_url)
    # Avoid duplicating known terminal subfolders if the provided ftp_url
    # already contains them (e.g. .../docs/ or .../tdoc/). Compute a
    # base root without a terminal docs/tdoc segment and try that plus
    # common subfolders.
    base_root = base_url
    for suffix in ("docs/", "tdoc/"):
        if base_root.lower().endswith(suffix):
            base_root = base_root[: -len(suffix)]
            if not base_root.endswith("/"):
                base_root += "/"

    failed_urls: list[tuple[str, str]] = []
    with ScraperClient() as client:
        for subfolder in ["", "docs/", "tdoc/"]:
            directory_url = urljoin(FTP_BASE_URL, base_root + subfolder)
            logger.debug("Trying FTP directory URL: %s", directory_url)
            try:
                html = client.get_text(directory_url)
            except httpx.HTTPError as exc:
                logger.debug(
                    "HTTP error fetching FTP directory %s: %s", directory_url, exc
                )
                failed_urls.append((directory_url, str(exc)))
                continue

            hrefs = _extract_hrefs(html)
            candidate = _find_tdoc_list_filename(hrefs)
            if not candidate:
                logger.debug("No TDoc list file found in %s", directory_url)
                continue

            file_url = urljoin(directory_url, candidate)
            logger.info("Found TDoc list file %s in %s", candidate, directory_url)
            try:
                xlsx_bytes = client.get_bytes(file_url)
            except httpx.HTTPError as exc:
                logger.debug("HTTP error fetching XLSX %s: %s", file_url, exc)
                failed_urls.append((file_url, str(exc)))
                continue

            records = read_tdoc_sheet(xlsx_bytes)
            logger.info("Parsed %s TDoc rows from %s", len(records), file_url)
            return [
                TDoc(
                    tdoc_id=row["tdoc"],
                    title=row.get("title"),
                    meeting_id=meeting_id,
                    ftp_url=_normalize_optional_url(row.get("tdoc_url")),
                    source=row.get("source"),
                    type=row.get("type"),
                    status=row.get("status"),
                    reservation_date=row.get("reservation_date"),
                    uploaded_date=row.get("uploaded_date"),
                    cr_cat=row.get("cr_cat"),
                    cr_pack=row.get("cr_pack"),
                    is_revision_of=row.get("is_revision_of"),
                    revised_to=row.get("revised_to"),
                    release=row.get("release"),
                    spec=row.get("spec"),
                    version=row.get("version"),
                    related_wis=row.get("related_wis"),
                    cr_num=row.get("cr_num"),
                )
                for row in records
            ]

    if failed_urls:
        details = "; ".join(f"{url} ({err})" for url, err in failed_urls)
        raise RuntimeError(
            f"Failed to fetch TDoc list for FTP url {ftp_url}: all subfolders failed. {details}"
        )

    logger.warning("No TDoc list file found for FTP url %s", ftp_url)
    return []


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
