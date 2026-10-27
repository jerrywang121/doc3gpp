"""A ``vec_tdoc_embeddings`` layout drop also clears the TDoc rebuild stamps.

``_create_vector_schema`` drops and recreates the vec0 table when it predates
the metadata columns. The recreated table is empty, so the rebuild stamps and
the resume cursor that describe the dropped rows must go too, matching
:meth:`SQLAlchemyVectorIndexRepository.reset_for_rebuild`. A DB already on the
new layout keeps its stamps, and a fresh DB stays unstamped.

Uses the real sqlite-vec index (``sqlite_env``) so the layout drop runs against
a real vec0 table.
"""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import text

pytest.importorskip("sqlite_vec")
pytestmark = pytest.mark.semantic

DIM = 4
_STAMPS = {
    "last_rebuild_at": "2026-01-02T03:04:05+00:00",
    "last_indexed_uploaded_date": "2026-01-02",
    "last_rebuild_last_tdoc_id": "R5-2600201",
    "last_rebuild_snapshot_uploaded_date": "2026-01-02",
}


def _seed_stamps(conn) -> None:
    for key, value in _STAMPS.items():
        conn.execute(
            text("INSERT OR REPLACE INTO vec_meta (key, value) VALUES (:k, :v)"),
            {"k": key, "v": value},
        )


def _stored_meta(conn) -> dict[str, str]:
    return {
        row[0]: row[1]
        for row in conn.execute(text("SELECT key, value FROM vec_meta")).all()
    }


def _make_old_layout(conn, width: int) -> None:
    from doc3gpp.storage.backends.sqlite import load_sqlite_vec

    load_sqlite_vec(conn.connection.driver_connection)
    conn.execute(text("DROP TABLE vec_tdoc_embeddings"))
    conn.execute(
        text(
            "CREATE VIRTUAL TABLE vec_tdoc_embeddings USING vec0(\n"
            "    chunk_id TEXT PRIMARY KEY,\n"
            "    tdoc_id TEXT,\n"
            "    chunk_index INTEGER,\n"
            f"    embedding FLOAT[{width}] distance_metric=cosine\n"
            ")"
        )
    )
    conn.execute(
        text(
            "INSERT INTO vec_tdoc_embeddings (chunk_id, tdoc_id, chunk_index, embedding) "
            "VALUES ('R5-A#0', 'R5-A', 0, :emb)"
        ),
        {"emb": np.zeros(width, dtype=np.float32).tobytes()},
    )


def test_layout_drop_clears_rebuild_stamps(sqlite_env):
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine

    create_schema("main")
    engine = get_engine()
    with engine.begin() as conn:
        _make_old_layout(conn, DIM)
        _seed_stamps(conn)

    create_schema("main")

    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(vec_tdoc_embeddings)"))}
        count = conn.execute(text("SELECT COUNT(*) FROM vec_tdoc_embeddings")).scalar_one()
        keys = set(_stored_meta(conn))

    assert {"meeting_id", "tsg", "uploaded_date"} <= columns
    assert count == 0
    assert not keys & set(_STAMPS), f"stale stamps survived the layout drop: {keys & set(_STAMPS)}"


def test_new_layout_keeps_rebuild_stamps(sqlite_env):
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine

    create_schema("main")
    engine = get_engine()
    with engine.begin() as conn:
        _seed_stamps(conn)

    create_schema("main")

    with engine.begin() as conn:
        stored = _stored_meta(conn)

    assert {key: stored[key] for key in _STAMPS} == _STAMPS


def test_fresh_db_schema_is_idempotent(sqlite_env):
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine

    create_schema()
    create_schema()

    with get_engine().begin() as conn:
        keys = set(_stored_meta(conn))

    assert not keys & set(_STAMPS)
