"""Shared header detection for LS TDoc parsers.

The 3GPP LS template carries a recognisable header shape — a tabbed
``Meeting`` / ``TDoc`` first line, a ``Title:`` cell whose value
starts with ``LS on`` (case-insensitive), and at least one of
``Source:`` / ``To:`` / ``Cc:`` cells. Variants (IEEE, ETSI, …) keep
their own header detection but share the same error contract via
:class:`LSHeaderMissingError`.

The detection works on raw markdown because the LS template is
already markdown-shaped when the converter hands it to the parser.
"""

from __future__ import annotations

import re

_LS_TITLE_PREFIX = re.compile(r"^\s*LS\s+on\b", re.IGNORECASE)
_FIRST_LINE_RE = re.compile(
    r"^3GPP\b.*\bMeeting\b\s*[#\w]",
    re.IGNORECASE,
)
_TDOC_ID_RE = re.compile(r"\bR[1-9]-\d{6}\b|\bR[1-9]s\d{6}\b", re.IGNORECASE)
_ANY_OF_SOURCE_TO_CC = re.compile(r"^(?:Source|To|Cc)\s*:", re.IGNORECASE)


class LSHeaderMissingError(ValueError):
    """Raised when an LS-marked markdown body lacks the LS header shape."""

    def __init__(self, message: str, snippet: str = "") -> None:
        super().__init__(message)
        self.snippet = snippet


def is_ls_header_present(markdown: str) -> tuple[bool, str]:
    """Return ``(present, header_blob)``.

    ``header_blob`` is the leading 100-line slice that the detector
    scanned — useful for error messages and the
    :class:`LSHeaderMissingError` snippet.
    """
    if not markdown:
        return False, ""

    lines = markdown.splitlines()
    head = lines[:100]
    blob = "\n".join(head)

    first_match = False
    for line in head:
        if _FIRST_LINE_RE.search(line):
            # An LS header always pairs the meeting reference with a
            # 3GPP TDoc id (``R5-…`` / ``R5s…``). Tab-separated
            # markdown carries the id verbatim; the docx→md converter
            # emits the same id bare on the same line or shortly after.
            if _TDOC_ID_RE.search(line):
                first_match = True
                break

    title_match = False
    for line in head:
        if line.startswith(("Title:", "Title :")) and _LS_TITLE_PREFIX.search(
            line.split(":", 1)[1]
        ):
            title_match = True
            break

    any_destination = any(_ANY_OF_SOURCE_TO_CC.match(line) for line in head)

    return (first_match and title_match and any_destination), blob


__all__ = ["LSHeaderMissingError", "is_ls_header_present"]
