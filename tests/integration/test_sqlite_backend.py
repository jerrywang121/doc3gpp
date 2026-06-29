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
