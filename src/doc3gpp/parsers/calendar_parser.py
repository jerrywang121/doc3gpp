from __future__ import annotations

import logging
import re
from datetime import date

from bs4 import BeautifulSoup

from doc3gpp.models.meeting import Meeting


logger = logging.getLogger(__name__)

_DATE_PATTERN = "%Y-%m-%d"


def parse_3gpp_calendar(html: str) -> list[Meeting]:
    """Parse meeting calendar rows from 3GPP DynaReport HTML.

    The 3GPP meeting calendar page contains a table of meetings with
    columns for name, title, location, start/end dates and FTP document links.
    This parser extracts only valid meeting rows and converts them into
    Meeting dataclass instances.

    Robustness:
      * Logs (and skips) the whole row when the upstream layout has fewer
        cells than expected, when a ``start_date``/``end_date`` is missing or
        not ISO-parseable, when the start date is after the end date, or
        when the FTP path cannot be decoded.
      * Logs a warning when the page contains content but no meeting table
        is found, so layout regressions surface in CI instead of presenting
        as a silent ``0 rows stored``.
    """

    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", {"class": "meetings"})
    if table is None:
        if _looks_like_meetings_page(soup):
            logger.warning(
                "3GPP calendar page has content but no <table class='meetings'>; "
                "the upstream layout may have changed and no meetings were extracted."
            )
        return []

    body = table.find("tbody") or table
    meetings_map: dict[int, Meeting] = {}

    for row in body.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 6:
            logger.debug("Skipping meeting row with %s cells (expected >=6)", len(cells))
            continue

        try:
            meeting = _row_to_meeting(cells)
        except _SkipRow as exc:
            logger.warning("Skipping malformed meeting row: %s", exc)
            continue

        if meeting is None:
            continue
        meetings_map[meeting.meeting_id] = meeting

    return list(meetings_map.values())


def _row_to_meeting(cells) -> Meeting | None:
    """Build a Meeting from the cell list, returning None to skip the row."""

    mtg_link = _first_href(cells[0])
    meeting_id = _extract_meeting_id(mtg_link)
    if meeting_id is None:
        raise _SkipRow("could not extract meeting id from first cell link")

    name = _normalize_text(cells[0].get_text())
    title = _normalize_text(cells[1].get_text())
    if _is_cancelled(title):
        return None

    location = _normalize_text(cells[2].get_text())
    start_date = _parse_date(_normalize_text(cells[3].get_text()))
    end_date = _parse_date(_normalize_text(cells[4].get_text()))
    if start_date is None or end_date is None:
        raise _SkipRow(
            f"unparseable date(s) start={cells[3].get_text()!r} end={cells[4].get_text()!r}"
        )
    if start_date > end_date:
        raise _SkipRow(
            f"start_date {start_date.isoformat()} is after end_date {end_date.isoformat()}"
        )

    try:
        ftp_url = _extract_ftp_path(_first_href(cells[5]))
    except Exception as exc:
        logger.debug("Failed to extract ftp_url from cell 5 (%s); trying cell 8", exc)
        ftp_url = None
    if ftp_url is None and len(cells) > 8:
        try:
            ftp_url = _extract_ftp_path(_first_href(cells[8]))
        except Exception as exc:
            logger.debug("Failed to extract ftp_url from cell 8 (%s)", exc)
            ftp_url = None

    start_doc, end_doc = _extract_doc_range(cells[5].get_text(" "))

    return Meeting(
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


def _extract_doc_range(doc_text: str) -> tuple[str | None, str | None]:
    """Pull ``start_doc`` and ``end_doc`` values from the docs cell text.

    Returns ``(None, None)`` when the cell has no document range. A bare
    ``-`` placeholder is treated as missing and rendered as ``None``.
    """
    cleaned = _normalize_text(doc_text).replace("full document list", "").strip()
    if not cleaned:
        return None, None
    parts = [p.strip() for p in cleaned.split(" - ", maxsplit=1)]
    if len(parts) != 2:
        return None, None
    start_doc = parts[0] if parts[0] != "-" else None
    end_doc = parts[1] if parts[1] != "-" else None
    return start_doc, end_doc


def _is_cancelled(title: str) -> bool:
    """Return True if a meeting title indicates the meeting was cancelled.

    Matches trailing ``CANCELLED`` (with optional punctuation), prefixes,
    and mid-sentence variants such as ``"Cancelled: …"`` or ``"… — Cancelled"``.
    """
    return "CANCELLED" in title.upper()


def _looks_like_meetings_page(soup: BeautifulSoup) -> bool:
    """Heuristic: does the soup look like a populated meetings page?

    Used to distinguish "no table because the upstream moved the layout" from
    "no table because the page is genuinely empty/unparseable".
    """
    if soup.title and soup.title.string and soup.title.string.strip():
        return True
    return bool(soup.find_all(["h1", "h2", "h3", "p", "table"]))


class _SkipRow(Exception):
    """Internal sentinel raised by row helpers to drop a malformed row."""


def _first_href(cell) -> str | None:
    """Return the first href value from an anchor element in a table cell."""
    link = cell.find("a")
    if link and link.has_attr("href"):
        return str(link["href"])
    return None


def _extract_meeting_id(href: str | None) -> int | None:
    """Extract the numeric meeting ID from a 3GPP meeting href."""
    if not href:
        return None
    match = re.search(r"MtgId=(\d+)", href)
    if match is None:
        return None
    return int(match.group(1))


def _extract_ftp_path(href: str | None) -> str | None:
    """Extract the FTP path from a document link href."""
    if not href:
        return None
    match = re.search(r"[\\/]+ftp[\\/]+(.+)", href, flags=re.IGNORECASE)
    if match is None:
        return None
    path = match.group(1).replace("\\", "/")
    path = re.sub(r"/{2,}", "/", path)
    return path


def _parse_date(text: str) -> date | None:
    """Normalize a date string and convert it to a date object.

    Returns ``None`` when the input is empty or not ISO-parseable so the
    caller can skip the row instead of aborting the whole sync.
    """
    normalized = text.strip()
    if not normalized:
        return None
    normalized = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]", "-", normalized)
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        logger.debug("Could not parse date string %r", text)
        return None


def _normalize_text(text: str) -> str:
    """Collapse internal whitespace runs (including newlines/tabs) into a single space."""
    return re.sub(r"\s+", " ", text).strip()
