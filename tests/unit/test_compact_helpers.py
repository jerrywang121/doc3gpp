"""Tests for ``_resolve_compact`` (the CLI → settings precedence helper)
and the ``_emit_json`` / ``_emit_records`` compact seam (Task 3)."""

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


def test_emit_json_compact_single_line_no_trailing_newline() -> None:
    """Compact JSON is one line, no operator-space, no trailing newline."""
    import io
    import json

    from doc3gpp.cli import _emit_json

    stream = io.StringIO()
    _emit_json(
        [["R5s260001", "RAN5#111", "38.331"]],
        stream,
        ["tdoc_id", "meeting_name", "spec"],
        compact=True,
    )
    text = stream.getvalue()
    assert "\n" not in text
    assert ", " not in text
    assert ": " not in text
    # Round-trips back to the original payload.
    assert json.loads(text) == [
        {"tdoc_id": "R5s260001", "meeting_name": "RAN5#111", "spec": "38.331"}
    ]


def test_emit_json_default_still_pretty_prints() -> None:
    """Default (non-compact) output is byte-identical to today."""
    import io

    from doc3gpp.cli import _emit_json

    stream = io.StringIO()
    _emit_json(
        [["R5s260001", "RAN5#111"]],
        stream,
        ["tdoc_id", "meeting_name"],
    )
    text = stream.getvalue()
    assert text.endswith("\n")
    assert ",\n" in text  # pretty-print indent survives
