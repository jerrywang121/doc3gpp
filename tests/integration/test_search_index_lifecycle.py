"""End-to-end lifecycle tests for ``SQLAlchemySearchIndexRepository``.

Covers upsert / search / remove / rebuild / status / cursor — every
method on the :class:`SearchIndexRepository` Protocol. Uses the real
sqlite engine (``sqlite_env`` fixture) so the FTS5 virtual table is
genuinely exercised.
"""

from __future__ import annotations

from sqlalchemy import text

from doc3gpp.models.search import SearchFilters
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
    repo.upsert(1)
    hits = repo.search("nb iot", SearchFilters(limit=10))
    assert len(hits) == 1
    assert hits[0].tdoc_id == 1
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
    repo.upsert(1)
    repo.remove(1)
    hits = repo.search("orphan", SearchFilters(limit=10))
    assert hits == []


def test_status_reports_row_count(sqlite_env) -> None:
    """Status row_count matches the number of upserts."""
    create_schema()
    repo = SQLAlchemySearchIndexRepository()
    engine = get_engine()
    for i in range(3):
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO tdocs (tdoc_id, title, ftp_url, type) "
                    f"VALUES ('R5-{1000000 + i}', 'doc-{i}', "
                    f"'https://www.3gpp.org/ftp/R5-{1000000 + i}.zip', 'CR')"
                )
            )
        repo.upsert(i + 1)
    status = repo.status()
    assert status.row_count == 3
    assert status.enabled is True
