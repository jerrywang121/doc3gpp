from __future__ import annotations

from urllib.parse import urljoin

from doc3gpp.models.tdoc_identity import normalize_ftp_path

#: Canonical 3GPP FTP root. Used as the join point when rebuilding full
#: URLs from relative paths stored in the database (see
#: :func:`build_ftp_url`).
FTP_BASE_URL = "https://www.3gpp.org/ftp/"
__all__ = [
    "FTP_BASE_URL",
    "build_ftp_url",
    "clean_whitespace",
    "normalize_ftp_path",
]


def clean_whitespace(value: str) -> str:
    """Normalize repeated whitespace in extracted text."""

    return " ".join(value.split())


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