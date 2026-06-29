from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse


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
