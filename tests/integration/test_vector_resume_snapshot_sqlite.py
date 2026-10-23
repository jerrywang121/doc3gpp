"""A TDoc vector rebuild resume stamps the snapshot of the fresh run that started it.

A fresh rebuild persists ``MAX(tdocs.uploaded_date)`` taken before its first batch as
``last_rebuild_snapshot_uploaded_date``. A resume reads that value instead of retaking the
maximum, so a TDoc synced during an interrupted run, or after a completed run, with an id
behind the cursor keeps an upload date at or above the stamp, and stale-only selects it.
A rebuild started before the key existed has no snapshot, so its resume retakes the
current maximum.

Uses the real sqlite-vec index (``sqlite_env``) and a deterministic embedder fake so the
rebuild paths run without a remote API.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import numpy as np
import pytest
from sqlalchemy import text

from doc3gpp.services.semantic_search_service import SemanticSearchService
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import TDocORM
from doc3gpp.storage.db.session import get_engine, get_session_factory
from doc3gpp.storage.repositories.vector_sql import SQLAlchemyVectorIndexRepository

pytestmark = pytest.mark.semantic

DIM = 384
MODEL = "nomic-embed-text"
_MEETING_ID = 100
_BEHIND_ID = "R5-2600200"  # sorts before the cursor in every test
_A_ID = "R5-2600201"
_B_ID = "R5-2600202"
_AHEAD_ID = "R5-2600203"
_UPLOADED = date(2026, 1, 2)
_GAP = date(2026, 1, 15)
_LATER = date(2026, 2, 1)
_SNAPSHOT_KEY = "last_rebuild_snapshot_uploaded_date"


class _Embedder:
    def __init__(self, model: str, dim: int = DIM) -> None:
        self.model_name = model
        self.dim = dim

    def encode(self, texts):
        vectors = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row in range(len(texts)):
            vectors[row, 0] = 1.0
        return vectors


def _service(model: str = MODEL, dim: int = DIM) -> SemanticSearchService:
    return SemanticSearchService(
        MagicMock(),
        _Embedder(model, dim),
        SQLAlchemyVectorIndexRepository(),
        get_settings(),
    )


def _run(*, stale_only: bool, resume: bool) -> None:
    list(
        _service().rebuild_embeddings(
            batch_size=1, stale_only=stale_only, quiet=True, resume=resume,
        )
    )


def _interrupt_after_first_batch() -> None:
    """Stop a fresh rebuild once the first batch's cursor (``_A_ID``) is persisted."""
    generator = _service().rebuild_embeddings(
        batch_size=1, stale_only=False, quiet=True, resume=False,
    )
    next(generator)
    next(generator)
    generator.close()
    assert _stored("last_rebuild_last_tdoc_id") == _A_ID


def _insert_tdoc(tdoc_id: str, uploaded: date) -> None:
    with get_session_factory()() as session:
        session.add(
            TDocORM(
                tdoc_id=tdoc_id,
                title=f"Metadata title for {tdoc_id}",
                meeting_id=_MEETING_ID,
                type="WID new",
                status="Agreed",
                uploaded_date=uploaded,
                release="Rel-18",
                spec="38.300",
            )
        )
        session.commit()


def _seed_schema() -> None:
    create_schema()
    with get_engine().begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tsgs (tsg_name, short_name, description) "
                "VALUES ('RAN WG1', 'RAN1', 'TSG RAN WG1')"
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO meetings (
                    meeting_id, name, title, location, tsg, start_date,
                    end_date, ftp_url, tdoc_list_last_sync
                ) VALUES (
                    100, 'RAN1#100', 'RAN1#100', 'Online', 'RAN1',
                    '2026-01-01', '2026-01-05',
                    'https://www.3gpp.org/ftp/meetings/RAN1_100',
                    '2026-01-05T00:00:00'
                )
                """
            )
        )


def _seed() -> None:
    _seed_schema()
    _insert_tdoc(_A_ID, _UPLOADED)
    _insert_tdoc(_B_ID, _UPLOADED)


def _delete_snapshot() -> None:
    with get_engine().begin() as connection:
        connection.execute(
            text("DELETE FROM vec_meta WHERE key = :key"), {"key": _SNAPSHOT_KEY},
        )


def _indexed_tdoc_ids() -> set[str]:
    with get_engine().begin() as connection:
        rows = connection.execute(
            text("SELECT DISTINCT tdoc_id FROM vec_tdoc_embeddings")
        ).all()
    return {row[0] for row in rows}


def _stored(key: str) -> str | None:
    with get_engine().begin() as connection:
        return connection.execute(
            text("SELECT value FROM vec_meta WHERE key = :key"), {"key": key},
        ).scalar()


def _stale_only_ids() -> set[str]:
    return {
        tdoc_id
        for batch in SQLAlchemyVectorIndexRepository().rebuild_batch(
            batch_size=10, after_id=None, stale_only=True,
        )
        for tdoc_id in batch
    }


def test_gap_tdoc_behind_cursor_is_stale_after_resume(sqlite_env) -> None:
    _seed()
    _interrupt_after_first_batch()
    _insert_tdoc(_BEHIND_ID, _GAP)
    _insert_tdoc(_AHEAD_ID, _LATER)

    _run(stale_only=False, resume=True)

    repo = SQLAlchemyVectorIndexRepository()
    assert repo.status().is_stale is True
    assert _BEHIND_ID in _stale_only_ids()
    _run(stale_only=True, resume=False)
    assert _BEHIND_ID in _indexed_tdoc_ids()
    assert repo.status().is_stale is False


def test_resume_after_completed_run_does_not_advance_stamp(sqlite_env) -> None:
    _seed()
    _run(stale_only=False, resume=False)
    _insert_tdoc(_BEHIND_ID, _GAP)
    _insert_tdoc(_AHEAD_ID, _LATER)

    _run(stale_only=False, resume=True)

    assert _stored("last_indexed_uploaded_date") == str(_UPLOADED)
    assert _BEHIND_ID in _stale_only_ids()


def test_resume_without_persisted_snapshot_falls_back_to_current_max(sqlite_env) -> None:
    _seed()
    _run(stale_only=False, resume=False)
    _delete_snapshot()
    _insert_tdoc(_AHEAD_ID, _LATER)

    _run(stale_only=False, resume=True)

    assert _stored(_SNAPSHOT_KEY) is None
    assert _stored("last_indexed_uploaded_date") == str(_LATER)


def test_fresh_empty_corpus_persists_marker_not_missing_key(sqlite_env) -> None:
    _seed_schema()
    _run(stale_only=False, resume=False)
    assert _stored(_SNAPSHOT_KEY) == ""

    _insert_tdoc(_A_ID, _UPLOADED)
    _run(stale_only=False, resume=True)

    assert _indexed_tdoc_ids() == {_A_ID}
    assert _stored("last_indexed_uploaded_date") is None
    assert _stored("last_rebuild_at") is not None
