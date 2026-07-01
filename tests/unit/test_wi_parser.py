"""Unit tests for the 3GPP Work Item (WI) HTML parser."""

from __future__ import annotations

from pathlib import Path

from doc3gpp.parsers.wi_parser import parse_3gpp_wis

FIXTURE_PATH = Path("tests/fixtures/wi_pages/R5.html")


def _load_fixture() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


def test_parse_extracts_real_rows() -> None:
    """Real-shape rows are mapped to Wi dataclasses with all fields populated."""
    wis = parse_3gpp_wis(_load_fixture(), tsg_short="R5")
    assert len(wis) == 6  # 5 real rows from fixture minus the no-anchor skip

    first = wis[0]
    assert first.wi_id == 31018
    assert first.acronym == "NTShar"
    assert first.release == "Rel-6"
    assert first.name == "Feature or Study Item: Network Sharing"
    assert first.tsg_short == "R5"


def test_parse_assigns_canonical_uppercase_tsg() -> None:
    """``tsg_short`` is uppercased so callers can pass either case."""
    wis = parse_3gpp_wis(_load_fixture(), tsg_short="r5")
    assert all(w.tsg_short == "R5" for w in wis)


def test_parse_collapses_whitespace_in_text() -> None:
    """Embedded newlines and tabs in the name are folded to single spaces."""
    wis = parse_3gpp_wis(_load_fixture(), tsg_short="R5")
    edge = next(w for w in wis if w.wi_id == 999991)
    assert edge.acronym == "EDGE_WS"
    assert edge.release == "Rel-99"
    # No double spaces, no trailing whitespace, no tabs.
    assert "  " not in edge.name
    assert "\t" not in edge.name
    assert edge.name.startswith("Feature or Study Item: whitespace and tabs")


def test_parse_skips_rows_without_anchor() -> None:
    """Rows lacking an <a href="...?workitemId=..."> entry are dropped."""
    wis = parse_3gpp_wis(_load_fixture(), tsg_short="R5")
    # The fixture contains one row whose first cell has no anchor -> must be skipped.
    assert all(w.wi_id != -1 for w in wis)


def test_parse_skips_rows_with_too_few_cells() -> None:
    """Rows with fewer than three <td> cells are silently dropped."""
    wis = parse_3gpp_wis(_load_fixture(), tsg_short="R5")
    # The fixture's "Single Cell Row" has only one <td> and must not appear.
    assert not any("Single Cell" in w.name for w in wis)


def test_parse_returns_empty_when_table_missing() -> None:
    """Pages without the expected ``dsp-tsgwgxwis`` table produce an empty list."""
    wis = parse_3gpp_wis("<html><body>No WIs page</body></html>", tsg_short="R5")
    assert wis == []
