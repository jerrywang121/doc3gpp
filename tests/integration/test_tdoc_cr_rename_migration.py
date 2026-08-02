"""Integration tests for the ``create_schema`` legacy-rename migration.

When the CR cover-page table was renamed from ``tdoc_cr_details`` to
``tdoc_cr_cover_page``, ``Base.metadata.create_all`` could not pick
that up because SQLAlchemy never renames an existing table. A new
``_migrate_rename_tdoc_cr_details`` step was added so that databases
created by a prior release — which still carry a ``tdoc_cr_details``
table — get transparently renamed on the next ``create_schema`` call.

These tests assert both halves of that contract:

* **Legacy → new** : an existing ``tdoc_cr_details`` table with real
  rows is renamed to ``tdoc_cr_cover_page`` and the rows survive.
* **Idempotent** : a database already on the new schema runs
  ``create_schema`` again with no error and no destructive change.

The tests are SQLite-only — SQLite is the sole backend, and the
rename relies on the SQLite ``ALTER TABLE ... RENAME TO`` branch in
``_migrate_rename_tdoc_cr_details``.
"""

from __future__ import annotations

from sqlalchemy import text

from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.migrate import (
    _migrate_rename_tdoc_cr_details,
    create_schema,
)
from doc3gpp.storage.db.session import get_engine


def _seed_legacy_db() -> None:
    """Build a legacy-shape ``tdoc_cr_details`` table on the active engine.

    Uses raw DDL that mirrors the ORM schema (same columns + FK to
    ``tdocs``). Avoids any SQLAlchemy MetaData coupling to the ORM's
    canonical binding.
    """
    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS tdoc_cr_cover_page"))
        conn.execute(
            text(
                """
                CREATE TABLE tdoc_cr_details (
                    ftp_url VARCHAR(1024) PRIMARY KEY,
                    tdoc_id VARCHAR(64) NOT NULL,
                    spec VARCHAR(64),
                    cr_num VARCHAR(64),
                    rev VARCHAR(64),
                    version VARCHAR(64),
                    title TEXT,
                    source VARCHAR(256),
                    tsg VARCHAR(16),
                    related_wis VARCHAR(512),
                    date DATE,
                    cr_cat VARCHAR(16),
                    release VARCHAR(64),
                    reason_for_change TEXT,
                    consequences_if_not_approved TEXT,
                    clauses_affected TEXT,
                    other_comments TEXT,
                    revision_history TEXT,
                    extracted_tdoc_id VARCHAR(64),
                    FOREIGN KEY (tdoc_id) REFERENCES tdocs(tdoc_id)
                )
                """
            )
        )
        conn.execute(
            text("CREATE INDEX ix_tdoc_cr_details_tdoc_id ON tdoc_cr_details(tdoc_id)")
        )


def _seed_fresh_db() -> None:
    """Build a current-shape ``tdoc_cr_cover_page`` table on the active engine."""
    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _seed_fresh_db() -> None:
    """Build a current-shape ``tdoc_cr_cover_page`` table on the active engine."""
    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_legacy_table_renames_and_data_survives(sqlite_env) -> None:
    """An existing ``tdoc_cr_details`` table is renamed in place.

    Seed a parent ``tdocs`` row + one cover-page row under the legacy
    name, run the migration, and assert the row is now reachable via
    the new name with all fields intact.
    """
    _seed_legacy_db()

    engine = get_engine()
    with engine.begin() as conn:
        # Parent TDoc (FK target).
        conn.execute(
            text(
                "INSERT INTO tdocs (tdoc_id, type, ftp_url) "
                "VALUES ('R5s260009', 'CR', 'tsg_ran/WG5/R5s260009.zip')"
            )
        )
        # Legacy cover-page row — must survive the rename.
        conn.execute(
            text(
                "INSERT INTO tdoc_cr_details "
                "(ftp_url, tdoc_id, spec, cr_num, rev, title) "
                "VALUES ('tsg_ran/WG5/R5s260009.zip', 'R5s260009', "
                "'38.523-1', '1234', '0', 'Legacy title')"
            )
        )

    _migrate_rename_tdoc_cr_details()

    with engine.begin() as conn:
        # Legacy name must be gone.
        legacy_present = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='tdoc_cr_details' LIMIT 1"
            )
        ).first()
        assert legacy_present is None, "legacy tdoc_cr_details table still present"

        # New name must exist with the seeded row intact.
        new_present = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='tdoc_cr_cover_page' LIMIT 1"
            )
        ).first()
        assert new_present is not None

        row = conn.execute(
            text(
                "SELECT tdoc_id, spec, cr_num, rev, title "
                "FROM tdoc_cr_cover_page WHERE ftp_url = "
                "'tsg_ran/WG5/R5s260009.zip'"
            )
        ).first()
        assert row is not None
        assert row.tdoc_id == "R5s260009"
        assert row.spec == "38.523-1"
        assert row.cr_num == "1234"
        assert row.rev == "0"
        assert row.title == "Legacy title"


def test_migration_is_idempotent_on_fresh_schema(sqlite_env) -> None:
    """A fresh DB (no legacy table) re-runs the migration safely."""
    _seed_fresh_db()

    # Re-running the migration must be a no-op (no exception).
    _migrate_rename_tdoc_cr_details()
    _migrate_rename_tdoc_cr_details()

    with get_engine().begin() as conn:
        new_present = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='tdoc_cr_cover_page' LIMIT 1"
            )
        ).first()
        assert new_present is not None

        legacy_present = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='tdoc_cr_details' LIMIT 1"
            )
        ).first()
        assert legacy_present is None


def test_create_schema_against_legacy_db_migrates(sqlite_env) -> None:
    """``create_schema`` (the public bootstrap entry point) triggers the rename."""
    _seed_legacy_db()

    # ``create_schema`` must orchestrate the rename before ``create_all``
    # — a single call lands both effects.
    create_schema()

    with get_engine().begin() as conn:
        legacy_present = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='tdoc_cr_details' LIMIT 1"
            )
        ).first()
        assert legacy_present is None

        new_present = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='tdoc_cr_cover_page' LIMIT 1"
            )
        ).first()
        assert new_present is not None
