"""End-to-end filter-flag tests against a populated sqlite DB."""

from __future__ import annotations

from sqlalchemy import text
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.services.factory import build_search_service
from doc3gpp.storage.db.migrate import create_schema


def _seed_minimal_db() -> None:
    """Insert one tdocs row + matching sidecar tables for the search index."""
    from doc3gpp.storage.db.session import get_engine

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tsgs (tsg_name, short_name, description) "
                "VALUES ('RAN WG1', 'RAN1', 'TSG RAN WG1')"
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO meetings (
                    meeting_id, name, title, location, tsg, start_date,
                    end_date, ftp_url, tdoc_list_last_sync
                ) VALUES (
                    1, 'RAN1#1', 'RAN1#1', 'Online', 'RAN1',
                    '2026-01-01', '2026-01-05',
                    'https://www.3gpp.org/ftp/meetings/RAN1_1',
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
                    uploaded_date, release, spec
                ) VALUES (
                    'R5-1000000', 1, 'NB-IoT scheduling study',
                    'https://www.3gpp.org/ftp/R5-1000000.zip',
                    'CR', 'TSG', '2026-01-02T00:00:00', 'Rel-17', '38.300'
                )
                """
            )
        )


def test_search_with_filters(sqlite_env) -> None:
    create_schema()
    _seed_minimal_db()
    svc = build_search_service()
    assert svc is not None
    svc.upsert_for_tdoc("R5-1000000")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "search", "search", "NB-IoT",
            "--tsg", "RAN1",
            "--release", "Rel-17",
            "--spec", "38.300",
            "--since", "2026-01-01",
            "--until", "2026-01-31",
            "--limit", "5",
            "--format", "json",
        ],
    )
    # The CLI may print a stale-index hint to stderr; exit 0 is the
    # contract here.
    assert result.exit_code == 0
    assert "R5-1000000" in result.output
