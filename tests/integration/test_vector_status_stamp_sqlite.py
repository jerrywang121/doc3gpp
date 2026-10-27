"""The TDoc vector rebuild stamps ``vec_meta`` so ``status()`` and stale-only are truthful.

A completed rebuild writes ``last_rebuild_at`` and ``last_indexed_uploaded_date``
(the snapshot of ``MAX(tdocs.uploaded_date)`` taken before the first batch). A reset
clears both, so an interrupted full rebuild reads as stale. A stale-only rebuild
over a kept index leaves the stamp in place until it completes.

Uses the real sqlite-vec index (``sqlite_env``) and a deterministic embedder fake so
the rebuild paths run without a remote API.
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

import numpy as np
import pytest
from sqlalchemy import event, text

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
_A_ID = "R5-2600201"
_B_ID = "R5-2600202"
_C_ID = "R5-2600203"
_EARLIER_ID = "R5-2600204"
_BLANK_ID = "R5-2600205"
_UPLOADED = date(2026, 1, 2)
_LATER = date(2026, 2, 1)
_EARLIER = date(2025, 12, 31)


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


def _rebuild(*, stale_only: bool) -> None:
    list(
        _service().rebuild_embeddings(
            batch_size=10, stale_only=stale_only, quiet=True, resume=False,
        )
    )


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


def _insert_blank_tdoc(tdoc_id: str, uploaded: date) -> None:
    """Insert a TDoc with no title and no meeting, so its embed text is empty."""
    with get_session_factory()() as session:
        session.add(
            TDocORM(
                tdoc_id=tdoc_id,
                title=None,
                meeting_id=None,
                type="WID new",
                status="Agreed",
                uploaded_date=uploaded,
                release="Rel-18",
                spec="38.300",
            )
        )
        session.commit()


def _seed() -> None:
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
    _insert_tdoc(_A_ID, _UPLOADED)
    _insert_tdoc(_B_ID, _UPLOADED)


def _max_uploaded() -> str:
    with get_engine().begin() as connection:
        return str(
            connection.execute(text("SELECT MAX(uploaded_date) FROM tdocs")).scalar()
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


def test_full_rebuild_stamps_status_and_is_not_stale(sqlite_env) -> None:
    _seed()

    _rebuild(stale_only=False)

    status = SQLAlchemyVectorIndexRepository().status()
    assert status.last_rebuild_at is not None
    assert status.last_indexed_uploaded_date == datetime.fromisoformat(_max_uploaded())
    assert status.is_stale is False


def test_newer_tdoc_is_stale_and_stale_only_selects_it(sqlite_env) -> None:
    _seed()
    _rebuild(stale_only=False)
    _insert_tdoc(_C_ID, _LATER)
    repo = SQLAlchemyVectorIndexRepository()
    assert repo.status().is_stale is True
    assert _stale_only_ids() == {_A_ID, _B_ID, _C_ID}  # >= cutoff: A and B share the snapshot day

    _rebuild(stale_only=True)

    assert _indexed_tdoc_ids() == {_A_ID, _B_ID, _C_ID}
    status = repo.status()
    assert status.is_stale is False
    assert status.last_indexed_uploaded_date == datetime.fromisoformat(_max_uploaded())


def test_interrupted_full_rebuild_leaves_status_stale(sqlite_env) -> None:
    _seed()
    _rebuild(stale_only=False)
    generator = _service().rebuild_embeddings(
        batch_size=10, stale_only=False, quiet=True, resume=False,
    )
    next(generator)

    generator.close()

    status = SQLAlchemyVectorIndexRepository().status()
    assert status.is_stale is True
    assert status.last_indexed_uploaded_date is None
    assert status.last_rebuild_at is None


def test_empty_corpus_rebuild_is_not_stale(sqlite_env) -> None:
    create_schema()

    _rebuild(stale_only=False)

    status = SQLAlchemyVectorIndexRepository().status()
    assert status.is_stale is False
    assert status.last_indexed_uploaded_date is None


def test_stale_only_keeps_stamp_when_index_is_kept(sqlite_env) -> None:
    _seed()
    _rebuild(stale_only=False)
    _insert_tdoc(_C_ID, _LATER)
    assert _stored("last_indexed_uploaded_date") == str(_UPLOADED)
    generator = _service().rebuild_embeddings(
        batch_size=10, stale_only=True, quiet=True, resume=False,
    )

    next(generator)

    assert _stored("last_indexed_uploaded_date") == str(_UPLOADED)
    assert _stored("last_rebuild_at") is not None
    list(generator)
    assert _stored("last_indexed_uploaded_date") == str(_LATER)


def test_tdoc_uploaded_during_rebuild_stays_stale(sqlite_env) -> None:
    _seed()
    generator = _service().rebuild_embeddings(
        batch_size=1, stale_only=False, quiet=True, resume=False,
    )
    next(generator)
    _insert_tdoc(_C_ID, _LATER)
    list(generator)

    repo = SQLAlchemyVectorIndexRepository()
    assert _stored("last_indexed_uploaded_date") == str(_UPLOADED)
    assert repo.status().is_stale is True
    assert _stale_only_ids() == {_A_ID, _B_ID, _C_ID}  # >= cutoff: A and B share the snapshot day


def test_same_day_title_only_tdoc_synced_after_rebuild_is_stale_only_selected(
    sqlite_env,
) -> None:
    _seed()
    _rebuild(stale_only=False)
    _insert_tdoc(_C_ID, _UPLOADED)
    assert _C_ID in _stale_only_ids()

    _rebuild(stale_only=True)

    assert _C_ID in _indexed_tdoc_ids()
    assert SQLAlchemyVectorIndexRepository().status().is_stale is False


def test_tdoc_before_snapshot_day_is_not_stale_only_selected(sqlite_env) -> None:
    _seed()
    _rebuild(stale_only=False)
    _insert_tdoc(_EARLIER_ID, _EARLIER)
    assert _EARLIER_ID not in _stale_only_ids()

    _rebuild(stale_only=True)

    assert _EARLIER_ID not in _indexed_tdoc_ids()


def test_snapshot_day_embedded_tdoc_is_reselected_by_stale_only(sqlite_env) -> None:
    """Accepted cost of the >= cutoff: embedded TDocs on the snapshot day are re-selected.

    The status stays fresh, because only a missing same-day TDoc makes it stale.
    """
    _seed()
    _rebuild(stale_only=False)

    assert {_A_ID, _B_ID} <= _stale_only_ids()
    assert SQLAlchemyVectorIndexRepository().status().is_stale is False


def test_same_day_title_only_tdoc_makes_status_stale(sqlite_env) -> None:
    _seed()
    _rebuild(stale_only=False)
    _insert_tdoc(_C_ID, _UPLOADED)

    assert SQLAlchemyVectorIndexRepository().status().is_stale is True


def test_same_day_blank_tdoc_without_embed_text_keeps_status_fresh(sqlite_env) -> None:
    """A same-day TDoc that can never be embedded must not make the status stale forever."""
    _seed()
    _rebuild(stale_only=False)
    _insert_blank_tdoc(_BLANK_ID, _UPLOADED)

    assert SQLAlchemyVectorIndexRepository().status().is_stale is False


def test_one_unindexed_same_day_tdoc_among_many_makes_status_stale(sqlite_env) -> None:
    _seed()
    _rebuild(stale_only=False)
    for index in range(5):
        _insert_tdoc(f"R5-26005{index:02d}", _UPLOADED)
    _rebuild(stale_only=True)
    assert SQLAlchemyVectorIndexRepository().status().is_stale is False

    _insert_tdoc("R5-2600599", _UPLOADED)

    assert SQLAlchemyVectorIndexRepository().status().is_stale is True


def _count_selects(call) -> int:
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", record)
    try:
        call()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return sum(1 for statement in statements if statement.lstrip().upper().startswith("SELECT"))


def test_status_select_count_does_not_grow_with_same_day_blank_tdocs(sqlite_env) -> None:
    _seed()
    _rebuild(stale_only=False)
    repo = SQLAlchemyVectorIndexRepository()
    _insert_blank_tdoc("R5-2600210", _UPLOADED)
    one = _count_selects(repo.status)

    for index in range(6):
        _insert_blank_tdoc(f"R5-26003{index:02d}", _UPLOADED)

    assert _count_selects(repo.status) == one


def test_rebuild_select_count_does_not_grow_with_tdoc_count(sqlite_env) -> None:
    _seed()
    two = _count_selects(lambda: _rebuild(stale_only=False))

    for index in range(6):
        _insert_tdoc(f"R5-26004{index:02d}", _UPLOADED)

    assert _count_selects(lambda: _rebuild(stale_only=False)) == two
