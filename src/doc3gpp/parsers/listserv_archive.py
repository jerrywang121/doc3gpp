"""Pure HTML parsers for ETSI LISTSERV archive index and listing pages."""

from __future__ import annotations

import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from doc3gpp.models.listserv import (
    ArchivePage,
    DatePrecision,
    ListservMessage,
    normalize_list_id,
    normalize_tdoc_ids,
)
from doc3gpp.parsers.cr.header import _TDOC_HEADER_PATTERN


class ListservArchiveParseError(ValueError):
    """Raised when a LISTSERV archive index or listing page is malformed or requires login."""


_MONTH_NAMES: dict[str, int] = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

_ARCHIVE_LABEL_RE = re.compile(
    r"^(?P<month>[A-Za-z]+)\s+(?P<year>\d{4})"
    r"(?:\s*,?\s*(?:week\s*)?(?P<week>[1-5]))?$",
    re.IGNORECASE,
)
_ISO_PERIOD_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])(?:-W(?P<week>[1-5]))?$")
_WEEKLY_PERIOD_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])-W[1-5]$")
_MONTHLY_PERIOD_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")

_ISO_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HUMAN_DMY_DATE_ONLY_RE = re.compile(
    r"^(?:[A-Za-z]{3},\s*)?(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]+)\s+(?P<year>\d{4})$"
)
_HUMAN_MDY_DATE_ONLY_RE = re.compile(
    r"^(?:[A-Za-z]{3},\s*)?(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s*(?P<year>\d{4})$"
)
_ISO_DATETIME_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}")
_EMPTY_LISTING_NOTICE_RE = re.compile(
    r"\b(?:no\s+messages|0\s+messages|there\s+are\s+no\s+messages)\b",
    re.IGNORECASE,
)
_NEXT_LINK_TEXT_RE = re.compile(
    r"^\[?\s*(?:next(?:\s+page)?|page\s+\d+)\s*(?:[»>→]+)?\s*\]?$",
    re.IGNORECASE,
)


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _is_login_or_empty(html: str, soup: BeautifulSoup) -> bool:
    if not html or not html.strip():
        return True
    if soup.find("input", attrs={"type": re.compile(r"^password$", re.IGNORECASE)}) is not None:
        return True
    title_text = _collapse_ws(soup.title.get_text()) if soup.title else ""
    return "login required" in title_text.lower()


def _parse_period_label(label: str) -> str | None:
    cleaned = _collapse_ws(label)
    if not cleaned:
        return None
    iso_match = _ISO_PERIOD_RE.match(cleaned)
    if iso_match:
        return cleaned
    match = _ARCHIVE_LABEL_RE.match(cleaned)
    if not match:
        return None
    month_num = _MONTH_NAMES.get(match.group("month").lower())
    if month_num is None:
        return None
    year = int(match.group("year"))
    week = match.group("week")
    if week is not None:
        return f"{year:04d}-{month_num:02d}-W{int(week)}"
    return f"{year:04d}-{month_num:02d}"


def _is_usable_href(href: str) -> bool:
    stripped = href.strip()
    if not stripped or stripped.startswith("#"):
        return False
    lowered = stripped.lower()
    return not lowered.startswith(("javascript:", "mailto:"))


def parse_archive_index(html: str, base_url: str, list_id: str) -> list[ArchivePage]:
    """Parse an ETSI LISTSERV archive index page into ordered :class:`ArchivePage` items."""
    normalized_list = normalize_list_id(list_id)
    soup = BeautifulSoup(html, "lxml")
    if _is_login_or_empty(html, soup):
        raise ListservArchiveParseError("Archive index HTML is empty or requires login.")

    pages: list[ArchivePage] = []
    seen_urls: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        raw_href = str(anchor.get("href", ""))
        if not _is_usable_href(raw_href):
            continue
        period = _parse_period_label(anchor.get_text(" ", strip=True))
        if period is None:
            continue
        resolved_url = urljoin(base_url, raw_href.strip())
        if resolved_url in seen_urls:
            continue
        seen_urls.add(resolved_url)
        pages.append(
            ArchivePage(
                list_id=normalized_list,
                url=resolved_url,
                period=period,
            )
        )

    if not pages:
        raise ListservArchiveParseError("No recognizable LISTSERV archive links found in index.")
    return pages


def _classify_header_cell(text: str) -> str | None:
    cleaned = re.sub(r"[^a-z]+", " ", text.lower()).strip()
    if not cleaned:
        return None
    first_word = cleaned.split()[0]
    if first_word in ("subject", "from", "date"):
        return first_word
    return None


def _header_column_map(tr: Tag) -> dict[str, int] | None:
    cells = tr.find_all(["th", "td"], recursive=False)
    if len(cells) < 3:
        return None
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(cells):
        role = _classify_header_cell(cell.get_text(" ", strip=True))
        if role is not None and role not in mapping:
            mapping[role] = idx
    if {"subject", "from", "date"} <= mapping.keys():
        return mapping
    return None


def _find_listing_table(soup: BeautifulSoup) -> tuple[Tag, Tag, dict[str, int]]:
    for tr in soup.find_all("tr"):
        mapping = _header_column_map(tr)
        if mapping is not None:
            table = tr.find_parent("table")
            if isinstance(table, Tag):
                return table, tr, mapping
    raise ListservArchiveParseError(
        "Missing LISTSERV message listing table with Subject/From/Date headers."
    )


def _normalize_iso_datetime_str(raw: str) -> str:
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # Normalize optional whitespace before numeric offset and compact +HHMM/-HHMM
    text = re.sub(
        r"\s+([+-]\d{2}):?(\d{2})$",
        r"\1:\2",
        text,
    )
    text = re.sub(
        r"([+-]\d{2})(\d{2})$",
        r"\1:\2",
        text,
    )
    return text


def _parse_message_date(raw_text: str) -> tuple[datetime, DatePrecision]:
    cleaned = _collapse_ws(raw_text)
    if not cleaned:
        raise ListservArchiveParseError("Message row has an empty Date cell.")

    if _ISO_DATE_ONLY_RE.match(cleaned):
        try:
            d = date.fromisoformat(cleaned)
        except ValueError as exc:
            raise ListservArchiveParseError(f"Invalid ISO date {cleaned!r}: {exc}") from exc
        return datetime(d.year, d.month, d.day), "date"  # noqa: DTZ001

    dmy_match = _HUMAN_DMY_DATE_ONLY_RE.match(cleaned)
    if dmy_match:
        month_num = _MONTH_NAMES.get(dmy_match.group("month").lower())
        if month_num is not None:
            try:
                d = date(
                    int(dmy_match.group("year")),
                    month_num,
                    int(dmy_match.group("day")),
                )
            except ValueError as exc:
                raise ListservArchiveParseError(
                    f"Invalid calendar date {cleaned!r}: {exc}"
                ) from exc
            return datetime(d.year, d.month, d.day), "date"  # noqa: DTZ001

    mdy_match = _HUMAN_MDY_DATE_ONLY_RE.match(cleaned)
    if mdy_match:
        month_num = _MONTH_NAMES.get(mdy_match.group("month").lower())
        if month_num is not None:
            try:
                d = date(
                    int(mdy_match.group("year")),
                    month_num,
                    int(mdy_match.group("day")),
                )
            except ValueError as exc:
                raise ListservArchiveParseError(
                    f"Invalid calendar date {cleaned!r}: {exc}"
                ) from exc
            return datetime(d.year, d.month, d.day), "date"  # noqa: DTZ001

    if _ISO_DATETIME_PREFIX_RE.match(cleaned):
        iso_candidate = _normalize_iso_datetime_str(cleaned)
        try:
            return datetime.fromisoformat(iso_candidate), "datetime"
        except ValueError as exc:
            raise ListservArchiveParseError(
                f"Invalid ISO datetime {cleaned!r}: {exc}"
            ) from exc

    try:
        parsed = parsedate_to_datetime(cleaned)
        return parsed, "datetime"
    except (TypeError, ValueError):
        pass

    for fmt in ("%d %b %Y %H:%M:%S", "%d %b %Y %H:%M", "%d %B %Y %H:%M:%S", "%d %B %Y %H:%M"):
        try:
            return datetime.strptime(cleaned, fmt), "datetime"  # noqa: DTZ007
        except ValueError:
            continue

    raise ListservArchiveParseError(f"Unrecognized LISTSERV message date {cleaned!r}.")


def _derive_week(page_period: str, msg_dt: datetime) -> str:
    if _WEEKLY_PERIOD_RE.match(page_period):
        return page_period
    if _MONTHLY_PERIOD_RE.match(page_period):
        bucket = (msg_dt.day - 1) // 7 + 1
        return f"{msg_dt.year:04d}-{msg_dt.month:02d}-W{bucket}"
    raise ListservArchiveParseError(f"Unsupported archive page period {page_period!r}.")


def _extract_subject_tdocs(subject: str) -> tuple[str, ...]:
    return normalize_tdoc_ids([m.group(0) for m in _TDOC_HEADER_PATTERN.finditer(subject)])


def _is_next_page_anchor(anchor: Tag) -> bool:
    rel = anchor.get("rel")
    if isinstance(rel, list) and any(str(item).lower() == "next" for item in rel):
        return True
    if isinstance(rel, str) and rel.lower() == "next":
        return True
    text = _collapse_ws(anchor.get_text(" ", strip=True))
    return bool(_NEXT_LINK_TEXT_RE.match(text))


def _extract_next_urls(
    soup: BeautifulSoup,
    *,
    base_url: str,
    header_tr: Tag,
    message_urls: set[str],
) -> list[str]:
    next_urls: list[str] = []
    seen: set[str] = {base_url, *message_urls}
    for anchor in soup.find_all("a", href=True):
        if header_tr in anchor.parents:
            continue
        raw_href = str(anchor.get("href", ""))
        if not _is_usable_href(raw_href):
            continue
        if not _is_next_page_anchor(anchor):
            continue
        resolved = urljoin(base_url, raw_href.strip())
        if resolved in seen:
            continue
        seen.add(resolved)
        next_urls.append(resolved)
    return next_urls


def parse_archive_page(
    html: str, page: ArchivePage
) -> tuple[list[ListservMessage], list[str]]:
    """Parse a LISTSERV archive listing page into messages and deduplicated next-page URLs."""
    if not (_WEEKLY_PERIOD_RE.match(page.period) or _MONTHLY_PERIOD_RE.match(page.period)):
        raise ListservArchiveParseError(f"Unsupported archive page period {page.period!r}.")

    soup = BeautifulSoup(html, "lxml")
    if _is_login_or_empty(html, soup):
        raise ListservArchiveParseError("Archive listing HTML is empty or requires login.")

    listing_table, header_tr, col_map = _find_listing_table(soup)
    subject_col = col_map["subject"]
    from_col = col_map["from"]
    date_col = col_map["date"]
    min_cols = max(subject_col, from_col, date_col) + 1

    table_rows = [
        tr for tr in listing_table.find_all("tr") if tr.find_parent("table") == listing_table
    ]
    try:
        header_idx = table_rows.index(header_tr)
    except ValueError as exc:
        raise ListservArchiveParseError("Header row not located in listing table.") from exc

    messages: list[ListservMessage] = []
    message_urls: set[str] = set()

    for tr in table_rows[header_idx + 1 :]:
        cells = tr.find_all(["td", "th"], recursive=False)
        if not cells:
            continue
        row_text = _collapse_ws(tr.get_text(" ", strip=True))
        if not row_text:
            continue
        if _header_column_map(tr) is not None:
            continue

        if len(cells) < min_cols:
            if _EMPTY_LISTING_NOTICE_RE.search(row_text) and tr.find("a", href=True) is None:
                continue
            if all(_is_next_page_anchor(a) for a in tr.find_all("a", href=True)) and tr.find(
                "a", href=True
            ):
                continue
            raise ListservArchiveParseError(
                f"Malformed row in LISTSERV listing table: {row_text!r}"
            )

        subject_cell = cells[subject_col]
        from_cell = cells[from_col]
        date_cell = cells[date_col]

        usable_links = [
            a
            for a in subject_cell.find_all("a", href=True)
            if _is_usable_href(str(a.get("href", "")))
        ]
        link = next(
            (a for a in usable_links if _collapse_ws(a.get_text(" ", strip=True))),
            usable_links[0] if usable_links else None,
        )
        if link is None:
            if (
                len(cells) == 1
                and _EMPTY_LISTING_NOTICE_RE.search(row_text)
            ):
                continue
            raise ListservArchiveParseError(
                f"Message row is missing a valid subject detail link: {row_text!r}"
            )

        detail_url = urljoin(page.url, str(link["href"]).strip())
        subject = _collapse_ws(link.get_text(" ", strip=True))
        if not subject:
            subject = _collapse_ws(subject_cell.get_text(" ", strip=True))
        if not subject:
            raise ListservArchiveParseError("Message row has an empty Subject.")

        sender = _collapse_ws(from_cell.get_text(" ", strip=True))
        if not sender:
            raise ListservArchiveParseError("Message row has an empty From cell.")

        msg_dt, precision = _parse_message_date(date_cell.get_text(" ", strip=True))
        week = _derive_week(page.period, msg_dt)
        subject_tdocs = _extract_subject_tdocs(subject)

        messages.append(
            ListservMessage(
                url=detail_url,
                list_id=page.list_id,
                subject=subject,
                sender=sender,
                date=msg_dt,
                date_precision=precision,
                week=week,
                subject_tdocs=subject_tdocs,
            )
        )
        message_urls.add(detail_url)

    next_urls = _extract_next_urls(
        soup,
        base_url=page.url,
        header_tr=header_tr,
        message_urls=message_urls,
    )
    return messages, next_urls
