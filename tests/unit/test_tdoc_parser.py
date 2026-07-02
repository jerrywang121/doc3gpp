from __future__ import annotations

import io
import logging

from openpyxl import Workbook

from doc3gpp.parsers.tdoc_parser import (
    _parse_date_cell,
    pick_col,
    read_tdoc_sheet,
    to_text,
)


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


# ---------------------------------------------------------------------------
# #23: to_text returns None for missing AND empty cells (not "").
# ---------------------------------------------------------------------------


def test_to_text_none_returns_none() -> None:
    assert to_text(None) is None


def test_to_text_empty_string_returns_none() -> None:
    assert to_text("") is None


def test_to_text_whitespace_returns_none() -> None:
    assert to_text("   \t\n ") is None


def test_to_text_trims_surrounding_whitespace() -> None:
    assert to_text("  hello  ") == "hello"


def test_to_text_coerces_non_string() -> None:
    assert to_text(42) == "42"
    assert to_text(3.14) == "3.14"


# ---------------------------------------------------------------------------
# #8: pick_col must prefer exact match over substring (fixes "Type" vs "Type of CR").
# ---------------------------------------------------------------------------


def test_pick_col_prefers_exact_match_over_substring() -> None:
    # When both "Type" and "Type of CR" columns are present, "Type" must
    # resolve to its own column, not "Type of CR" via substring.
    header_map = {
        "tdoc": 0,
        "title": 1,
        "type": 2,
        "type of cr": 3,
    }

    assert pick_col(header_map, ["Type"]) == 2


def test_pick_col_falls_back_to_substring_when_no_exact() -> None:
    # If no exact "Type" header exists, substring match is the fallback.
    header_map = {
        "tdoc": 0,
        "title": 1,
        "type of cr": 3,
    }

    assert pick_col(header_map, ["Type"]) == 3


def test_pick_col_returns_none_when_no_match() -> None:
    assert pick_col({"foo": 0, "bar": 1}, ["Type"]) is None


# ---------------------------------------------------------------------------
# #9: skipped (non-TDoc) rows are counted and logged at WARNING.
# ---------------------------------------------------------------------------


def test_skipped_rows_trigger_warning_log(caplog) -> None:
    xlsx_bytes = _make_xlsx_bytes(
        [
            ["TDoc", "Title", "Source", "Type"],
            # First two rows are valid TDoc IDs.
            ["R5-260001", "Doc A", "Acme", "CR"],
            ["R5-260002", "Doc B", "Acme", "CR"],
            # The next three are NOT TDoc IDs (regex miss).
            ["meeting_minutes", "Minutes", "WG", "info"],
            ["attendance_list", "Attendance", "WG", "info"],
            ["agenda", "Agenda", "WG", "info"],
        ]
    )

    with caplog.at_level(logging.WARNING, logger="doc3gpp.parsers.tdoc_parser"):
        records = read_tdoc_sheet(xlsx_bytes)

    assert len(records) == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Skipped 3 row(s)" in r.getMessage() for r in warnings)


def test_no_skipped_rows_no_warning(caplog) -> None:
    xlsx_bytes = _make_xlsx_bytes(
        [
            ["TDoc", "Title", "Source", "Type"],
            ["R5-260001", "Doc A", "Acme", "CR"],
        ]
    )

    with caplog.at_level(logging.WARNING, logger="doc3gpp.parsers.tdoc_parser"):
        read_tdoc_sheet(xlsx_bytes)

    skip_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Skipped" in r.getMessage()
    ]
    assert skip_warnings == []


# ---------------------------------------------------------------------------
# #6 parser: reservation_date / uploaded_date are parsed as date objects.
# ---------------------------------------------------------------------------


def test_reservation_and_uploaded_dates_parsed_as_date() -> None:
    xlsx_bytes = _make_xlsx_bytes(
        [
            ["TDoc", "Title", "Reservation Date", "Uploaded Date"],
            ["R5-260001", "Doc A", "2026-06-01", "2026-06-02"],
        ]
    )

    import datetime

    records = read_tdoc_sheet(xlsx_bytes)
    assert records[0]["reservation_date"] == datetime.date(2026, 6, 1)
    assert records[0]["uploaded_date"] == datetime.date(2026, 6, 2)


def test_unparseable_date_becomes_none() -> None:
    xlsx_bytes = _make_xlsx_bytes(
        [
            ["TDoc", "Title", "Reservation Date", "Uploaded Date"],
            ["R5-260001", "Doc A", "not-a-date", "2026-06-02"],
        ]
    )

    records = read_tdoc_sheet(xlsx_bytes)
    assert records[0]["reservation_date"] is None
    assert records[0]["uploaded_date"] is not None


def test_empty_date_cell_is_none() -> None:
    xlsx_bytes = _make_xlsx_bytes(
        [
            ["TDoc", "Title", "Reservation Date", "Uploaded Date"],
            ["R5-260001", "Doc A", "", ""],
        ]
    )

    records = read_tdoc_sheet(xlsx_bytes)
    assert records[0]["reservation_date"] is None
    assert records[0]["uploaded_date"] is None


def test_parse_date_cell_handles_datetime_instances() -> None:
    import datetime

    dt = datetime.datetime(2026, 6, 1, 12, 30)
    assert _parse_date_cell(dt) == datetime.date(2026, 6, 1)


def test_parse_date_cell_handles_none() -> None:
    assert _parse_date_cell(None) is None