"""Contract tests for the DOCX member pool in ``doc3gpp.parsers.docx_converter``.

The inline pool runs the matching worker in-process and captures each
member's exception as its outcome, so one bad member never aborts the batch.
"""

from __future__ import annotations

from doc3gpp.parsers.docx_converter import (
    InlineDocxPool,
    convert_document_to_blocks,
    convert_document_to_markdown,
)
from tests.fixtures.docx_builders import build_golden_docx


def test_inline_pool_markdown_matches_direct_conversion() -> None:
    golden = build_golden_docx()
    outcome = InlineDocxPool().convert_members("markdown", [("a.docx", golden)])[0]
    assert outcome == convert_document_to_markdown(golden, "a.docx")


def test_inline_pool_blocks_match_direct_conversion() -> None:
    golden = build_golden_docx()
    outcome = InlineDocxPool().convert_members("blocks", [("a.docx", golden)])[0]
    assert outcome == convert_document_to_blocks(golden, "a.docx")


def test_inline_pool_captures_invalid_bytes_as_exception() -> None:
    outcomes = InlineDocxPool().convert_members("markdown", [("x.docx", b"not a zip")])
    assert isinstance(outcomes[0], Exception)


def test_inline_pool_captures_legacy_doc_as_value_error() -> None:
    outcomes = InlineDocxPool().convert_members("markdown", [("legacy.doc", build_golden_docx())])
    assert isinstance(outcomes[0], ValueError)


def test_inline_pool_returns_outcomes_in_input_order() -> None:
    golden = build_golden_docx()
    outcomes = InlineDocxPool().convert_members(
        "markdown",
        [
            ("first.docx", golden),
            ("second.doc", golden),
            ("third.docx", b"not a zip"),
        ],
    )
    assert len(outcomes) == 3
    assert outcomes[0] == convert_document_to_markdown(golden, "first.docx")
    assert isinstance(outcomes[1], ValueError)
    assert isinstance(outcomes[2], Exception)
