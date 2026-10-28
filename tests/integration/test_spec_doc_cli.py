"""CLI tests for the `spec doc` sub-app (Task 11)."""
from typing import ClassVar

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
        successes: ClassVar[dict] = {}
        skipped: ClassVar[dict] = {}
        failures: ClassVar[dict] = {}

    class FakeService:
        def parse_many(self, spec_ids, *, release, version, force):
            calls.append((spec_ids, release, version, force))
            return FakeResult()

    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda _scope: None)
    monkeypatch.setattr("doc3gpp.cli.build_spec_doc_service", lambda **_kwargs: FakeService())

    result = CliRunner().invoke(
        app,
        ["spec", "doc", "parse", "--spec", "38.331", "--force"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [(["38.331"], None, None, True)]


def test_spec_doc_parse_rejects_repeated_spec(monkeypatch):
    calls = []

    class FakeResult:
        successes: ClassVar[dict] = {}
        skipped: ClassVar[dict] = {}
        failures: ClassVar[dict] = {}

    class FakeService:
        def parse_many(self, spec_ids, *, release, version, force):
            calls.append((spec_ids, release, version, force))
            return FakeResult()

    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda _scope: None)
    monkeypatch.setattr("doc3gpp.cli.build_spec_doc_service", lambda **_kwargs: FakeService())

    result = CliRunner().invoke(
        app,
        ["spec", "doc", "parse", "--spec", "38.331", "--spec", "38.523-1"],
    )

    assert result.exit_code != 0, result.output
    assert calls == []


def test_spec_doc_parse_rejects_misaligned_release_and_version(monkeypatch):
    calls = []

    class FakeResult:
        successes: ClassVar[dict] = {}
        skipped: ClassVar[dict] = {}
        failures: ClassVar[dict] = {}

    class FakeService:
        def parse_many(self, spec_ids, *, release, version, force):
            calls.append((spec_ids, release, version, force))
            return FakeResult()

    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda _scope: None)
    monkeypatch.setattr("doc3gpp.cli.build_spec_doc_service", lambda **_kwargs: FakeService())

    result = CliRunner().invoke(
        app,
        [
            "spec",
            "doc",
            "parse",
            "--spec",
            "38.331",
            "--release",
            "Rel-18",
            "--version",
            "17.5.0",
        ],
    )

    assert result.exit_code != 0, result.output
    assert "does not match" in result.output
    assert calls == []


def _install_recording_docx_pool(monkeypatch, events, entered):
    class RecordingPool:
        def __init__(self, workers):
            events.append(("init", workers))

        def __enter__(self):
            events.append("enter")
            entered.append(self)
            return self

        def __exit__(self, *exc_info):
            events.append("exit")

    monkeypatch.setattr("doc3gpp.cli.DocxConvertPool", RecordingPool)


def test_spec_doc_parse_passes_docx_pool_and_closes_it(monkeypatch):
    from doc3gpp.config import get_settings

    events = []
    entered = []
    seen = {}

    class FakeResult:
        successes: ClassVar[dict] = {}
        skipped: ClassVar[dict] = {}
        failures: ClassVar[dict] = {}

    class FakeService:
        def parse_many(self, spec_ids, *, release, version, force):
            events.append("parse")
            return FakeResult()

    def build_spec_doc_service(*, docx_pool):
        seen["pool"] = docx_pool
        events.append("build")
        return FakeService()

    _install_recording_docx_pool(monkeypatch, events, entered)
    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda _scope: None)
    monkeypatch.setattr("doc3gpp.cli.build_spec_doc_service", build_spec_doc_service)

    result = CliRunner().invoke(app, ["spec", "doc", "parse", "--spec", "38.331"])

    assert result.exit_code == 0, result.output
    assert events == [
        ("init", get_settings().pipeline.docx_workers),
        "enter",
        "build",
        "parse",
        "exit",
    ]
    assert seen["pool"] is entered[0]


def test_spec_doc_parse_closes_docx_pool_when_parse_raises(monkeypatch):
    events = []

    class FakeService:
        def parse_many(self, spec_ids, *, release, version, force):
            events.append("parse")
            raise RuntimeError("boom")

    _install_recording_docx_pool(monkeypatch, events, [])
    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda _scope: None)
    monkeypatch.setattr(
        "doc3gpp.cli.build_spec_doc_service", lambda **_kwargs: FakeService()
    )

    result = CliRunner().invoke(app, ["spec", "doc", "parse", "--spec", "38.331"])

    assert result.exit_code != 0
    assert events[-2:] == ["parse", "exit"]
