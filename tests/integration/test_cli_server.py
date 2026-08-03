"""Integration tests for the ``doc3gpp server`` CLI commands (T11).

These tests exercise the real command bodies (start/stop/status/logs/
install/uninstall) against a sqlite database and a tempfile cache dir,
monkeypatching only the OS-facing seams (``subprocess.Popen``,
``os.kill``, ``urllib.request.urlopen``, the systemd/launchd runner) so
nothing touches the real OS service manager or spawns a live server.
"""
from __future__ import annotations

import sys

import click
import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.cli_server import _require_server_enabled
from doc3gpp.settings.schema import CacheSettings, ServerSettings, Settings

runner = CliRunner()


def _invoke(args: list[str]):
    return runner.invoke(app, ["server", *args])


@pytest.fixture()
def cli_server(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Patch ``cli_server.get_settings`` to a server-enabled temp-cache Settings."""
    settings = Settings(
        cache=CacheSettings(dir=tmp_path / "cache"),
        server=ServerSettings(enabled=True),
    )
    monkeypatch.setattr("doc3gpp.cli_server.get_settings", lambda: settings)
    return settings


def test_install_user_dry_run_prints_unit_only(cli_server: Settings) -> None:
    result = _invoke(["install", "systemd", "--user", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "X-Doc3gpp-Managed=true" in result.output


def test_start_refuses_when_disabled() -> None:
    settings = Settings(server=ServerSettings(enabled=False))
    with pytest.raises(click.UsageError):
        _require_server_enabled(settings)


def test_status_reports_not_running_when_pidfile_missing(cli_server: Settings) -> None:
    result = _invoke(["status"])
    assert result.exit_code == 0, result.output
    assert "not-installed" in result.output


def test_status_reports_running_when_pidfile_alive(cli_server: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_path = cli_server.cache.dir / "server.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("999999\n", encoding="utf-8")

    import doc3gpp.cli_server as cli_server_module

    monkeypatch.setattr(cli_server_module.os, "kill", lambda pid, sig: None)
    result = _invoke(["status"])
    assert result.exit_code == 0, result.output
    assert "running" in result.output
    assert "999999" in result.output


def test_start_background_writes_pid_and_launches(cli_server: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    import doc3gpp.cli_server as cli_server_module

    captured: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        captured.append(cmd)
        return _FakePopen(pid=4242)

    monkeypatch.setattr(cli_server_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli_server_module, "_wait_healthy", lambda url: True)
    monkeypatch.setattr(cli_server_module, "_open_browser", lambda url: None)

    result = _invoke(["start", "--port", "8765", "--no-open"])
    assert result.exit_code == 0, result.output
    assert "server running" in result.output
    assert (cli_server.cache.dir / "server.pid").exists()
    assert any("doc3gpp.web.app:build_app" in cmd for cmd in captured)


def test_start_foreground_reload(cli_server: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict = {}

    def fake_run(app, **kwargs):
        called.update(kwargs)
        called["app"] = app

    fake_uvicorn = type("FakeUvicorn", (), {"run": staticmethod(fake_run)})()
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    result = _invoke(["start", "--reload"])
    assert result.exit_code == 0, result.output
    assert called.get("reload") is True


def test_logs_job_id(cli_server: Settings, sqlite_env, monkeypatch: pytest.MonkeyPatch) -> None:
    from doc3gpp.models.jobs import JobKind
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_session_factory
    from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository

    create_schema()
    repo = SQLAlchemyJobRepository(get_session_factory())
    job = repo.create(kind=JobKind.SYNC_TDOCS, params={"a": 1})
    repo.mark_running(job.id, message="hello")
    repo.append_log(job.id, line="world")
    repo.mark_succeeded(job.id, summary={"ok": True})

    result = _invoke(["logs", "--job", job.id])
    assert result.exit_code == 0, result.output
    assert "hello" in result.output
    assert "world" in result.output


class _FakePopen:
    def __init__(self, pid: int) -> None:
        self.pid = pid
