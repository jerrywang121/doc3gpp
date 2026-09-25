from dataclasses import fields

from doc3gpp.models.spec_doc import (
    ChunkDraft,
    SpecDocChunk,
    SpecDocHit,
    SpecDocSearchFilters,
    SpecDocToc,
)
from doc3gpp.settings.schema import _SPEC_DOC_SNIPPET_COLUMNS


def test_chunk_contract_has_combined_metadata_only():
    chunk = ChunkDraft(
        file_order=0,
        source_file="part.docx",
        sections="7.2A.3 Scope\n7.2A.3A Details",
        tables="Table 1 UE values\nTable 2 Timers",
        text="body",
    )
    assert chunk.sections == "7.2A.3 Scope\n7.2A.3A Details"
    assert chunk.tables == "Table 1 UE values\nTable 2 Timers"
    assert [field.name for field in fields(chunk)] == [
        "file_order",
        "source_file",
        "sections",
        "tables",
        "text",
    ]


def test_spec_doc_contract_removes_all_split_metadata_fields():
    removed = {
        "section",
        "section_no",
        "section_title",
        "table_no",
        "table_title",
    }
    for dto in (ChunkDraft, SpecDocChunk, SpecDocHit, SpecDocSearchFilters):
        assert not removed & {field.name for field in fields(dto)}


def test_spec_doc_snippet_columns_are_in_fts_order():
    assert _SPEC_DOC_SNIPPET_COLUMNS == (
        "text",
        "sections",
        "tables",
        "spec_id",
        "version",
        "release",
    )


def test_search_filters_use_plural_metadata_names():
    filters = SpecDocSearchFilters(sections="%handover%", tables="%UE%")
    assert filters.sections == "%handover%"
    assert filters.tables == "%UE%"
    assert not hasattr(filters, "section")


def test_spec_doc_hit_uses_combined_metadata():
    hit = SpecDocHit(
        "id", "38.331", "19.0.0", "Rel-19", "5 Scope", "Table 1 Values",
        0, "text", 0.1, {},
    )
    assert hit.sections == "5 Scope"
    assert hit.tables == "Table 1 Values"


def test_chunk_id_shape():
    c = SpecDocChunk(chunk_id="38.331@18.5.0#3", spec_id="38.331", version="18.5.0",
        release="Rel-18", file_order=0, source_file="38331-j30.docx",
        chunk_index=3, sections="5.2 Intro", tables=None, text="hello")
    assert c.chunk_id == "38.331@18.5.0#3"
    assert c.sections == "5.2 Intro"


def test_toc_defaults():
    t = SpecDocToc(spec_id="38.331", version="18.5.0", release="Rel-18",
        entries=[], files=[], docx_count=1, created_at=None)
    assert t.entries == []
