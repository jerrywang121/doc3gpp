"""Integration tests for the spec rapporteurs / comment migrations."""

from __future__ import annotations

from sqlalchemy import text

from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine


def _seed_legacy_spec_db() -> None:
    """Build a legacy-shape ``specs`` (no rapporteurs) + ``spec_versions``
    (with comment) on the active engine."""
    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS spec_versions"))
        conn.execute(text("DROP TABLE IF EXISTS specs"))
        conn.execute(
            text(
                """
                CREATE TABLE specs (
                    spec_id VARCHAR(32) PRIMARY KEY,
                    type VARCHAR(8),
                    title TEXT,
                    status VARCHAR(32),
                    radio_tech VARCHAR(64),
                    initial_release VARCHAR(16),
                    tsg VARCHAR(16),
                    wis VARCHAR(512),
                    last_synced_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE spec_versions (
                    spec_id VARCHAR(32) NOT NULL,
                    version VARCHAR(16) NOT NULL,
                    ftp_url VARCHAR(1024) NOT NULL,
                    release VARCHAR(16),
                    meeting_id INTEGER,
                    meeting_name VARCHAR(64),
                    upload_date DATE,
                    version_id INTEGER,
                    pdf_url VARCHAR(1024),
                    crs TEXT,
                    comment VARCHAR(256),
                    PRIMARY KEY (spec_id, version),
                    FOREIGN KEY (spec_id) REFERENCES specs(spec_id) ON DELETE CASCADE
                )
                """
            )
        )


def test_create_schema_adds_rapporteurs_and_drops_comment(sqlite_env) -> None:
    _seed_legacy_spec_db()
    create_schema()
    with get_engine().begin() as conn:
        spec_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(specs)")).all()}
        assert "rapporteurs" in spec_cols
        version_cols = {
            r[1] for r in conn.execute(text("PRAGMA table_info(spec_versions)")).all()
        }
        assert "comment" not in version_cols


def test_migration_is_idempotent_on_fresh_schema(sqlite_env) -> None:
    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    create_schema()
    create_schema()
    with get_engine().begin() as conn:
        spec_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(specs)")).all()}
        assert "rapporteurs" in spec_cols
