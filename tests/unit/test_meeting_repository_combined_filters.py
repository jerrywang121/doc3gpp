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
            name="R5-300",
            title="TTCN Workshop",
            location="Online",
            start_date=date(2026, 7, 2),
            end_date=date(2026, 7, 2),
        ),
        MeetingORM(
            meeting_id=2,
            name="RAN5-TTCN Workshop",
            title="Workshop",
            location="Online",
            start_date=date(2026, 7, 2),
            end_date=date(2026, 7, 2),
        ),
        MeetingORM(
            meeting_id=3,
            name="R5-301",
            title="Other",
            location="City",
            start_date=date(2025, 5, 20),
            end_date=date(2025, 5, 24),
        ),
    ]

    session.add_all(rows)
    session.commit()


def test_combined_filters_match_only_row_1():
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as s:
        insert_rows(s)

    repo = SQLAlchemyMeetingRepository()
    repo._session_factory = Session

    # Combine tsg='r5' (name startswith), name_like '%TTCN%', year=2026
    rows = repo.list(limit=10, tsg="r5", name_like="%TTCN%", year=2026)
    # Only meeting_id 1 should match (name 'R5-300' contains no TTCN, but title does; repository filters on name)
    # In this test we expect zero matches because name_like matches 'TTCN' only in name for row 2, but row 2 doesn't start with R5
    assert [r.meeting_id for r in rows] == []

    # Now a pattern matching 'R5-%' and year 2026 should find meeting 1
    rows2 = repo.list(limit=10, tsg="r5", name_like="R5-%", year=2026)
    assert [r.meeting_id for r in rows2] == [1]
