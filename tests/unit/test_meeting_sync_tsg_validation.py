from __future__ import annotations

from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.tsg import Tsg
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.tsg_service import TsgService


_KNOWN_SHORT = ["C1", "C3", "C4", "C6", "R1", "R2", "R3", "R4", "R5", "RT",
                "S1", "S2", "S3", "S4", "S5", "S6"]


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
        "doc3gpp.cli.build_tsg_service", lambda: TsgService(_StaticRepo(16))
    )

    sync_called_with: list[str] = []

    def fake_sync(self, url, max_year_closed=2, max_year_future=1):
        sync_called_with.append(url)
        return 0

    monkeypatch.setattr(MeetingService, "sync", fake_sync)

    result = runner.invoke(app, ["meeting", "sync", "--tsg", "r5"])
    assert result.exit_code == 0, result.output
    assert "R5" in sync_called_with[0]


def test_meeting_sync_rejects_unknown_short_name(monkeypatch) -> None:
    runner = CliRunner()

    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda: None)
    monkeypatch.setattr(
        "doc3gpp.cli.build_tsg_service", lambda: TsgService(_StaticRepo(16))
    )

    result = runner.invoke(app, ["meeting", "sync", "--tsg", "r99"])
    assert result.exit_code != 0
    assert "Unknown TSG short name 'r99'" in result.output
    assert "Run 'doc3gpp tsg list'" in result.output


def test_meeting_sync_uppercases_canonical_form(monkeypatch) -> None:
    runner = CliRunner()

    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda: None)
    monkeypatch.setattr(
        "doc3gpp.cli.build_tsg_service", lambda: TsgService(_StaticRepo(16))
    )

    captured: list[str] = []

    def fake_sync(self, url, max_year_closed=2, max_year_future=1):
        captured.append(url)
        return 0

    monkeypatch.setattr(MeetingService, "sync", fake_sync)

    result = runner.invoke(app, ["meeting", "sync", "--tsg", "s2"])
    assert result.exit_code == 0, result.output
    assert "Meetings-S2.htm" in captured[0]


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
        lambda self: (seed_calls.update(count=seed_calls["count"] + 1) or 16),
    )

    def fake_sync(self, url, max_year_closed=2, max_year_future=1):
        return 0

    monkeypatch.setattr(MeetingService, "sync", fake_sync)

    result = runner.invoke(app, ["meeting", "sync", "--tsg", "r1"])
    assert result.exit_code == 0, result.output
    assert seed_calls["count"] == 1
