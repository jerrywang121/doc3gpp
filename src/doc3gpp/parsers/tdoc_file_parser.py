"""Parser for 3GPP FTP directory listings.

Extracts auxiliary TDoc files (revisions, drafts, reviews, support docs) from a
3GPP FTP directory listing page. The parser is pure: it takes the raw
HTML and a set of known TDoc IDs, and returns a list of
:class:`TDocFile` domain objects. URL construction and HTTP retrieval
live in :mod:`doc3gpp.scraping.ftp_source`.

The 3GPP FTP server renders directory listings as an ASP.NET page. File
links carry the ``class="file"`` attribute; subfolder links do not, so
the parser filters on that class to skip ``Inbox/``, ``Docs/``,
``tdoc/`` and ``Review/`` navigation entries.

Each file row is rendered as a ``<tr>`` whose second ``<td>`` after the
checkbox/icon prefix carries the ``Last Modified`` column. That date is
parsed as ``YYYY/MM/DD HH:MM`` and attached to the resulting
:class:`TDocFile` as ``uploaded_date``. The date column is optional —
legacy listings may omit it, and the parser returns ``None`` rather than
dropping the file when the cell is missing or unparseable.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import date, datetime
from functools import lru_cache
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from doc3gpp.models.tdoc_file import (
    TDocFile,
    TDocFileTypeDraft,
    TDocFileTypeReview,
    TDocFileTypeRevision,
    TDocFileTypeSupport,
)
from doc3gpp.parsers.normalizers import normalize_ftp_path

logger = logging.getLogger(__name__)

_FILE_SUFFIXES = (".zip", ".doc", ".docx", ".ppt", ".pptx")
_FTP_DATE_FORMAT = "%Y/%m/%d %H:%M"


def classify_tdoc_filename(
    filename: str,
    tdoc_ids: Iterable[str],
    *,
    folder: str = "inbox",
) -> tuple[str, str] | None:
    """Match the first known ID anywhere in a filename and classify by root."""
    ids = tuple(sorted({tid for tid in tdoc_ids if tid}, key=lambda x: (-len(x), x)))
    return _classify(filename, ids, folder)


@lru_cache(maxsize=8)
def _id_pattern(ids: tuple[str, ...]) -> re.Pattern[str] | None:
    if not ids:
        return None
    return re.compile("(?:" + "|".join(map(re.escape, ids)) + r")(?!\d)", re.IGNORECASE)


def _classify(filename: str, ids: tuple[str, ...], folder: str) -> tuple[str, str] | None:
    if not filename.lower().endswith(_FILE_SUFFIXES):
        return None
    pattern = _id_pattern(ids)
    match = pattern.search(filename) if pattern else None
    if match is None:
        return None
    matching_id = next(tid for tid in ids if tid.casefold() == match.group().casefold())
    if filename.casefold() == f"{matching_id}.zip".casefold():
        return None
    if folder.casefold() == "review":
        kind = TDocFileTypeReview if "mcc160comment" in filename.casefold() else TDocFileTypeSupport
    else:
        kind = TDocFileTypeDraft if filename.casefold().startswith("draft") else TDocFileTypeRevision
    return matching_id, kind


def parse_tdoc_files_from_listing(
    html: str,
    base_url: str,
    tdoc_ids: Iterable[str],
    *,
    folder: str = "inbox",
    restrict_to_directory: bool = False,
) -> list[TDocFile]:
    """Extract :class:`TDocFile` records from a 3GPP FTP directory listing.

    Args:
        html: Raw HTML of the directory listing page returned by
            ``https://www.3gpp.org/ftp/.../``.
        base_url: Fully-qualified base URL of the directory being listed
            (used to resolve relative hrefs into absolute URLs).
        tdoc_ids: Iterable of TDoc IDs known to the local database.
            Files that do not contain any of these IDs are dropped.
        folder: Top-level meeting subtree used for classification.
        restrict_to_directory: Accept only direct, same-host children of base_url.

    Returns:
        A list of :class:`TDocFile` records, one per recognised file.
        Unrecognised files, subfolder navigation entries, and the base
        TDoc ZIP for each TDoc are silently skipped.
    """
    ids = tuple(sorted({tid for tid in tdoc_ids if tid}, key=lambda x: (-len(x), x)))
    if not ids:
        return []

    soup = BeautifulSoup(html, "lxml")
    results: list[TDocFile] = []
    for anchor in soup.find_all("a", class_="file", href=True):
        href = anchor["href"]
        filename = anchor.get_text(strip=True) or unquote(href.rsplit("/", 1)[-1])
        classification = _classify(filename, ids, folder)
        if classification is None:
            continue
        tdoc_id, file_type = classification
        absolute_url = urljoin(base_url, href)
        if restrict_to_directory and not _is_direct_child(absolute_url, base_url):
            continue
        results.append(
            TDocFile(
                tdoc_id=tdoc_id,
                type=file_type,
                file=filename,
                ftp_url=normalize_ftp_path(absolute_url),
                uploaded_date=_extract_uploaded_date(anchor),
            )
        )
    return results


def _is_direct_child(url: str, base_url: str) -> bool:
    parsed = urlsplit(url)
    current = urlsplit(base_url)
    if parsed.scheme != current.scheme or parsed.netloc.lower() != current.netloc.lower():
        return False
    if parsed.query or parsed.fragment:
        return False
    path, current_path = parsed.path, current.path
    while True:
        path, current_path = unquote(path), unquote(current_path)
        if "\\" in path or any(part in (".", "..") for part in path.split("/")):
            return False
        parent = current_path.rstrip("/") + "/"
        if not path.casefold().startswith(parent.casefold()):
            return False
        child = path[len(parent):].strip("/")
        if not child or "/" in child:
            return False
        if unquote(path) == path and unquote(current_path) == current_path:
            return True


def parse_tdoc_subfolders(html: str, base_url: str) -> list[str]:
    """Return direct, same-host folder children from a 3GPP listing."""
    soup = BeautifulSoup(html, "lxml")
    results: list[str] = []
    for anchor in soup.find_all("a", href=True):
        if "file" in (anchor.get("class") or []):
            continue
        href = str(anchor["href"])
        absolute = urljoin(base_url, href)
        if not _is_direct_child(absolute, base_url):
            continue
        child = unquote(urlsplit(absolute).path).rstrip("/").rsplit("/", 1)[-1]
        row = anchor.find_parent("tr")
        icon = row.find("img", class_="icon") if row else None
        has_folder_icon = icon is not None and str(icon.get("src", "")).endswith("?file=")
        if not href.endswith("/") and not has_folder_icon and "." in child:
            continue
        results.append(absolute.rstrip("/") + "/")
    return results


def _extract_uploaded_date(anchor) -> date | None:
    """Parse the ``Last Modified`` column from a 3GPP FTP listing row.

    The 3GPP FTP directory listing renders each file as a ``<tr>`` whose
    cells are: checkbox, icon, file link, ``Last Modified`` date, size.
    The anchor's parent ``<td>`` is the file-link cell; the date lives in
    the immediately following sibling ``<td>`` and is formatted as
    ``YYYY/MM/DD HH:MM`` (server-local time).

    Returns ``None`` when the anchor is not wrapped in a ``<td>`` (e.g.
    legacy or stripped listings), when the date cell is missing, or when
    the cell text does not match the expected format. A missing date
    must never cause an otherwise-valid file to be dropped — the
    ``file``, ``url``, ``tdoc_id`` and ``type`` fields are sufficient to
    persist the row.
    """
    parent_td = anchor.find_parent("td")
    if parent_td is None:
        return None
    date_td = parent_td.find_next_sibling("td")
    if date_td is None:
        return None
    text = date_td.get_text(strip=True)
    if not text:
        return None
    try:
        return datetime.strptime(text, _FTP_DATE_FORMAT).date()
    except ValueError:
        logger.debug("Could not parse uploaded_date %r from FTP listing", text)
        return None
