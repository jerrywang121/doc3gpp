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
from doc3gpp.models.sync import BulkSyncFailure
from doc3gpp.models.sync import BulkSyncOutcome
from doc3gpp.models.sync import SyncOutcome
from doc3gpp.services.tdoc_sync_coordinator import (
    MeetingMissingFtpUrlError,
    MeetingNotFoundError,
)


class _FakeCoordinator:
    """In-memory coordinator double that records selector routing."""

    def __init__(self) -> None:
        self.id_calls: list[tuple[int, bool]] = []
        self.name_calls: list[tuple[str, bool]] = []
        self.bulk_calls: list[bool] = []
        self.id_raises: Exception | None = None
        self.name_raises: Exception | None = None
        self.bulk_outcome: BulkSyncOutcome = BulkSyncOutcome()

    def sync_for_meeting_id(self, meeting_id: int, force: bool = False) -> SyncOutcome:
        self.id_calls.append((meeting_id, force))
        if self.id_raises is not None:
            raise self.id_raises
        return SyncOutcome(
            status="synced",
            reason="TDoc sync complete: 7 TDoc row(s) and 0 auxiliary TDoc file(s) stored",
            synced_count=7,
            file_count=0,
        )

    def sync_for_meeting_name(self, meeting_name: str, force: bool = False) -> SyncOutcome:
        self.name_calls.append((meeting_name, force))
        if self.name_raises is not None:
            raise self.name_raises
        return SyncOutcome(
            status="synced",
            reason="TDoc sync complete: 3 TDoc row(s) and 0 auxiliary TDoc file(s) stored",
            synced_count=3,
            file_count=0,
        )

    def sync_all_tracked_meetings(self, force: bool = False) -> BulkSyncOutcome:
        self.bulk_calls.append(force)
        return self.bulk_outcome


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


def test_tdoc_sync_routes_bulk_when_no_selector(monkeypatch) -> None:
    runner = CliRunner()
    fake = _FakeCoordinator()
    fake.bulk_outcome = BulkSyncOutcome(
        outcomes=(
            SyncOutcome(status="synced", reason="synced 1"),
            SyncOutcome(status="skipped", reason="skipped 1"),
        ),
    )
    _patch_coordinator(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "sync"])
    assert result.exit_code == 0, result.output
    assert fake.bulk_calls == [False]
    assert fake.id_calls == []
    assert fake.name_calls == []
    assert "TDoc bulk sync: 2 meeting(s) processed" in result.output
    assert "  Synced:  1" in result.output
    assert "  Skipped: 1" in result.output
    assert "  Failed:  0" in result.output


def test_tdoc_sync_bulk_prints_summary_with_failures(monkeypatch) -> None:
    runner = CliRunner()
    fake = _FakeCoordinator()
    fake.bulk_outcome = BulkSyncOutcome(
        outcomes=(
            SyncOutcome(status="synced", reason="synced 1"),
            SyncOutcome(status="skipped", reason="skipped 1"),
        ),
        failures=(
            BulkSyncFailure(
                meeting_id=42,
                error="MeetingNotFoundError",
                reason="Meeting not found with id 42",
            ),
        ),
    )
    _patch_coordinator(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "sync"])
    assert result.exit_code == 0, result.output
    assert "TDoc bulk sync: 3 meeting(s) processed" in result.output
    assert "  Synced:  1" in result.output
    assert "  Skipped: 1" in result.output
    assert "  Failed:  1" in result.output
    assert "Failed meetings:" in result.output
    assert "meeting_id=42" in result.output


def test_tdoc_sync_bulk_with_empty_discovery(monkeypatch) -> None:
    runner = CliRunner()
    fake = _FakeCoordinator()
    _patch_coordinator(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "sync"])
    assert result.exit_code == 0, result.output
    assert "No stored meetings with TDocs found" in result.output


def test_tdoc_sync_bulk_all_failed_exits_nonzero(monkeypatch) -> None:
    runner = CliRunner()
    fake = _FakeCoordinator()
    fake.bulk_outcome = BulkSyncOutcome(
        failures=(
            BulkSyncFailure(
                meeting_id=1,
                error="MeetingMissingFtpUrlError",
                reason="no ftp",
            ),
        ),
    )
    _patch_coordinator(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "sync"])
    assert result.exit_code == 1, result.output
    assert "  Failed:  1" in result.output


def test_tdoc_sync_bulk_force_flag_is_forwarded(monkeypatch) -> None:
    runner = CliRunner()
    fake = _FakeCoordinator()
    _patch_coordinator(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "sync", "--force"])
    assert result.exit_code == 0, result.output
    assert fake.bulk_calls == [True]


def test_tdoc_sync_routes_meeting_id(monkeypatch) -> None:
    runner = CliRunner()
    fake = _FakeCoordinator()
    _patch_coordinator(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "sync", "--meeting-id", "42"])
    assert result.exit_code == 0, result.output
    assert fake.id_calls == [(42, False)]
    assert fake.name_calls == []
    assert "7 TDoc row(s)" in result.output
    assert "0 auxiliary TDoc file(s)" in result.output


def test_tdoc_sync_routes_meeting_name(monkeypatch) -> None:
    runner = CliRunner()
    fake = _FakeCoordinator()
    _patch_coordinator(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "sync", "--meeting", "RAN5#111"])
    assert result.exit_code == 0, result.output
    assert fake.name_calls == [("RAN5#111", False)]
    assert fake.id_calls == []
    assert "3 TDoc row(s)" in result.output
    assert "0 auxiliary TDoc file(s)" in result.output


def test_tdoc_sync_force_flag_is_forwarded(monkeypatch) -> None:
    runner = CliRunner()
    fake = _FakeCoordinator()
    _patch_coordinator(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "sync", "--meeting-id", "42", "--force"])
    assert result.exit_code == 0, result.output
    assert fake.id_calls == [(42, True)]


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
