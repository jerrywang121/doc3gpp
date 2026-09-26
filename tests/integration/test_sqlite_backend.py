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


def test_sqlite_creates_parent_for_absolute_database_path(tmp_path) -> None:
    from sqlalchemy import create_engine, text

    from doc3gpp.storage.backends.sqlite import configure_sqlite_engine

    db_path = tmp_path / "nested" / "test.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine(
        database_url,
        **configure_sqlite_engine(database_url, db_echo=False),
    )

    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar_one() == 1
    assert db_path.is_file()


def test_sqlite_vec_loads_with_extension_loading_disabled_by_default() -> None:
    import sqlite3

    import pytest

    sqlite_vec = pytest.importorskip("sqlite_vec")
    from doc3gpp.storage.backends.sqlite import load_sqlite_vec

    connection = sqlite3.connect(":memory:")
    try:
        load_sqlite_vec(connection)
        assert connection.execute("SELECT vec_version()").fetchone()[0]
        with pytest.raises(sqlite3.OperationalError, match="not authorized"):
            sqlite_vec.load(connection)
    finally:
        connection.close()


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
