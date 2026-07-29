"""Tests for the new body-change knobs on TDocParseSettings."""

from __future__ import annotations

from doc3gpp.settings.schema import TDocParseSettings


def test_body_change_defaults() -> None:
    settings = TDocParseSettings()
    assert settings.body_change_enabled is True
    assert settings.body_change_gap_window == 2
    assert settings.body_change_context_padding == 2


def test_body_change_gap_window_bounds() -> None:
    """Negative or oversized gap windows are rejected at the boundary."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TDocParseSettings(body_change_gap_window=-1)
    with pytest.raises(ValidationError):
        TDocParseSettings(body_change_gap_window=21)


def test_body_change_context_padding_bounds() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TDocParseSettings(body_change_context_padding=-1)
    with pytest.raises(ValidationError):
        TDocParseSettings(body_change_context_padding=51)
