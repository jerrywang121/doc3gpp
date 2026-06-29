from __future__ import annotations

import io
import re
from typing import Dict, Iterable, List, Optional

from openpyxl import load_workbook


CR_ID_RE = re.compile(r"[RSC][1-9][-sw]\d{6}(?:r\d{1})?", re.IGNORECASE)


def normalize_header(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    return re.sub(r"\s+", " ", text)


def to_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def pick_col(header_map: Dict[str, int], candidates: Iterable[str]) -> Optional[int]:
    for candidate in candidates:
        candidate_norm = normalize_header(candidate)
        for header, col in header_map.items():
            if header == candidate_norm or candidate_norm in header:
                return col
    return None


def read_tdoc_sheet(xlsx_bytes: bytes) -> List[Dict[str, str]]:
    workbook = load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    if workbook is None or not workbook.sheetnames:
        raise RuntimeError("Could not read TDoc list sheet from XLSX file.")

    worksheet = workbook.active
    rows = worksheet.iter_rows(values_only=True)
    header_map: Dict[str, int] = {}
    for row in rows:
        potential = {normalize_header(cell): idx for idx, cell in enumerate(row) if normalize_header(cell)}
        if any("tdoc" in key for key in potential):
            header_map = potential
            break

    if not header_map:
        raise RuntimeError("Could not find header row in TDoc list sheet.")

    col_tdoc = pick_col(header_map, ["Tdoc", "TDoc"])
    if col_tdoc is None:
        raise RuntimeError("Could not find TDoc column in TDoc list sheet.")

    mapping = {
        "title": pick_col(header_map, ["Title"]),
        "source": pick_col(header_map, ["Source"]),
    }

    result: List[Dict[str, str]] = []
    for row in rows:
        raw_tdoc = to_text(row[col_tdoc]) if col_tdoc < len(row) else ""
        if not raw_tdoc:
            continue

        match = CR_ID_RE.search(raw_tdoc)
        if not match:
            continue

        record = {"tdoc": match.group(0)}
        for key, col in mapping.items():
            record[key] = to_text(row[col]) if col is not None and col < len(row) else ""
        result.append(record)

    return result
