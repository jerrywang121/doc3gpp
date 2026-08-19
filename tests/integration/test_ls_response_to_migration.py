"""Integration tests for the ``tdoc_cr_ls_details`` response-to migration.

The v1 LS sidecar split the ``Response to:`` cell into
``response_to_doc`` / ``response_to_title`` / ``response_to_group``;
the current schema collapses to a single ``response_to`` column
holding the raw cell text. ``_migrate_ls_response_to_columns``
migrates legacy databases:

* ``response_to`` = legacy ``response_to_title`` (or the doc / group
  text when the title is absent), ``NULL`` when all old columns are
  empty.
* The legacy ``response_to_title`` / ``response_to_group`` /
  ``response_to_doc`` columns are dropped (sqlite >= 3.35).

These tests assert both halves of that contract:

* **Legacy → new** : a legacy-shape table with real rows migrates and
  the data survives.
* **Idempotent** : a database already on the new schema runs
  ``create_schema`` again with no error and no destructive change.
"""

from __future__ import annotations

from sqlalchemy import text

from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine


def _seed_legacy_ls_db() -> None:
    """Build a legacy-shape ``tdoc_cr_ls_details`` table on the active engine."""
    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS tdoc_cr_ls_details"))
        conn.execute(
            text(
                """
                CREATE TABLE tdoc_cr_ls_details (
                    ftp_url TEXT PRIMARY KEY,
                    tdoc_id TEXT NOT NULL,
                    variant TEXT NOT NULL DEFAULT '3gpp',
                    title TEXT,
                    response_to_doc TEXT,
                    response_to_title TEXT,
                    response_to_group TEXT,
                    release TEXT,
                    work_item_name TEXT,
                    work_item_code TEXT,
                    source TEXT,
                    to_groups TEXT,
                    cc_groups TEXT,
                    attachments_json BLOB,
                    parser_version TEXT NOT NULL,
                    extracted_at DATETIME NOT NULL,
                    FOREIGN KEY (tdoc_id) REFERENCES tdocs(tdoc_id) ON DELETE CASCADE
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO tsgs (tsg_name, short_name, description) "
                "VALUES ('RAN WG5', 'RAN5', '')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO meetings (meeting_id, name, title, location, tsg, "
                "start_date, end_date, ftp_url, tdoc_list_last_sync) "
                "VALUES (100, 'RAN5#120', 'RAN5 #120', 'Online', 'RAN5', "
                "'2026-08-01', '2026-08-05', 'https://x/ran5-120', "
                "'2026-08-05T00:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO tdocs (tdoc_id, meeting_id, title, ftp_url, type) "
                "VALUES ('R5-240001', 100, 'LS on foo', 'tsg/ls/R5-240001.doc', 'LS')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO tdoc_cr_ls_details "
                "(ftp_url, tdoc_id, title, response_to_doc, response_to_title, "
                "response_to_group, parser_version, extracted_at) "
                "VALUES ('tsg/ls/R5-240001.doc', 'R5-240001', 'LS on foo', "
                "'R5-234567', '5G_eHealth WI status', 'RAN WG3', '1.0.0', "
                "CURRENT_TIMESTAMP)"
            )
        )


def test_create_schema_migrates_ls_response_to_columns(sqlite_env) -> None:
    _seed_legacy_ls_db()
    create_schema()
    with get_engine().begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(tdoc_cr_ls_details)")).all()}
        assert "response_to" in cols
        assert "response_to_title" not in cols
        assert "response_to_group" not in cols
        assert "response_to_doc" not in cols
        row = conn.execute(
            text("SELECT response_to FROM tdoc_cr_ls_details WHERE ftp_url = 'tsg/ls/R5-240001.doc'")
        ).first()
        assert row is not None
        assert row[0] == "5G_eHealth WI status"


def test_migration_is_idempotent_on_fresh_schema(sqlite_env) -> None:
    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    create_schema()
    create_schema()
    with get_engine().begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(tdoc_cr_ls_details)")).all()}
        assert "response_to" in cols
