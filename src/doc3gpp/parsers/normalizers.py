from __future__ import annotations

import re
from urllib.parse import urljoin


#: Canonical 3GPP FTP root. Used as the join point when rebuilding full
#: URLs from relative paths stored in the database (see
#: :func:`build_ftp_url`).
FTP_BASE_URL = "https://www.3gpp.org/ftp/"


def clean_whitespace(value: str) -> str:
    """Normalize repeated whitespace in extracted text."""

    return " ".join(value.split())


def normalize_ftp_path(path: str) -> str:
    """Normalize a meeting FTP path to a canonical relative path.

    Strips any leading ``ftp://`` / ``ftp:/`` prefix, the
    ``https://www.3gpp.org/ftp/`` (and ``http://``) variant of the FTP
    root, leading slashes, and collapses repeated slashes. Backslashes
    are converted to forward slashes. The result is the path the
    database stores as ``ftp_url`` (matching the ``meetings.ftp_url``
    convention).

    The 3GPP FTP server is case-insensitive across the whole path
    (probed: upper/lower root, deep dirs, filename, and
    ``Joint_Meetings`` all serve identical bytes), but the DB joins
    sidecars to ``tdocs`` on exact ``ftp_url`` equality — so the whole
    relative path is lowercased. Without this, the ``tdocs`` row
    (XLSX hyperlink spelling, e.g. ``TSG_RAN/WG5_...``) and the
    parsed sidecar rows (serving-URL spelling, e.g.
    ``tsg_ran/wg5_...``) diverge and ``tdoc show --tdoc`` misses the
    cover/TTCN/extract rows.
    """
    normalized = path.strip().replace("\\", "/")
    normalized = re.sub(r"^https?://www\.3gpp\.org/ftp/", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"^ftp:/+", "", normalized, flags=re.IGNORECASE)
    normalized = normalized.lstrip("/")
    normalized = re.sub(r"/{2,}", "/", normalized)
    normalized = normalized.lower()
    return normalized


def build_ftp_url(relative: str) -> str:
    """Reconstruct a full 3GPP FTP URL from a relative path.

    Inverse of :func:`normalize_ftp_path`: prepends the canonical FTP
    root and ensures exactly one trailing slash before the relative
    path. The result is safe to hand to ``httpx``/the scraper layer.
    Stored paths are lowercase (the 3GPP FTP server is
    case-insensitive across the whole path), so the rebuilt URL
    carries that canonical form.
    """
    cleaned = relative.strip().lstrip("/")
    return urljoin(FTP_BASE_URL, cleaned)