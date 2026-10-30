"""Optional real binary-Word round trip; ordinary tests double only LibreOffice."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from doc3gpp.parsers.docx_converter import (
    HeadingBlock,
    TableBlock,
    convert_document_to_blocks,
    convert_document_to_markdown,
)

OFFICE = shutil.which("libreoffice") or shutil.which("soffice")


@pytest.mark.skipif(OFFICE is None, reason="optional LibreOffice Writer executable is not installed")
def test_real_legacy_word_preserves_structured_content(tmp_path):
    pytest.importorskip("docx")
    from tests.fixtures.docx_builders import build_golden_docx

    source = tmp_path / "example.docx"
    source.write_bytes(build_golden_docx())
    completed = subprocess.run(
        [
            OFFICE, f"-env:UserInstallation={(tmp_path / 'export-profile').as_uri()}",
            "--headless", "--convert-to", "doc:MS Word 97", "--outdir", str(tmp_path), str(source),
        ],
        capture_output=True, text=True, timeout=120, check=True,
    )
    legacy = tmp_path / "example.doc"
    assert legacy.is_file(), completed.stdout + completed.stderr
    payload = legacy.read_bytes()
    assert payload.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))  # Actual OLE .doc, not renamed OOXML.

    markdown = convert_document_to_markdown(payload, "example.doc")
    assert "1 Scope" in markdown
    assert "Spanned body" in markdown
    assert "inserted text" in markdown
    assert "deleted text" in markdown
    blocks = convert_document_to_blocks(payload, "example.doc")
    assert any(isinstance(block, HeadingBlock) and block.section_no == "1" for block in blocks)
    assert any(isinstance(block, TableBlock) and "Header A" in block.gfm for block in blocks)
