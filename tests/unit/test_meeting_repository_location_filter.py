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
            name="R5-400",
            title="Event",
            location="Online",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 2),
        ),
        MeetingORM(
            meeting_id=2,
            name="R5-401",
            title="Event",
            location="Bengaluru",
            start_date=date(2026, 2, 1),
            end_date=date(2026, 2, 2),
        ),
    ]

    session.add_all(rows)
    session.commit()


def test_location_like_filter():
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as s:
        insert_rows(s)

    repo = SQLAlchemyMeetingRepository()
    repo._session_factory = Session

    rows = repo.list(limit=10, location_like='%Online%')
    assert len(rows) == 1
    assert rows[0].meeting_id == 1
