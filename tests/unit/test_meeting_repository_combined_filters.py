from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.models import MeetingORM, TsgORM
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository


def _make_engine():
    return create_engine("sqlite+pysqlite:///:memory:", future=True)


def insert_rows(session):
    # Seed TSG reference rows first; meetings.tsg is an FK to tsgs.short_name
    session.add_all(
        [
            TsgORM(
                tsg_name="RAN WG5",
                short_name="R5",
                description="Mobile terminal conformance testing",
                url=None,
            ),
            TsgORM(
                tsg_name="SA WG2",
                short_name="S2",
                description="System Architecture",
                url=None,
            ),
        ]
    )
    session.flush()
    rows = [
        MeetingORM(
            meeting_id=1,
            name="R5-300",
            title="TTCN Workshop",
            location="Online",
            start_date=date(2026, 7, 2),
            end_date=date(2026, 7, 2),
            tsg="R5",
        ),
        MeetingORM(
            meeting_id=2,
            name="RAN5-TTCN Workshop",
            title="Workshop",
            location="Online",
            start_date=date(2026, 7, 2),
            end_date=date(2026, 7, 2),
            tsg="R5",
        ),
        MeetingORM(
            meeting_id=3,
            name="R5-301",
            title="Other",
            location="City",
            start_date=date(2025, 5, 20),
            end_date=date(2025, 5, 24),
            tsg="R5",
        ),
        MeetingORM(
            meeting_id=4,
            name="S2-150",
            title="Other",
            location="City",
            start_date=date(2026, 7, 2),
            end_date=date(2026, 7, 2),
            tsg="S2",
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

    # tsg='r5' + name_like='%TTCN%' + year=2026 — row 2 (its name contains TTCN)
    rows = repo.list(limit=10, tsg="r5", name_like="%TTCN%", year=2026)
    assert [r.meeting_id for r in rows] == [2]

    # tsg='r5' + name_like='R5-%' + year=2026 — row 1 (its name starts with "R5-")
    rows2 = repo.list(limit=10, tsg="r5", name_like="R5-%", year=2026)
    assert [r.meeting_id for r in rows2] == [1]

    # FK filter alone skips the SA WG2 fixture
    r5_only = repo.list(limit=10, tsg="r5")
    assert {r.meeting_id for r in r5_only} == {1, 2, 3}
