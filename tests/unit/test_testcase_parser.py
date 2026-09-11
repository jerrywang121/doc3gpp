"""Unit tests for the pure RAN5 testcase workbook parser (Task 5)."""

from __future__ import annotations

import io

from openpyxl import Workbook


def _wb_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "5G_TC_Status"
    ws.append(
        [
            "TC", "Title", "GCF/PTCRB", "TTCN-Status",
            "x", "x", "x", "x", "x", "x", "x", "x", "x", "x", "x",
            "GCF/PTCRB?", "TTCN Status",
            "ATS", "Feature", "Release", "RAN WIC", "part of", "TTCN-Status",
        ]
    )
    ws.append(
        [
            "TC_1", "Title1", "Approved", "Approved",
            *[None] * 11,
            "x", "Not approved",
            "ATS1", "F1", "Rel-17", "W1", "",
        ]
    )
    ws2 = wb.create_sheet("IMS_TC_Status")
    ws2.append(["TC", "Title", "GCF/PTCRB", "TTCN Status", "ATS", "Feature", "Release", "RAN WIC", "part of"])
    ws2.append(["TC_9", "T9", "g", "t", "a", "f", "Rel-18", "w", "34.229-1"])
    ws2.append(["", "skip me", "g", "t", "a", "f", "r", "w", "p"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_workbook():
    from doc3gpp.parsers.testcase_parser import parse_testcase_workbook
    cases, statuses, notes = parse_testcase_workbook(_wb_bytes())
    by_id = {c.testcase_id: c for c in cases}
    assert by_id["TC_1"].spec == "38.523-1"
    assert by_id["TC_9"].spec == "34.229-1"
    assert "TC_9" in by_id and "" not in by_id
    fr1 = [s for s in statuses if s.testcase_id == "TC_1" and s.path == "FR1"]
    assert fr1 and fr1[0].ttcn_status == "Approved"
    assert any(s.path == "default" for s in statuses if s.testcase_id == "TC_9")
    # Relabelled TTCN header at the FR2 position (col P holds "GCF/PTCRB?")
    # must cause that pair to be skipped: no FR2 status for TC_1.
    assert not [s for s in statuses if s.testcase_id == "TC_1" and s.path == "FR2"]
