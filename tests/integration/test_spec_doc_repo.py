"""Integration round-trip for the specdata ORM + repository (Task 7)."""
from doc3gpp.models.spec_doc import ChunkDraft, SpecDocToc
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.spec_doc_sql import SQLAlchemySpecDocRepository


def test_roundtrip(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository()
    assert repo.get_source("38.331", "18.5.0") is None
    repo.record_download(
        "38.331",
        "18.5.0",
        release="Rel-18",
        ftp_url="https://x/38331.zip",
        docx_count=1,
    )
    src = repo.get_source("38.331", "18.5.0")
    assert src is not None and src.parsed_at is None
    toc = SpecDocToc(
        spec_id="38.331",
        version="18.5.0",
        release="Rel-18",
        entries=[],
        files=[],
        docx_count=1,
    )
    repo.upsert_toc(toc)
    assert repo.get_toc("38.331", "18.5.0") is not None
    repo.replace_chunks(
        "38.331",
        "18.5.0",
        release="Rel-18",
        drafts=[ChunkDraft(file_order=0, source_file="a.docx", text="hello world")],
    )
    chunks = repo.list_chunks("38.331", version="18.5.0")
    assert len(chunks) == 1 and chunks[0].chunk_id == "38.331@18.5.0#0"
    src2 = repo.get_source("38.331", "18.5.0")
    assert src2.chunk_count == 1 and src2.parsed_at is not None
