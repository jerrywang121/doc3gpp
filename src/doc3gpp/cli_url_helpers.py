"""URL-shape helpers shared by the CLI dispatcher and the auto-sync glue.

The CLI's ``--from-url`` direct-parse path needs a tiny set of
predicate functions to decide whether a URL points at a 3GPP FTP
asset, whether it ends in a known file extension, and whether it
trails with a slash (a folder marker). The same predicates are
needed from :mod:`doc3gpp.cli_auto_sync` so the auto-sync helper can
derive TDoc-id candidates from a URL without dragging the full
parser module into its import surface.

This module is the single home for that surface so both call sites
import one consistent set of names:

* :func:`is_3gpp_ftp_url` — re-exported from
  :func:`doc3gpp.parsers.direct_extractor.is_3gpp_ftp_url` so the
  parser implementation stays in one place.
* :func:`_looks_like_3gpp_file_url` — true when the URL ends with a
  known document extension.
* :func:`_looks_like_3gpp_folder_url` — true when the URL ends with
  a slash (folder marker).
"""

from __future__ import annotations

from doc3gpp.parsers.direct_extractor import is_3gpp_ftp_url

__all__ = [
    "_looks_like_3gpp_file_url",
    "_looks_like_3gpp_folder_url",
    "is_3gpp_ftp_url",
]


def _looks_like_3gpp_folder_url(url: str) -> bool:
    """Return ``True`` when ``url`` is unambiguously a folder path."""
    return url.endswith("/")


def _looks_like_3gpp_file_url(url: str) -> bool:
    """Return ``True`` when ``url`` ends with a known file extension."""
    return url.lower().endswith((".docx", ".zip"))