from __future__ import annotations

import io

from openpyxl import Workbook

from doc3gpp.parsers.tdoc_parser import read_tdoc_sheet


def _make_xlsx_bytes(rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fix 2: header detection must reject title-only rows that happen to mention
# "tdoc" (e.g. "TDoc List — RAN5#111").
# ---------------------------------------------------------------------------


def test_title_only_row_is_not_treated_as_header() -> None:
    xlsx_bytes = _make_xlsx_bytes(
        [
            ["TDoc List — RAN5#111", "", "", ""],
            ["TDoc", "Title", "Source", "Type"],
            ["R5-260001", "Agenda", "WG Chair", "agenda"],
        ]
    )

    records = read_tdoc_sheet(xlsx_bytes)

    assert len(records) == 1
    assert records[0]["tdoc"] == "R5-260001"
    assert records[0]["title"] == "Agenda"
    assert records[0]["source"] == "WG Chair"
    assert records[0]["type"] == "agenda"


def test_real_header_detected_on_first_attempt() -> None:
    xlsx_bytes = _make_xlsx_bytes(
        [
            ["TDoc", "Title", "Source", "Type"],
            ["R5-260001", "Doc A", "Acme", "CR"],
            ["R5-260002", "Doc B", "Acme", "CR"],
        ]
    )

    records = read_tdoc_sheet(xlsx_bytes)

    assert len(records) == 2
    assert [r["tdoc"] for r in records] == ["R5-260001", "R5-260002"]


def test_multiple_intro_rows_before_real_header() -> None:
    xlsx_bytes = _make_xlsx_bytes(
        [
            ["3GPP TSG-RAN WG5 #111", "", "", ""],
            ["Document Listing", "", "", ""],
            ["TDoc", "Title", "Source", "Status"],
            ["R5-260001", "Doc A", "Acme", "Agreed"],
        ]
    )

    records = read_tdoc_sheet(xlsx_bytes)

    assert len(records) == 1
    assert records[0]["status"] == "Agreed"


def test_tdoc_row_alone_is_not_header() -> None:
    # A row with only a TDoc column and no other expected header marker must
    # be rejected; the parser should walk past it.
    xlsx_bytes = _make_xlsx_bytes(
        [
            ["TDoc"],
            ["TDoc", "Title", "Source", "Type"],
            ["R5-260001", "Doc", "Acme", "CR"],
        ]
    )

    records = read_tdoc_sheet(xlsx_bytes)

    assert len(records) == 1
    assert records[0]["title"] == "Doc"


def test_no_header_row_raises_runtime_error() -> None:
    # No row has both "tdoc" and any other marker — parser must give up.
    xlsx_bytes = _make_xlsx_bytes(
        [
            ["Random Title", "value"],
            ["Some other", "thing"],
        ]
    )

    import pytest

    with pytest.raises(RuntimeError, match="Could not find header row"):
        read_tdoc_sheet(xlsx_bytes)


# ---------------------------------------------------------------------------
# Fix 3: empty cells must surface as None, not "".
# ---------------------------------------------------------------------------


def test_empty_cells_become_none() -> None:
    xlsx_bytes = _make_xlsx_bytes(
        [
            ["TDoc", "Title", "Source", "Type"],
            ["R5-260001", "", "", ""],
        ]
    )

    records = read_tdoc_sheet(xlsx_bytes)

    assert len(records) == 1
    assert records[0]["tdoc"] == "R5-260001"
    assert records[0]["title"] is None
    assert records[0]["source"] is None
    assert records[0]["type"] is None


def test_title_required_key_present_with_value() -> None:
    xlsx_bytes = _make_xlsx_bytes(
        [
            ["TDoc", "Title", "Source", "Type"],
            ["R5-260001", "A real title", "Acme", "CR"],
        ]
    )

    records = read_tdoc_sheet(xlsx_bytes)

    assert records[0]["title"] == "A real title"
    assert records[0]["source"] == "Acme"


def test_whitespace_only_cell_becomes_none() -> None:
    xlsx_bytes = _make_xlsx_bytes(
        [
            ["TDoc", "Title", "Source", "Type"],
            ["R5-260001", "   ", "\t", "CR"],
        ]
    )

    records = read_tdoc_sheet(xlsx_bytes)

    assert records[0]["title"] is None
    assert records[0]["source"] is None
    assert records[0]["type"] == "CR"