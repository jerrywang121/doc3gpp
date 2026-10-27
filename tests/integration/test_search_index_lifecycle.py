"""End-to-end lifecycle tests for ``SQLAlchemySearchIndexRepository``.

Covers upsert / search / remove / rebuild / status / cursor — every
method on the :class:`SearchIndexRepository` Protocol. Uses the real
sqlite engine (``sqlite_env`` fixture) so the FTS5 virtual table is
genuinely exercised.
"""

from __future__ import annotations

from sqlalchemy import text

from doc3gpp.models.search import SearchFilters
from doc3gpp.storage.compression import compress_json
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.search_sql import (
    SQLAlchemySearchIndexRepository,
)


def test_upsert_then_search_title(sqlite_env) -> None:
    """Upsert a single TDoc; search for its title returns it."""
    create_schema()
    repo = SQLAlchemySearchIndexRepository()
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO tsgs (tsg_name, short_name, description)
                VALUES ('RAN WG1', 'RAN1', 'TSG RAN WG1')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO meetings (
                    meeting_id, name, title, location, tsg, start_date,
                    end_date, ftp_url, tdoc_list_last_sync
                ) VALUES (
                    100, 'RAN1#100', 'RAN1#100', 'Online', 'RAN1',
                    '2026-01-01', '2026-01-05',
                    'https://www.3gpp.org/ftp/meetings/RAN1_100',
                    '2026-01-05T00:00:00'
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO tdocs (
                    tdoc_id, meeting_id, title, ftp_url, type, source,
                    uploaded_date, release
                ) VALUES (
                    'R5-1234567', 100, 'NB-IoT scheduling study',
                    'https://www.3gpp.org/ftp/R5-1234567.zip',
                    'CR', 'TSG', '2026-01-02T00:00:00', 'Rel-17'
                )
                """
            )
        )
    repo.upsert("R5-1234567")
    hits = repo.search("nb iot", SearchFilters(limit=10))
    assert len(hits) == 1
    assert hits[0].tdoc_id == "R5-1234567"
    assert "NB-IoT" in hits[0].title


def test_search_after_delete_returns_empty(sqlite_env) -> None:
    """Remove + search returns no hits."""
    create_schema()
    repo = SQLAlchemySearchIndexRepository()
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tdocs (tdoc_id, title, ftp_url, type) "
                "VALUES ('R5-9999999', 'orphan', "
                "'https://www.3gpp.org/ftp/R5-9999999.zip', 'CR')"
            )
        )
    repo.upsert("R5-9999999")
    repo.remove("R5-9999999")
    hits = repo.search("orphan", SearchFilters(limit=10))
    assert hits == []


def test_stale_only_rebuild_batch_runs_freshness_query_once(sqlite_env) -> None:
    from sqlalchemy import event

    create_schema()
    repo = SQLAlchemySearchIndexRepository()
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tdocs (tdoc_id, title, ftp_url, type) "
                "VALUES (:tdoc_id, :title, :ftp_url, 'CR')"
            ),
            [
                {
                    "tdoc_id": f"R5-KEYSET-{index:03d}",
                    "title": f"TDoc {index}",
                    "ftp_url": f"archive/{index}.zip",
                }
                for index in range(1, 4)
            ],
        )

    stale_queries: list[str] = []

    def record_stale_query(
        _conn, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        if "stale_tdocs" in statement.lower():
            stale_queries.append(statement)

    event.listen(engine, "before_cursor_execute", record_stale_query)
    try:
        batches = list(repo.rebuild_batch(2, after_id=None, stale_only=True))
    finally:
        event.remove(engine, "before_cursor_execute", record_stale_query)

    assert batches == [
        ["R5-KEYSET-001", "R5-KEYSET-002"],
        ["R5-KEYSET-003"],
    ]
    assert len(stale_queries) == 1


def test_status_reports_row_count(sqlite_env) -> None:
    """Status row_count counts logical TDocs rather than FTS source rows."""
    create_schema()
    repo = SQLAlchemySearchIndexRepository()
    engine = get_engine()
    seeded_ids = [f"R5-{1000000 + i}" for i in range(3)]
    with engine.begin() as conn:
        for tdoc_id in seeded_ids:
            conn.execute(
                text(
                    "INSERT INTO tdocs (tdoc_id, title, ftp_url, type) "
                    "VALUES (:tdoc_id, 'doc', :ftp_url, 'CR')"
                ),
                {
                    "tdoc_id": tdoc_id,
                    "ftp_url": f"https://www.3gpp.org/ftp/{tdoc_id}.zip",
                },
            )
        extraction_url = "archive/R5-1000000.zip"
        conn.execute(
            text(
                """
                INSERT INTO tdoc_extracts (
                    ftp_url, tdoc_id, cache_file, doc_filename, parser_version,
                    extraction_version, document_count
                ) VALUES (
                    :ftp_url, :tdoc_id, 'R5-1000000.zip', 'main.docx', '1.0.0', 1, 2
                )
                """
            ),
            {"ftp_url": extraction_url, "tdoc_id": seeded_ids[0]},
        )
        for index, content in enumerate(("alpha support", "beta support")):
            conn.execute(
                text(
                    """
                    INSERT INTO tdoc_cr_annexes (
                        ftp_url, doc_filename, document_id, tdoc_id, content,
                        content_hash
                    ) VALUES (
                        :ftp_url, :doc_filename, :document_id, :tdoc_id,
                        :content, :content_hash
                    )
                    """
                ),
                {
                    "document_id": str(index + 1) * 64,
                    "ftp_url": extraction_url,
                    "doc_filename": f"support/{index}.docx",
                    "tdoc_id": seeded_ids[0],
                    "content": compress_json(content),
                    "content_hash": "a" * 64,
                },
            )
    for tdoc_id in seeded_ids:
        repo.upsert(tdoc_id)
    status = repo.status()
    with engine.begin() as conn:
        physical_count = conn.execute(
            text("SELECT COUNT(*) FROM tdoc_search")
        ).scalar_one()
    assert status.row_count == 3
    assert physical_count == 5
    assert status.enabled is True
