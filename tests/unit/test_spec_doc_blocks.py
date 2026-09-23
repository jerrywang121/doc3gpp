# tests/unit/test_spec_doc_blocks.py
import io
from docx import Document
from doc3gpp.parsers.docx_converter import convert_document_to_blocks, HeadingBlock, ParagraphBlock, TableBlock

def _make_docx():
    doc = Document()
    doc.add_heading("1 Scope", level=1)
    doc.add_paragraph("Some text here.")
    doc.add_paragraph("Table 1: My caption")
    t = doc.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "A"
    t.cell(0, 1).text = "B"
    t.cell(1, 0).text = "C"
    t.cell(1, 1).text = "D"
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()

def test_blocks_heading_paragraph_table():
    blocks = convert_document_to_blocks(_make_docx(), "38331-j30.docx")
    assert isinstance(blocks[0], HeadingBlock) and blocks[0].level == 1
    assert blocks[0].section_no == "1" and blocks[0].title == "Scope"
    assert any(isinstance(b, ParagraphBlock) for b in blocks)
    tables = [b for b in blocks if isinstance(b, TableBlock)]
    assert len(tables) == 1 and tables[0].table_no == "1"

def test_rejects_dot_doc():
    import pytest
    with pytest.raises(ValueError):
        convert_document_to_blocks(b"xx", "old.doc")
