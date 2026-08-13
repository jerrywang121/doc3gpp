"""Integration tests for the ``_migrate_drop_tsg_spec_last_sync`` migration.

``Base.metadata.create_all`` is a no-op on tables that already exist,
so a pre-existing ``tsgs`` table on an older database would carry the
obsolete ``spec_last_sync`` column forever. This test pins the
post-condition: ``create_schema`` drops the legacy column when it is
genuinely present, and is a no-op when it is already absent.
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine


def test_create_schema_drops_tsg_spec_last_sync(sqlite_env) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS tsgs ("
                "  short_name VARCHAR(16) PRIMARY KEY,"
                "  tsg_name VARCHAR(120),"
                "  description VARCHAR(500),"
                "  url TEXT"
                ")"
            )
        )
        try:
            conn.execute(text("ALTER TABLE tsgs ADD COLUMN spec_last_sync DATETIME"))
        except Exception:
            pass

    create_schema()

    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("tsgs")}
    assert "spec_last_sync" not in cols


def test_create_schema_drops_tsg_spec_last_sync_is_noop_when_absent(sqlite_env) -> None:
    """When ``tsgs`` does not carry the legacy column, ``create_schema``
    must not raise. The migration probe should short-circuit and leave
    the schema alone."""
    create_schema()
    create_schema()

    insp = inspect(get_engine())
    cols = {c["name"] for c in insp.get_columns("tsgs")}
    assert "spec_last_sync" not in cols
