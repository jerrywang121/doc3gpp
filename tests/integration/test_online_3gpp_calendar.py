from __future__ import annotations

from datetime import date

import httpx
import pytest

from doc3gpp.scraping.calendar_source import fetch_calendar

pytestmark = pytest.mark.online


def _fetch_live_or_skip() -> list:
    try:
        return fetch_calendar("https://www.3gpp.org/dynareport?code=Meetings-R5.htm")
    except httpx.HTTPError as exc:
        pytest.skip(f"online calendar endpoint not reachable in this environment: {exc}")


def test_online_fetch_r5_calendar_returns_rows() -> None:
    """Online integration test against 3gpp.org calendar endpoint."""

    meetings = _fetch_live_or_skip()

    assert len(meetings) > 0
    assert any(m.meeting_id > 0 for m in meetings)
    assert any((m.ftp_url is not None) or (m.start_doc is not None) for m in meetings)


def test_online_calendar_rows_have_valid_dates() -> None:
    """Online integration test validating parsed date fields from live data."""

    meetings = _fetch_live_or_skip()

    sample = meetings[0]
    assert isinstance(sample.start_date, date)
    assert isinstance(sample.end_date, date)
    assert sample.start_date <= sample.end_date
