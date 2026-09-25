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
    assert src2.chunk_count == 0 and src2.parsed_at is None
    repo.record_parsed("38.331", "18.5.0", chunk_count=len(chunks))
    src3 = repo.get_source("38.331", "18.5.0")
    assert src3.chunk_count == 1 and src3.parsed_at is not None

    repo.invalidate_parse_state("38.331", "18.5.0")
    src4 = repo.get_source("38.331", "18.5.0")
    assert src4.chunk_count == 0 and src4.parsed_at is None
    assert src4.release == "Rel-18"
    assert src4.ftp_url == "https://x/38331.zip"
    assert src4.downloaded_at is not None
    assert src4.docx_count == 1


def test_list_parsed_versions_returns_only_parsed_rows_in_numeric_order(sqlite_env):
    create_schema("specdata")
    repo = SQLAlchemySpecDocRepository()
    for version in ("18.2.1", "19.1.0", "18.10.1"):
        repo.record_parsed("36.579-5", version, chunk_count=1)
    repo.record_download(
        "36.579-5",
        "20.0.0",
        release="Rel-20",
        ftp_url="ftp://unparsed",
        docx_count=1,
    )
    repo.record_parsed("38.331", "18.5.0", chunk_count=1)

    assert repo.list_parsed_versions() == {
        "36.579-5": ["19.1.0", "18.10.1", "18.2.1"],
        "38.331": ["18.5.0"],
    }
    assert repo.list_parsed_versions(["36.579-5"]) == {
        "36.579-5": ["19.1.0", "18.10.1", "18.2.1"]
    }
    assert repo.list_parsed_versions(["99.999"]) == {}


def test_list_parsed_versions_missing_table_is_empty(sqlite_env):
    create_schema("main")
    repo = SQLAlchemySpecDocRepository()

    assert repo.list_parsed_versions(["36.579-5"]) == {}


def test_spec_doc_chunk_schema_has_only_combined_metadata(sqlite_env):
    from sqlalchemy import inspect

    from doc3gpp.storage.db.session import get_specdata_engine

    create_schema("specdata")
    columns = {
        column["name"]
        for column in inspect(get_specdata_engine()).get_columns("spec_doc_chunks")
    }
    assert columns == {
        "chunk_id",
        "spec_id",
        "version",
        "release",
        "file_order",
        "source_file",
        "chunk_index",
        "sections",
        "tables",
        "text",
    }


def test_replace_and_list_chunks_round_trip_metadata(sqlite_env):
    create_schema("specdata")
    repo = SQLAlchemySpecDocRepository()
    repo.replace_chunks(
        "38.331",
        "19.0.0",
        release="Rel-19",
        drafts=[
            ChunkDraft(
                0,
                "part.docx",
                "5 Scope\n6 Details",
                "Table 1 Values",
                "body",
            )
        ],
    )
    rows = repo.list_chunks(
        "38.331",
        version="19.0.0",
        sections="%Details%",
        tables="%Values%",
    )
    assert rows[0].sections == "5 Scope\n6 Details"
    assert rows[0].tables == "Table 1 Values"


def test_count_chunks_applies_all_filters(sqlite_env):
    create_schema("specdata")
    repo = SQLAlchemySpecDocRepository()
    repo.replace_chunks(
        "38.331",
        "19.0.0",
        release="Rel-19",
        drafts=[
            ChunkDraft(0, "a.docx", "5 Scope", "Table 1 Values", "a"),
            ChunkDraft(0, "a.docx", "6 Details", "Table 2 Timers", "b"),
        ],
    )
    repo.replace_chunks(
        "38.331",
        "18.5.0",
        release="Rel-18",
        drafts=[ChunkDraft(0, "b.docx", "5 Scope", "Table 1 Values", "c")],
    )

    assert repo.count_chunks("38.331") == 3
    assert repo.count_chunks("38.331", version="19.0.0") == 2
    assert repo.count_chunks("38.331", release="Rel-18") == 1
    assert repo.count_chunks("38.331", sections="%Details%") == 1
    assert repo.count_chunks("38.331", tables="%Timers%") == 1
    assert repo.count_chunks(
        "38.331", version="19.0.0", sections="%Scope%", tables="%Values%"
    ) == 1


def test_replace_chunks_without_source_keeps_unparsed_source_row(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository()

    repo.replace_chunks(
        "38.331",
        "18.5.0",
        release="Rel-18",
        drafts=[ChunkDraft(file_order=0, source_file="a.docx", text="hello")],
    )

    source = repo.get_source("38.331", "18.5.0")
    assert source is not None
    assert source.parsed_at is None
    assert source.chunk_count == 0
