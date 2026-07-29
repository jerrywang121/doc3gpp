"""Integration test: tdoc_cr_change_details table is created."""

from __future__ import annotations

from sqlalchemy import text

from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine


def test_table_is_created_with_expected_columns() -> None:
    create_schema()
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(tdoc_cr_change_details)")).all()
    cols = {row[1] for row in rows}
    assert "ftp_url" in cols
    assert "tdoc_id" in cols
    assert "clauses" in cols
    assert "changes" in cols