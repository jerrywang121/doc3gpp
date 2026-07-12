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


def test_cli_meeting_sync_and_list(sqlite_env, monkeypatch) -> None:
    runner = CliRunner()
    fixture = Path("tests/fixtures/sample_pages/3GPP-meeting-R5.html")
    html = fixture.read_text(encoding="utf-8")

    def fake_fetch_calendar(_: str):
        from doc3gpp.parsers.calendar_parser import parse_3gpp_calendar

        return parse_3gpp_calendar(html)

    import doc3gpp.services.meetings_service as meetings_service_module

    monkeypatch.setattr(meetings_service_module, "fetch_calendar", fake_fetch_calendar)

    assert runner.invoke(app, ["db", "init"]).exit_code == 0
    sync_res = runner.invoke(
        app,
        [
            "meeting",
            "sync",
            "--tsg",
            "r5",
        ],
    )
    assert sync_res.exit_code == 0
    assert "Meeting sync complete: 6 meeting rows stored" in sync_res.stdout

    list_res = runner.invoke(
        app,
        [
            "meeting",
            "list",
            "--limit",
            "5",
            "--fields",
            "meeting_id,name,start_date,end_date",
        ],
    )
    assert list_res.exit_code == 0
    assert "82711\tR5-116\t2027-08-30\t2027-09-03" in list_res.stdout
    assert "85434\tR5--TTCN Workshop#74\t2026-07-02\t2026-07-02" in list_res.stdout

    filtered_res = runner.invoke(
        app,
        [
            "meeting",
            "list",
            "--limit",
            "5",
            "--tsg",
            "r5",
            "--fields",
            "meeting_id,name,start_date,end_date",
        ],
    )
    assert filtered_res.exit_code == 0
    assert "82711\tR5-116\t2027-08-30\t2027-09-03" in filtered_res.stdout
    assert "85434\tR5--TTCN Workshop#74\t2026-07-02\t2026-07-02" in filtered_res.stdout