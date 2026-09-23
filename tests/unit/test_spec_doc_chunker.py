from doc3gpp.parsers.docx_converter import HeadingBlock, ParagraphBlock, TableBlock
from doc3gpp.parsers.spec_doc_chunker import chunk_blocks


def test_paragraph_chunks_carry_section():
    blocks = [HeadingBlock(1, "5", "Intro", "# 5 Intro"), ParagraphBlock("alpha beta gamma delta epsilon")]
    chunks = chunk_blocks(blocks, chunk_size=3, chunk_overlap=1, max_chunk_chars=1500)
    assert len(chunks) >= 2 and all(c.section_no == "5" for c in chunks)
    assert any("alpha" in c.text for c in chunks)
    assert all(c.file_order == 0 and c.source_file == "" for c in chunks)


def test_file_tags_propagate():
    blocks = [ParagraphBlock("hello world")]
    chunks = chunk_blocks(blocks, chunk_size=512, chunk_overlap=0, max_chunk_chars=1500,
        file_order=2, source_file="b.docx")
    assert len(chunks) == 1 and chunks[0].file_order == 2 and chunks[0].source_file == "b.docx"


def test_fitting_table_stays_whole():
    # Fitting = fits the char ceiling: kept whole even though 15 tokens overflow chunk_size=2.
    gfm = "| A | B |\n| --- | --- |\n| C | D |"
    blocks = [TableBlock(gfm, "1", "Cap")]
    chunks = chunk_blocks(blocks, chunk_size=2, chunk_overlap=0, max_chunk_chars=5000)
    assert len(chunks) == 1 and chunks[0].table_no == "1"


def test_oversized_table_splits_rowwise_with_repeated_meta():
    rows = "\n".join(f"| r{i} | v{i} |" for i in range(10))
    gfm = "| A | B |\n| --- | --- |\n" + rows
    blocks = [TableBlock(gfm, "2", "Big")]
    chunks = chunk_blocks(blocks, chunk_size=100, chunk_overlap=0, max_chunk_chars=60)
    assert len(chunks) > 1
    assert all(c.table_no == "2" and c.table_title == "Big" for c in chunks)
    assert all(len(c.text) <= 600 for c in chunks)  # header repeated + rows bounded


def test_char_ceiling_wins():
    # Overlap 0 isolates the ceiling: a single 500-word sentence must split into token
    # windows bounded by max_chunk_chars.
    blocks = [ParagraphBlock("word " * 500)]
    chunks = chunk_blocks(blocks, chunk_size=512, chunk_overlap=0, max_chunk_chars=100)
    assert len(chunks) > 1 and all(len(c.text) <= 100 for c in chunks)
