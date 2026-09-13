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


def _lte_mcx_wb_bytes() -> bytes:
    """Workbook with an LTE sheet (FDD+TDD pair) and an MCX sheet (prefix strip).

    LTE ``STATUS_PAIRS`` are ``(2, 3, "FDD")`` + ``(15, 16, "TDD")``
    (0-based); the header therefore carries the GCF/TTCN pair at
    indices 2/3 and again at 15/16. MCX's first pair is ``(2, 3,
    "IPCAN-4G")``; the remaining MCX pairs point past the short
    header and are skipped with warnings.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "LTE_TC_Status"
    ws.append(
        ["TC", "Title", "GCF/PTCRB", "TTCN-Status"]
        + ["x"] * 11
        + ["GCF/PTCRB", "TTCN-Status"]
    )
    ws.append(
        ["TC_L1", "LteTitle", "Approved", "Approved"]
        + [None] * 11
        + ["g-tdd", "t-tdd"]
    )
    ws2 = wb.create_sheet("MCX_TC_Status")
    ws2.append(["TC", "Title", "GCF/PTCRB", "TTCN-Status"])
    ws2.append(["TC_M1", "McxTitle", "Approved", "t"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_workbook_lte_tdd_pair():
    """The LTE sheet emits both the FDD pair and the TDD pair."""
    from doc3gpp.parsers.testcase_parser import parse_testcase_workbook
    cases, statuses, _notes = parse_testcase_workbook(_lte_mcx_wb_bytes())
    by_id = {c.testcase_id: c for c in cases}
    assert by_id["TC_L1"].spec == "36.523-1"
    paths = {s.path for s in statuses if s.testcase_id == "TC_L1"}
    assert paths == {"FDD", "TDD"}


def test_parse_workbook_mcx_prefix_stripped():
    """Stored MCX paths are the stripped vocab entries, not raw headers."""
    from doc3gpp.parsers.testcase_parser import parse_testcase_workbook
    cases, statuses, _notes = parse_testcase_workbook(_lte_mcx_wb_bytes())
    by_id = {c.testcase_id: c for c in cases}
    assert by_id["TC_M1"].spec is None
    mcx_paths = [s.path for s in statuses if s.testcase_id == "TC_M1"]
    assert mcx_paths == ["IPCAN-4G"]
    assert all(not p.lower().startswith("mcx-") for p in mcx_paths)


def test_parse_workbook_skips_duplicate_tc_across_sheets():
    """A TC id repeated in a later sheet keeps both group rows.

    Identity is ``(testcase_id, group)``: the same TC id in IMS and
    UTRA yields two headers, each with its own group-scoped status.
    """
    from doc3gpp.parsers.testcase_parser import parse_testcase_workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "IMS_TC_Status"
    ws.append(
        ["TC", "Title", "GCF/PTCRB", "TTCN Status", "ATS", "Feature",
         "Release", "RAN WIC", "part of"]
    )
    ws.append(["TC_D", "ImsTitle", "g", "t", "a", "f", "r", "w", "34.229-1"])
    ws2 = wb.create_sheet("UTRA_TC_Status")
    ws2.append(
        ["TC", "Title", "GCF/PTCRB", "TTCN Status", "ATS", "Feature",
         "Release", "RAN WIC", "part of"]
    )
    ws2.append(["TC_D", "UtraTitle", "g2", "t2", "a2", "f2", "r2", "w2", "p2"])
    buf = io.BytesIO()
    wb.save(buf)
    cases, statuses, _notes = parse_testcase_workbook(buf.getvalue())
    dupes = [c for c in cases if c.testcase_id == "TC_D"]
    assert len(dupes) == 2
    by_group = {c.group: c for c in dupes}
    assert by_group["IMS"].title == "ImsTitle"
    assert by_group["IMS"].spec == "34.229-1"
    assert by_group["UTRA"].title == "UtraTitle"
    assert len([s for s in statuses if s.testcase_id == "TC_D"]) == 2
    assert {(s.group, s.ttcn_status) for s in statuses if s.testcase_id == "TC_D"} == {
        ("IMS", "t"),
        ("UTRA", "t2"),
    }
