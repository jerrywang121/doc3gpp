"""Thin re-export shim for the modular CR parser package.

The original :mod:`doc3gpp.parsers.cr_parser` module (1,059 lines) is
split into the :mod:`doc3gpp.parsers.cr` subpackage. This module
preserves the legacy public API so existing callers and tests continue
to work without changes:

* :func:`parse_cr_details` — top-level entry point.
* :func:`extract_docx_from_zip` — pulls a single ``(filename, bytes)``
  pair out of a CR zip.
* :func:`derive_tech_from_spec` — derives a 5G/LTE/etc. label from a
  spec number.
* :class:`CRHeaderMissingError` — raised when the input lacks the
  ``3GPP TSG-`` header.
* Private helpers (``_TDOC_HEADER_PATTERN``, ``_collapse_whitespace``,
  ``_remove_markdown_formatting``, ``_search_pattern_in_lines``,
  ``_COVER_FIELDS``) are also re-exported because the service layer
  and tests rely on them.
"""

from __future__ import annotations

from doc3gpp.models.tdoc_cr import TDocCRParseResult
from doc3gpp.parsers.cr.helpers import (
    _COVER_FIELDS,
    _collapse_whitespace,
    _remove_markdown_formatting,
    _search_pattern_in_lines,
    derive_tech_from_spec,
    extract_docx_from_zip,
)
from doc3gpp.parsers.cr.header import (
    CRHeaderMissingError,
    _TDOC_HEADER_PATTERN,
)
from doc3gpp.parsers.tdoc_parsers import build_default_registry


def parse_cr_details(
    markdown: str,
    *,
    tdoc_id: str,
    max_text_length: int = 0,
    full: bool = False,
) -> TDocCRParseResult:
    """Parse a CR markdown body into a :class:`TDocCRParseResult`.

    Thin wrapper around the default :class:`TDocParserRegistry` for
    backwards compatibility. TTCN routing is handled by the registry
    (specific parsers are registered before generic ones). The
    returned :class:`TDocCRParseResult` bundles the slim cover-page
    fields on ``.cover`` with the optional ``.ttcn`` sidecar (populated
    when the parser detected a TTCN CR).
    """
    registry = build_default_registry()
    parser = registry.resolve(tdoc_id, tdoc_type="CR")
    return parser.parse(
        markdown,
        tdoc_id=tdoc_id,
        max_text_length=max_text_length,
        full=full,
    )


__all__ = [
    "CRHeaderMissingError",
    "_COVER_FIELDS",
    "_TDOC_HEADER_PATTERN",
    "_collapse_whitespace",
    "_remove_markdown_formatting",
    "_search_pattern_in_lines",
    "derive_tech_from_spec",
    "extract_docx_from_zip",
    "parse_cr_details",
]