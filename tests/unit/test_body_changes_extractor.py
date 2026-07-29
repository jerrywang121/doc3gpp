"""Tests for the body-change line-by-line extractor."""

from __future__ import annotations

from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
from doc3gpp.parsers.cr.body_changes import extract_body_changes


def test_empty_input() -> None:
    result = extract_body_changes([])
    assert result == TDocCRChangeDetails(ftp_url=None, tdoc_id=None)


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
        "<ins>new text</ins>",
        "Plain line below.",
    ]
    result = extract_body_changes(lines, context_padding=1)
    assert "5.2.3" in result.clauses
    assert len(result.changes) == 1
    block = result.changes[0]
    # Each block is a dict with a concatenated ``text`` field.
    assert isinstance(block, dict)
    text = block["text"]
    assert "Plain line above." in text
    assert "<ins>new text</ins>" in text
    assert "Plain line below." in text


def test_table_number_added_to_clauses() -> None:
    lines = [
        "## 5.2.3 Heading",
        "Table 5.2.3-1: caption",
        "| col1 | col2 |",
        "Plain context.",
        "<ins>X</ins>",
    ]
    result = extract_body_changes(lines)
    assert "5.2.3" in result.clauses
    assert "Table 5.2.3-1" in result.clauses
    assert len(result.changes) == 1


def test_heading_terminates_block() -> None:
    lines = [
        "## 5.2.3 First",
        "<ins>A</ins>",
        "## 5.2.4 Second",
        "<ins>B</ins>",
    ]
    result = extract_body_changes(lines, context_padding=0)
    assert len(result.changes) == 2
    assert "5.2.3" in result.clauses
    assert "5.2.4" in result.clauses


def test_gap_window_groups_nearby_markers() -> None:
    lines = [
        "## 5.2.3",
        "<ins>A</ins>",
        "Plain line 1.",
        "Plain line 2.",
        "Plain line 3.",
        "<ins>B</ins>",
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
        "<ins>X</ins>",
        "Below",
        "Below-2",
    ]
    result = extract_body_changes(lines, context_padding=0)
    assert len(result.changes) == 1
    block = result.changes[0]
    # No "Above" / "Below" context should be present.
    text = block["text"]
    assert "Above" not in text
    assert "Below" not in text
    assert "<ins>X</ins>" in text


def test_block_pre_clauses_record_heading_before_run() -> None:
    """The first marker in a block should carry the heading in
    block_pre_clauses even if no heading line is inside the captured
    slice."""
    lines = [
        "## 5.2.3 Heading",
        "Plain line 1.",
        "Plain line 2.",
        "Plain line 3.",
        "<ins>X</ins>",
    ]
    result = extract_body_changes(lines, context_padding=0, gap_window=2)
    assert "5.2.3" in result.clauses


# ---------------------------------------------------------------------------
# New-shape tests (clauses prefix, (new)/(del) suffixes, per-block dict,
# concatenated text).
# ---------------------------------------------------------------------------


def test_table_number_in_clauses_is_prefixed() -> None:
    """Captured table-derived clauses carry a ``Table `` prefix."""
    lines = [
        "## 5.2.3 Heading",
        "Table 5.2.3-1: caption",
        "| col1 | col2 |",
        "Plain context.",
        "<ins>X</ins>",
    ]
    result = extract_body_changes(lines)
    assert "Table 5.2.3-1" in result.clauses


def test_per_block_dict_has_clauses_and_text_keys() -> None:
    """Each item in ``result.changes`` is a dict with ``clauses`` and
    ``text`` keys."""
    lines = [
        "## 5.2.3 Heading",
        "<ins>X</ins>",
    ]
    result = extract_body_changes(lines)
    assert len(result.changes) == 1
    block = result.changes[0]
    assert isinstance(block, dict)
    assert set(block.keys()) >= {"clauses", "text"}
    assert isinstance(block["clauses"], list)
    assert isinstance(block["text"], str)


def test_change_block_text_is_concatenated_lines() -> None:
    """Each block's ``text`` is the concatenation of the captured lines
    joined with newlines (not a list of lines)."""
    lines = [
        "## 5.2.3 Heading",
        "Plain above.",
        "<ins>X</ins>",
        "Plain below.",
    ]
    result = extract_body_changes(lines, context_padding=1)
    block = result.changes[0]
    text = block["text"]
    assert isinstance(text, str)
    assert "\n" in text
    assert "Plain above." in text
    assert "<ins>X</ins>" in text
    assert "Plain below." in text


def test_block_clauses_include_heading_above_run() -> None:
    """The clauses list inside the dict contains the heading that was
    immediately above the run when it started."""
    lines = [
        "## 5.2.3 Heading",
        "Plain line 1.",
        "<ins>X</ins>",
    ]
    result = extract_body_changes(lines, context_padding=0)
    assert "5.2.3" in result.changes[0]["clauses"]


def test_inside_block_new_heading_recorded_with_new_suffix() -> None:
    """A heading that lives inside a marker line gets recorded with the
    ``(new)`` suffix in the block's clauses list."""
    lines = [
        "## 5.2.3 Heading",
        "<ins># 5.2.4 New heading</ins>",
    ]
    result = extract_body_changes(lines)
    block = result.changes[0]
    assert "5.2.4 (new)" in block["clauses"]


def test_inside_block_deleted_heading_recorded_with_del_suffix() -> None:
    """A heading that lives inside a <del> marker line gets the
    ``(del)`` suffix."""
    lines = [
        "## 5.2.3 Heading",
        "<del># 5.2.5 Old heading</del>",
        "<ins>X</ins>",
    ]
    result = extract_body_changes(lines)
    block = result.changes[0]
    assert "5.2.5 (del)" in block["clauses"]


def test_inside_block_new_table_recorded_with_new_suffix() -> None:
    """A table caption inside <ins> markers is recorded as
    ``Table <n> (new)``."""
    lines = [
        "## 5.2.3 Heading",
        "<ins>Table 5.2.3-2: caption</ins>",
        "<ins>X</ins>",
    ]
    result = extract_body_changes(lines)
    block = result.changes[0]
    assert "Table 5.2.3-2 (new)" in block["clauses"]


def test_renumbered_heading_produces_both_del_and_new() -> None:
    """When a heading's number is partly deleted and partly inserted
    (renumbering), both the old and new numbers appear with their
    respective suffixes."""
    lines = [
        "## 5.2.3 Heading",
        "<ins># 5.2.3</ins><del># 5.2.4</del>",
    ]
    result = extract_body_changes(lines)
    block = result.changes[0]
    # Both numbers should appear with their suffixes.
    has_new = any(c == "5.2.3 (new)" for c in block["clauses"])
    has_del = any(c == "5.2.4 (del)" for c in block["clauses"])
    assert has_new, f"missing 5.2.3 (new) in {block['clauses']!r}"
    assert has_del, f"missing 5.2.4 (del) in {block['clauses']!r}"


def test_renumbered_table_produces_both_del_and_new() -> None:
    """A table caption with a renumbered suffix produces both numbers."""
    lines = [
        "## 5.2.3 Heading",
        "<del>Table 5.3.1-3: caption</del>",
        "<ins>Table 5.3.1-4: caption</ins>",
    ]
    result = extract_body_changes(lines)
    block = result.changes[0]
    # The deleted old number should be marked (del) and the new
    # number should be marked (new).
    has_new = any(c == "Table 5.3.1-4 (new)" for c in block["clauses"])
    has_del = any(c == "Table 5.3.1-3 (del)" for c in block["clauses"])
    assert has_new, f"missing Table 5.3.1-4 (new) in {block['clauses']!r}"
    assert has_del, f"missing Table 5.3.1-3 (del) in {block['clauses']!r}"
