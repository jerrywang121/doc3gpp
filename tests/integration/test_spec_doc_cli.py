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
