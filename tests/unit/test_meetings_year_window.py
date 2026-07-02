"""Tests for the year-window filter and calendar arithmetic helpers.

The original ``_filter_by_year_window`` static method used
``timedelta(days=356 * N)`` which drifted ~9 days per calendar year in
the wrong direction. These tests pin the new helper to a deterministic
``today`` so the cutoff can be asserted to the day.
"""

from __future__ import annotations

from datetime import date

from doc3gpp.models.meeting import Meeting
from doc3gpp.services.meetings_service import (
    filter_by_year_window,
    years_ago,
)


def _meeting(start: date, end: date) -> Meeting:
    return Meeting(
        meeting_id=1,
        name="x",
        title="x",
        location="x",
        start_date=start,
        end_date=end,
    )


def test_years_ago_simple() -> None:
    assert years_ago(date(2026, 7, 2), 0) == date(2026, 7, 2)
    assert years_ago(date(2026, 7, 2), 2) == date(2024, 7, 2)
    assert years_ago(date(2026, 7, 2), 10) == date(2016, 7, 2)


def test_years_ago_clamps_feb_29_to_feb_28() -> None:
    assert years_ago(date(2024, 2, 29), 1) == date(2023, 2, 28)
    assert years_ago(date(2024, 2, 29), 4) == date(2020, 2, 29)


def test_filter_keeps_meeting_exactly_at_closed_cutoff() -> None:
    today = date(2026, 7, 2)
    meetings = [
        _meeting(start=date(2024, 6, 30), end=date(2024, 7, 1)),  # 1 day before cutoff
        _meeting(start=date(2024, 7, 2), end=date(2024, 7, 2)),   # exactly 2y ago
    ]
    kept = filter_by_year_window(meetings, max_year_closed=2, max_year_future=1, today=today)
    assert [m.end_date for m in kept] == [date(2024, 7, 2)]


def test_filter_keeps_meeting_exactly_at_future_cutoff() -> None:
    today = date(2026, 7, 2)
    meetings = [
        _meeting(start=date(2027, 7, 3), end=date(2027, 7, 4)),   # 1 day past cutoff
        _meeting(start=date(2027, 7, 2), end=date(2027, 7, 2)),   # exactly 1y ahead
    ]
    kept = filter_by_year_window(meetings, max_year_closed=2, max_year_future=1, today=today)
    assert [m.start_date for m in kept] == [date(2027, 7, 2)]


def test_filter_does_not_drift_in_calendar_year_boundary() -> None:
    """The 356-day bug under-shoots ~9 days per year.

    A meeting ending exactly 2 calendar years ago should remain, while one
    ending 2 calendar years - 1 day ago should not. (With the 356-day bug,
    a meeting ending 2y+10d ago would also remain.)
    """
    today = date(2026, 7, 2)
    meetings = [
        _meeting(start=date(2024, 6, 22), end=date(2024, 6, 22)),  # 2y + 10d ago
        _meeting(start=date(2024, 7, 2), end=date(2024, 7, 2)),   # exactly 2y ago
    ]
    kept = filter_by_year_window(meetings, max_year_closed=2, max_year_future=1, today=today)
    assert [m.end_date for m in kept] == [date(2024, 7, 2)]


def test_filter_zero_window_keeps_only_today() -> None:
    """closed_years=0 + future_years=0 keeps only meetings that touch today."""

    today = date(2026, 7, 2)
    meetings = [
        _meeting(start=date(2026, 7, 2), end=date(2026, 7, 2)),
        _meeting(start=date(2026, 7, 3), end=date(2026, 7, 3)),
        _meeting(start=date(2026, 7, 1), end=date(2026, 7, 1)),
    ]
    kept = filter_by_year_window(
        meetings, max_year_closed=0, max_year_future=0, today=today
    )
    assert [m.end_date for m in kept] == [date(2026, 7, 2)]


def test_filter_no_meetings_returns_empty() -> None:
    assert filter_by_year_window([], max_year_closed=2, max_year_future=1) == []


def test_filter_falls_back_to_date_today_when_unspecified(monkeypatch) -> None:
    """When ``today`` is omitted, ``filter_by_year_window`` must call ``date.today()``.

    The test fakes ``date.today`` to a known value via monkeypatch so the
    result is deterministic regardless of when the test runs.
    """

    class _FrozenDate(date):
        @classmethod
        def today(cls):
            return date(2026, 7, 2)

    monkeypatch.setattr(
        "doc3gpp.services.meetings_service.date", _FrozenDate
    )
    kept = filter_by_year_window(
        [_meeting(start=date(2026, 7, 2), end=date(2026, 7, 2))],
        max_year_closed=2,
        max_year_future=1,
    )
    assert kept
