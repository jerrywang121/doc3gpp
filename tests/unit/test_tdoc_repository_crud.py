from __future__ import annotations

import datetime as _dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from doc3gpp.models.tdoc import TDoc
from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.models import TDocORM
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository


def _make_repo() -> SQLAlchemyTDocRepository:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    repo = SQLAlchemyTDocRepository()
    repo._session_factory = Session
    return repo


def _make_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


# ---------------------------------------------------------------------------
# Fix 1: upsert_many batch + atomic behavior.
# ---------------------------------------------------------------------------


def test_upsert_many_inserts_new_records() -> None:
    repo = _make_repo()

    count = repo.upsert_many(
        [
            TDoc(tdoc_id="R5-260001", title="Doc A"),
            TDoc(tdoc_id="R5-260002", title="Doc B"),
            TDoc(tdoc_id="S2-260100", title="Doc C"),
        ]
    )

    assert count == 3
    rows = repo.list(limit=10)
    assert {r.tdoc_id for r in rows} == {"R5-260001", "R5-260002", "S2-260100"}


def test_upsert_many_updates_existing_records() -> None:
    repo = _make_repo()
    repo.upsert_many(
        [
            TDoc(tdoc_id="R5-260001", title="Original Title", source="Acme"),
            TDoc(tdoc_id="R5-260002", title="Other"),
        ]
    )

    count = repo.upsert_many(
        [
            TDoc(tdoc_id="R5-260001", title="Updated Title", source="NewCo"),
            TDoc(tdoc_id="R5-260003", title="Brand New"),
        ]
    )

    assert count == 2
    rows = {r.tdoc_id: r for r in repo.list(limit=10)}
    assert rows["R5-260001"].title == "Updated Title"
    assert rows["R5-260001"].source == "NewCo"
    assert rows["R5-260002"].title == "Other"
    assert rows["R5-260003"].title == "Brand New"


def test_upsert_many_empty_list_returns_zero_no_op() -> None:
    repo = _make_repo()

    assert repo.upsert_many([]) == 0
    assert repo.list(limit=10) == []


def test_upsert_delegates_to_upsert_many() -> None:
    repo = _make_repo()

    repo.upsert(TDoc(tdoc_id="R5-260001", title="Single"))

    rows = repo.list(limit=10)
    assert len(rows) == 1
    assert rows[0].title == "Single"


# ---------------------------------------------------------------------------
# Fix 3: empty / None title survives the round-trip through the ORM.
# ---------------------------------------------------------------------------


def test_upsert_with_none_title_persists() -> None:
    repo = _make_repo()

    repo.upsert(TDoc(tdoc_id="R5-260001", title=None))

    rows = repo.list(limit=10)
    assert len(rows) == 1
    assert rows[0].title is None


def test_orm_column_is_nullable() -> None:
    # If the column were NOT NULL, this insert would raise IntegrityError.
    session = _make_session()
    session.add(TDocORM(tdoc_id="R5-260001", title=None))
    session.commit()

    row = session.query(TDocORM).filter_by(tdoc_id="R5-260001").one()
    assert row.title is None


# ---------------------------------------------------------------------------
# Fix 6: reservation_date / uploaded_date are Date columns, not strings.
# ---------------------------------------------------------------------------


def test_reservation_date_persists_as_date() -> None:
    repo = _make_repo()
    repo.upsert(
        TDoc(
            tdoc_id="R5-260001",
            title="Dated",
            reservation_date=_dt.date(2026, 6, 1),
            uploaded_date=_dt.date(2026, 6, 2),
        )
    )

    rows = repo.list(limit=10)
    assert len(rows) == 1
    assert rows[0].reservation_date == _dt.date(2026, 6, 1)
    assert rows[0].uploaded_date == _dt.date(2026, 6, 2)


def test_reservation_date_orm_column_is_date_type() -> None:
    """The ORM column type must be Date (not String) so SQL can enforce it."""
    from sqlalchemy import Date

    assert isinstance(TDocORM.reservation_date.type, Date)
    assert isinstance(TDocORM.uploaded_date.type, Date)


# ---------------------------------------------------------------------------
# Fix 7: updated_at is stamped on every write, and bumped on update.
# ---------------------------------------------------------------------------


def test_upsert_stamps_updated_at_on_new_row() -> None:
    repo = _make_repo()
    # SQLite strips timezone info on read; compare naive UTC stamps.
    before = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    repo.upsert(TDoc(tdoc_id="R5-260001", title="First"))
    after = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)

    rows = repo.list(limit=10)
    assert len(rows) == 1
    assert rows[0].updated_at is not None
    # The stamp should fall between the before/after wall clock.
    assert before <= rows[0].updated_at <= after


def test_upsert_bumps_updated_at_on_existing_row() -> None:
    repo = _make_repo()
    repo.upsert(TDoc(tdoc_id="R5-260001", title="First"))

    initial = repo.list(limit=10)[0].updated_at
    assert initial is not None

    # Force a measurable gap so the second stamp is strictly later.
    import time as _time
    _time.sleep(0.01)

    repo.upsert(TDoc(tdoc_id="R5-260001", title="Second"))

    updated = repo.list(limit=10)[0].updated_at
    assert updated is not None
    assert updated > initial


def test_new_rows_have_updated_at_set() -> None:
    """New rows start with updated_at populated (not NULL) so the
    ``ORDER BY updated_at`` style queries have a deterministic value."""
    repo = _make_repo()
    repo.upsert(TDoc(tdoc_id="R5-260001", title="First"))

    # Use the repo's session factory to read the row back.
    with repo._session_factory() as session:
        row = session.query(TDocORM).filter_by(tdoc_id="R5-260001").one()
    assert row.updated_at is not None