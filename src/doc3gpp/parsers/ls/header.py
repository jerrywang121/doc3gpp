"""Shared header detection for LS TDoc parsers.

The 3GPP LS template carries a recognisable header shape — a
``Meeting`` reference line (``3GPP TSG-RAN WG5 Meeting #110`` or the
shorter ``SA WG2 Meeting #S2-176``), a ``Title:`` cell whose value
carries an ``LS`` token (``LS on …``, ``Reply LS on …``,
``[draft] Reply LS on …``, ``LS to … on …``, ``LSout on …``, …), and
at least one of ``Source:`` / ``To:`` / ``Cc:`` cells. Variants
(IEEE, ETSI, …) keep their own header detection but share the same
error contract via :class:`LSHeaderMissingError`.

The detection works on raw markdown because the LS template is
already markdown-shaped when the converter hands it to the parser.
"""

from __future__ import annotations

import re

_LS_TITLE_RE = re.compile(r"\bLS(?:out)?\b", re.IGNORECASE)
# First-line meeting reference: ``… Meeting …`` (with the meeting
# number / code). The tdoc id is deliberately ignored — real LS docs
# put the id on the same line, on a later line, or omit it.
_FIRST_LINE_RE = re.compile(
    r"\bMeeting\b\s*[#\w]",
    re.IGNORECASE,
)
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

    first_match = any(_FIRST_LINE_RE.search(line) for line in head)

    title_match = False
    for line in head:
        # The docx→md converter emits header cells as plain lines
        # (``Title:``) or as markdown headings (``# Title:``) when the
        # source document styles them as heading paragraphs.
        stripped = line.lstrip("#").strip()
        if stripped.startswith(("Title:", "Title :")) and _LS_TITLE_RE.search(
            stripped.split(":", 1)[1]
        ):
            title_match = True
            break

    any_destination = any(
        _ANY_OF_SOURCE_TO_CC.match(line.lstrip("#").strip()) for line in head
    )

    return (first_match and title_match and any_destination), blob


__all__ = ["LSHeaderMissingError", "is_ls_header_present"]
