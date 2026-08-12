from __future__ import annotations

from sqlalchemy import text

from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine


def test_sqlite_connects() -> None:
    create_schema()
    engine = get_engine()
    with engine.connect() as conn:
        value = conn.execute(text("SELECT 1")).scalar_one()
    assert value == 1


def test_sqlite_wal_mode_enabled() -> None:
    """SQLite connections run in WAL journal mode for concurrent writers."""
    create_schema()
    engine = get_engine()
    with engine.connect() as conn:
        journal_mode = conn.execute(text("PRAGMA journal_mode")).scalar_one()
    assert journal_mode == "wal"


def test_sqlite_busy_timeout_set() -> None:
    """SQLite connections set a busy_timeout so concurrent writers wait."""
    create_schema()
    engine = get_engine()
    with engine.connect() as conn:
        busy_timeout = conn.execute(text("PRAGMA busy_timeout")).scalar_one()
    assert busy_timeout > 0
