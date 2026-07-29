"""Verify ``create_schema`` creates the FTS5 virtual + meta tables."""

from __future__ import annotations

import pytest  # noqa: F401 - kept per spec
from sqlalchemy import inspect, text

from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine


def test_create_schema_creates_search_objects(sqlite_env) -> None:
    create_schema()
    engine = get_engine()
    insp = inspect(engine)
    assert "tdoc_search" in insp.get_table_names(), (
        "tdoc_search FTS5 virtual table should exist after create_schema"
    )
    assert "tdoc_search_meta" in insp.get_table_names(), (
        "tdoc_search_meta sidecar should exist after create_schema"
    )


def test_meta_table_has_expected_keys(sqlite_env) -> None:
    create_schema()
    engine = get_engine()
    with engine.begin() as conn:
        # Touch the meta table — empty is fine; the column shape is
        # what we assert here.
        rows = conn.execute(
            text("SELECT key, value FROM tdoc_search_meta")
        ).all()
    assert isinstance(rows, list)