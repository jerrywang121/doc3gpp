from __future__ import annotations

import re

from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.sync import SyncOutcome
from doc3gpp.models.tsg import Tsg
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.tsg_service import TsgService


_KNOWN_SHORT = ["C1", "C3", "C4", "C6", "CP", "R1", "R2", "R3", "R4", "R5",
                "RP", "RT", "S1", "S2", "S3", "S4", "S5", "S6", "SP"]


class _StaticRepo:
    """In-memory TsgRepository double used by the CLI sync validation tests."""

    def __init__(self, count: int) -> None:
        self._count = count

    def count(self) -> int:
        return self._count

    def get_by_short_name(self, short_name: str) -> Tsg | None:
        if short_name.upper() in _KNOWN_SHORT:
            return Tsg(
                tsg_name="x",
                short_name=short_name.upper(),
                description="x",
                url=None,
            )
        return None

    def list_all(self) -> list[Tsg]:
        return [
            Tsg(tsg_name="x", short_name=s, description="x", url=None)
            for s in _KNOWN_SHORT
        ]

    def upsert_many(self, tsgs):
        self._count = len(tsgs)
        return len(tsgs)


def test_meeting_sync_accepts_known_short_name(monkeypatch) -> None:
    runner = CliRunner()

    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda: None)
    monkeypatch.setattr(
        "doc3gpp.cli.build_tsg_service", lambda: TsgService(_StaticRepo(19))
    )

    sync_called_with: list[dict] = []

    def fake_sync(self, url, tsg=None, force=False):
        sync_called_with.append({"url": url, "tsg": tsg, "force": force})
        return SyncOutcome(status="synced", reason="ok", synced_count=0)

    monkeypatch.setattr(MeetingService, "sync", fake_sync)

    result = runner.invoke(app, ["meeting", "sync", "--tsg", "r5"])
    assert result.exit_code == 0, result.output
    assert "R5" in sync_called_with[0]["url"]
    # CLI must hand the canonical (uppercase) short name to the service for the meetings.tsg FK
    assert sync_called_with[0]["tsg"] == "R5"


def test_meeting_sync_rejects_unknown_short_name(monkeypatch) -> None:
    runner = CliRunner()

    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda: None)
    monkeypatch.setattr(
        "doc3gpp.cli.build_tsg_service", lambda: TsgService(_StaticRepo(19))
    )

    result = runner.invoke(app, ["meeting", "sync", "--tsg", "r99"])
    assert result.exit_code != 0
    assert "Unknown TSG short name 'r99'" in result.output
    # Typer may wrap the error message at the terminal width; allow the wrap
    # (a line break plus box-drawing `│`) between "doc3gpp" and "tsg list".
    assert re.search(r"Run 'doc3gpp[\s│]+tsg list'", result.output) is not None


def test_meeting_sync_uppercases_canonical_form(monkeypatch) -> None:
    runner = CliRunner()

    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda: None)
    monkeypatch.setattr(
        "doc3gpp.cli.build_tsg_service", lambda: TsgService(_StaticRepo(19))
    )

    captured: list[dict] = []

    def fake_sync(self, url, tsg=None, force=False):
        captured.append({"url": url, "tsg": tsg, "force": force})
        return SyncOutcome(status="synced", reason="ok", synced_count=0)

    monkeypatch.setattr(MeetingService, "sync", fake_sync)

    result = runner.invoke(app, ["meeting", "sync", "--tsg", "s2"])
    assert result.exit_code == 0, result.output
    assert "Meetings-S2.htm" in captured[0]["url"]
    assert captured[0]["tsg"] == "S2"


def test_meeting_sync_force_flag_is_forwarded(monkeypatch) -> None:
    runner = CliRunner()

    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda: None)
    monkeypatch.setattr(
        "doc3gpp.cli.build_tsg_service", lambda: TsgService(_StaticRepo(19))
    )

    captured: list[dict] = []

    def fake_sync(self, url, tsg=None, force=False):
        captured.append({"url": url, "tsg": tsg, "force": force})
        return SyncOutcome(status="synced", reason="ok", synced_count=0)

    monkeypatch.setattr(MeetingService, "sync", fake_sync)

    result = runner.invoke(app, ["meeting", "sync", "--tsg", "r5", "--force"])
    assert result.exit_code == 0, result.output
    assert captured[0]["force"] is True


def test_meeting_sync_auto_seeds_when_table_empty(monkeypatch) -> None:
    """A fresh database should auto-seed and still validate successfully."""
    runner = CliRunner()

    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda: None)
    monkeypatch.setattr(
        "doc3gpp.cli.build_tsg_service", lambda: TsgService(_StaticRepo(0))
    )

    seed_calls = {"count": 0}
    monkeypatch.setattr(
        TsgService,
        "seed_defaults",
        lambda self: (seed_calls.update(count=seed_calls["count"] + 1) or 19),
    )

    def fake_sync(self, url, tsg=None, force=False):
        return SyncOutcome(status="synced", reason="ok", synced_count=0)

    monkeypatch.setattr(MeetingService, "sync", fake_sync)

    result = runner.invoke(app, ["meeting", "sync", "--tsg", "r1"])
    assert result.exit_code == 0, result.output
    assert seed_calls["count"] == 1


def test_meeting_sync_without_tsg_syncs_all_stored_tsgs(monkeypatch) -> None:
    """When --tsg is omitted, sync every distinct TSG found in meetings."""
    runner = CliRunner()

    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda: None)
    monkeypatch.setattr(
        "doc3gpp.cli.build_tsg_service", lambda: TsgService(_StaticRepo(19))
    )

    sync_called_with: list[dict] = []

    def fake_sync(self, url, tsg=None, force=False):
        sync_called_with.append({"url": url, "tsg": tsg, "force": force})
        return SyncOutcome(status="synced", reason=f"ok {tsg}", synced_count=0)

    monkeypatch.setattr(MeetingService, "sync", fake_sync)
    monkeypatch.setattr(MeetingService, "list_distinct_tsgs", lambda self: ["R5", "S2"])

    result = runner.invoke(app, ["meeting", "sync"])
    assert result.exit_code == 0, result.output
    assert len(sync_called_with) == 2
    assert {c["tsg"] for c in sync_called_with} == {"R5", "S2"}
    assert all("Meetings-" in c["url"] for c in sync_called_with)


def test_meeting_sync_without_tsg_reports_nothing_when_no_stored_tsgs(monkeypatch) -> None:
    """When --tsg is omitted and meetings is empty, report no work."""
    runner = CliRunner()

    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda: None)
    monkeypatch.setattr(
        "doc3gpp.cli.build_tsg_service", lambda: TsgService(_StaticRepo(19))
    )

    sync_called_with: list[dict] = []

    def fake_sync(self, url, tsg=None, force=False):
        sync_called_with.append({"url": url, "tsg": tsg, "force": force})
        return SyncOutcome(status="synced", reason="ok", synced_count=0)

    monkeypatch.setattr(MeetingService, "sync", fake_sync)
    monkeypatch.setattr(MeetingService, "list_distinct_tsgs", lambda self: [])

    result = runner.invoke(app, ["meeting", "sync"])
    assert result.exit_code == 0, result.output
    assert sync_called_with == []
    assert "No stored meetings with a TSG found" in result.output


def test_meeting_sync_without_tsg_skips_unknown_stored_tsgs(monkeypatch) -> None:
    """Discovered TSGs that are not in the reference table are skipped."""
    runner = CliRunner()

    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda: None)
    monkeypatch.setattr(
        "doc3gpp.cli.build_tsg_service", lambda: TsgService(_StaticRepo(19))
    )

    sync_called_with: list[dict] = []

    def fake_sync(self, url, tsg=None, force=False):
        sync_called_with.append({"url": url, "tsg": tsg, "force": force})
        return SyncOutcome(status="synced", reason=f"ok {tsg}", synced_count=0)

    monkeypatch.setattr(MeetingService, "sync", fake_sync)
    monkeypatch.setattr(MeetingService, "list_distinct_tsgs", lambda self: ["R5", "R99"])

    result = runner.invoke(app, ["meeting", "sync"])
    assert result.exit_code == 0, result.output
    assert len(sync_called_with) == 1
    assert sync_called_with[0]["tsg"] == "R5"
    assert "Skipping unknown TSG 'R99'" in result.output
