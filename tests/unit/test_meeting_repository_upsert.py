"""Tests for ``SQLAlchemyMeetingRepository`` upsert/trim behaviour."""

from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from doc3gpp.models.meeting import Meeting
from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.models import MeetingORM
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository


def _make_repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    repo = SQLAlchemyMeetingRepository()
    repo._session_factory = Session
    return repo, Session


def _make_meeting(meeting_id: int, name: str = "R5", end: date | None = date(2026, 7, 2)) -> Meeting:
    return Meeting(
        meeting_id=meeting_id,
        name=name,
        title="x",
        location="x",
        start_date=date(2026, 7, 2),
        end_date=end,
    )


def test_upsert_many_updates_existing_row_in_place() -> None:
    repo, Session = _make_repo()
    repo.upsert_many([_make_meeting(1)])

    repo.upsert_many(
        [
            Meeting(
                meeting_id=1,
                name="R5",
                title="updated title",
                location="Paris",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 3),
                ftp_url="tsg_ran/docs/",
                start_doc="R5w260300",
                end_doc="R5w260301",
            )
        ]
    )

    with Session() as session:
        rows = session.scalars(select(MeetingORM)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.title == "updated title"
        assert row.location == "Paris"
        assert row.start_date == date(2026, 8, 1)
        assert row.end_date == date(2026, 8, 3)
        assert row.ftp_url == "tsg_ran/docs/"
        assert row.start_doc == "R5w260300"
        assert row.end_doc == "R5w260301"


def test_upsert_many_no_op_when_empty() -> None:
    repo, _ = _make_repo()
    assert repo.upsert_many([]) == 0


def test_delete_with_end_before_removes_only_out_of_window_rows() -> None:
    repo, Session = _make_repo()
    repo.upsert_many(
        [
            _make_meeting(1, end=date(2026, 1, 1)),
            _make_meeting(2, end=date(2025, 1, 1)),
            _make_meeting(3, end=date(2020, 1, 1)),
        ]
    )

    deleted = repo.delete_with_end_before(date(2025, 6, 1))

    assert deleted == 2
    with Session() as session:
        remaining_ids = sorted(r.meeting_id for r in session.scalars(select(MeetingORM)).all())
        assert remaining_ids == [1]


def test_delete_with_end_before_returns_zero_when_nothing_matches() -> None:
    repo, _ = _make_repo()
    repo.upsert_many([_make_meeting(1, end=date(2026, 1, 1))])
    assert repo.delete_with_end_before(date(2025, 1, 1)) == 0


def test_list_ordering_ties_break_by_meeting_id_desc() -> None:
    repo, _ = _make_repo()
    repo.upsert_many(
        [
            _make_meeting(1, name="R5-1"),
            _make_meeting(2, name="R5-2"),
            _make_meeting(3, name="R5-3"),
        ]
    )

    rows = repo.list(limit=10)

    assert [r.meeting_id for r in rows] == [3, 2, 1]


def test_orm_to_domain_helper_round_trips() -> None:
    """Sanity: every code path that maps ORM→domain preserves the row data."""

    repo, _ = _make_repo()
    repo.upsert_many([_make_meeting(1, name="R5-XYZ")])

    rows = repo.list(limit=1)
    assert len(rows) == 1
    assert rows[0].meeting_id == 1
    assert rows[0].name == "R5-XYZ"

    one = repo.get_by_id(1)
    assert one is not None
    assert one.name == "R5-XYZ"

    by_name = repo.get_by_name("R5-XYZ")
    assert by_name is not None
    assert by_name.meeting_id == 1

