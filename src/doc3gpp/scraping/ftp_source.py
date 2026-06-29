from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from doc3gpp.models.tdoc import TDoc
from doc3gpp.parsers.tdoc_parser import read_tdoc_sheet
from doc3gpp.scraping.client import ScraperClient


def _normalize_ftp_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    normalized = re.sub(r"^ftp:/+", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"^https?://www\.3gpp\.org/ftp/", "", normalized, flags=re.IGNORECASE)
    normalized = normalized.lstrip("/")
    normalized = re.sub(r"/{2,}", "/", normalized)
    return normalized


def _find_tdoc_list_filename(hrefs: list[str]) -> str | None:
    for href in hrefs:
        lower = href.lower()
        if lower.startswith("tdoc_list_meeting_") and lower.endswith(".xlsx"):
            return href
    return None


def _extract_hrefs(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    return [a["href"] for a in soup.find_all("a", href=True)]


def fetch_tdocs_from_meeting_ftp(ftp_url: str, meeting: str | None = None) -> list[TDoc]:
    """Discover a meeting TDoc list XLSX from the FTP path and parse its rows."""
    base_url = _normalize_ftp_path(ftp_url)
    if not base_url.endswith("/"):
        base_url += "/"

    with ScraperClient() as client:
        for subfolder in ["", "docs/", "tdoc/"]:
            directory_url = urljoin("https://www.3gpp.org/ftp/", base_url + subfolder)
            try:
                html = client.get_text(directory_url)
            except Exception:
                continue

            hrefs = _extract_hrefs(html)
            candidate = _find_tdoc_list_filename(hrefs)
            if not candidate:
                continue

            file_url = urljoin(directory_url, candidate)
            xlsx_bytes = client.get_bytes(file_url)
            records = read_tdoc_sheet(xlsx_bytes)
            return [
                TDoc(
                    tdoc_id=row["tdoc"],
                    title=row.get("title", ""),
                    meeting=meeting,
                    url=file_url,
                )
                for row in records
            ]

    return []
