from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from doc3gpp.cli import app


def test_cli_db_init_and_check(sqlite_env) -> None:
    runner = CliRunner()

    init_res = runner.invoke(app, ["db", "init"])
    assert init_res.exit_code == 0
    assert "Database schema initialized" in init_res.stdout

    check_res = runner.invoke(app, ["db", "check"])
    assert check_res.exit_code == 0
    assert "Database connection OK:" in check_res.stdout


def test_cli_tdoc_add_and_list(sqlite_env) -> None:
    runner = CliRunner()

    assert runner.invoke(app, ["db", "init"]).exit_code == 0
    add_res = runner.invoke(
        app,
        [
            "tdoc",
            "add",
            "--tdoc-id",
            "R3-000001",
            "--title",
            "Intro",
            "--meeting",
            "RAN3#100",
            "--url",
            "https://example.test/r3-1",
        ],
    )
    assert add_res.exit_code == 0
    assert "Saved R3-000001" in add_res.stdout

    list_res = runner.invoke(app, ["tdoc", "list", "--limit", "10"])
    assert list_res.exit_code == 0
    assert "R3-000001\tIntro\tRAN3#100\thttps://example.test/r3-1" in list_res.stdout


def test_cli_calendar_sync_and_list(sqlite_env, monkeypatch) -> None:
    runner = CliRunner()
    fixture = Path("tests/fixtures/sample_pages/meetings_r5_sample.html")
    html = fixture.read_text(encoding="utf-8")

    def fake_fetch_calendar(_: str):
        from doc3gpp.parsers.calendar_parser import parse_3gpp_calendar

        return parse_3gpp_calendar(html)

    import doc3gpp.services.calendar_service as calendar_service_module

    monkeypatch.setattr(calendar_service_module, "fetch_calendar", fake_fetch_calendar)

    assert runner.invoke(app, ["db", "init"]).exit_code == 0
    sync_res = runner.invoke(
        app,
        [
            "calendar",
            "sync",
            "--url",
            "https://example.invalid",
            "--closed-years",
            "10",
            "--future-years",
            "2",
        ],
    )
    assert sync_res.exit_code == 0
    assert "Calendar sync complete: 1 meeting rows stored" in sync_res.stdout

    list_res = runner.invoke(app, ["calendar", "list", "--limit", "5"])
    assert list_res.exit_code == 0
    assert "85434\tR5--TTCN Workshop#74\t2026-07-02\t2026-07-02" in list_res.stdout
