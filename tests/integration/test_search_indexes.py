"""Verify ``create_schema`` creates the composite filter indexes.

Task 4 of the FTS5 perf follow-up plan. The three composite indexes
are append-only DDL on the regular ``tdocs`` and ``meetings`` tables,
gated on the same sqlite + FTS5 availability check the FTS5 virtual
table uses (FTS5 is sqlite-only, so anything that gates the FTS5
schema on sqlite will also gate the indexes; the index DDL is plain
SQL and would work cross-dialect but the surrounding code is
sqlite-only by convention).
"""

from __future__ import annotations

from sqlalchemy import text

from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine


EXPECTED_TDOC_INDEXES = {
    "idx_tdocs_release_spec",
    "idx_tdocs_uploaded_date",
}
EXPECTED_MEETING_INDEXES = {
    "idx_meetings_name_tsg",
}


def test_create_schema_creates_filter_indexes(sqlite_env) -> None:
    create_schema()
    engine = get_engine()
    with engine.begin() as conn:
        tdoc_indexes = {
            row[1]
            for row in conn.execute(text("PRAGMA index_list('tdocs')")).all()
        }
        meeting_indexes = {
            row[1]
            for row in conn.execute(text("PRAGMA index_list('meetings')")).all()
        }
    missing_tdoc = EXPECTED_TDOC_INDEXES - tdoc_indexes
    missing_meeting = EXPECTED_MEETING_INDEXES - meeting_indexes
    assert not missing_tdoc, (
        f"create_schema() did not create tdoc indexes: {sorted(missing_tdoc)}"
    )
    assert not missing_meeting, (
        f"create_schema() did not create meeting indexes: {sorted(missing_meeting)}"
    )
