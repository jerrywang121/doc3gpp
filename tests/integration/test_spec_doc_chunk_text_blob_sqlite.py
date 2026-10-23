"""SQLite coverage for the gzip-JSON blob ``spec_doc_chunks.text`` column."""

from contextlib import contextmanager

from sqlalchemy import event, inspect, text

from doc3gpp.models.spec_doc import ChunkDraft, SpecDocSearchFilters
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_specdata_engine
from doc3gpp.storage.repositories.spec_doc_search_sql import (
    SQLAlchemySpecDocSearchRepository,
)
from doc3gpp.storage.repositories.spec_doc_sql import SQLAlchemySpecDocRepository

LEGACY_CHUNKS_DDL = """
CREATE TABLE spec_doc_chunks (
    chunk_id VARCHAR(128) NOT NULL PRIMARY KEY,
    spec_id VARCHAR(32) NOT NULL,
    version VARCHAR(16) NOT NULL,
    release VARCHAR(16),
    file_order INTEGER NOT NULL,
    source_file VARCHAR(256) NOT NULL,
    chunk_index INTEGER NOT NULL,
    sections TEXT,
    tables TEXT,
    text TEXT NOT NULL DEFAULT ''
)
"""

INSERT_LEGACY_ROW = text(
    "INSERT INTO spec_doc_chunks (chunk_id, spec_id, version, release, "
    "file_order, source_file, chunk_index, sections, tables, text) VALUES "
    "(:chunk_id, '38.331', '18.5.0', 'Rel-18', 0, 'a.docx', :chunk_index, "
    "'5.1 Handover', NULL, :body)"
)


def _rebuild_legacy_chunks_table(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS spec_doc_chunks"))
        conn.execute(text(LEGACY_CHUNKS_DDL))
        conn.execute(
            text("CREATE INDEX ix_spec_doc_chunks_spec_id ON spec_doc_chunks (spec_id)")
        )
        conn.execute(
            text("CREATE INDEX ix_spec_doc_chunks_version ON spec_doc_chunks (version)")
        )


def _insert_legacy_rows(engine, bodies: list[str], start: int = 0) -> None:
    rows = [
        {
            "chunk_id": f"38.331@18.5.0#{index}",
            "chunk_index": index,
            "body": body,
        }
        for index, body in enumerate(bodies, start=start)
    ]
    with engine.begin() as conn:
        conn.execute(INSERT_LEGACY_ROW, rows)


def _stored_types(engine) -> list[str]:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT typeof(text) FROM spec_doc_chunks ORDER BY chunk_id")
        ).scalars().all()


def _stored_bytes(engine) -> dict[str, bytes]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT chunk_id, text FROM spec_doc_chunks")
        ).all()
    return {chunk_id: bytes(value) for chunk_id, value in rows}


@contextmanager
def _record_chunk_updates(engine):
    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("UPDATE SPEC_DOC_CHUNKS"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_fresh_schema_declares_text_as_blob(sqlite_env):
    create_schema("specdata")
    with get_specdata_engine().connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(spec_doc_chunks)")).all()
    declared = {row[1]: row[2] for row in rows}
    assert declared["text"] == "BLOB"


def test_repository_roundtrip_stores_blob_and_returns_text(sqlite_env):
    create_schema("specdata")
    repo = SQLAlchemySpecDocRepository()
    bodies = ["plain body", "Grüße — 規格 ✓", ""]
    repo.replace_chunks(
        "38.331",
        "18.5.0",
        release="Rel-18",
        drafts=[
            ChunkDraft(file_order=0, source_file="a.docx", text=body)
            for body in bodies
        ],
    )
    assert _stored_types(get_specdata_engine()) == ["blob"] * 3

    listed = repo.list_chunks("38.331", version="18.5.0", limit=10)
    assert [chunk.text for chunk in listed] == bodies

    by_id = repo.get_chunks_by_ids([f"38.331@18.5.0#{i}" for i in range(3)])
    assert [by_id[f"38.331@18.5.0#{i}"].text for i in range(3)] == bodies

    searched = repo.list_for_search(SpecDocSearchFilters(spec_id="38.331", limit=10))
    assert [chunk.text for chunk in searched] == bodies


def test_search_returns_decoded_text(sqlite_env):
    create_schema("specdata")
    repo = SQLAlchemySpecDocRepository()
    body = "Handover procedure signalling, see clause 5.1"
    repo.replace_chunks(
        "38.331",
        "18.5.0",
        release="Rel-18",
        drafts=[
            ChunkDraft(
                file_order=0,
                source_file="a.docx",
                sections="5.1 Handover",
                tables="Table 1 Values",
                text=body,
            )
        ],
    )
    assert _stored_types(get_specdata_engine()) == ["blob"]

    fts = SQLAlchemySpecDocSearchRepository()
    fts.upsert_for_version("38.331", "18.5.0")
    hits = fts.search("handover", SpecDocSearchFilters(spec_id="38.331"))
    assert [hit.text for hit in hits] == [body]


def test_migration_compresses_pre_change_rows(sqlite_env):
    engine = get_specdata_engine()
    _rebuild_legacy_chunks_table(engine)
    bodies = ["plain body", "Grüße 規格"]
    _insert_legacy_rows(engine, bodies)

    create_schema("specdata")

    assert _stored_types(engine) == ["blob"] * len(bodies)
    chunks = SQLAlchemySpecDocRepository().list_chunks(
        "38.331", version="18.5.0", limit=10
    )
    assert [chunk.text for chunk in chunks] == bodies


def test_migration_is_idempotent_and_handles_mixed_rows(sqlite_env):
    engine = get_specdata_engine()

    create_schema("specdata")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE spec_doc_chunks"))
    with _record_chunk_updates(engine) as absent_updates:
        create_schema("specdata")
    assert absent_updates == []
    with engine.connect() as conn:
        assert inspect(conn).has_table("spec_doc_chunks")

    with _record_chunk_updates(engine) as empty_updates:
        create_schema("specdata")
    assert empty_updates == []

    _rebuild_legacy_chunks_table(engine)
    bodies = [f"legacy body {index}" for index in range(600)]
    _insert_legacy_rows(engine, bodies)
    with _record_chunk_updates(engine) as first_updates:
        create_schema("specdata")
    assert len(first_updates) == len(bodies)
    first_bytes = _stored_bytes(engine)

    with _record_chunk_updates(engine) as second_updates:
        create_schema("specdata")
    assert second_updates == []
    assert _stored_bytes(engine) == first_bytes

    repo = SQLAlchemySpecDocRepository()
    repo.replace_chunks(
        "38.331",
        "19.0.0",
        release="Rel-19",
        drafts=[ChunkDraft(file_order=0, source_file="b.docx", text="fresh body")],
    )
    _insert_legacy_rows(engine, ["late legacy body"], start=len(bodies))
    with _record_chunk_updates(engine) as mixed_updates:
        create_schema("specdata")
    assert len(mixed_updates) == 1
    assert _stored_types(engine) == ["blob"] * (len(bodies) + 2)

    listed = repo.list_chunks("38.331", version="19.0.0", limit=10)
    assert [chunk.text for chunk in listed] == ["fresh body"]
    legacy = repo.list_chunks("38.331", version="18.5.0", limit=1000)
    assert [chunk.text for chunk in legacy] == [*bodies, "late legacy body"]


def test_unmigrated_plain_text_rows_list_as_text(sqlite_env):
    create_schema("specdata")
    engine = get_specdata_engine()
    _rebuild_legacy_chunks_table(engine)
    _insert_legacy_rows(engine, ["legacy handover body"])

    chunks = SQLAlchemySpecDocRepository().list_chunks(
        "38.331", version="18.5.0", limit=10
    )

    assert [chunk.text for chunk in chunks] == ["legacy handover body"]


def test_unmigrated_plain_text_rows_search_as_text(sqlite_env):
    create_schema("specdata")
    engine = get_specdata_engine()
    _rebuild_legacy_chunks_table(engine)
    _insert_legacy_rows(engine, ["legacy handover body"])
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO spec_doc_search (chunk_id, text, sections, tables, "
                "spec_id, version, release) VALUES ('38.331@18.5.0#0', "
                "'legacy handover body', NULL, NULL, '38_331', '18_5_0', NULL)"
            )
        )

    hits = SQLAlchemySpecDocSearchRepository().search(
        "handover", SpecDocSearchFilters()
    )

    assert [hit.text for hit in hits] == ["legacy handover body"]
