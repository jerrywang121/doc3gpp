"""CLI flag-parsing tests for the ``search`` sub-app."""

from __future__ import annotations

from typer.testing import CliRunner

from doc3gpp.cli import app


def test_search_help_lists_filters() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["search", "search", "--help"])
    assert result.exit_code == 0
    for flag in (
        "--tsg", "--meeting", "--meeting-id", "--tdoc-id",
        "--release", "--spec", "--since", "--until",
        "--limit", "--format", "--compact", "--rerank",
        "--snippet-tokens", "--explain", "--quiet",
    ):
        assert flag in result.output, f"missing flag {flag} in search help"


def test_index_help_lists_rebuild_flags() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["search", "index", "--help"])
    assert result.exit_code == 0
    for flag in ("--rebuild", "--batch", "--resume", "--stale-only", "--quiet"):
        assert flag in result.output, f"missing flag {flag} in index help"
