"""Tests for the ``doc3gpp server`` Typer sub-app scaffold.

The ``server_app`` is added in T2 as a placeholder Typer app with six
stub commands. T10 (install/uninstall) and T11 (start/stop/status/logs)
have since replaced the stubs with real implementations. The tests in
this module lock in:

* the sub-app is registered under the name ``server`` with the full
  set of six subcommands listed in the T2 brief;
* the ``[server] enabled = true`` gate rejects the subcommand before
  the command body runs when the operator has not opted in.

The brief's literal ``runner.invoke(server_app, [...])`` invocation
pattern is used directly because Typer's :class:`CliRunner` accepts it
once the sub-app has its ``info.name`` populated (which happens during
``app.add_typer`` registration — see :mod:`doc3gpp.cli`).
"""
from __future__ import annotations

import click
import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.cli_server import (
    _require_server_enabled,
    server_app,
)
from doc3gpp.settings.schema import ServerSettings, Settings
from doc3gpp.settings.loader import get_settings


EXPECTED_SUBCOMMAND_NAMES = ("start", "stop", "status", "logs", "install", "uninstall")


def test_subcommands_registered() -> None:
    """``doc3gpp server --help`` lists every stub subcommand from the T2 brief."""
    runner = CliRunner()
    result = runner.invoke(app, ["server", "--help"])
    assert result.exit_code == 0, result.output
    for name in EXPECTED_SUBCOMMAND_NAMES:
        assert name in result.output, (
            f"subcommand {name!r} missing from `doc3gpp server --help` output:\n"
            f"{result.output}"
        )


def test_guard_raises_when_disabled() -> None:
    """``_require_server_enabled`` rejects the call when ``[server] enabled = False``."""
    settings = Settings()
    assert settings.server.enabled is False
    with pytest.raises(click.UsageError) as exc_info:
        _require_server_enabled(settings)
    assert "[server] enabled = true" in str(exc_info.value), (
        f"UsageError must mention the TOML flag; got {exc_info.value!r}"
    )


def test_guard_does_not_raise_when_enabled() -> None:
    """``_require_server_enabled`` is a no-op when ``[server] enabled = True``."""
    settings = Settings(server=ServerSettings(enabled=True))
    assert _require_server_enabled(settings) is None


def test_start_blocked_by_disabled_gate(tmp_path, monkeypatch) -> None:
    """Without ``[server] enabled = true``, ``server start`` is rejected by the guard.

    The real ``server start`` reads the ambient config through
    ``get_settings()``, which falls through to the user-wide
    ``~/.config/doc3gpp/config.toml`` when no ``DOC3GPP_CONFIG`` or
    project ``doc3gpp.toml`` is present. Point ``DOC3GPP_CONFIG`` at an
    empty temp file so the resolved settings keep the ``enabled = False``
    default regardless of what the operator's real config contains.
    """
    cfg = tmp_path / "config.toml"
    cfg.write_text("", encoding="utf-8")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    get_settings.cache_clear()
    runner = CliRunner()
    try:
        result = runner.invoke(server_app, ["start"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, click.UsageError)
    assert "[server] enabled = true" in str(result.exception), (
        f"UsageError must mention the TOML flag; got {result.exception!r}"
    )
