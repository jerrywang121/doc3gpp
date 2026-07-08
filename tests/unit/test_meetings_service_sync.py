"""Tests for ``MeetingService.sync`` year-window filtering and trimming.

The trim behaviour (deleting meetings that fall outside the year window
after upsert) is exercised via both an in-memory repository double (for
predicates and happy-path counts) and the real SQLAlchemy repository (for
the trim log/integration path).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from doc3gpp.models.meeting import Meeting
from doc3gpp.services.meetings_service import (
    MeetingService,
    filter_by_year_window,
    years_ago,
)
from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.models import MeetingORM
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository


class _FakeRepo:
    def __init__(self) -> None:
        self.upserts: list[list[Meeting]] = []
        self.deleted_cutoffs: list[date] = []
        self.deleted_returns: list[int] = [0]

    def upsert_many(self, meetings):
        self.upserts.append(list(meetings))
        return len(meetings)

    def delete_with_end_before(self, cutoff):
        self.deleted_cutoffs.append(cutoff)
        return self.deleted_returns.pop(0) if self.deleted_returns else 0

    def list(self, **kwargs):
        return []


def _load_fixture() -> str:
    return Path("tests/fixtures/sample_pages/3GPP-meeting-R5.html").read_text(encoding="utf-8")


def test_sync_uses_module_level_year_filter_and_trims(monkeypatch) -> None:
    repo = _FakeRepo()
    service = MeetingService(repo)  # type: ignore[arg-type]

    from doc3gpp.parsers.calendar_parser import parse_3gpp_calendar

    meetings = parse_3gpp_calendar(_load_fixture())
    assert meetings  # fixture must have at least one row

    monkeypatch.setattr(
        "doc3gpp.services.meetings_service.fetch_calendar",
        lambda _url: meetings,
    )

    today = date(2026, 7, 2)
    written = service.sync(
        "https://example.invalid",
        max_year_closed=10,
        max_year_future=2,
        today=today,
    )

    # Fixture in-window ids with today=2026-07-02, closed=10, future=2:
    # R5-116 (2027), TTCN Workshop#74 (2026), R5-95-e (2022), R5-79 (2018).
    assert written == 4
    assert len(repo.upserts) == 1
    assert {m.meeting_id for m in repo.upserts[0]} == {82711, 85434, 60240, 18788}
    assert repo.deleted_cutoffs == [years_ago(today, 10)]


def test_sync_with_narrow_window_passes_correct_cutoff(monkeypatch) -> None:
    repo = _FakeRepo()
    service = MeetingService(repo)  # type: ignore[arg-type]

    today = date(2026, 7, 2)
    wide_meetings = [
        Meeting(
            meeting_id=1,
            name="recent",
            title="t",
            location="x",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 2),
        ),
        Meeting(
            meeting_id=2,
            name="old",
            title="t",
            location="x",
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 2),
        ),
    ]

    monkeypatch.setattr(
        "doc3gpp.services.meetings_service.fetch_calendar",
        lambda _url: wide_meetings,
    )

    written = service.sync(
        "https://example.invalid",
        max_year_closed=2,
        max_year_future=1,
        today=today,
    )

    assert written == 1
    assert len(repo.upserts) == 1
    assert [m.meeting_id for m in repo.upserts[0]] == [1]
    assert repo.deleted_cutoffs == [years_ago(today, 2)]


def test_sync_logs_trim_count_when_repo_deletes_rows(monkeypatch, caplog) -> None:
    """When delete_with_end_before removes rows, sync must log the count."""

    repo = _FakeRepo()
    repo.deleted_returns = [3]
    service = MeetingService(repo)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "doc3gpp.services.meetings_service.fetch_calendar",
        lambda _url: [
            Meeting(
                meeting_id=1,
                name="x",
                title="x",
                location="x",
                start_date=date(2026, 7, 2),
                end_date=date(2026, 7, 2),
            )
        ],
    )

    with caplog.at_level(logging.INFO, logger="doc3gpp.services.meetings_service"):
        service.sync("https://example.invalid", max_year_closed=2, max_year_future=1, today=date(2026, 7, 2))

    assert any("Trimmed 3 meeting rows" in record.message for record in caplog.records)


def test_sync_trims_via_real_sqlalchemy_repo(monkeypatch) -> None:
    """End-to-end: re-sync with narrow window deletes old rows from the DB.

    Covers the SQLAlchemy delete_with_end_before path exercised through
    ``MeetingService.sync`` (i.e. covers the ``if deleted: logger.info``
    branch via a real round-trip).
    """

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    repo = SQLAlchemyMeetingRepository()
    repo._session_factory = Session

    with Session() as s:
        s.add_all(
            [
                MeetingORM(
                    meeting_id=1,
                    name="recent",
                    title="x",
                    location="x",
                    start_date=date(2026, 1, 1),
                    end_date=date(2026, 1, 2),
                ),
                MeetingORM(
                    meeting_id=2,
                    name="ancient",
                    title="x",
                    location="x",
                    start_date=date(2019, 1, 1),
                    end_date=date(2019, 1, 2),
                ),
            ]
        )
        s.commit()

    monkeypatch.setattr(
        "doc3gpp.services.meetings_service.fetch_calendar",
        lambda _url: [
            Meeting(
                meeting_id=1,
                name="recent",
                title="x",
                location="x",
                start_date=date(2026, 7, 2),
                end_date=date(2026, 7, 2),
            )
        ],
    )

    service = MeetingService(repo)  # type: ignore[arg-type]
    written = service.sync(
        "https://example.invalid",
        max_year_closed=2,
        max_year_future=1,
        today=date(2026, 7, 2),
    )

    assert written == 1
    with Session() as s:
        rows = s.scalars(select(MeetingORM)).all()
        assert [r.meeting_id for r in rows] == [1]


def test_filter_does_not_drop_recent_and_future_endpoints() -> None:
    """Smoke test for ``filter_by_year_window`` via the public export."""

    meetings = [
        Meeting(
            meeting_id=1,
            name="x",
            title="x",
            location="x",
            start_date=date(2024, 7, 2),
            end_date=date(2024, 7, 3),
        ),
        Meeting(
            meeting_id=2,
            name="x",
            title="x",
            location="x",
            start_date=date(2027, 7, 2),
            end_date=date(2027, 7, 3),
        ),
        Meeting(
            meeting_id=3,
            name="x",
            title="x",
            location="x",
            start_date=date(2020, 7, 2),
            end_date=date(2020, 7, 3),
        ),
    ]
    kept = filter_by_year_window(meetings, max_year_closed=2, max_year_future=1, today=date(2026, 7, 2))
    assert {m.meeting_id for m in kept} == {1, 2}


def test_sync_stamps_tsg_on_every_meeting_row(monkeypatch) -> None:
    """``sync(tsg=...)`` must populate ``Meeting.tsg`` on every upserted row."""
    repo = _FakeRepo()
    service = MeetingService(repo)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "doc3gpp.services.meetings_service.fetch_calendar",
        lambda _url: [
            Meeting(
                meeting_id=1,
                name="R5-200",
                title="x",
                location="x",
                start_date=date(2026, 7, 2),
                end_date=date(2026, 7, 3),
            ),
            Meeting(
                meeting_id=2,
                name="RAN5-TTCN Workshop",
                title="x",
                location="x",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 1),
            ),
        ],
    )

    service.sync(
        "https://example.invalid",
        max_year_closed=2,
        max_year_future=1,
        today=date(2026, 7, 2),
        tsg="r5",
    )

    assert len(repo.upserts) == 1
    stamped = repo.upserts[0]
    assert {m.meeting_id for m in stamped} == {1, 2}
    assert all(m.tsg == "R5" for m in stamped)


def test_sync_leaves_tsg_unset_when_not_provided(monkeypatch) -> None:
    """``sync(tsg=None)`` (default) must not stamp the field — bulk-import / tests use this path."""
    repo = _FakeRepo()
    service = MeetingService(repo)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "doc3gpp.services.meetings_service.fetch_calendar",
        lambda _url: [
            Meeting(
                meeting_id=1,
                name="R5-200",
                title="x",
                location="x",
                start_date=date(2026, 7, 2),
                end_date=date(2026, 7, 3),
            )
        ],
    )

    service.sync("https://example.invalid", today=date(2026, 7, 2))

    assert repo.upserts[0][0].tsg is None
