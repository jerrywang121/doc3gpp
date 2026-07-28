"""CR header exception, sniffing, and shared patterns."""

from __future__ import annotations

import re


class CRHeaderMissingError(ValueError):
    """Raised when input lacks the ``3GPP TSG-`` cover-page header.

    Inherits from :class:`ValueError` so callers that catch the broad
    class still handle it correctly. The error message includes the
    first 100 chars of the input so a downstream operator can
    diagnose the upstream document without re-reading the file.
    """

    def __init__(self, message: str, *, snippet: str | None = None) -> None:
        if snippet is not None:
            message = f"{message} (input starts with: {snippet!r})"
        super().__init__(message)


_HEADER_PATTERN = re.compile(r"3GPP\s+TSG", re.IGNORECASE)
# Union: ``R5-227476`` (dash) AND ``R5s260009`` / ``R5w260176`` (single
# letter) AND ``C6-250028`` (dash) AND RAN4's 7-digit ``R4-2607922``.
# Matches in the document header near the top of the markdown.
_TDOC_HEADER_PATTERN = re.compile(r"([RSC][1-6](?:[-sw])\d{6,7})", re.IGNORECASE)
# Email-meeting TTCN pattern. Overview / corrections are only parsed
# for TDocs matching this shape.
_TTCN_TDOC_PATTERN = re.compile(r"R5s\d{6}", re.IGNORECASE)


def _collapse_whitespace(text: str) -> str:
    """Collapse every whitespace run to a single space and ``strip()``.

    Used to derive a compact header from the markdown before running
    the ``3GPP TSG-`` sniff — markdown rendering scatters spaces and
    line breaks across the cover-page banner and we want a single
    regex against a single line of text.
    """
    return re.sub(r"\s+", " ", text).strip()


def is_cr_header_present(markdown: str) -> tuple[bool, str]:
    """Return whether the first three lines contain a ``3GPP TSG-`` header.

    Returns:
        ``(present, header_blob)`` where ``header_blob`` is the
        collapsed first-three-line text.
    """
    lines = markdown.splitlines()
    if not lines:
        return False, ""
    header_blob = _collapse_whitespace("\n".join(lines[:3]))
    return bool(_HEADER_PATTERN.search(header_blob)), header_blob


def is_ttcn_tdoc(tdoc_id: str) -> bool:
    """Return ``True`` iff ``tdoc_id`` looks like a TTCN email-meeting id."""
    return bool(_TTCN_TDOC_PATTERN.fullmatch(tdoc_id))
