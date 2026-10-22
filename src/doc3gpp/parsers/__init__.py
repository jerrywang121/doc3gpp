"""HTML and document parsing helpers."""

from doc3gpp.parsers.calendar_parser import parse_3gpp_calendar
from doc3gpp.parsers.cr_parser import (
    CRHeaderMissingError,
    TDocZipLimitError,
    TDocZipValidationError,
    build_markdown_archive,
    derive_tech_from_spec,
    document_id_for,
    enumerate_docx_members,
    ephemeral_document_id_for,
    extract_docx_from_zip,
    parse_cr_details,
    read_markdown_archive,
)
from doc3gpp.parsers.wi_parser import parse_3gpp_wis

__all__ = [
    "CRHeaderMissingError",
    "TDocZipLimitError",
    "TDocZipValidationError",
    "build_markdown_archive",
    "derive_tech_from_spec",
    "document_id_for",
    "enumerate_docx_members",
    "ephemeral_document_id_for",
    "extract_docx_from_zip",
    "parse_3gpp_calendar",
    "parse_3gpp_wis",
    "parse_cr_details",
    "read_markdown_archive",
]
