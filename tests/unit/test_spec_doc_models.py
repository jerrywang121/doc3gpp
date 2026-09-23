from doc3gpp.models.spec_doc import SpecDocChunk, SpecDocToc


def test_chunk_id_shape():
    c = SpecDocChunk(chunk_id="38.331@18.5.0#3", spec_id="38.331", version="18.5.0",
        release="Rel-18", file_order=0, source_file="38331-j30.docx",
        chunk_index=3, section_no="5.2", section_title="Intro",
        table_no=None, table_title=None, text="hello")
    assert c.chunk_id == "38.331@18.5.0#3"
    assert c.section_no == "5.2"


def test_toc_defaults():
    t = SpecDocToc(spec_id="38.331", version="18.5.0", release="Rel-18",
        entries=[], files=[], docx_count=1, created_at=None)
    assert t.entries == []
