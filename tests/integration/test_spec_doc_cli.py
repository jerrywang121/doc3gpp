"""CLI tests for the `spec doc` sub-app (Task 11)."""
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.storage.db.migrate import create_schema


def test_spec_doc_schema(sqlite_env):
    create_schema("all")
    r = CliRunner().invoke(app, ["spec", "doc", "schema", "--format", "json"])
    assert r.exit_code == 0 and "spec_doc_chunks" in r.output


def test_spec_doc_toc_miss(sqlite_env):
    create_schema("all")
    r = CliRunner().invoke(
        app, ["spec", "doc", "toc", "show", "--spec", "38.331", "--version", "18.5.0"]
    )
    assert r.exit_code != 0 and "spec doc parse" in r.output


def test_spec_doc_fetch_command_is_removed(sqlite_env):
    create_schema("all")
    runner = CliRunner()

    help_result = runner.invoke(app, ["spec", "doc", "--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "fetch" not in help_result.output

    fetch_result = runner.invoke(
        app,
        ["spec", "doc", "fetch", "--spec", "38.331"],
    )
    assert fetch_result.exit_code != 0
    assert "No such command" in fetch_result.output
    assert "fetch" in fetch_result.output


def test_spec_doc_parse_forwards_force(monkeypatch):
    calls = []

    class FakeResult:
        successes = {}
        skipped = {}
        failures = {}

    class FakeService:
        def parse_many(self, spec_ids, *, release, version, force):
            calls.append((spec_ids, release, version, force))
            return FakeResult()

    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda _scope: None)
    monkeypatch.setattr("doc3gpp.cli.build_spec_doc_service", lambda: FakeService())

    result = CliRunner().invoke(
        app,
        ["spec", "doc", "parse", "--spec", "38.331", "--force"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [(["38.331"], None, None, True)]
