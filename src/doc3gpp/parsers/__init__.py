"""HTML and document parsing helpers."""

from doc3gpp.parsers.cr_parser import (
    CRHeaderMissingError,
    derive_tech_from_spec,
    extract_docx_from_zip,
    parse_cr_details,
)
from doc3gpp.parsers.calendar_parser import parse_3gpp_calendar
from doc3gpp.parsers.wi_parser import parse_3gpp_wis

__all__ = [
    "CRHeaderMissingError",
    "derive_tech_from_spec",
    "extract_docx_from_zip",
    "parse_3gpp_calendar",
    "parse_cr_details",
    "parse_3gpp_wis",
]
