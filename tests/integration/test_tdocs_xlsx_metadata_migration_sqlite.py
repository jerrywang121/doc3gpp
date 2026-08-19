"""Integration tests for the ``_migrate_tdocs_xlsx_metadata`` migration.

``Base.metadata.create_all`` is a no-op on tables that already exist,
so a pre-existing ``tdocs`` table on an older database would never gain
the six XLSX-metadata columns. This test pins the post-condition:
``create_schema`` ALTERs the missing columns in, is a no-op when they
are already present, and lets fresh databases pick them up via
``Base.metadata.create_all`` on the upgraded ``TDocORM`` model.
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine

XLSX_METADATA_COLUMNS = (
    "tdoc_for",
    "abstract",
    "secretary_remarks",
    "ls_to",
    "ls_cc",
    "original_ls",
)


def _seed_legacy_tdocs_db() -> None:
    """Build a legacy-shape ``tdocs`` table (original 21 columns, no
    XLSX metadata) on the active engine."""
    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS tdocs"))
        conn.execute(
            text(
                """
                CREATE TABLE tdocs (
                    tdoc_id VARCHAR(64) PRIMARY KEY,
                    title TEXT,
                    meeting_id INTEGER,
                    ftp_url TEXT,
                    source VARCHAR(256),
                    type VARCHAR(128),
                    status VARCHAR(128),
                    reservation_date DATE,
                    uploaded_date DATE,
                    cr_cat VARCHAR(64),
                    is_revision_of VARCHAR(64),
                    revised_to VARCHAR(64),
                    release VARCHAR(64),
                    spec VARCHAR(64),
                    version VARCHAR(64),
                    related_wis VARCHAR(256),
                    cr_num VARCHAR(64),
                    cr_pack VARCHAR(128)
                )
                """
            )
        )


def test_create_schema_adds_six_xlsx_metadata_columns_to_tdocs(sqlite_env) -> None:
    _seed_legacy_tdocs_db()
    create_schema()
    insp = inspect(get_engine())
    cols = {c["name"] for c in insp.get_columns("tdocs")}
    for col in XLSX_METADATA_COLUMNS:
        assert col in cols


def test_create_schema_is_idempotent_on_xlsx_metadata_columns(sqlite_env) -> None:
    _seed_legacy_tdocs_db()
    create_schema()
    create_schema()

    with get_engine().begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(tdocs)")).all()
    column_names = [row[1] for row in rows]
    for col in XLSX_METADATA_COLUMNS:
        assert column_names.count(col) == 1


def test_create_schema_no_op_when_tdocs_table_absent(sqlite_env) -> None:
    create_schema()

    with get_engine().begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(tdocs)")).all()
        column_names = {row[1] for row in rows}
    for col in XLSX_METADATA_COLUMNS:
        assert col in column_names
