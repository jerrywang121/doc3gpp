from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import event
from sqlalchemy.engine import Engine


def configure_sqlite_engine(database_url: str, db_echo: bool) -> dict:
    """Return SQLAlchemy engine kwargs for sqlite and ensure local path exists."""

    parsed = urlparse(database_url)
    if parsed.path and parsed.path != ":memory:":
        raw_path = parsed.path
        if raw_path.startswith("/~/"):
            raw_path = "~" + raw_path[2:]
        db_file = Path(raw_path).expanduser()
        db_file.parent.mkdir(parents=True, exist_ok=True)

    return {
        "echo": db_echo,
        "future": True,
        "connect_args": {"check_same_thread": False},
    }


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
        finally:
            cursor.close()
