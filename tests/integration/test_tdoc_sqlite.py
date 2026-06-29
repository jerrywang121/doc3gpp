from __future__ import annotations

import io

from openpyxl import Workbook
from pathlib import Path
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.meeting import Meeting
from doc3gpp.models.tdoc import TDoc
from doc3gpp.services.tdoc_service import TDocService
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository


def _make_tdoc_xlsx_bytes() -> bytes:
    # Use a real example XLSX placed under tests/fixtures/tdoc_xlsx/
    fn = Path(__file__).parent.parent / "fixtures" / "tdoc_xlsx" / "TDoc_List_Meeting_RAN5#111.xlsx"
    return fn.read_bytes()


def test_tdoc_repository_upsert_and_list(sqlite_env) -> None:
    create_schema()
    repo = SQLAlchemyTDocRepository()

    repo.upsert(TDoc(tdoc_id="R1-000001", title="First", meeting_id=100, url="https://x/1"))
    repo.upsert(TDoc(tdoc_id="R1-000002", title="Second"))
    repo.upsert(TDoc(tdoc_id="R1-000001", title="First updated", meeting_id=100, url="https://x/1a"))

    rows = repo.list(limit=10)

    assert len(rows) == 2
    by_id = {r.tdoc_id: r for r in rows}
    assert by_id["R1-000001"].title == "First updated"
    assert by_id["R1-000002"].title == "Second"


def test_tdoc_service_save_and_list(sqlite_env) -> None:
    create_schema()
    service = TDocService(SQLAlchemyTDocRepository())

    service.save(TDoc(tdoc_id="R2-000010", title="Agenda"))
    service.save(TDoc(tdoc_id="R2-000011", title="CR pack", meeting_id=130))

    rows = service.list_recent(limit=5)
    ids = {r.tdoc_id for r in rows}

    assert ids == {"R2-000010", "R2-000011"}


def test_tdoc_service_sync_from_meeting_ftp(monkeypatch, sqlite_env) -> None:
    create_schema()
    service = TDocService(SQLAlchemyTDocRepository())

    class DummyClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self) -> "DummyClient":
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def get_text(self, url: str) -> str:
            return '<a href="TDoc_List_Meeting_sample.xlsx">list</a>'

        def get_bytes(self, url: str) -> bytes:
            return _make_tdoc_xlsx_bytes()

        def close(self) -> None:
            pass

    monkeypatch.setattr("doc3gpp.scraping.ftp_source.ScraperClient", DummyClient)

    count = service.sync_from_meeting_ftp(
        ftp_url="tsg_ran/WG5_Test_ex-T1/Workshop/TSGR5_Workshop_2026/docs/",
        meeting_id=1,
    )
    assert count >= 1

    rows = SQLAlchemyTDocRepository().list(limit=10)
    assert len(rows) >= 1
    assert rows[0].tdoc_id
    assert rows[0].title
    assert rows[0].meeting_id == 1


def test_cli_tdoc_sync_from_meeting_ftp(monkeypatch, sqlite_env) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0
    # Use local fixtures: meeting HTML and TDoc XLSX
    html_fn = Path(__file__).parent.parent / "fixtures" / "sample_pages" / "3GPP-meeting-R5.html"
    xlsx_fn = Path(__file__).parent.parent / "fixtures" / "tdoc_xlsx" / "TDoc_List_Meeting_RAN5#111.xlsx"
    html_text = html_fn.read_text(encoding="utf-8")
    xlsx_bytes = xlsx_fn.read_bytes()

    class DummyClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def get_text(self, url: str) -> str:
            # calendar page
            if "Meetings-" in url or "dynareport" in url:
                return html_text
            # ftp directory listing
            if "3gpp.org/ftp/" in url:
                return '<a href="TDoc_List_Meeting_RAN5#111.xlsx">list</a>'
            return ""

        def get_bytes(self, url: str) -> bytes:
            return xlsx_bytes

    monkeypatch.setattr("doc3gpp.scraping.client.ScraperClient", DummyClient)

    # sync meetings into the DB using the local HTML fixture
    res = runner.invoke(app, ["meetings", "sync", "--tsg", "r5"])
    assert res.exit_code == 0

    # run tdoc sync by meeting name present in the DB (choose one with an FTP URL)
    from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository

    meeting_repo = SQLAlchemyMeetingRepository()
    meetings = meeting_repo.list(limit=50, tsg="r5")
    assert meetings
    # pick a meeting that has an ftp_url populated
    m = next((m for m in meetings if m.ftp_url), meetings[0])

    result = runner.invoke(app, ["tdoc", "sync", "--meeting", m.name])
    assert result.exit_code == 0
    assert "TDoc sync complete:" in result.stdout


def test_cli_tdoc_sync_from_meeting_id(monkeypatch, sqlite_env) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0
    # Use local fixtures for meetings and tdoc xlsx
    html_fn = Path(__file__).parent.parent / "fixtures" / "sample_pages" / "3GPP-meeting-R5.html"
    xlsx_fn = Path(__file__).parent.parent / "fixtures" / "tdoc_xlsx" / "TDoc_List_Meeting_RAN5#111.xlsx"
    html_text = html_fn.read_text(encoding="utf-8")
    xlsx_bytes = xlsx_fn.read_bytes()

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

    monkeypatch.setattr("doc3gpp.scraping.client.ScraperClient", DummyClient)

    # sync meetings into DB
    res = runner.invoke(app, ["meetings", "sync", "--tsg", "r5"])
    assert res.exit_code == 0

    # get meeting id
    meeting_repo = SQLAlchemyMeetingRepository()
    meetings = meeting_repo.list(limit=50, tsg="r5")
    assert meetings
    # pick a meeting that has an ftp_url populated
    meeting = next((m for m in meetings if m.ftp_url), None)
    assert meeting is not None
    mid = meeting.meeting_id

    result = runner.invoke(app, ["tdoc", "sync", "--meeting-id", str(mid)])
    assert result.exit_code == 0
    assert "TDoc sync complete:" in result.stdout


def test_cli_tdoc_list_filters(sqlite_env) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0

    repo = SQLAlchemyTDocRepository()
    meeting_repo = SQLAlchemyMeetingRepository()
    from datetime import date

    m1 = Meeting(meeting_id=100, name="RAN3#100", title="RAN3 meeting", location="Online", start_date=date(2026,1,1), end_date=date(2026,1,2))
    m2 = Meeting(meeting_id=101, name="RAN3#101", title="RAN3 meeting 2", location="Online", start_date=date(2026,2,1), end_date=date(2026,2,2))
    m3 = Meeting(meeting_id=110, name="RAN6#110", title="RAN6 meeting", location="Online", start_date=date(2026,3,1), end_date=date(2026,3,2))
    meeting_repo.upsert_many([m1, m2, m3])

    repo.upsert(TDoc(tdoc_id="R5s260001", title="Example A", meeting_id=100, url="https://x/1"))
    repo.upsert(TDoc(tdoc_id="R5s260002", title="Example B", meeting_id=101, url="https://x/2"))
    repo.upsert(TDoc(tdoc_id="R6s260003", title="Example C", meeting_id=110, url="https://x/3"))

    result = runner.invoke(
        app,
        [
            "tdoc",
            "list",
            "--tsg",
            "R5",
            "--meeting",
            "%RAN3%",
            "--year",
            "26",
            "--limit",
            "10",
        ],
    )

    assert result.exit_code == 0
    assert "R5s260001\tExample A\tRAN3#100\thttps://x/1" in result.stdout
    assert "R5s260002\tExample B\tRAN3#101\thttps://x/2" in result.stdout
    assert "R6s260003" not in result.stdout


def test_cli_tdoc_sync_meeting_args_are_exclusive(sqlite_env) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0

    result = runner.invoke(
        app,
        [
            "tdoc",
            "sync",
            "--meeting-id",
            "85434",
            "--meeting",
            "R5--TTCN Workshop#74",
        ],
    )
    assert result.exit_code != 0
    assert "Specify exactly one of --meeting-id or --meeting." in result.stderr
