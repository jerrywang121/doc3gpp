from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.models import MeetingORM
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository


def _make_engine():
    return create_engine("sqlite+pysqlite:///:memory:", future=True)


def insert_rows(session):
    rows = [
        MeetingORM(
            meeting_id=1,
            name="R5-100",
            title="Test 100",
            location="Online",
            start_date=date(2025, 1, 10),
            end_date=date(2025, 1, 14),
        ),
        MeetingORM(
            meeting_id=2,
            name="R5-101",
            title="Test 101",
            location="Rome",
            start_date=date(2026, 5, 20),
            end_date=date(2026, 5, 24),
        ),
        MeetingORM(
            meeting_id=3,
            name="RAN5-TTCN Workshop",
            title="Workshop",
            location="Online",
            start_date=date(2026, 7, 2),
            end_date=date(2026, 7, 2),
        ),
    ]

    session.add_all(rows)
    session.commit()


def test_list_filters_by_tsg_and_year_and_like():
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    # seed
    with Session() as s:
        insert_rows(s)

    repo = SQLAlchemyMeetingRepository()
    # inject our session factory
    repo._session_factory = Session

    # tsg filter (names starting with R5) should match R5-100 and R5-101
    r5 = repo.list(limit=10, tsg="r5")
    assert len(r5) == 2

    # year filter
    y2026 = repo.list(limit=10, year=2026)
    assert {m.meeting_id for m in y2026} == {2, 3}

    # name_like filter (SQL LIKE, match Workshop)
    w = repo.list(limit=10, name_like="%Workshop%")
    assert len(w) == 1
    assert w[0].meeting_id == 3
