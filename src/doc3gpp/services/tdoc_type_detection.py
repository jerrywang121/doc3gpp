"""Direct-mode TDoc type detection (spec section 6, steps 5 to 7).

Pure functions: the caller reads ``tdocs.type`` and supplies the main
document's Markdown lazily, so a known type never converts a document.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from doc3gpp.models.tdoc_content_families import (
    TDOC_CONTENT_STORES,
    TDocContentStore,
    store_for_type,
)
from doc3gpp.parsers.cr.header import is_cr_header_present

_WID_MARKER = "3GPP™ Work Item Description"
_LS_TITLE = re.compile(r"^\s*Title:\s+(Reply\s+){0,1}LS\s+on\s+")
_DISCUSSION_TITLE = re.compile(r"^\s*Title:\s+Discussion\s+on\s+")
_WID_LINES = 20
_TITLE_LINES = 10

_CR_STORE = next(store for store in TDOC_CONTENT_STORES if store.key == "cr")
_FAMILY_STORES = {store.key: store for store in TDOC_CONTENT_STORES if store.includes_main}

TYPE_UNDETERMINED = "type could not be determined"


@dataclass(frozen=True, slots=True)
class DirectTypeDecision:
    """Routing outcome for one direct-mode source."""

    kind: Literal["cr", "family", "skipped"]
    store: TDocContentStore | None
    reason: str | None = None


def _non_empty_lines(markdown: str, limit: int) -> list[str]:
    return [line for line in markdown.splitlines() if line.strip()][:limit]


def sniff_content_family(markdown: str) -> TDocContentStore | None:
    """Return the family store whose marker appears in the document's first lines."""
    if any(_WID_MARKER in line for line in _non_empty_lines(markdown, _WID_LINES)):
        return _FAMILY_STORES["wid"]
    title_lines = _non_empty_lines(markdown, _TITLE_LINES)
    if any(_LS_TITLE.match(line) for line in title_lines):
        return _FAMILY_STORES["ls"]
    if any(_DISCUSSION_TITLE.match(line) for line in title_lines):
        return _FAMILY_STORES["discussion"]
    return None


def _unsniffed_main() -> str:
    raise AssertionError("a known TDoc type never sniffs the main document")


def decide_known_type(tdoc_id: str, tdoc_type: str) -> DirectTypeDecision:
    """Route a TDoc whose ``tdocs.type`` is already known."""
    return decide_direct_type(tdoc_id, tdoc_type=tdoc_type, main_markdown=_unsniffed_main)


def decide_direct_type(
    tdoc_id: str,
    *,
    tdoc_type: str | None,
    main_markdown: Callable[[], str],
) -> DirectTypeDecision:
    """Decide the parse path for a direct-mode source.

    A known ``tdoc_type`` decides without calling ``main_markdown``. Otherwise
    the main document is sniffed for family markers, then for the CR cover.
    """
    if tdoc_type is not None and tdoc_type.strip(" \t\r\n"):
        store = store_for_type(tdoc_type)
        if store is None:
            return DirectTypeDecision(
                "skipped", None, f"type {tdoc_type!r} is not parsed",
            )
        if store.key == _CR_STORE.key:
            return DirectTypeDecision("cr", store)
        return DirectTypeDecision("family", store)

    markdown = main_markdown()
    family = sniff_content_family(markdown)
    if family is not None:
        return DirectTypeDecision("family", family)
    if is_cr_header_present(markdown)[0]:
        return DirectTypeDecision("cr", _CR_STORE)
    return DirectTypeDecision("skipped", None, TYPE_UNDETERMINED)
