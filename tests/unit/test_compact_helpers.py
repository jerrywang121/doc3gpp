"""Tests for ``_resolve_compact`` (the CLI → settings precedence helper)."""

from __future__ import annotations


def test_resolve_compact_cli_true_wins_over_setting_false(monkeypatch) -> None:
    """``--compact`` on the command line forces ``True`` even when the
    setting is ``False`` (the CLI is the highest-precedence layer)."""
    from doc3gpp.cli import _resolve_compact
    from doc3gpp.settings.loader import get_settings

    monkeypatch.setattr(get_settings(), "output",
                        type(get_settings().output)(format="table", compact=False))
    assert _resolve_compact(True) is True


def test_resolve_compact_cli_false_setting_true(monkeypatch) -> None:
    """When the CLI flag is absent (``False``) the setting can still opt in."""
    from doc3gpp.cli import _resolve_compact
    from doc3gpp.settings.loader import get_settings

    monkeypatch.setattr(get_settings(), "output",
                        type(get_settings().output)(format="table", compact=True))
    assert _resolve_compact(False) is True


def test_resolve_compact_default_false(monkeypatch) -> None:
    """Default (no CLI flag, default setting) yields ``False``."""
    from doc3gpp.cli import _resolve_compact
    from doc3gpp.settings.loader import get_settings

    monkeypatch.setattr(get_settings(), "output",
                        type(get_settings().output)(format="table", compact=False))
    assert _resolve_compact(False) is False
