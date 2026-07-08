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
            name="R5-100",
            title="Test 100",
            location="Online",
            start_date=date(2025, 1, 10),
            end_date=date(2025, 1, 14),
            tsg="R5",
        ),
        MeetingORM(
            meeting_id=2,
            name="R5-101",
            title="Test 101",
            location="Rome",
            start_date=date(2026, 5, 20),
            end_date=date(2026, 5, 24),
            tsg="R5",
        ),
        MeetingORM(
            meeting_id=3,
            name="RAN5-TTCN Workshop",
            title="Workshop",
            location="Online",
            start_date=date(2026, 7, 2),
            end_date=date(2026, 7, 2),
            tsg="R5",
        ),
        # TTCN email meeting: starts Dec 2025 and runs through Dec 2026.
        # The TDoc numbering on its FTP server uses the end_date year (2026),
        # so the --year filter must key off end_date.year to match those TDocs.
        MeetingORM(
            meeting_id=4,
            name="RAN5-TTCN-WS#75",
            title="TTCN email meeting",
            location="Online",
            start_date=date(2025, 12, 1),
            end_date=date(2026, 12, 1),
            tsg="R5",
        ),
        # Cross-TSG row to assert the FK filter excludes it.
        MeetingORM(
            meeting_id=5,
            name="S2-150",
            title="Test 150",
            location="Vienna",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 5),
            tsg="S2",
        ),
        # Legacy row without an owning TSG — excluded by the FK filter.
        MeetingORM(
            meeting_id=6,
            name="LEGACY-no-tsg",
            title="Imported before column was added",
            location="Online",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 2),
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

    # FK equality; SA WG2 row and legacy null-tsg row must be excluded.
    r5 = repo.list(limit=10, tsg="r5")
    assert {m.meeting_id for m in r5} == {1, 2, 3, 4}

    # year filter (end_date year): TTCN row ends Dec 2026, so it joins the 2026 bucket
    y2026 = repo.list(limit=10, year=2026)
    assert {m.meeting_id for m in y2026} == {2, 3, 4, 5, 6}

    # TTCN row starts in 2025 but must NOT match year=2025 because its end_date is 2026
    y2025 = repo.list(limit=10, year=2025)
    assert {m.meeting_id for m in y2025} == {1}

    # name_like filter (SQL LIKE, match Workshop); TTCN row name has no "Workshop" substring
    w = repo.list(limit=10, name_like="%Workshop%")
    assert len(w) == 1
    assert w[0].meeting_id == 3
