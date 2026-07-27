"""TTCN-specific :class:`SectionParser` implementations."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from doc3gpp.parsers.cr.header import _TDOC_HEADER_PATTERN


logger = logging.getLogger(__name__)


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
    r"^(?:[#\d\.\s]*)?(Corrections\s+required)\b.*$", re.IGNORECASE
)
# Every 3GPP TTCN CR ships a Table of Contents whose entries use the
# pattern ``<title>     ...... <page>`` — whitespace + six leader dots
# + whitespace + page number at end-of-line. The ``6`` is exact: the
# docx-to-markdown converter renders the source one-dot-leader tab as
# exactly six ``.`` characters, and a survey of 545 TOC-leader lines
# across the 2026-TTCN corpus confirms 100% uniformity. Pinning the
# count is what lets the prefix anchor stay permissive — the TOC
# rejection is a positive signal, not a negative-prefix heuristic.
_TOC_LEADER_RE = re.compile(r"\s\.{6}\s+\d+\s*$")

_SINGLE_CORRECTION_FUNCTION_RE = re.compile(
    r"^\s*\|\s*(?:[\w\s]+\s+name)\s*\|\s*(.+?)\s*\|", re.IGNORECASE
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
    r"^\s*Before\s+changes{0,1}\s*:{0,1}\s*$", re.IGNORECASE
)
_SINGLE_CORRECTION_AFTER_RE = re.compile(
    r"^\s*After\s+changes{0,1}\s*:{0,1}\s*$", re.IGNORECASE
)
_SINGLE_CORRECTION_MCC_IMPL_RE = re.compile(
    r"^\s*MCC160\s+Implementation\s*:{0,1}\s*$", re.IGNORECASE
)
_SINGLE_CORRECTION_NEW_RE = re.compile(
    r"^\s*New\s+changes{0,1}\s*:{0,1}\s*$", re.IGNORECASE
)
_TABLE_CELL_RE = re.compile(r"^\s*\|\s*(.+?)\s*\|")
_FUNCTION_NAME_START_RE = re.compile(
    r"^\s*\|\s*(?:[\w\s]+\s+name)\s*\|\s*(.+?)\s*\|", re.IGNORECASE
)
_TTCN_MODULE_END_RE = re.compile(r"^\s*\|\s*TTCN\s+module\s*\|", re.IGNORECASE)
_MCC160_END_RE = re.compile(r"^\s*\|\s*MCC160\s+Comment\s*\|", re.IGNORECASE)


class TTCNOverviewParser:
    """Parse the ``Overview`` / ``Table of Contents`` section of a TTCN CR."""

    name = "overview"

    def parse(
        self,
        lines: Sequence[str],
        *,
        max_text_length: int = 0,
        full: bool = False,
    ) -> tuple[bool, dict[str, str], int]:
        details: dict[str, str] = {}
        start_idx: int | None = None
        end_idx: int | None = None

        for idx, line in enumerate(lines):
            if _OVERVIEW_TABLE_OF_CONTENTS_RE.match(line):
                start_idx = idx
                continue
            if _OVERVIEW_OVERVIEW_RE.match(line):
                start_idx = idx
                continue
            if _CORRECTIONS_REQUIRED_RE.match(line) and not _TOC_LEADER_RE.search(line):
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
        for line in overview_lines:
            advancing += 1

            match = _OVERVIEW_TESTCASE_RE.search(line + " ")
            if match and "testcase" not in details:
                details["testcase"] = match.group(1).strip()
                next_line_number = start_idx + advancing
                continue

            match = _OVERVIEW_UE_RE.match(line)
            if match and "ue" not in details:
                details["ue"] = match.group(1).strip()
                next_line_number = start_idx + advancing
                continue

            match = _OVERVIEW_SS_RE.match(line)
            if match and "ss" not in details:
                details["ss"] = match.group(1).strip()
                next_line_number = start_idx + advancing
                continue

            match = _OVERVIEW_ATS_RE.search(line)
            if match and "ats_version" not in details:
                ats_version = match.group(1)
                details["ats_version"] = ats_version
                if len(ats_version) >= 6:
                    details["ttcn_release"] = ats_version[-6:]
                next_line_number = start_idx + advancing

            match = _OVERVIEW_TESTSUITE_RE.search(line)
            if match and "test_suite" not in details:
                details["test_suite"] = match.group(1).strip()
                next_line_number = start_idx + advancing

        if next_line_number == 0:
            next_line_number = end_idx
        return next_line_number > 0, details, next_line_number


class TTCNCorrectionsParser:
    """Parse the ``Corrections required`` section of a TTCN CR."""

    name = "corrections"

    def parse(
        self,
        lines: Sequence[str],
        *,
        max_text_length: int = 0,
        full: bool = False,
    ) -> tuple[bool, list[dict[str, str]], int]:
        changes: list[dict[str, str]] = []
        next_line_number = 0
        total_lines = len(lines)

        for idx in range(total_lines):
            if _CORRECTIONS_REQUIRED_RE.match(lines[idx]) and not _TOC_LEADER_RE.search(lines[idx]):
                next_line_number = idx + 1
                break
        if next_line_number == 0:
            logger.warning(
                "Could not find 'Corrections required' section in TTCN CR; "
                "skipping corrections parsing"
            )
            return False, changes, 0

        advancing = -1
        for idx in range(next_line_number, total_lines):
            is_heading = lines[idx].strip().startswith("#")
            if is_heading:
                advancing = idx - next_line_number
                break
            if _FUNCTION_NAME_START_RE.match(lines[idx]):
                advancing = idx - 2 - next_line_number
                break
        if advancing < 0:
            logger.warning(
                "Could not find any correction metadata table in 'Corrections "
                "required' section; skipping corrections parsing"
            )
            return False, changes, 0

        if advancing > 0:
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
                break
            next_line_number += advancing
        return number_of_extracted > 0, changes, next_line_number


def _extract_tdoc_from_text(text: str) -> list[str]:
    """Pull TDoc ids out of a free-form block of text."""
    return [m.group(0).lower() for m in _TDOC_HEADER_PATTERN.finditer(text)]


def _parse_ttcn_cr_single_correction(
    lines: Sequence[str],
    *,
    max_text_length: int = 0,
    full: bool = False,
) -> tuple[bool, dict[str, str], int]:
    """Parse a single correction metadata table from TTCN markdown."""
    change: dict[str, str] = {}
    total_lines = len(lines)
    start_idx: int | None = None
    end_idx: int | None = None
    for idx, line in enumerate(lines):
        if _FUNCTION_NAME_START_RE.match(line):
            start_idx = idx
            continue
        if _TTCN_MODULE_END_RE.match(line):
            end_idx = idx + 1
            continue
        if _MCC160_END_RE.match(line):
            end_idx = idx + 1
            break
        if (
            start_idx is not None
            and end_idx is not None
            and "|" not in line
        ):
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
            match = label_re.match(line)
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
                text = lines[scan_idx]
                scan_idx += 1
                if text.startswith("#"):
                    scan_idx -= 1
                    break
                if _FUNCTION_NAME_START_RE.match(text):
                    scan_idx -= 2
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
                if _SINGLE_CORRECTION_MCC_IMPL_RE.match(text):
                    key = "mcc_implementation"
                    continue
                if _TABLE_CELL_RE.match(text):
                    # ``text`` is the cell line; ``scan_idx`` was just
                    # incremented past it at the top of the loop.
                    # Step back so ``scan_idx`` points at the cell
                    # line, then extract content starting there
                    # (``_extract_change_from_table`` skips empty /
                    # separator rows like ``| --- |`` until the first
                    # real content row). ``advancing2`` is the count of
                    # rows consumed; if it is zero (the cell line did
                    # not match inside the helper — defensive, should
                    # not happen since we just matched it), advance by
                    # one to avoid an infinite loop on the same cell.
                    scan_idx -= 1
                    advancing2, content = _extract_change_from_table(
                        lines[scan_idx:], max_lines=5
                    )
                    scan_idx += advancing2 if advancing2 > 0 else 1
                    if content:
                        if key is None:
                            key = "new_change"
                        existing = change.get(key)
                        if existing is None:
                            change[key] = content
                        else:
                            change[key] = existing + "\n\n/*********************************/\n\n" + content
                        key = None
            next_line_number = scan_idx

        if max_text_length > 0:
            for key_name in (
                "reason_for_change",
                "summary_of_change",
                "mcc160_comment",
                "before_change",
                "after_change",
                "new_change",
                "mcc_implementation",
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
    """Pull a single content row out of a ``| ... |`` table."""
    advancing = 0
    for idx, line in enumerate(lines):
        if idx >= max_lines:
            break
        match = _TABLE_CELL_RE.match(line)
        if match:
            advancing = idx + 1
            content = match.group(1).strip()
            remaining_content = content.replace("-", "").replace("|", "").strip()
            if not remaining_content:
                continue
            content = content.replace("<br>", "\n")
            return advancing, content
        else:
            break
    return advancing, None