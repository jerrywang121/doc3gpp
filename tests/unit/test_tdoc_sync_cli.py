"""Unit tests for the ``tdoc sync`` CLI command.

Covers the test-gap entries around selector validation:
- Both ``--meeting-id`` and ``--meeting`` set ⇒ ``BadParameter``.
- Neither set ⇒ ``BadParameter``.
- ``--meeting`` resolves via ``coordinator.sync_for_meeting_name``.
- ``--meeting-id`` resolves via ``coordinator.sync_for_meeting_id``.
- Typed coordinator errors are converted to ``BadParameter`` with the
  original message preserved.
"""

from __future__ import annotations

from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.services.tdoc_sync_coordinator import (
    MeetingMissingFtpUrlError,
    MeetingNotFoundError,
)


class _FakeCoordinator:
    """In-memory coordinator double that records selector routing."""

    def __init__(self) -> None:
        self.id_calls: list[int] = []
        self.name_calls: list[str] = []
        self.id_raises: Exception | None = None
        self.name_raises: Exception | None = None

    def sync_for_meeting_id(self, meeting_id: int) -> str:
        self.id_calls.append(meeting_id)
        if self.id_raises is not None:
            raise self.id_raises
        return "TDoc sync complete: 7 TDoc row(s) and 0 auxiliary TDoc file(s) stored"

    def sync_for_meeting_name(self, meeting_name: str) -> str:
        self.name_calls.append(meeting_name)
        if self.name_raises is not None:
            raise self.name_raises
        return "TDoc sync complete: 3 TDoc row(s) and 0 auxiliary TDoc file(s) stored"


def _patch_coordinator(monkeypatch, fake: _FakeCoordinator) -> None:
    """Stub ``build_tdoc_sync_coordinator`` to return ``fake``."""
    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_sync_coordinator",
        lambda: fake,  # type: ignore[arg-type]
    )


def test_tdoc_sync_rejects_both_selectors(monkeypatch) -> None:
    runner = CliRunner()
    fake = _FakeCoordinator()
    _patch_coordinator(monkeypatch, fake)

    result = runner.invoke(
        app, ["tdoc", "sync", "--meeting-id", "10", "--meeting", "R5#100"]
    )
    assert result.exit_code != 0
    assert "Specify exactly one of --meeting-id or --meeting." in result.output
    assert fake.id_calls == []
    assert fake.name_calls == []


def test_tdoc_sync_rejects_missing_selectors(monkeypatch) -> None:
    runner = CliRunner()
    fake = _FakeCoordinator()
    _patch_coordinator(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "sync"])
    assert result.exit_code != 0
    assert "Specify exactly one of --meeting-id or --meeting." in result.output


def test_tdoc_sync_routes_meeting_id(monkeypatch) -> None:
    runner = CliRunner()
    fake = _FakeCoordinator()
    _patch_coordinator(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "sync", "--meeting-id", "42"])
    assert result.exit_code == 0, result.output
    assert fake.id_calls == [42]
    assert fake.name_calls == []
    assert "7 TDoc row(s)" in result.output
    assert "0 auxiliary TDoc file(s)" in result.output


def test_tdoc_sync_routes_meeting_name(monkeypatch) -> None:
    runner = CliRunner()
    fake = _FakeCoordinator()
    _patch_coordinator(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "sync", "--meeting", "RAN5#111"])
    assert result.exit_code == 0, result.output
    assert fake.name_calls == ["RAN5#111"]
    assert fake.id_calls == []
    assert "3 TDoc row(s)" in result.output
    assert "0 auxiliary TDoc file(s)" in result.output


def test_tdoc_sync_meeting_not_found_becomes_bad_parameter(monkeypatch) -> None:
    runner = CliRunner()
    fake = _FakeCoordinator()
    fake.name_raises = MeetingNotFoundError("Meeting not found with name nope")
    _patch_coordinator(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "sync", "--meeting", "nope"])
    assert result.exit_code != 0
    assert "Meeting not found" in result.output


def test_tdoc_sync_missing_ftp_url_becomes_bad_parameter(monkeypatch) -> None:
    runner = CliRunner()
    fake = _FakeCoordinator()
    fake.id_raises = MeetingMissingFtpUrlError(
        "Meeting 10 (R5-100) has no FTP URL stored"
    )
    _patch_coordinator(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "sync", "--meeting-id", "10"])
    assert result.exit_code != 0
    assert "no FTP URL stored" in result.output
