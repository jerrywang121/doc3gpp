"""Tests for the ``doc3gpp server`` Typer sub-app scaffold.

The ``server_app`` is added in T2 as a placeholder Typer app with six
stub commands; T11 (start/stop/status/logs) and T10 (install/uninstall)
will replace the stubs with real implementations. Until then the tests
in this module lock in:

* the sub-app is registered under the name ``server`` with the full
  set of six subcommands listed in the T2 brief;
* every stub raises ``NotImplementedError("task N")`` so a premature
  run surfaces a clear "not implemented yet" diagnostic;
* the ``[server] enabled = true`` gate rejects the subcommand before
  the stub body runs when the operator has not opted in.

The brief's literal ``runner.invoke(server_app, [...])`` invocation
pattern is used directly because Typer's :class:`CliRunner` accepts it
once the sub-app has its ``info.name`` populated (which happens during
``app.add_typer`` registration — see :mod:`doc3gpp.cli`).
"""
from __future__ import annotations

from typing import Iterator

import click
import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.cli_server import (
    _require_server_enabled,
    server_app,
)
from doc3gpp.settings.schema import ServerSettings, Settings


EXPECTED_SUBCOMMAND_NAMES = ("start", "stop", "status", "logs", "install", "uninstall")


@pytest.fixture()
def enabled_server_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Patch ``cli_server.get_settings`` so the stub bodies can run past the gate.

    The default :class:`Settings` keeps ``server.enabled = False`` so the
    guard helper short-circuits with ``click.UsageError`` before the
    stub body's ``NotImplementedError`` ever fires. Swapping
    :func:`get_settings` in the ``cli_server`` module for a callable
    that returns an enabled :class:`Settings` lets the stub bodies run,
    which is exactly what ``test_start_stub_raises`` /
    ``test_install_stub_raises`` want to assert against.
    """
    enabled = Settings(server=ServerSettings(enabled=True))

    def _fake_get_settings() -> Settings:
        return enabled

    monkeypatch.setattr("doc3gpp.cli_server.get_settings", _fake_get_settings)
    yield enabled


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


def test_start_stub_raises(enabled_server_settings: Settings) -> None:
    """``server start`` raises ``NotImplementedError`` tagged with the task number."""
    runner = CliRunner()
    result = runner.invoke(server_app, ["start"])
    assert result.exit_code != 0, result.output
    assert result.exception is not None
    assert isinstance(result.exception, NotImplementedError)
    assert "task 11" in str(result.exception), (
        f"start stub must raise NotImplementedError('task 11'); got {result.exception!r}"
    )


def test_install_stub_raises(enabled_server_settings: Settings) -> None:
    """``server install`` raises ``NotImplementedError`` tagged with the task number."""
    runner = CliRunner()
    result = runner.invoke(server_app, ["install", "systemd"])
    assert result.exit_code != 0, result.output
    assert result.exception is not None
    assert isinstance(result.exception, NotImplementedError)
    assert "task 10" in str(result.exception), (
        f"install stub must raise NotImplementedError('task 10'); got {result.exception!r}"
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


def test_start_blocked_by_disabled_gate() -> None:
    """Without ``[server] enabled = true``, ``server start`` is rejected by the guard."""
    runner = CliRunner()
    result = runner.invoke(server_app, ["start"])
    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, click.UsageError)
    assert "[server] enabled = true" in str(result.exception), (
        f"UsageError must mention the TOML flag; got {result.exception!r}"
    )
