"""Integration tests for ``storage.db.migrate`` bootstrap helpers.

These tests cover additive migrations that ``Base.metadata.create_all``
does NOT perform — notably ``ALTER TABLE tsgs ADD COLUMN
spec_last_sync`` for databases created before that column existed.

The bootstrap path is a one-shot, idempotent sequence; each test pins
the post-condition of a specific helper so a future regression that
silently drops the migration is caught.
"""

from __future__ import annotations

from sqlalchemy import text

from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine


def _tsgs_columns() -> set[str]:
    """Return the set of column names on the ``tsgs`` table."""
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(tsgs)")).all()
    return {row[1] for row in rows}


def test_create_schema_adds_spec_last_sync_to_pre_existing_tsgs(sqlite_env) -> None:
    """``create_schema`` brings an older ``tsgs`` table (without
    ``spec_last_sync``) up to the current ORM schema in place.

    Simulates a database created before Task 6 added the column: the
    table exists with only the legacy columns, the column is missing,
    and ``create_schema`` must issue ``ALTER TABLE`` so the ORM can
    SELECT it.
    """
    engine = get_engine()
    with engine.begin() as conn:
        # Pre-Task 6 schema — no spec_last_sync.
        conn.execute(
            text(
                "CREATE TABLE tsgs ("
                "  tsg_name TEXT NOT NULL, "
                "  short_name TEXT NOT NULL PRIMARY KEY, "
                "  description TEXT NOT NULL, "
                "  url TEXT, "
                "  meeting_last_sync DATETIME"
                ")"
            )
        )

    assert "spec_last_sync" not in _tsgs_columns()

    create_schema()

    cols = _tsgs_columns()
    assert "spec_last_sync" in cols
    # Legacy columns preserved.
    assert {"tsg_name", "short_name", "description", "url", "meeting_last_sync"} <= cols


def test_create_schema_is_idempotent_when_column_already_present(sqlite_env) -> None:
    """``create_schema`` is a no-op on a database whose ``tsgs`` table
    already has ``spec_last_sync`` — no spurious ALTER TABLE attempts."""
    create_schema()
    first_run_cols = _tsgs_columns()
    assert "spec_last_sync" in first_run_cols

    # Second call must not raise.
    create_schema()

    assert _tsgs_columns() == first_run_cols


def test_create_schema_skips_migration_when_tsgs_does_not_exist(sqlite_env) -> None:
    """Fresh databases (no ``tsgs`` table yet) skip the ALTER step —
    ``Base.metadata.create_all`` is responsible for creating it with
    the column already in place."""
    engine = get_engine()
    with engine.begin() as conn:
        assert conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='tsgs' LIMIT 1"
            )
        ).first() is None

    create_schema()

    cols = _tsgs_columns()
    assert "spec_last_sync" in cols
    # All current ORM columns present.
    expected = {
        "tsg_name",
        "short_name",
        "description",
        "url",
        "meeting_last_sync",
        "spec_last_sync",
    }
    assert expected <= cols