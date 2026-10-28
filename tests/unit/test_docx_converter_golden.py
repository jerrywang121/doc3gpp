"""Golden characterisation of the DOCX converter output.

Pins the markdown and block output of the synthetic document from
:func:`tests.fixtures.docx_builders.build_golden_docx`. A converter change
that alters either output must regenerate the fixtures deliberately.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from doc3gpp.parsers.docx_converter import (
    convert_document_to_blocks,
    convert_document_to_markdown,
)
from tests.fixtures.docx_builders import build_golden_branches_docx, build_golden_docx

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "docx_golden"
EXPECTED_MD = (GOLDEN_DIR / "expected.md").read_text(encoding="utf-8")
EXPECTED_BLOCKS = json.loads(
    (GOLDEN_DIR / "expected_blocks.json").read_text(encoding="utf-8")
)
EXPECTED_BRANCHES_MD = (GOLDEN_DIR / "expected_branches.md").read_text(encoding="utf-8")
EXPECTED_BRANCHES_BLOCKS = json.loads(
    (GOLDEN_DIR / "expected_branches_blocks.json").read_text(encoding="utf-8")
)


def _as_json(block) -> dict:
    return {"type": type(block).__name__, **dataclasses.asdict(block)}


def test_golden_markdown_matches_expected() -> None:
    assert convert_document_to_markdown(build_golden_docx(), "golden.docx") == EXPECTED_MD


def test_golden_blocks_match_expected() -> None:
    blocks = convert_document_to_blocks(build_golden_docx(), "golden.docx")
    assert [_as_json(b) for b in blocks] == EXPECTED_BLOCKS


def test_branches_markdown_matches_expected() -> None:
    markdown = convert_document_to_markdown(build_golden_branches_docx(), "golden_branches.docx")
    assert markdown == EXPECTED_BRANCHES_MD


def test_branches_blocks_match_expected() -> None:
    blocks = convert_document_to_blocks(build_golden_branches_docx(), "golden_branches.docx")
    assert [_as_json(b) for b in blocks] == EXPECTED_BRANCHES_BLOCKS
