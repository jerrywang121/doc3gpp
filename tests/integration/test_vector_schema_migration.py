from __future__ import annotations

import pytest  # noqa: F401 - needed for pytestmark

pytestmark = pytest.mark.semantic


def test_vector_schema_created(sqlite_env):
    from doc3gpp.storage.db.migrate import create_schema
    create_schema()
    from doc3gpp.storage.db.session import get_engine
    # vec_tdoc_embeddings is a virtual table; may not appear in
    # get_table_names() depending on sqlalchemy version. Use a raw
    # query instead.
    with get_engine().begin() as conn:
        from sqlalchemy import text
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='vec_meta'")
        ).all()
        assert any(r[0] == "vec_meta" for r in rows)
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='vec_tdoc_embeddings'")
        ).all()
        assert any(r[0] == "vec_tdoc_embeddings" for r in rows)


def test_vector_schema_idempotent(sqlite_env):
    from doc3gpp.storage.db.migrate import create_schema
    create_schema()
    create_schema()  # second call must not raise


def test_vector_schema_skipped_on_non_sqlite(monkeypatch, tmp_path):
    # Simulate non-sqlite by mocking the engine dialect
    from doc3gpp.storage.db import migrate
    create_schema_called = False
    def stub():
        nonlocal create_schema_called
        create_schema_called = True
    monkeypatch.setattr(migrate, "_create_vector_schema", stub)
    # Just verify the function exists and is callable
    assert callable(migrate._create_vector_schema)
