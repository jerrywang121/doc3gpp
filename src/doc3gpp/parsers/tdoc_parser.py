from __future__ import annotations

import io
import logging
import re
from typing import Dict, Iterable, List, Optional

from openpyxl import load_workbook

logger = logging.getLogger(__name__)


CR_ID_RE = re.compile(r"[RSC][1-9][-sw]\d{6}(?:r\d{1})?", re.IGNORECASE)

# Substrings that disambiguate the real header row from a title row that
# happens to mention "tdoc" (e.g. "TDoc List — RAN5#111").
_HEADER_ROW_MARKERS = frozenset(
    {"title", "source", "type", "status", "spec", "version", "release"}
)


def _is_header_row(potential: Dict[str, int]) -> bool:
    has_tdoc = any("tdoc" in key for key in potential)
    if not has_tdoc:
        return False
    return any(marker in key for key in potential for marker in _HEADER_ROW_MARKERS)


def normalize_header(value: object) -> str:
    """Normalize a cell header value to lowercase whitespace-normalized text."""
    if value is None:
        return ""
    text = str(value).strip().lower()
    return re.sub(r"\s+", " ", text)


def to_text(value: object) -> str:
    """Convert a cell value to a trimmed string for display or comparison."""
    if value is None:
        return ""
    return str(value).strip()


def pick_col(header_map: Dict[str, int], candidates: Iterable[str]) -> Optional[int]:
    """Find the first matching column index for a set of header candidates."""
    for candidate in candidates:
        candidate_norm = normalize_header(candidate)
        for header, col in header_map.items():
            if header == candidate_norm or candidate_norm in header:
                return col
    return None


def read_tdoc_sheet(xlsx_bytes: bytes) -> List[Dict[str, Optional[str]]]:
    logger.debug("Reading TDoc XLSX bytes (%s bytes)", len(xlsx_bytes))
    workbook = load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    if workbook is None or not workbook.sheetnames:
        logger.error("Could not read TDoc list sheet from XLSX file")
        raise RuntimeError("Could not read TDoc list sheet from XLSX file.")

    worksheet = workbook.active
    rows = worksheet.iter_rows(values_only=True)
    header_map: Dict[str, int] = {}
    for row in rows:
        potential = {normalize_header(cell): idx for idx, cell in enumerate(row) if normalize_header(cell)}
        if _is_header_row(potential):
            header_map = potential
            logger.debug("Detected header map: %s", header_map)
            break

    if not header_map:
        logger.error("Could not find header row in TDoc list sheet")
        raise RuntimeError("Could not find header row in TDoc list sheet.")

    col_tdoc = pick_col(header_map, ["Tdoc", "TDoc"])
    if col_tdoc is None:
        logger.error("Could not find TDoc column in TDoc list sheet")
        raise RuntimeError("Could not find TDoc column in TDoc list sheet.")

    mapping = {
        "title": pick_col(header_map, ["Title"]),
        "source": pick_col(header_map, ["Source"]),
        "type": pick_col(header_map, ["Type"]),
        "status": pick_col(header_map, ["Status", "TDoc Status"]),
        "reservation_date": pick_col(header_map, ["Reservation Date", "Reservation date"]),
        "uploaded_date": pick_col(header_map, ["Uploaded Date", "Uploaded"]),
        "cr_cat": pick_col(header_map, ["CR Cat", "CR category", "Cat"]),
        "is_revision_of": pick_col(header_map, ["Is revision of"]),
        "revised_to": pick_col(header_map, ["Revised to"]),
        "release": pick_col(header_map, ["Release"]),
        "spec": pick_col(header_map, ["Spec"]),
        "version": pick_col(header_map, ["Version"]),
        "related_wis": pick_col(header_map, ["Related WIs", "Work item code"]),
        "cr_num": pick_col(header_map, ["CR"]),
        "cr_pack": pick_col(header_map, ["TSG CR Pack", "TSG CR pack", "CR Pack"]),
    }

    result: List[Dict[str, Optional[str]]] = []
    for row_index, row in enumerate(rows, start=1):
        raw_tdoc = to_text(row[col_tdoc]) if col_tdoc < len(row) else ""
        if not raw_tdoc:
            logger.debug("Skipping empty row %s", row_index)
            continue

        match = CR_ID_RE.search(raw_tdoc)
        if not match:
            logger.debug("Skipping non-TDoc row %s: %s", row_index, raw_tdoc)
            continue

        record: Dict[str, Optional[str]] = {"tdoc": match.group(0)}
        for key, col in mapping.items():
            if col is not None and col < len(row):
                value = to_text(row[col])
                # Empty cells map to ``None`` so optional ORM columns stay nullable.
                record[key] = value if value else None
            else:
                record[key] = None
        result.append(record)

    logger.info("Parsed %s TDoc records from XLSX", len(result))
    return result
