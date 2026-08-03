"""Unit tests for the shared Jinja template filters used by the web UI."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from doc3gpp.web.templates_setup import dt_short, sync_state


def test_dt_short_formats_aware_datetime() -> None:
    dt = datetime(2026, 8, 3, 14, 5, 30, tzinfo=timezone.utc)
    assert dt_short(dt) == "2026-08-03 14:05"


def test_dt_short_none_returns_none() -> None:
    assert dt_short(None) is None


def test_sync_state_fresh_within_24h() -> None:
    value = datetime.now(timezone.utc) - timedelta(hours=1)
    assert sync_state(value) == "fresh"


def test_sync_state_fresh_exactly_24h() -> None:
    fixed = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
    value = fixed - timedelta(hours=24)
    with patch("doc3gpp.web.templates_setup.datetime") as mock_datetime:
        mock_datetime.now.return_value = fixed
        assert sync_state(value) == "fresh"


def test_sync_state_stale_older_than_24h() -> None:
    value = datetime.now(timezone.utc) - timedelta(hours=25)
    assert sync_state(value) == "stale"


def test_sync_state_never_for_none() -> None:
    assert sync_state(None) == "never"
