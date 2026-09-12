"""Pure parser for RAN5 TTCN status workbooks (no network).

Mirrors :mod:`doc3gpp.parsers.tdoc_parser`: the workbook is loaded with
``load_workbook(..., read_only=True, data_only=True)`` and iterated via
``iter_rows(values_only=True)`` with row 1 as the header.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from doc3gpp.models.testcase import (
    TestCase,
    TestCaseStatus,
    TestcaseWorkbookNotFoundError,
)

logger = logging.getLogger(__name__)

SHEET_TO_GROUP = {"5G_TC_Status": "5G", "LTE_TC_Status": "LTE", "IMS_TC_Status": "IMS",
                  "UTRA_TC_Status": "UTRA", "Positioning_TC_Status": "POS", "MCX_TC_Status": "MCX"}
PATH_RANK = ["FR1", "FR2", "FR1+FR2", "FDD", "TDD", "IPCAN-4G", "EUTRA", "IPCAN-5G", "NR5GC", "default"]
STATUS_PAIRS: dict[str, list[tuple[int, int, str]]] = {
    "5G": [(2, 3, "FR1"), (2, 15, "FR2"), (2, 22, "FR1+FR2")],
    "LTE": [(2, 3, "FDD"), (15, 16, "TDD")],
    "IMS": [(2, 3, "default")], "UTRA": [(2, 3, "default")], "POS": [(2, 3, "default")],
    "MCX": [(2, 3, "IPCAN-4G"), (17, 18, "EUTRA"), (26, 27, "IPCAN-5G"), (37, 38, "NR5GC")],
}
FIXED_SPEC = {"5G": "38.523-1", "LTE": "36.523-1"}

_COMMON_COLS = frozenset({"tc", "title", "ats", "feature", "release", "ran wic", "part of"})

_MCX_PREFIX_RE = re.compile(r"^mcx-", re.IGNORECASE)


def _norm_header(cell: object) -> str:
    return re.sub(r"\s+", " ", str(cell).strip().casefold())


def _is_gcf_header(header: object) -> bool:
    return "gcfptcrb" in re.sub(r"[^a-z0-9]", "", str(header).casefold())


def _is_ttcn_header(header: object) -> bool:
    return "ttcnstatus" in re.sub(r"[^a-z0-9]", "", str(header).casefold())


def _cell(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _strip_mcx_prefix(value: str) -> str:
    return _MCX_PREFIX_RE.sub("", value)


def _at(row: tuple[object, ...], idx: int) -> object:
    return row[idx] if idx < len(row) else None


def extract_workbook(zip_bytes: bytes) -> bytes:
    """Return the ``*.xlsx`` workbook bytes held in ``zip_bytes``.

    Matching is case-insensitive. Zero members raises
    :class:`TestcaseWorkbookNotFoundError`; more than one picks the
    lexically-first name with a warning.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = sorted(n for n in zf.namelist() if n.lower().endswith(".xlsx"))
        if not names:
            raise TestcaseWorkbookNotFoundError(
                "status zip holds no *.xlsx workbook member"
            )
        if len(names) > 1:
            logger.warning(
                "Multiple *.xlsx members in status zip; using lexically-first %r",
                names[0],
            )
        return zf.read(names[0])


def parse_testcase_workbook(
    xlsx_bytes: bytes,
) -> tuple[list[TestCase], list[TestCaseStatus], list[str]]:
    """Parse every known group sheet into headers, statuses, and skip notes.

    A missing sheet warns and appends a note; it never raises.
    """
    workbook = load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    cases: list[TestCase] = []
    statuses: list[TestCaseStatus] = []
    notes: list[str] = []
    seen_keys: set[tuple[str, str]] = set()
    for sheet_name, group in SHEET_TO_GROUP.items():
        if sheet_name not in workbook.sheetnames:
            note = f"sheet {sheet_name} (group {group}) missing; skipped"
            logger.warning("Testcase workbook: %s", note)
            notes.append(note)
            continue
        rows = workbook[sheet_name].iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            note = f"sheet {sheet_name} (group {group}) has no header row; skipped"
            logger.warning("Testcase workbook: %s", note)
            notes.append(note)
            continue
        cols: dict[str, int] = {}
        for idx, cell in enumerate(header):
            norm = _norm_header(cell)
            if norm in _COMMON_COLS and norm not in cols:
                cols[norm] = idx

        def _col(row: tuple[object, ...], name: str) -> str | None:
            idx = cols.get(name)
            return _cell(_at(row, idx)) if idx is not None else None

        valid_pairs: list[tuple[int, int, str]] = []
        for gcf_idx, ttcn_idx, path in STATUS_PAIRS.get(group, []):
            gcf_header = _at(header, gcf_idx)
            ttcn_header = _at(header, ttcn_idx)
            if not _is_gcf_header(gcf_header):
                logger.warning(
                    "%s: skipping path %s: expected GCF/PTCRB header at column %s "
                    "but found %r",
                    sheet_name,
                    path,
                    get_column_letter(gcf_idx + 1),
                    gcf_header,
                )
                continue
            if not _is_ttcn_header(ttcn_header):
                logger.warning(
                    "%s: skipping path %s: expected TTCN-Status header at column %s "
                    "but found %r",
                    sheet_name,
                    path,
                    get_column_letter(ttcn_idx + 1),
                    ttcn_header,
                )
                continue
            valid_pairs.append((gcf_idx, ttcn_idx, path))

        for row in rows:
            testcase_id = _col(row, "tc")
            if testcase_id is None:
                logger.debug("%s: skipping row with empty TC", sheet_name)
                continue
            if group in FIXED_SPEC:
                spec = FIXED_SPEC[group]
            else:
                spec = _col(row, "part of")
            if (testcase_id, group) in seen_keys:
                logger.warning(
                    "%s: skipping duplicate TC %r in group %r "
                    "(already seen in an earlier sheet)",
                    sheet_name,
                    testcase_id,
                    group,
                )
                continue
            seen_keys.add((testcase_id, group))
            cases.append(
                TestCase(
                    testcase_id=testcase_id,
                    group=group,
                    title=_col(row, "title"),
                    ats=_col(row, "ats"),
                    feature=_col(row, "feature"),
                    release=_col(row, "release"),
                    wis=_col(row, "ran wic"),
                    spec=spec,
                )
            )
            for gcf_idx, ttcn_idx, path in valid_pairs:
                gcf = _cell(_at(row, gcf_idx))
                ttcn = _cell(_at(row, ttcn_idx))
                if gcf is None and ttcn is None:
                    continue
                statuses.append(
                    TestCaseStatus(
                        testcase_id=testcase_id,
                        group=group,
                        path=_strip_mcx_prefix(path),
                        gcf_ptcrb=gcf,
                        ttcn_status=ttcn,
                    )
                )
    return cases, statuses, notes
