from doc3gpp.parsers.docx_converter import HeadingBlock, ParagraphBlock, TableBlock
from doc3gpp.parsers.spec_doc_chunker import chunk_blocks


def test_paragraph_chunks_carry_section():
    blocks = [HeadingBlock(1, "5", "Intro", "# 5 Intro"), ParagraphBlock("alpha beta gamma delta epsilon")]
    chunks = chunk_blocks(blocks, chunk_size=3, chunk_overlap=1, max_chunk_chars=1500)
    assert len(chunks) >= 2 and all(c.sections == "5 Intro" for c in chunks)
    assert any("alpha" in c.text for c in chunks)
    assert all(c.file_order == 0 and c.source_file == "" for c in chunks)


def test_chunk_collects_sections_spanning_one_chunk():
    blocks = [
        HeadingBlock(1, "7.2A.3", "Scope", "# 7.2A.3 Scope"),
        ParagraphBlock("alpha beta"),
        HeadingBlock(2, "7.2A.3A", "Details", "## 7.2A.3A Details"),
        ParagraphBlock("gamma delta"),
    ]
    chunks = chunk_blocks(blocks, chunk_size=20, chunk_overlap=0, max_chunk_chars=1500)
    assert chunks[0].sections == "7.2A.3 Scope\n7.2A.3A Details"


def test_chunk_metadata_does_not_retain_historical_sections():
    blocks = [
        HeadingBlock(1, None, "Foreword", "# Foreword"),
        ParagraphBlock("foreword text"),
        HeadingBlock(1, "3.3", "Abbreviations", "# 3.3 Abbreviations"),
        ParagraphBlock("abbreviation text"),
        HeadingBlock(1, "4.1", "Environmental conditions", "# 4.1 Environmental conditions"),
        ParagraphBlock("environment text"),
        HeadingBlock(2, "4.1.1", "Temperature", "## 4.1.1 Temperature"),
        ParagraphBlock("temperature text"),
        HeadingBlock(2, "4.1.2", "Voltage", "## 4.1.2 Voltage"),
        ParagraphBlock("voltage text"),
    ]

    chunks = chunk_blocks(blocks, chunk_size=6, chunk_overlap=0, max_chunk_chars=1500)

    assert chunks[-1].sections == "4.1.2 Voltage"
    assert "Foreword" not in (chunks[-1].sections or "")
    assert "3.3 Abbreviations" not in (chunks[-1].sections or "")


def test_heading_markdown_is_kept_in_chunk_text():
    chunks = chunk_blocks(
        [HeadingBlock(3, "3.3", "Abbreviations", "### 3.3 Abbreviations"), ParagraphBlock("body")],
        chunk_overlap=0,
    )

    assert "### 3.3 Abbreviations" in chunks[0].text
    assert "body" in chunks[0].text


def test_chunk_collects_multiple_tables():
    blocks = [
        TableBlock("| A |\n| --- |\n| 1 |", "7.2A.3", "First values"),
        TableBlock("| B |\n| --- |\n| 2 |", "7.2A.3A", "Second values"),
    ]
    chunks = chunk_blocks(blocks, chunk_size=100, chunk_overlap=0, max_chunk_chars=1500)
    assert chunks[0].tables == "7.2A.3 First values\n7.2A.3A Second values"


def test_overlap_carries_previous_section_metadata():
    blocks = [
        HeadingBlock(1, "5", "Previous", "# 5 Previous"),
        ParagraphBlock("one two three four"),
        HeadingBlock(1, "6", "Current", "# 6 Current"),
        ParagraphBlock("five six seven eight"),
    ]
    chunks = chunk_blocks(blocks, chunk_size=4, chunk_overlap=2, max_chunk_chars=1500)
    assert chunks[1].text.startswith("three four")
    assert "5 Previous" in (chunks[1].sections or "")
    assert "6 Current" in (chunks[1].sections or "")


def test_overlap_carries_only_metadata_from_trailing_section_tokens():
    blocks = [
        HeadingBlock(1, "5", "Earlier", "# 5 Earlier"),
        ParagraphBlock("one two"),
        HeadingBlock(1, "6", "Later", "# 6 Later"),
        ParagraphBlock("three four"),
        ParagraphBlock("five six"),
    ]

    chunks = chunk_blocks(blocks, chunk_size=4, chunk_overlap=2, max_chunk_chars=1500)

    assert chunks[0].sections == "5 Earlier\n6 Later"
    assert chunks[1].text.startswith("three four")
    assert chunks[1].sections == "6 Later"


def test_table_metadata_and_caption_text_are_scoped_to_chunk():
    first = "| A |\n| --- |\n| 1 |"
    second = "| B |\n| --- |\n| 2 |"
    chunks = chunk_blocks(
        [
            ParagraphBlock("Table 1: First values"),
            TableBlock(first, "1", "First values"),
            TableBlock(second, "2", "Second values"),
        ],
        chunk_size=8,
        chunk_overlap=0,
        max_chunk_chars=1500,
    )

    assert chunks[0].tables == "1 First values"
    assert "Table 1: First values" in chunks[0].text
    assert first in chunks[0].text
    assert chunks[1].tables == "2 Second values"
    assert "1 First values" not in (chunks[1].tables or "")
    assert second in chunks[1].text


def test_overlap_carries_only_metadata_for_copied_tokens():
    chunks = chunk_blocks(
        [
            HeadingBlock(1, "5", "Earlier", "# 5 Earlier"),
            ParagraphBlock("one two"),
            HeadingBlock(1, "6", "Later", "# 6 Later"),
            ParagraphBlock("three four"),
            ParagraphBlock("five six"),
        ],
        chunk_size=4,
        chunk_overlap=2,
        max_chunk_chars=1500,
    )

    assert chunks[1].text.startswith("three four")
    assert chunks[1].sections == "6 Later"


def test_overlap_carries_only_metadata_from_trailing_table_tokens():
    first = "| A |\n| --- |\n| 1 |"
    second = "| B |\n| --- |\n| 2 |"
    blocks = [
        TableBlock(first, "1", "Earlier"),
        TableBlock(second, "2", "Later"),
        ParagraphBlock("after table"),
    ]

    chunks = chunk_blocks(blocks, chunk_size=18, chunk_overlap=3, max_chunk_chars=1500)

    assert chunks[0].tables == "1 Earlier\n2 Later"
    assert chunks[1].text.startswith("| 2 | after table")
    assert chunks[1].tables == "2 Later"


def test_repeated_metadata_is_deduplicated_in_source_order():
    blocks = [
        HeadingBlock(1, "5", "Scope", "# 5 Scope"),
        ParagraphBlock("one two"),
        HeadingBlock(2, "5", "Scope", "## 5 Scope"),
        ParagraphBlock("three four"),
        TableBlock("| A |\n| --- |\n| 1 |", "1", "Values"),
        TableBlock("| B |\n| --- |\n| 2 |", "1", "Values"),
    ]

    chunks = chunk_blocks(blocks, chunk_size=100, chunk_overlap=0, max_chunk_chars=1500)

    assert chunks[0].sections == "5 Scope"
    assert chunks[0].tables == "1 Values"


def test_empty_metadata_is_none():
    chunks = chunk_blocks([ParagraphBlock("plain text")], chunk_overlap=0)
    assert chunks[0].sections is None
    assert chunks[0].tables is None


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
    assert len(chunks) == 1 and chunks[0].tables == "1 Cap"


def test_oversized_table_splits_rowwise_with_repeated_meta():
    rows = "\n".join(f"| r{i} | v{i} |" for i in range(10))
    gfm = "| A | B |\n| --- | --- |\n" + rows
    blocks = [TableBlock(gfm, "2", "Big")]
    chunks = chunk_blocks(blocks, chunk_size=100, chunk_overlap=0, max_chunk_chars=60)
    assert len(chunks) > 1
    assert all(c.tables == "2 Big" for c in chunks)
    assert all(len(c.text) <= 600 for c in chunks)  # header repeated + rows bounded


def test_char_ceiling_wins():
    # Overlap 0 isolates the ceiling: a single 500-word sentence must split into token
    # windows bounded by max_chunk_chars.
    blocks = [ParagraphBlock("word " * 500)]
    chunks = chunk_blocks(blocks, chunk_size=512, chunk_overlap=0, max_chunk_chars=100)
    assert len(chunks) > 1 and all(len(c.text) <= 100 for c in chunks)
