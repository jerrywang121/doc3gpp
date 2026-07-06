"""Markdown → structured CR-fields parser.

1. **Header sniff raises.** A markdown whose first three lines do not
   contain the literal ``3GPP TSG-`` substring raises
   :class:`CRHeaderMissingError`. The reference returned ``{}`` —
   silent and impossible to debug.
2. **TDoc id fallback.** The header regex accepts both
   ``R5-227476`` / ``C6-250028`` and the email-meeting ``R5s260009``
   forms (single character union at positions 3 of the prefix).
3. **TTCN-only overview / corrections.** ``parse_cr_details`` runs
   the TTCN-specific overview and corrections sub-parsers **only**
   when ``tdoc_id`` matches ``R5s\\d{6}``.
4. **Partial-cover-page tolerance.** When cover-page regexes miss
   some fields, we record the value as ``None`` and log a warning 
   rather than aborting the whole extraction (the reference returned
   ``False`` and dropped the whole document).
5. **Module-level regex compilation.** All patterns are ``re.compile``
   -d once at module import.

Public API:

* :func:`parse_cr_details` — the top-level entry point.
* :func:`extract_docx_from_zip` — pulls a single ``(filename, bytes)``
  pair out of a CR zip.
* :func:`derive_tech_from_spec` — derives a 5G/LTE/etc. label from a
  spec number.
* :class:`CRHeaderMissingError` — raised when the input lacks the
  ``3GPP TSG-`` header.
* :func:`_remove_markdown_formatting` and :func:`_collapse_whitespace`
  — exported with a leading underscore because they are shared
  utilities (Phase 6's service layer composes on them) but the
  convention is to treat them as internal.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import fields
from typing import Sequence

from doc3gpp.models.tdoc_cr import TDocCRDetails

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Regex constants (compiled once at import — the reference compiles them
# inside the parsing functions, which is noisier without being faster).
# ---------------------------------------------------------------------------


_HEADER_PATTERN = re.compile(r"3GPP\s+TSG-", re.IGNORECASE)
# Union: ``R5-227476`` (dash) AND ``R5s260009`` / ``R5w260176`` (single
# letter) AND ``C6-250028`` (dash). Matches in the document header
# near the top of the markdown.
_TDOC_HEADER_PATTERN = re.compile(r"([RSC][1-6](?:[-sw])\d{6})", re.IGNORECASE)
# Email-meeting TTCN pattern. Overview / corrections are only parsed
# for TDocs matching this shape.
_TTCN_TDOC_PATTERN = re.compile(r"R5s\d{6}", re.IGNORECASE)

# Cover-page row that holds spec / CR / rev / version. The leading
# ``(?:\|[^|]*\|){1,3}`` matches up to three blank-prefixed cells

_COVER_SPEC_RE = re.compile(
    r"\|\s*\b(\d{2}\.\d{3}(?:-\d)?)\b\s*\|\s*CR\s*\|"
    r"\s*([\d\w-]+)\s*\|\s*rev\s*\|\s*([\d\w-]+)\s*\|"
    r"\s*Current version:\s*\|\s*(\d{1,2}\.\d{1,2}\.\d{1,2})\s*\|"
)
_COVER_TITLE_RE = re.compile(r"\|\s*Title:\s*\|\s*(.*?)\s*\|")
_COVER_SOURCE_RE = re.compile(r"\|\s*Source to WG:\s*\|\s*(.*?)\s*\|")
_COVER_TSG_RE = re.compile(r"\|\s*Source to TSG:\s*\|\s*(.*?)\s*\|")
_COVER_WI_RE = re.compile(r"\|\s*Work item code:\s*\|\s*(.*?)\s*\|")
# Category + Release on the same line.
_COVER_CATREL_RE = re.compile(
    r"\|\s*Category:\s*\|\s*([^\s|]+)(?:\s*\|)+"
    r"\s*Release:(?:\s*\|)*\s*([^\s|]+)\s*\|"
)
_COVER_REASON_RE = re.compile(r"\|\s*Reason for change:(?:\s*\|)+\s*(.*?)\s*\|")
_COVER_CONSEQUENCES_RE = re.compile(
    r"\|\s*Consequences if not approved:(?:\s*\|)+\s*(.*?)\s*\|"
)
_COVER_CLAUSES_RE = re.compile(r"\|\s*Clauses affected:(?:\s*\|)+\s*(.*?)\s*\|")
_COVER_OTHER_RE = re.compile(r"\|\s*Other comments:(?:\s*\|)+\s*(.*?)\s*\|")
_COVER_REVHIST_RE = re.compile(
    r"\|\s*This CR's revision history:(?:\s*\|)+\s*(.*?)\s*\|"
)

# TTCN overview fields.
_OVERVIEW_TESTCASE_RE = re.compile(r"Test\s+Case:\s*(.+?)\s*$", re.IGNORECASE)
_OVERVIEW_UE_RE = re.compile(r"UE\s+used:\s*(.*)$", re.IGNORECASE)
_OVERVIEW_SS_RE = re.compile(r"System\s+Simulator\s+used:\s*(.*)$", re.IGNORECASE)
_OVERVIEW_ATS_RE = re.compile(
    r"(iwd-TTCN3-B\d{4}-\d{2}_D\d{2}wk\d{2})", re.IGNORECASE
)
_OVERVIEW_TESTSUITE_RE = re.compile(
    r"is\s+part\s+of\s+the\s+(.*?)\s+test\s+suite", re.IGNORECASE
)
_OVERVIEW_OVERVIEW_RE = re.compile(
    r"^(?:[#\d\.\s]*)?(Overview)\s*$", re.IGNORECASE
)
_OVERVIEW_TABLE_OF_CONTENTS_RE = re.compile(
    r"^(?:[#\d\.\s]*)?(Table of Contents)\s*$", re.IGNORECASE
)
_CORRECTIONS_REQUIRED_RE = re.compile(
    r"^(?:[#\d\.\s]*)?(Corrections\s+required)\s*$", re.IGNORECASE
)

# TTCN single-correction metadata table.
_SINGLE_CORRECTION_FUNCTION_RE = re.compile(
    r"^\s*\|\s*Function\s+name\s*\|\s*(.+?)\s*\|", re.IGNORECASE
)
_SINGLE_CORRECTION_REASON_RE = re.compile(
    r"^\s*\|\s*Reason\s+for\s+change\s*\|\s*(.+?)\s*\|", re.IGNORECASE
)
_SINGLE_CORRECTION_SUMMARY_RE = re.compile(
    r"^\s*\|\s*Summary\s+of\s+change\s*\|\s*(.+?)\s*\|", re.IGNORECASE
)
_SINGLE_CORRECTION_TTCN_RE = re.compile(
    r"^\s*\|\s*TTCN\s+module\s*\|\s*(.+?)\s*\|", re.IGNORECASE
)
_SINGLE_CORRECTION_MCC160_RE = re.compile(
    r"^\s*\|\s*MCC160\s+Comment\s*\|\s*(.+?)\s*\|", re.IGNORECASE
)
_SINGLE_CORRECTION_BEFORE_RE = re.compile(
    r"^\s*Before\s+change\s*:{0,1}\s*$", re.IGNORECASE
)
_SINGLE_CORRECTION_AFTER_RE = re.compile(
    r"^\s*After\s+change\s*:{0,1}\s*$", re.IGNORECASE
)
_SINGLE_CORRECTION_NEW_RE = re.compile(
    r"^\s*New\s+change\s*:{0,1}\s*$", re.IGNORECASE
)
_TABLE_CELL_RE = re.compile(r"^\s*\|\s*(.+?)\s*\|")
_FUNCTION_NAME_START_RE = re.compile(
    r"^\s*\|\s*Function\s+name\s*\|", re.IGNORECASE
)
_TTCN_MODULE_END_RE = re.compile(r"^\s*\|\s*TTCN\s+module\s*\|", re.IGNORECASE)
_MCC160_END_RE = re.compile(r"^\s*\|\s*MCC160\s+Comment\s*\|", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Text normalisation helpers
# ---------------------------------------------------------------------------


def _collapse_whitespace(text: str) -> str:
    """Collapse every whitespace run to a single space and ``strip()``.

    Used to derive a compact header from the markdown before running
    the ``3GPP TSG-`` sniff — markdown rendering scatters spaces and
    line breaks across the cover-page banner and we want a single
    regex against a single line of text.
    """
    return re.sub(r"\s+", " ", text).strip()


def _remove_markdown_formatting(text: str, flags: int = 0) -> str:
    """Strip common markdown formatting from ``text``.

    Default behaviour removes **bold**, *italic*, ``code``,
    ``[text](url)`` link markup, ``# heading`` prefixes, and ``>``
    blockquote markers. Bit flags can keep selected constructs in
    place; that is reserved for TTCN overview parsing where we want
    to retain heading lines (``MD_RM_KEEP_HEADERS``).

    This helper is re-exported with a leading underscore because the
    service layer (Phase 6) and other parsers occasionally need to
    test the formatting rules independently.
    """
    text = re.sub(r"(?<!\\)\*\*([^\s*].*?)(?<![\s\\])\*\*", r"\1", text)
    text = re.sub(r"(?<!\\)__([^\s*].*?)(?<![\s\\])__", r"\1", text)
    text = re.sub(r"(?<!\\)\*([^\s*].*?)(?<![\s\\])\*", r"\1", text)
    text = re.sub(r"(?<!\\)_([^\s*].*?)(?<![\s\\])_", r"\1", text)

    text = text.replace("\\_", "_")
    text = text.replace("\\*", "*")

    if not (flags & _MD_RM_KEEP_HEADERS):
        text = re.sub(r"^#+\s*(.*)", r"\1", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*={3,}\s*$", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*-{3,}\s*$", "", text, flags=re.MULTILINE)
    if not (flags & _MD_RM_KEEP_LINKS):
        text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
        text = re.sub(r"!\[([^\]]*)\]\([^\)]+\)", "", text)
    if not (flags & _MD_RM_KEEP_CODE):
        text = re.sub(r"`([^`]+)`", r"\1", text)
        text = re.sub(r"```[\s\S]*?```", "", text)
    if not (flags & _MD_RM_KEEP_BLOCKQUOTES):
        text = re.sub(r"^>\s*(.*)", r"\1", text, flags=re.MULTILINE)
    return text.strip()


# Bit flags for :func:`_remove_markdown_formatting`. Stored as
# module-level constants so callers can opt-in cleanly.
_MD_RM_KEEP_HEADERS = 0x01
_MD_RM_KEEP_BLOCKQUOTES = 0x02
_MD_RM_KEEP_CODE = 0x04
_MD_RM_KEEP_LINKS = 0x08
_MD_RM_KEEP_HORIZONTAL = 0x10


def derive_tech_from_spec(spec: str) -> str:
    """Derive a technology label from a 3GPP spec number.

    Examples:
        ``"38.523"`` → ``"5G"``
        ``"36.523"`` → ``"LTE"``
        ``"34.229"`` → ``"IMS"``
        ``""`` / ``None``-like → ``""``
    """
    spec = (spec or "").strip()
    if not spec:
        return ""
    m = re.match(r"(\d+\.\d+)", spec)
    if not m:
        return ""
    base = m.group(1)
    exact = {
        "34.229": "IMS",
        "37.579": "MCX",
        "36.579": "MCX",
        "37.571": "POS",
        "38.523": "5G",
        "36.523": "LTE",
    }
    return exact.get(base, "")


# ---------------------------------------------------------------------------
# Zip extraction
# ---------------------------------------------------------------------------


def extract_docx_from_zip(zip_bytes: bytes) -> tuple[str, bytes]:
    """Pull a single ``(filename, bytes)`` pair out of a CR zip.

    Mirrors the reference's behaviour:

    * Skips ``__MACOSX/`` resource-fork entries (Apple drops them on
      many macs; they are not real Word files).
    * Prefers ``.docx`` over ``.doc`` when both are present
      (only ``docx`` parses are supported).
    * Falls back to the lexicographically smaller filename when
      ``.docx`` is not present, matching the reference's stable
      ordering.

    Args:
        zip_bytes: Raw bytes of the CR zip.

    Returns:
        ``(filename_in_zip, bytes_of_word_doc)``.

    Raises:
        zipfile.BadZipFile: If the input is not a valid zip archive.
        ValueError: If the zip contains no ``.docx`` / ``.doc`` entry
            (only ``__MACOSX/``-style entries were present, or the
            archive was empty).
    """
    if not zip_bytes:
        raise ValueError("Cannot extract from empty zip bytes")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        word_docs = [
            name
            for name in zf.namelist()
            if name.lower().endswith((".docx", ".doc"))
            and not name.lower().startswith("__macosx/")
        ]
        if not word_docs:
            raise ValueError(
                "No .docx or .doc entries found in CR zip "
                "(after skipping __MACOSX/ resource fork)"
            )
        word_docs.sort(
            key=lambda n: (0 if n.lower().endswith(".docx") else 1, n.lower())
        )
        target = word_docs[0]
        return target, zf.read(target)


# ---------------------------------------------------------------------------
# Cover-page parser
# ---------------------------------------------------------------------------


def _year_from_tdoc_id(tdoc_id: str) -> int | None:
    """Derive meeting year from a TDoc id (``positions 3–4`` → ``20YY``)."""
    if len(tdoc_id) < 5:
        return None
    digits = tdoc_id[3:5]
    if not digits.isdigit():
        return None
    return 2000 + int(digits)


def _parse_cr_cover_page(
    lines: Sequence[str],
    *,
    max_text_length: int = 0,
    cover_page_end: int = 0,
) -> tuple[bool, dict[str, str], int]:
    """Extract cover-page fields from the markdown output.

    Each cover-page pattern is a labelled cell (``| Title: |``,
    ``| Source to TSG: |`` etc.); none of them depend on line-relative
    ordering, so we scan the whole cover-page window for every pattern.
    The ``cover_page_end`` argument bounds the search — typically the
    line where the first ``| Reason for change`` cell or the first
    table separator row begins, so we don't accidentally match
    summary-of-change text further down the document.

    Returns ``(success, details, next_line_number)`` where
    ``next_line_number`` is the position to continue parsing from
    (after the cover page proper). ``success`` is ``True`` when at
    least one cover-page field was located; the parser no longer
    aborts the whole extraction on a missing required field because
    some fixtures store spec / cr_num in docx field codes that does 
    not render.
    """
    details: dict[str, str] = {}
    cover_window = lines[:cover_page_end] if cover_page_end else lines

    patterns: list[tuple[bool, list[str], list[int], re.Pattern[str]]] = [
        (
            False,
            ["tdoc_id"],
            [1],
            _TDOC_HEADER_PATTERN,
        ),
        (
            False,
            ["spec", "cr_num", "rev", "version"],
            [1, 2, 3, 4],
            _COVER_SPEC_RE,
        ),
        (False, ["title"], [1], _COVER_TITLE_RE),
        (False, ["source"], [1], _COVER_SOURCE_RE),
        (False, ["tsg"], [1], _COVER_TSG_RE),
        (True, ["related_wis"], [1], _COVER_WI_RE),
        (
            False,
            ["cr_cat", "release"],
            [1, 2],
            _COVER_CATREL_RE,
        ),
        (False, ["reason_for_change"], [1], _COVER_REASON_RE),
        (
            False,
            ["consequences_if_not_approved"],
            [1],
            _COVER_CONSEQUENCES_RE,
        ),
        (True, ["clauses_affected"], [1], _COVER_CLAUSES_RE),
        (True, ["other_comments"], [1], _COVER_OTHER_RE),
        (True, ["revision_history"], [1], _COVER_REVHIST_RE),
    ]

    for optional, field_names, group_nums, regex in patterns:
        matched_at = _search_pattern_in_lines(
            cover_window,
            details,
            field_names,
            group_nums,
            regex,
        )
        if matched_at == 0:
            if not optional:
                # Required field missing — log warning, do NOT abort
                # so subsequent fields still get a chance.
                logger.warning(
                    "CR Cover Page does not contain a valid %s; "
                    "leaving the corresponding dataclass field as None",
                    field_names[0],
                )
            else:
                logger.debug(
                    "Optional field(s) missing in CR Cover Page: %s; skipping",
                    field_names,
                )

    # Coerce cr_num to digits-only; non-digit inputs become empty.
    cr_num = details.get("cr_num", "") or ""
    if cr_num and not re.fullmatch(r"\d+", cr_num):
        logger.warning(
            "CR number in Cover Page is not digits-only: %r; clearing",
            cr_num,
        )
        details["cr_num"] = ""
    # Normalise rev: ``-`` placeholder -> ``0`` per the reference.
    rev = details.get("rev", "") or ""
    if rev == "-":
        details["rev"] = "0"
    elif rev and not re.fullmatch(r"\d+", rev):
        logger.warning(
            "CR revision in Cover Page is not digits-only: %r; clearing", rev
        )
        details["rev"] = ""

    # Optional truncation for very large free-text fields.
    if max_text_length > 0:
        for field_name in (
            "reason_for_change",
            "consequences_if_not_approved",
            "other_comments",
            "revision_history",
        ):
            value = details.get(field_name)
            if value and len(value) > max_text_length:
                logger.warning(
                    "Truncating %s to %d characters for cover page parsing",
                    field_name,
                    max_text_length,
                )
                details[field_name] = value[:max_text_length]

    _blank_cells_to_none(details)
    return True, details, len(cover_window)


def _blank_cells_to_none(details: dict[str, str | None]) -> None:
    """Coerce blank cover-page cell values to ``None`` in place.

    Cover-page fields where markdown converter dropped a docx field code
    render as empty strings rather than absent rows; the dataclass
    contract treats them the same as missing rows.
    """
    for key in list(details):
        if not details[key]:
            details[key] = None


def _search_pattern_in_lines(
    lines: Sequence[str],
    data: dict[str, str],
    field_names: Sequence[str],
    group_numbers: Sequence[int],
    pattern: re.Pattern[str],
) -> int:
    """Find ``pattern`` in the next ``len(lines)`` lines, populating ``data``.

    Mirrors the reference function — when matched, assigns
    ``match.group(n).strip()`` (with a trailing ``|`` cleanup) to each
    ``field_names[i]`` from group ``group_numbers[i]``.

    Raises:
        ValueError: When ``len(field_names) != len(group_numbers)``;
            the reference raises this on a programming error.
    """
    if len(field_names) != len(group_numbers):
        raise ValueError(
            f"field_names ({len(field_names)}) and group_numbers "
            f"({len(group_numbers)}) must have the same length"
        )
    advancing = 0
    for line in lines:
        advancing += 1
        text = _remove_markdown_formatting(line)
        match = pattern.search(text)
        if match:
            for field_name, group_number in zip(field_names, group_numbers):
                raw_value = match.group(group_number).strip()
                raw_value = re.sub(r"\s*\|+\s*$", "", raw_value)
                data[field_name] = raw_value
            break
    return advancing


# ---------------------------------------------------------------------------
# TTCN-only sub-parsers (Overview + Corrections)
# ---------------------------------------------------------------------------


def _is_ttcn_tdoc(tdoc_id: str) -> bool:
    """True iff ``tdoc_id`` looks like a TTCN email-meeting id."""
    return bool(_TTCN_TDOC_PATTERN.fullmatch(tdoc_id))


def _parse_ttcn_cr_overview(
    lines: Sequence[str],
) -> tuple[bool, dict[str, str], int]:
    """Parse the ``Overview`` / ``Table of Contents`` section of a TTCN CR.

    Returns ``(success, details, next_line_number)``. The ``details``
    dict carries the TTCN-specific overview fields
    (``ats_version``, ``ttcn_release``, ``test_case``, ``test_suite``,
    ``ue``, ``ss``).
    """
    details: dict[str, str] = {}
    start_idx: int | None = None
    end_idx: int | None = None

    for idx, line in enumerate(lines):
        text = _remove_markdown_formatting(line)
        if _OVERVIEW_TABLE_OF_CONTENTS_RE.match(text):
            start_idx = idx
            continue
        if _OVERVIEW_OVERVIEW_RE.match(text):
            start_idx = idx
            continue
        if _CORRECTIONS_REQUIRED_RE.match(text):
            end_idx = idx
            break

    if start_idx is None:
        logger.warning(
            "Could not find 'Overview' or 'Table of Contents' section in TTCN "
            "CR; assuming start of document"
        )
        start_idx = 0
    if end_idx is None:
        logger.warning(
            "Could not find 'Corrections required' section in TTCN CR; "
            "assuming end of document"
        )
        end_idx = len(lines)
    if end_idx == 0 or start_idx >= end_idx:
        logger.warning(
            "Overview section boundaries are invalid (start=%s end=%s); "
            "skipping overview parse",
            start_idx,
            end_idx,
        )
        return False, details, 0

    overview_lines = lines[start_idx:end_idx]
    advancing = 0
    next_line_number = 0
    ats_version: str | None = None
    for line in overview_lines:
        advancing += 1
        text = _remove_markdown_formatting(line)

        match = _OVERVIEW_TESTCASE_RE.search(text + " ")
        if match and "testcase" not in details:
            details["testcase"] = match.group(1).strip()
            next_line_number = start_idx + advancing
            continue

        match = _OVERVIEW_UE_RE.match(text)
        if match and "ue" not in details:
            details["ue"] = match.group(1).strip()
            next_line_number = start_idx + advancing
            continue

        match = _OVERVIEW_SS_RE.match(text)
        if match and "ss" not in details:
            details["ss"] = match.group(1).strip()
            next_line_number = start_idx + advancing
            continue

        match = _OVERVIEW_ATS_RE.search(text)
        if match and "ats_version" not in details:
            ats_version = match.group(1)
            details["ats_version"] = ats_version
            if len(ats_version) >= 6:
                details["ttcn_release"] = ats_version[-6:]
            next_line_number = start_idx + advancing

        match = _OVERVIEW_TESTSUITE_RE.search(text)
        if match and "test_suite" not in details:
            details["test_suite"] = match.group(1).strip()
            next_line_number = start_idx + advancing

    if next_line_number == 0:
        next_line_number = end_idx
    return next_line_number > 0, details, next_line_number


def _parse_ttcn_cr_corrections(
    lines: Sequence[str],
    *,
    max_text_length: int = 0,
    full: bool = False,
) -> tuple[bool, list[dict[str, str]], int]:
    """Parse the ``Corrections required`` section of a TTCN CR.

    Returns ``(success, list_of_correction_dicts, next_line_number)``.
    Each entry is one per-correction metadata table.
    """
    changes: list[dict[str, str]] = []
    next_line_number = 0
    total_lines = len(lines)

    # Find the start of the Corrections required section.
    for idx in range(total_lines):
        text = _remove_markdown_formatting(lines[idx])
        if _CORRECTIONS_REQUIRED_RE.match(text):
            next_line_number = idx + 1
            break
    if next_line_number == 0:
        logger.warning(
            "Could not find 'Corrections required' section in TTCN CR; "
            "skipping corrections parsing"
        )
        return False, changes, 0

    # Look for either a heading or the first Function name table to
    # determine where the "common change" preamble ends.
    advancing = -1
    for idx in range(next_line_number, total_lines):
        is_heading = lines[idx].strip().startswith("#")
        if is_heading:
            advancing = idx - next_line_number
            break
        text = _remove_markdown_formatting(lines[idx])
        if _FUNCTION_NAME_START_RE.match(text):
            # Back up two lines to capture any preamble text rows.
            advancing = idx - 2 - next_line_number
            break
    if advancing < 0:
        logger.warning(
            "Could not find any correction metadata table in 'Corrections "
            "required' section; skipping corrections parsing"
        )
        return False, changes, 0

    if advancing > 0:
        # Pre-table preamble; extract any dependent CR ids so we can
        # capture a relationship note.
        preamble_lines = lines[next_line_number : next_line_number + advancing]
        preamble_text = "\n".join(preamble_lines).strip()
        dependent_crs = _extract_tdoc_from_text(preamble_text)
        if dependent_crs:
            changes.append({"dependent_crs": ", ".join(dependent_crs)})

    number_of_extracted = 0
    while next_line_number < total_lines:
        logger.debug(
            "Parsing single TTCN correction starting at line %d",
            next_line_number,
        )
        success, change, advancing = _parse_ttcn_cr_single_correction(
            lines[next_line_number:],
            max_text_length=max_text_length,
            full=full,
        )
        if not success:
            break
        if change:
            changes.append(change)
            number_of_extracted += 1
            logger.debug("Extracted %d corrections", number_of_extracted)
        if advancing <= 0:
            # Safety net so a buggy sub-parser cannot spin the loop.
            break
        next_line_number += advancing
    return number_of_extracted > 0, changes, next_line_number


def _extract_tdoc_from_text(text: str) -> list[str]:
    """Pull TDoc ids out of a free-form block of text.

    Uses the same union regex as the cover-page header — accepts
    ``R5-227476``, ``R5s260009``, ``C6-250028`` etc. Lower-cases the
    result for stable ordering.
    """
    return [m.group(0).lower() for m in _TDOC_HEADER_PATTERN.finditer(text)]


def _parse_ttcn_cr_single_correction(
    lines: Sequence[str],
    *,
    max_text_length: int = 0,
    full: bool = False,
) -> tuple[bool, dict[str, str], int]:
    """Parse a single correction metadata table from TTCN markdown.

    Returns ``(success, change_dict, next_line_number)``.
    """
    change: dict[str, str] = {}
    total_lines = len(lines)
    start_idx: int | None = None
    end_idx: int | None = None
    for idx, line in enumerate(lines):
        text = _remove_markdown_formatting(line)
        if _FUNCTION_NAME_START_RE.match(text):
            start_idx = idx
            continue
        if _TTCN_MODULE_END_RE.match(text):
            end_idx = idx + 1
            continue
        if _MCC160_END_RE.match(text):
            end_idx = idx + 1
            break
        if (
            start_idx is not None
            and end_idx is not None
            and "|" not in text
        ):
            # found a non-table line after the table; assume the table is done
            break

    if start_idx is None or end_idx is None:
        if start_idx is not None and end_idx is None:
            logger.warning(
                "Found start of correction metadata table but no end; "
                "consuming the rest of the document"
            )
            end_idx = total_lines
        else:
            logger.warning(
                "Could not find a valid correction metadata table; skipping"
            )
            return False, {}, 0
    elif end_idx <= start_idx:
        logger.error(
            "Found end of correction metadata table before start; skipping"
        )
        return False, {}, 0

    change_lines = lines[start_idx:end_idx]
    advancing = 0
    next_line_number = 0

    def _grab_one(label_re: re.Pattern[str], target: str) -> bool:
        nonlocal advancing, next_line_number
        for line in change_lines[advancing:]:
            advancing += 1
            text = _remove_markdown_formatting(line)
            match = label_re.match(text)
            if match:
                change[target] = match.group(1).strip()
                next_line_number = start_idx + advancing
                return True
        return False

    _grab_one(_SINGLE_CORRECTION_FUNCTION_RE, "function_name")
    _grab_one(_SINGLE_CORRECTION_REASON_RE, "reason_for_change")
    _grab_one(_SINGLE_CORRECTION_SUMMARY_RE, "summary_of_change")
    _grab_one(_SINGLE_CORRECTION_TTCN_RE, "ttcn_module")
    _grab_one(_SINGLE_CORRECTION_MCC160_RE, "mcc160_comment")

    if next_line_number == 0:
        next_line_number = end_idx
    else:
        if full:
            key: str | None = None
            scan_idx = next_line_number
            while scan_idx < total_lines:
                text = _remove_markdown_formatting(
                    lines[scan_idx], _MD_RM_KEEP_HEADERS
                )
                scan_idx += 1
                if text.startswith("#"):
                    scan_idx -= 1
                    break
                if _SINGLE_CORRECTION_BEFORE_RE.match(text):
                    key = "before_change"
                    continue
                if _SINGLE_CORRECTION_AFTER_RE.match(text):
                    key = "after_change"
                    continue
                if _SINGLE_CORRECTION_NEW_RE.match(text):
                    key = "new_change"
                    continue
                if _TABLE_CELL_RE.match(text):
                    scan_idx -= 1
                    advancing2, content = _extract_change_from_table(
                        lines[scan_idx - 1 :], max_lines=5
                    )
                    scan_idx += advancing2
                    if content:
                        if key is None:
                            key = "new_change"
                        change[key] = content
                    # for before_change, there shall be after_change, so we continue to look for after_change
                    if key != "before_change":
                        break
            next_line_number = scan_idx

        if max_text_length > 0:
            for key_name in (
                "reason_for_change",
                "summary_of_change",
                "mcc160_comment",
                "before_change",
                "after_change",
                "new_change",
            ):
                value = change.get(key_name)
                if value and len(value) > max_text_length:
                    logger.warning(
                        "Truncating %s to %d characters for single correction",
                        key_name,
                        max_text_length,
                    )
                    change[key_name] = value[:max_text_length]

    return next_line_number > 0, change, next_line_number


def _extract_change_from_table(
    lines: Sequence[str], *, max_lines: int = 5
) -> tuple[int, str | None]:
    """Pull a single content row out of a ``| ... |`` table.
    Ingnores empty rows (e.g. ``|  |  |``) and separators (``| --- |``) and returns the first non-empty content row.

    Returns ``(advancing, content_or_None)``.
    """
    advancing = 0
    for idx, line in enumerate(lines):
        if idx >= max_lines:
            break
        text = _remove_markdown_formatting(line)
        match = _TABLE_CELL_RE.match(text)
        if match:
            advancing = idx + 1
            content = match.group(1).strip()
            # check if content contains only dashes (e.g. | --- |) or `|` or is empty, if so, skip it
            remaining_content = content.replace("-", "").replace("|", "").strip()
            if not remaining_content:
                continue
            # replace <br> with newline, and replace double spaces with newline
            content = content.replace("<br>", "\n")
            return advancing, content
        else:
            break
    return advancing, None


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


# Cover-page dataclass field names. Used by ``parse_cr_details`` to
# project the cover-page ``dict[str, str]`` into the strongly-typed
# dataclass. Excludes ``tdoc_id`` (always from input), ``year`` /
# ``tech`` (derived), and ``corrections`` / TTCN fields (filled from
# the sub-parsers).
_COVER_FIELDS = (
    "spec",
    "cr_num",
    "rev",
    "version",
    "title",
    "source",
    "tsg",
    "related_wis",
    "cr_cat",
    "release",
    "reason_for_change",
    "consequences_if_not_approved",
    "clauses_affected",
    "other_comments",
    "revision_history",
    "date",
)


def parse_cr_details(
    markdown: str,
    *,
    tdoc_id: str,
    max_text_length: int = 0,
    full: bool = False,
) -> TDocCRDetails:
    """Parse a CR markdown body into a :class:`TDocCRDetails`.

    Args:
        markdown: The full markdown conversion output of the CR ``.docx``.
        tdoc_id: Canonical TDoc identifier (e.g. ``"R5s260009"``,
            ``"R5-227476"``, ``"C6-250028"``). Always stored on the
            result as ``tdoc_id``; the parser records what the
            header actually contained under ``extracted_tdoc_id``.
        max_text_length: When > 0, truncate free-text cover-page
            fields and per-correction values to this length. Useful
            for very long ``Reason for change`` blocks; default
            preserves the full text.
        full: When ``True`` (forwarded to the TTCN corrections
            parser), include ``before_change`` / ``after_change`` /
            ``new_change`` content in each correction entry.

    Returns:
        A typed :class:`TDocCRDetails`. The ``tsg``, ``related_wis``,
        ``date``, ``year`` and ``tech`` fields are derived — tsg and
        related_wis from the cover page; date from the cover page;
        year from ``tdoc_id``; tech from :func:`derive_tech_from_spec`.

    Raises:
        ValueError: ``tdoc_id`` is empty after stripping.
        CRHeaderMissingError: The first three lines of ``markdown``
            contain no ``3GPP TSG-`` header (i.e. this document
            does not look like a 3GPP CR).
    """
    if not (tdoc_id or "").strip():
        raise ValueError("tdoc_id is required and cannot be empty")

    lines = markdown.splitlines()
    if not lines:
        raise CRHeaderMissingError(
            "Refusing to parse empty markdown as a CR document",
            snippet="",
        )

    # --- header sniff: raise loudly when the 3GPP banner is absent ---
    header_blob = _collapse_whitespace(
        _remove_markdown_formatting("\n".join(lines[:3]))
    )
    if not _HEADER_PATTERN.search(header_blob):
        snippet = header_blob[:100]
        raise CRHeaderMissingError(
            "Markdown does not contain a '3GPP TSG-' header; "
            "this does not look like a 3GPP CR document.",
            snippet=snippet,
        )

    # If the title line has a "3GPP TSG-... #NNNN-TTCN email" layout,
    # warn the user — non-conforming headers tend to produce partial
    # cover-page data.
    if not re.search(
        r"3GPP\s+TSG-\w+\s+Meeting\s+#\d{4}-TTCN\s+email",
        header_blob,
        re.IGNORECASE,
    ):
        logger.warning(
            "CR header does not match the expected "
            "'3GPP TSG-<WG> Meeting #<YEAR>-TTCN email' layout; "
            "extraction may be incomplete"
        )

    # --- cover-page (always) ---
    cover_success, cover_details, _adv = _parse_cr_cover_page(
        lines, max_text_length=max_text_length
    )
    if not cover_success:
        # Partial cover data is acceptable; we still produce a
        # result, just with None fields for whatever regexes missed.
        logger.warning(
            "Cover page parsing was partial for tdoc_id=%s; "
            "some dataclass fields may be None",
            tdoc_id,
        )

    # --- TTCN-only overview + corrections ---
    is_ttcn = _is_ttcn_tdoc(tdoc_id)
    overview: dict[str, str] = {}
    corrections: list[dict[str, str]] = []
    if is_ttcn:
        overview_success, overview, _adv = _parse_ttcn_cr_overview(lines)
        if not overview_success:
            logger.warning(
                "TTCN overview section parse was partial for tdoc_id=%s",
                tdoc_id,
            )
        corr_success, corrections, _adv = _parse_ttcn_cr_corrections(
            lines,
            max_text_length=max_text_length,
            full=full,
        )
        if not corr_success:
            logger.warning(
                "TTCN corrections section parse was partial for tdoc_id=%s",
                tdoc_id,
            )

    # --- resolution of the header vs. input tdoc ids ---
    extracted = cover_details.get("tdoc_id")
    final_tdoc_id = tdoc_id.strip()
    if extracted and extracted.lower() != final_tdoc_id.lower():
        logger.warning(
            "Header tdoc_id %r differs from input %r; using input as canonical",
            extracted,
            final_tdoc_id,
        )

    # --- tsg fallback: empty cover -> tdoc-derived tsg ---
    resolved_tsg = cover_details.get("tsg") or None
    if not resolved_tsg and len(final_tdoc_id) >= 2:
        fallback_tsg = final_tdoc_id[:2].upper()
        logger.warning(
            "Cover-page tsg was empty; falling back to '%s' from input tdoc_id",
            fallback_tsg,
        )
        resolved_tsg = fallback_tsg

    # --- date is not in the cover-page regex set; skip ---
    date_value: str | None = None

    # --- derived: year (from input id), tech (from spec) ---
    year = _year_from_tdoc_id(final_tdoc_id)
    spec_for_tech = cover_details.get("spec")
    tech = derive_tech_from_spec(spec_for_tech) if spec_for_tech else ""

    # --- project into the dataclass ---
    payload: dict[str, object] = {"tdoc_id": final_tdoc_id}
    for key in _COVER_FIELDS:
        payload[key] = cover_details.get(key)
    payload["tsg"] = resolved_tsg
    payload["date"] = date_value
    # TTCN-only overview fields.
    payload["ats_version"] = overview.get("ats_version")
    payload["ttcn_release"] = overview.get("ttcn_release")
    payload["test_case"] = overview.get("testcase")
    payload["test_suite"] = overview.get("test_suite")
    payload["ue"] = overview.get("ue")
    payload["ss"] = overview.get("ss")
    payload["corrections"] = corrections
    payload["year"] = year
    payload["tech"] = tech or None
    payload["extracted_tdoc_id"] = extracted

    # Drop keys that don't correspond to dataclass fields and convert
    # values to the right type (the dict came back as ``str`` /
    # ``list`` / ``None``, all of which the dataclass accepts).
    valid_keys = {f.name for f in fields(TDocCRDetails)}
    filtered = {k: v for k, v in payload.items() if k in valid_keys}
    return TDocCRDetails(**filtered)


__all__ = [
    "CRHeaderMissingError",
    "extract_docx_from_zip",
    "parse_cr_details",
    "derive_tech_from_spec",
]
