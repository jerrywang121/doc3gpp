"""Integration tests for ``doc3gpp spec`` CLI commands.

Uses a stubbed :class:`SpecService` injected via monkeypatch on
``doc3gpp.cli.build_spec_service`` so the CLI commands can be
exercised without any network or schema bootstrap.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.spec import Spec, SpecVersion
from doc3gpp.models.sync import SyncOutcome

runner = CliRunner()


def test_spec_sync_help() -> None:
    result = runner.invoke(app, ["spec", "sync", "--help"])
    assert result.exit_code == 0
    assert "--tsg" in result.stdout
    assert "--force" in result.stdout


def test_spec_list(monkeypatch) -> None:
    svc = MagicMock()
    svc.list_recent.return_value = [
        Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5")
    ]
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)
    result = runner.invoke(app, ["spec", "list", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    assert "36.579-5" in result.stdout


def test_spec_sync(sqlite_env, monkeypatch) -> None:
    svc = MagicMock()
    svc.sync.return_value = SyncOutcome(
        status="synced",
        reason="Spec sync complete for TSG R5: 3 specs, 5 versions stored",
        synced_count=3,
        version_count=5,
    )
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)
    result = runner.invoke(app, ["spec", "sync", "--tsg", "R5", "--force"])
    assert result.exit_code == 0, result.stdout
    assert "Spec sync complete" in result.stdout


def test_spec_show_json(monkeypatch) -> None:
    spec = Spec(
        spec_id="36.579-5",
        type="TS",
        title="NR conformance",
        status="published",
        radio_tech="5G",
        initial_release="Rel-15",
        tsg="R5",
        wis="eNB",
    )
    version = SpecVersion(
        spec_id="36.579-5",
        version="18.3.0",
        ftp_url="https://www.3gpp.org/ftp/Specs/archive/36_series/36.579-5.zip",
        release="Rel-18",
        meeting_id=108,
        meeting_name="RAN#108",
        crs="R5-260013,R5-260014",
        comment="-",
    )
    svc = MagicMock()
    svc.get.return_value = spec
    svc.list_versions.return_value = [version]
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)

    result = runner.invoke(app, ["spec", "show", "36.579-5", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    assert "36.579-5" in result.stdout
    assert "18.3.0" in result.stdout


def test_spec_show_table(monkeypatch) -> None:
    spec = Spec(
        spec_id="36.579-5",
        type="TS",
        title="NR conformance",
        tsg="R5",
    )
    version = SpecVersion(
        spec_id="36.579-5",
        version="18.3.0",
        ftp_url="https://www.3gpp.org/ftp/Specs/archive/36_series/36.579-5.zip",
        release="Rel-18",
    )
    svc = MagicMock()
    svc.get.return_value = spec
    svc.list_versions.return_value = [version]
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)

    result = runner.invoke(app, ["spec", "show", "36.579-5"])
    assert result.exit_code == 0, result.stdout
    assert "36.579-5" in result.stdout
    assert "18.3.0" in result.stdout


def test_spec_show_unknown(monkeypatch) -> None:
    svc = MagicMock()
    svc.get.return_value = None
    svc.list_versions.return_value = []
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)

    result = runner.invoke(app, ["spec", "show", "99.999-9"])
    assert result.exit_code != 0
    assert "Unknown spec id" in (result.output + result.stdout)
