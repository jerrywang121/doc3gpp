from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

from doc3gpp.models.meeting import Meeting


_DATE_PATTERN = "%Y-%m-%d"


def parse_3gpp_calendar(html: str) -> list[Meeting]:
    """Parse meeting calendar rows from 3GPP DynaReport HTML."""

    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", {"class": "meetings"})
    if table is None:
        return []

    body = table.find("tbody") or table
    meetings: list[Meeting] = []

    for row in body.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 6:
            continue

        mtg_link = _first_href(cells[0])
        meeting_id = _extract_meeting_id(mtg_link)
        if meeting_id is None:
            continue

        name = cells[0].get_text(strip=True)
        title = cells[1].get_text(strip=True)
        if title.upper().endswith("CANCELLED"):
            continue

        location = cells[2].get_text(strip=True)
        start_date = _parse_date(cells[3].get_text(strip=True))
        end_date = _parse_date(cells[4].get_text(strip=True))

        ftp_url = _extract_ftp_path(_first_href(cells[5]))
        if ftp_url is None and len(cells) > 8:
            ftp_url = _extract_ftp_path(_first_href(cells[8]))

        start_doc = None
        end_doc = None
        doc_text = cells[5].get_text(" ", strip=True).replace("full document list", "").strip()
        if doc_text:
            parts = [p.strip() for p in doc_text.split(" - ", maxsplit=1)]
            if len(parts) == 2:
                start_doc = parts[0] if parts[0] != "-" else None
                end_doc = parts[1] if parts[1] != "-" else None

        meetings.append(
            Meeting(
                meeting_id=meeting_id,
                name=name,
                title=title,
                location=location,
                start_date=start_date,
                end_date=end_date,
                ftp_url=ftp_url,
                start_doc=start_doc,
                end_doc=end_doc,
            )
        )

    return meetings


def _first_href(cell) -> str | None:
    link = cell.find("a")
    if link and link.has_attr("href"):
        return str(link["href"])
    return None


def _extract_meeting_id(href: str | None) -> int | None:
    if not href:
        return None
    match = re.search(r"MtgId=(\d+)", href)
    if match is None:
        return None
    return int(match.group(1))


def _extract_ftp_path(href: str | None) -> str | None:
    if not href:
        return None
    match = re.search(r"[\\/]+ftp[\\/]+(.+)", href, flags=re.IGNORECASE)
    if match is None:
        return None
    path = match.group(1).replace("\\", "/")
    path = re.sub(r"/{2,}", "/", path)
    return path


def _parse_date(text: str) -> date:
    normalized = text.strip()
    normalized = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]", "-", normalized)
    return date.fromisoformat(normalized)
