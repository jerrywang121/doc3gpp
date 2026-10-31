"""Contract tests for the DOCX member pool in ``doc3gpp.parsers.docx_converter``.

The inline pool runs the matching worker in-process and captures each
member's exception as its outcome, so one bad member never aborts the batch.
"""

from __future__ import annotations

from doc3gpp.parsers.docx_converter import (
    InlineDocxPool,
    ParagraphBlock,
    convert_document_to_blocks,
    convert_document_to_markdown,
)
from tests.fixtures.docx_builders import build_golden_docx
from tests.fixtures.presentation_builders import (
    build_empty_presentation,
    build_presentation_with_content,
)


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


def test_inline_pool_captures_unsupported_format_as_value_error() -> None:
    outcomes = InlineDocxPool().convert_members("markdown", [("unsupported.pdf", build_golden_docx())])
    assert isinstance(outcomes[0], ValueError)


def test_inline_pool_returns_outcomes_in_input_order() -> None:
    golden = build_golden_docx()
    outcomes = InlineDocxPool().convert_members(
        "markdown",
        [
            ("first.docx", golden),
            ("second.pdf", golden),
            ("third.docx", b"not a zip"),
        ],
    )
    assert len(outcomes) == 3
    assert outcomes[0] == convert_document_to_markdown(golden, "first.docx")
    assert isinstance(outcomes[1], ValueError)
    assert isinstance(outcomes[2], Exception)



def test_inline_pool_dispatches_mixed_presentation_markdown_in_order() -> None:
    word = build_golden_docx()
    presentation = build_presentation_with_content()
    outcomes = InlineDocxPool().convert_members(
        "markdown", [("word.docx", word), ("folder/SLIDES.PPTX", presentation)]
    )

    assert len(outcomes) == 2
    assert outcomes[0] == convert_document_to_markdown(word, "word.docx")
    assert isinstance(outcomes[1], str)
    assert "## Slide 1" in outcomes[1] and "日本語" in outcomes[1]
    assert "## Slide 2" in outcomes[1] and "speaker secret" in outcomes[1]


def test_inline_pool_wraps_presentation_markdown_as_one_paragraph() -> None:
    word = build_golden_docx()
    presentation = build_presentation_with_content()
    outcomes = InlineDocxPool().convert_members(
        "blocks", [("word.docx", word), ("slides.pptx", presentation)]
    )

    assert len(outcomes) == 2
    assert outcomes[0] == convert_document_to_blocks(word, "word.docx")
    assert isinstance(outcomes[1], list)
    assert len(outcomes[1]) == 1
    assert isinstance(outcomes[1][0], ParagraphBlock)
    markdown = convert_document_to_markdown(presentation, "slides.pptx")
    assert outcomes[1] == [ParagraphBlock(markdown)]


def test_inline_pool_returns_no_blocks_for_empty_presentation() -> None:
    outcomes = InlineDocxPool().convert_members(
        "blocks", [("empty.pptx", build_empty_presentation())]
    )

    assert outcomes == [[]]
