"""CR header exception, sniffing, and shared patterns."""

from __future__ import annotations

import re


class CRHeaderMissingError(ValueError):
    """Raised when input lacks the structural CR cover-page markers.

    Inherits from :class:`ValueError` so callers that catch the broad
    class still handle it correctly. The error message includes the
    first 100 chars of the input so a downstream operator can
    diagnose the upstream document without re-reading the file.
    """

    def __init__(self, message: str, *, snippet: str | None = None) -> None:
        if snippet is not None:
            message = f"{message} (input starts with: {snippet!r})"
        super().__init__(message)


# Union: ``R5-227476`` (dash) AND ``R5s260009`` / ``R5w260176`` (single
# letter) AND ``C6-250028`` (dash) AND RAN4's 7-digit ``R4-2607922``.
# Matches in the document header near the top of the markdown.
_TDOC_HEADER_PATTERN = re.compile(r"([RSC][1-6](?:[-sw])\d{6,7})", re.IGNORECASE)
# Email-meeting TTCN pattern. Overview / corrections are only parsed
# for TDocs matching this shape.
_TTCN_TDOC_PATTERN = re.compile(r"R5s\d{6}", re.IGNORECASE)

# A single-line cover-page marker. The line ``| CHANGE REQUEST |`` is
# the universal 3GPP CR-form header cell — every real 3GPP CR cover
# sheet renders it (often inside a one-cell row that is a sibling of
# the spec/CR/rev/version row), and it never appears in non-CR
# markdown that the parser should accept.
_CHANGE_REQUEST_LINE_RE = re.compile(r"\|\s*CHANGE\s+REQUEST\s*\|", re.IGNORECASE)

# The structural cover-page row that holds spec / CR / rev / version.
# This is the row that uniquely identifies a 3GPP CR-form document
# (it does not appear in non-CR content). Each listed cell is required
# as a marker; values may be empty (e.g. a draft template's
# ``|  | 23.369 | CR |  | rev |  | Current version: |  |``), and any
# number of empty cells may sit between the listed cells (e.g.
# ``|  | 23.369 |  |  | CR |  |  | 0171 | rev | 10 |  | Current
# version: |  |  | 19.3.0 |``). Groups: 1=spec, 2=cr_num, 3=rev_num,
# 4=version. ``DOTALL`` lets the row span line breaks that some
# renderers insert inside a wide table.
_COVER_ROW_RE = re.compile(
    r"\|"
    r"(?:\s*[^|]*)?"
    r"\|\s*\b(\d{2}\.\d{3}(?:-\d)?)\b\s*\|"
    r"(?:\s*\|)*"
    r"\s*CR\s*\|"
    r"(?:\s*\|)*"
    r"\s*([^|]*?)\s*\|"
    r"(?:\s*\|)*"
    r"\s*rev\s*\|"
    r"(?:\s*\|)*"
    r"\s*([^|]*?)\s*\|"
    r"(?:\s*\|)*"
    r"\s*Current\s+version\s*:\s*\|"
    r"(?:\s*\|)*"
    r"\s*([^|]*?)\s*\|"
    r"(?:\s*\|)*",
    re.IGNORECASE | re.DOTALL,
)


def _collapse_whitespace(text: str) -> str:
    """Collapse every whitespace run to a single space and ``strip()``.

    Used to derive a compact header from the markdown before running
    the TTCN-layout sniff — markdown rendering scatters spaces and
    line breaks across the cover-page banner and we want a single
    regex against a single line of text.
    """
    return re.sub(r"\s+", " ", text).strip()


def is_cr_header_present(markdown: str) -> tuple[bool, str]:
    """Return whether ``markdown`` carries the structural CR cover-page markers.

    The document is treated as a 3GPP CR cover sheet when **both** of
    the following appear somewhere in the body:

    1. a ``| CHANGE REQUEST |`` line marker (the universal 3GPP
       CR-form header cell), and
    2. a cover-page row that names the spec (``NN.NNN`` /
       ``NN.NNN-N``), the literal ``CR`` cell, the literal ``rev``
       cell, and the literal ``Current version:`` cell. The
       ``cr_num`` / ``rev_num`` / ``version`` values may be empty
       (draft templates), and any number of empty cells may sit
       between the listed cells.

    This replaces the old "first three lines contain ``3GPP TSG-``"
    sniff, which rejected real SA WG2 CRs whose cover-sheet banner
    omits the literal ``3GPP TSG-`` token (e.g. ``SA WG2 Meeting
    S2#175    S2-2605184`` or ``3GPP SA WG2 Meeting #175    S2-
    2605497``). The structural check is 3GPP-CR-specific and does not
    fire on random markdown that happens to mention a meeting.

    Returns:
        ``(present, header_blob)`` where ``header_blob`` is the
        collapsed first-three-line text (kept for the caller's
        diagnostic snippet / TTCN-layout sniff; the gate itself is
        structural, not banner-based).
    """
    if not markdown:
        return False, ""
    lines = markdown.splitlines()
    header_blob = _collapse_whitespace("\n".join(lines[:3]))
    has_change_request = any(_CHANGE_REQUEST_LINE_RE.search(line) for line in lines)
    has_cover_row = _COVER_ROW_RE.search(markdown) is not None
    return has_change_request and has_cover_row, header_blob


def is_ttcn_tdoc(tdoc_id: str) -> bool:
    """Return ``True`` iff ``tdoc_id`` looks like a TTCN email-meeting id."""
    return bool(_TTCN_TDOC_PATTERN.fullmatch(tdoc_id))
