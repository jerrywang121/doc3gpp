from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine, make_url


def configure_sqlite_engine(database_url: str, db_echo: bool) -> dict:
    """Return SQLAlchemy engine kwargs for sqlite and ensure local path exists."""

    parsed = make_url(database_url)
    raw_path = parsed.database
    if raw_path and raw_path != ":memory:":
        db_file = Path(raw_path).expanduser()
        db_file.parent.mkdir(parents=True, exist_ok=True)

    return {
        "echo": db_echo,
        "future": True,
        "connect_args": {"check_same_thread": False},
    }


def load_sqlite_vec(dbapi_connection) -> None:
    """Load sqlite-vec safely when SQLite extension loading is disabled."""
    import sqlite_vec

    dbapi_connection.enable_load_extension(True)
    try:
        sqlite_vec.load(dbapi_connection)
    finally:
        dbapi_connection.enable_load_extension(False)


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    """Enable ``PRAGMA foreign_keys=ON`` for every new SQLite connection.

    SQLite ships with FK enforcement disabled per-connection. Without this
    hook, foreign-key declarations on tables such as ``wis.tsg_short`` would
    be persisted to the schema but silently ignored by the engine. The
    hook is a no-op on non-SQLite dialects because the ``dbapi_connection``
    module path check keeps the PRAGMA scoped to SQLite only.
    """
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            # WAL journal mode lets concurrent readers/writers proceed
            # without blocking, and a busy_timeout makes a writer wait for
            # a lock instead of failing immediately. Together these make
            # the thread-pool spec sync safe against mid-write interruption
            # (a Ctrl-C that previously tore the header page).
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()
