"""Tests for the body-change line-by-line extractor."""

from __future__ import annotations

from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
from doc3gpp.parsers.cr.body_changes import extract_body_changes


def test_empty_input() -> None:
    result = extract_body_changes([])
    assert result == TDocCRChangeDetails(ftp_url="", tdoc_id="")


def test_no_marker_lines_returns_empty() -> None:
    lines = [
        "## 5.2.3 Heading",
        "Plain prose, no revisions here.",
        "### 5.2.3.1 Sub-heading",
        "More plain prose.",
    ]
    result = extract_body_changes(lines)
    assert result.clauses == ()
    assert result.changes == ()


def test_single_marker_line_yields_one_block() -> None:
    lines = [
        "## 5.2.3 Heading",
        "Plain line above.",
        "<ins>[Inserted: new text]</ins>",
        "Plain line below.",
    ]
    result = extract_body_changes(lines, context_padding=1)
    assert result.clauses == ("5.2.3",)
    assert len(result.changes) == 1
    block = result.changes[0]
    assert any("Plain line above" in ln for ln in block)
    assert any("<ins>" in ln for ln in block)
    assert any("Plain line below" in ln for ln in block)


def test_table_number_added_to_clauses() -> None:
    lines = [
        "## 5.2.3 Heading",
        "Table 5.2.3-1: caption",
        "| col1 | col2 |",
        "Plain context.",
        "<ins>[Inserted: X]</ins>",
    ]
    result = extract_body_changes(lines)
    assert "5.2.3" in result.clauses
    assert "5.2.3-1" in result.clauses
    assert len(result.changes) == 1


def test_heading_terminates_block() -> None:
    lines = [
        "## 5.2.3 First",
        "<ins>[Inserted: A]</ins>",
        "## 5.2.4 Second",
        "<ins>[Inserted: B]</ins>",
    ]
    result = extract_body_changes(lines, context_padding=0)
    assert len(result.changes) == 2
    assert "5.2.3" in result.clauses
    assert "5.2.4" in result.clauses


def test_gap_window_groups_nearby_markers() -> None:
    lines = [
        "## 5.2.3",
        "<ins>[Inserted: A]</ins>",
        "Plain line 1.",
        "Plain line 2.",
        "Plain line 3.",
        "<ins>[Inserted: B]</ins>",
    ]
    # Default gap_window=2 → 2 plain lines fit in the same block.
    grouped = extract_body_changes(lines, gap_window=2, context_padding=0)
    assert len(grouped.changes) == 1
    # With gap_window=1 the 3 plain lines split it.
    split = extract_body_changes(lines, gap_window=1, context_padding=0)
    assert len(split.changes) == 2


def test_context_padding_zero_returns_only_marker_block() -> None:
    lines = [
        "Above",
        "Above-2",
        "<ins>[Inserted: X]</ins>",
        "Below",
        "Below-2",
    ]
    result = extract_body_changes(lines, context_padding=0)
    assert len(result.changes) == 1
    block = result.changes[0]
    # No "Above" / "Below" context should be present.
    assert not any("Above" in ln for ln in block)
    assert not any("Below" in ln for ln in block)
    assert any("<ins>" in ln for ln in block)


def test_block_pre_clauses_record_heading_before_run() -> None:
    """The first marker in a block should carry the heading in
    block_pre_clauses even if no heading line is inside the captured
    slice."""
    lines = [
        "## 5.2.3 Heading",
        "Plain line 1.",
        "Plain line 2.",
        "Plain line 3.",
        "<ins>[Inserted: X]</ins>",
    ]
    result = extract_body_changes(lines, context_padding=0, gap_window=2)
    assert "5.2.3" in result.clauses
