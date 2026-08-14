"""Unit tests for the ``doc3gpp --version`` flag."""
from __future__ import annotations

from typer.testing import CliRunner

from doc3gpp import __version__
from doc3gpp.cli import app


runner = CliRunner()


def test_version_flag_prints_version_and_exits_zero() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout == f"doc3gpp {__version__}\n"


def test_version_flag_ignores_trailing_args() -> None:
    """``doc3gpp --version meeting list`` still prints the version and exits 0."""
    result = runner.invoke(app, ["--version", "meeting", "list"])
    assert result.exit_code == 0
    assert result.stdout == f"doc3gpp {__version__}\n"


def test_help_text_mentions_version_flag() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--version" in result.stdout


def test_root_no_args_still_prints_help() -> None:
    """``doc3gpp`` with no args must keep its current help behaviour (regression guard)."""
    result = runner.invoke(app, [])
    # Typer's bare-app behaviour: exit 0 with --help-like output, or exit 2 with usage.
    # We don't pin the exit code tightly; we just confirm the new flag did not
    # accidentally register as required and break the no-args invocation.
    assert "--version" in result.stdout or "Usage" in result.stdout
