"""Parser for 3GPP FTP directory listings.

Extracts auxiliary TDoc files (revisions, reviews, support docs) from a
3GPP FTP directory listing page. The parser is pure: it takes the raw
HTML and a set of known TDoc IDs, and returns a list of
:class:`TDocFile` domain objects. URL construction and HTTP retrieval
live in :mod:`doc3gpp.scraping.ftp_source`.

The 3GPP FTP server renders directory listings as an ASP.NET page. File
links carry the ``class="file"`` attribute; subfolder links do not, so
the parser filters on that class to skip ``Inbox/``, ``Docs/``,
``Tdocs/`` and ``Review/`` navigation entries.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from doc3gpp.models.tdoc_file import (
    TDocFile,
    TDocFileTypeRevision,
    TDocFileTypeReview,
    TDocFileTypeSupport,
)

_RE_REVISION_SUFFIX = re.compile(r"^r\d+$", re.IGNORECASE)
_RE_REVIEW_SUFFIX = re.compile(r"^_MCC160Comments(_r\d+)?$", re.IGNORECASE)
_ZIP_SUFFIX = ".zip"


def classify_tdoc_filename(
    filename: str,
    tdoc_ids: Iterable[str],
) -> tuple[str, str] | None:
    """Classify ``filename`` as an auxiliary file for a known TDoc.

    The match logic finds the longest known TDoc ID that prefixes the
    filename (longest-first iteration disambiguates cases like
    ``R5s260001`` vs. ``R5s2600012``) and inspects the remainder to
    determine the file type.

    Returns a ``(tdoc_id, file_type)`` tuple for a recognized filename,
    or ``None`` if the file is:

    - not a ``.zip`` file,
    - the base TDoc itself (e.g. ``R5s260001.zip``),
    - related to a TDoc ID not in ``tdoc_ids``, or
    - has a suffix that does not match any of the recognised patterns.
    """
    if not filename:
        return None
    if not filename.lower().endswith(_ZIP_SUFFIX):
        return None

    name = filename[: -len(_ZIP_SUFFIX)]
    if not name:
        return None

    name_lower = name.lower()
    matching_id: str | None = None
    suffix = ""
    for tid in sorted({tid for tid in tdoc_ids if tid}, key=len, reverse=True):
        tid_lower = tid.lower()
        if not name_lower.startswith(tid_lower):
            continue
        suffix = name[len(tid):]
        if not suffix:
            return None
        matching_id = tid
        break

    if matching_id is None:
        return None

    if _RE_REVISION_SUFFIX.match(suffix):
        return matching_id, TDocFileTypeRevision
    if _RE_REVIEW_SUFFIX.match(suffix):
        return matching_id, TDocFileTypeReview
    if suffix.startswith("_"):
        return matching_id, TDocFileTypeSupport
    return None


def parse_tdoc_files_from_listing(
    html: str,
    base_url: str,
    tdoc_ids: Iterable[str],
) -> list[TDocFile]:
    """Extract :class:`TDocFile` records from a 3GPP FTP directory listing.

    Args:
        html: Raw HTML of the directory listing page returned by
            ``https://www.3gpp.org/ftp/.../``.
        base_url: Fully-qualified base URL of the directory being listed
            (used to resolve relative hrefs into absolute URLs).
        tdoc_ids: Iterable of TDoc IDs known to the local database.
            Files that do not start with any of these IDs are dropped.

    Returns:
        A list of :class:`TDocFile` records, one per recognised file.
        Unrecognised files, subfolder navigation entries, and the base
        TDoc ZIP for each TDoc are silently skipped.
    """
    if not tdoc_ids:
        return []

    soup = BeautifulSoup(html, "lxml")
    results: list[TDocFile] = []
    for anchor in soup.find_all("a", class_="file", href=True):
        href = anchor["href"]
        filename = anchor.get_text(strip=True) or href.rsplit("/", 1)[-1]
        classification = classify_tdoc_filename(filename, tdoc_ids)
        if classification is None:
            continue
        tdoc_id, file_type = classification
        results.append(
            TDocFile(
                tdoc_id=tdoc_id,
                type=file_type,
                file=filename,
                url=urljoin(base_url, href),
            )
        )
    return results
