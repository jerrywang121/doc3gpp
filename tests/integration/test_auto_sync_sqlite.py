"""Integration tests for the auto-sync behaviour of read commands.

These tests exercise the full CLI path with local fixtures standing in for
3GPP network responses. They verify that:
- auto-sync is enabled by default,
- enabling ``sync.auto_sync`` causes ``meeting list``, ``tdoc list`` and
  ``tdoc show`` to internally trigger syncs,
- DB-mode ``tdoc parse`` triggers auto-sync,
- direct-mode ``tdoc parse --from-path`` triggers auto-sync for the
  filename's TDoc id (Blocker-7 ruling; previously it did not).
"""

from __future__ import annotations

from datetime import date
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.config import get_settings
from doc3gpp.models.meeting import Meeting
from doc3gpp.models.tdoc import TDoc
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository


@pytest.fixture(autouse=True)
def _disable_auto_sync_by_default(monkeypatch):
    """Force ``sync.auto_sync`` off for every test in this file unless the
    test explicitly opts in via :func:`_enable_auto_sync`.

    A user-level ``~/.config/doc3gpp/config.toml`` with ``auto_sync = "true"``
    would otherwise leak into these tests (the loader merges the TOML with
    env vars), causing the "default off" test to spuriously hit the network
    and the auto-sync-on tests to behave as if their opt-in was a no-op.
    """
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _future_date(days: int = 7) -> date:
    """Return a date far enough in the future to avoid the closed window."""
    return date.today() + timedelta(days=days)


def _enable_auto_sync(monkeypatch) -> None:
    """Enable the sync.auto_sync setting and clear the settings cache."""
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "true")
    get_settings.cache_clear()


def _load_meeting_fixture() -> str:
    fn = Path(__file__).parent.parent / "fixtures" / "sample_pages" / "3GPP-meeting-R5.html"
    return fn.read_text(encoding="utf-8")


def _load_tdoc_xlsx_fixture() -> bytes:
    fn = (
        Path(__file__).parent.parent
        / "fixtures"
        / "tdoc_xlsx"
        / "TDoc_List_Meeting_RAN5#111.xlsx"
    )
    return fn.read_bytes()


def _patch_scraper_client(monkeypatch) -> None:
    """Replace ScraperClient with a fixture-backed implementation."""
    html_text = _load_meeting_fixture()
    xlsx_bytes = _load_tdoc_xlsx_fixture()

    class DummyClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def get_text(self, url: str) -> str:
            if "Meetings-" in url or "dynareport" in url:
                return html_text
            if "3gpp.org/ftp/" in url:
                return '<a href="TDoc_List_Meeting_RAN5#111.xlsx">list</a>'
            return ""

        def get_bytes(self, url: str) -> bytes:
            return xlsx_bytes

        def close(self) -> None:
            return None

    monkeypatch.setattr("doc3gpp.scraping.client.ScraperClient", DummyClient)
    monkeypatch.setattr("doc3gpp.scraping.portal_source.ScraperClient", DummyClient)


def test_meeting_list_default_does_not_auto_sync(sqlite_env, monkeypatch) -> None:
    """With auto_sync disabled (default), meeting list must not hit the network."""
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0

    # No ScraperClient patch installed; any network access would fail.
    result = runner.invoke(app, ["meeting", "list", "--tsg", "r5"])
    assert result.exit_code == 0
    assert "[auto-sync]" not in result.stdout


def test_meeting_list_auto_sync_triggers_meeting_sync(
    monkeypatch, sqlite_env
) -> None:
    """With auto_sync enabled, meeting list --tsg fetches and stores meetings."""
    _patch_scraper_client(monkeypatch)
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0
    _enable_auto_sync(monkeypatch)

    result = runner.invoke(app, ["meeting", "list", "--tsg", "r5"])
    assert result.exit_code == 0
    assert "[auto-sync] Meeting sync complete:" in result.stdout

    # Verify rows were actually persisted.
    meeting_repo = SQLAlchemyMeetingRepository()
    meetings = meeting_repo.list(limit=5, tsg="R5")
    assert len(meetings) > 0


def test_tdoc_list_auto_sync_triggers_tdoc_sync(
    monkeypatch, sqlite_env
) -> None:
    """With auto_sync enabled, tdoc list --meeting-id syncs the meeting's TDocs."""
    _patch_scraper_client(monkeypatch)
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0

    # Seed a meeting row manually with the FTP URL shape the fixture expects.
    meeting_repo = SQLAlchemyMeetingRepository()
    meeting_repo.upsert_many(
        [
            Meeting(
                meeting_id=111,
                name="RAN5#111",
                title="RAN5 meeting 111",
                location="Online",
                start_date=date(2026, 1, 1),
                end_date=_future_date(),
                ftp_url="https://www.3gpp.org/ftp/tsg_ran/TSG_RAN/TSGR_111/Docs/",
                tsg="R5",
            ),
        ]
    )

    _enable_auto_sync(monkeypatch)

    result = runner.invoke(
        app,
        ["tdoc", "list", "--meeting-id", "111", "--limit", "10"],
    )
    assert result.exit_code == 0
    assert "[auto-sync] Meeting sync complete:" in result.stdout
    assert "[auto-sync] TDoc sync complete:" in result.stdout


def test_tdoc_show_auto_sync_triggers_sync(
    monkeypatch, sqlite_env
) -> None:
    """With auto_sync enabled, tdoc show fires the same auto-sync as tdoc list."""
    _patch_scraper_client(monkeypatch)
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0

    meeting_repo = SQLAlchemyMeetingRepository()
    meeting_repo.upsert_many(
        [
            Meeting(
                meeting_id=111,
                name="RAN5#111",
                title="RAN5 meeting 111",
                location="Online",
                start_date=date(2026, 1, 1),
                end_date=_future_date(),
                ftp_url="https://www.3gpp.org/ftp/tsg_ran/TSG_RAN/TSGR_111/Docs/",
                tsg="R5",
            ),
        ]
    )

    # Seed the TDoc row so `tdoc show` can display it after auto-sync.
    tdoc_repo = SQLAlchemyTDocRepository()
    tdoc_repo.upsert(
        TDoc(
            tdoc_id="R5s260009",
            title="Example",
            meeting_id=111,
            ftp_url="x/1",
            type="CR",
            status="Agreed",
        )
    )

    _enable_auto_sync(monkeypatch)

    result = runner.invoke(app, ["tdoc", "show", "--tdoc", "R5s260009"])
    assert result.exit_code == 0
    assert "[auto-sync]" in result.stdout
    assert "tdoc_id: R5s260009" in result.stdout


def test_tdoc_parse_db_mode_auto_sync_triggers_sync(
    monkeypatch, sqlite_env
) -> None:
    """DB-mode tdoc parse triggers auto-sync before parsing."""
    _patch_scraper_client(monkeypatch)
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0

    meeting_repo = SQLAlchemyMeetingRepository()
    meeting_repo.upsert_many(
        [
            Meeting(
                meeting_id=111,
                name="RAN5#111",
                title="RAN5 meeting 111",
                location="Online",
                start_date=date(2026, 1, 1),
                end_date=_future_date(),
                ftp_url="https://www.3gpp.org/ftp/tsg_ran/TSG_RAN/TSGR_111/Docs/",
                tsg="R5",
            ),
        ]
    )

    _enable_auto_sync(monkeypatch)

    # Use a TDoc id pattern that selects nothing, so the command exits after
    # auto-sync without invoking the parser. This keeps the test independent
    # of the optional python-docx dependency.
    result = runner.invoke(
        app,
        ["tdoc", "parse", "--tdoc", "R5s999999", "--yes"],
    )
    # No TDocs matched the filters, so the parse command exits with code 1,
    # but auto-sync must still have fired first.
    assert result.exit_code == 1
    assert "[auto-sync]" in result.stdout


def test_tdoc_parse_direct_mode_auto_syncs_filename_id(
    monkeypatch, sqlite_env
) -> None:
    """Direct-mode tdoc parse (--from-path) fires auto-sync for the filename's id.

    Per the adjudicated Task 15 Blocker-7 ruling, the direct-mode path
    extracts the tdoc_id from the filename and triggers auto-sync for
    it (gated on ``Settings.sync.auto_sync``); when the id is still
    missing after auto-sync the parse assumes CR. This test patches the
    scraper so the internal meeting sync succeeds without the network.
    """
    _patch_scraper_client(monkeypatch)
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0
    _enable_auto_sync(monkeypatch)

    fixture_path = (
        Path(__file__).parent.parent
        / "fixtures"
        / "tdoc_cr_doc"
        / "R5s260009.zip"
    )
    result = runner.invoke(
        app,
        [
            "tdoc",
            "parse",
            "--from-path",
            str(fixture_path),
            "--format",
            "raw",
        ],
    )
    assert result.exit_code == 0
    assert "[auto-sync]" in result.stdout
