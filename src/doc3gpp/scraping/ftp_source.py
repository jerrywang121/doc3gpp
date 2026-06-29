from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from doc3gpp.models.tdoc import TDoc
from doc3gpp.parsers.tdoc_parser import read_tdoc_sheet
from doc3gpp.scraping.client import ScraperClient

logger = logging.getLogger(__name__)


def _normalize_ftp_path(path: str) -> str:
    """Normalize a meeting FTP path to a canonical relative path."""
    normalized = path.strip().replace("\\", "/")
    normalized = re.sub(r"^ftp:/+", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"^https?://www\.3gpp\.org/ftp/", "", normalized, flags=re.IGNORECASE)
    normalized = normalized.lstrip("/")
    normalized = re.sub(r"/{2,}", "/", normalized)
    return normalized


def _find_tdoc_list_filename(hrefs: list[str]) -> str | None:
    """Select a TDoc list XLSX filename from directory listing links."""
    for href in hrefs:
        lower = href.lower()
        if lower.startswith("tdoc_list_meeting_") and lower.endswith(".xlsx"):
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
    base_url = _normalize_ftp_path(ftp_url)
    if not base_url.endswith("/"):
        base_url += "/"

    logger.debug("Normalized FTP base path: %s", base_url)
    with ScraperClient() as client:
        for subfolder in ["", "docs/", "tdoc/"]:
            directory_url = urljoin("https://www.3gpp.org/ftp/", base_url + subfolder)
            logger.debug("Trying FTP directory URL: %s", directory_url)
            try:
                html = client.get_text(directory_url)
            except Exception:
                logger.debug("Failed to fetch FTP directory %s", directory_url, exc_info=True)
                continue

            hrefs = _extract_hrefs(html)
            candidate = _find_tdoc_list_filename(hrefs)
            if not candidate:
                logger.debug("No TDoc list file found in %s", directory_url)
                continue

            file_url = urljoin(directory_url, candidate)
            logger.info("Found TDoc list file %s in %s", candidate, directory_url)
            xlsx_bytes = client.get_bytes(file_url)
            records = read_tdoc_sheet(xlsx_bytes)
            logger.info("Parsed %s TDoc rows from %s", len(records), file_url)
            return [
                    TDoc(
                    tdoc_id=row["tdoc"],
                    title=row.get("title", ""),
                    meeting_id=meeting_id,
                    url=file_url,
                    source=row.get("source", ""),
                    type=row.get("type", ""),
                    status=row.get("status", ""),
                    reservation_date=row.get("reservation_date", ""),
                    uploaded_date=row.get("uploaded_date", ""),
                    cr_cat=row.get("cr_cat", ""),
                        cr_pack=row.get("cr_pack", ""),
                    is_revision_of=row.get("is_revision_of", ""),
                    revised_to=row.get("revised_to", ""),
                    release=row.get("release", ""),
                    spec=row.get("spec", ""),
                    version=row.get("version", ""),
                    related_wis=row.get("related_wis", ""),
                    cr_num=row.get("cr_num", ""),
                )
                for row in records
            ]

    logger.warning("No TDoc list file found for FTP url %s", ftp_url)
    return []
