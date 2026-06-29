from __future__ import annotations

import io

from openpyxl import Workbook
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.meeting import Meeting
from doc3gpp.models.tdoc import TDoc
from doc3gpp.services.tdoc_service import TDocService
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository


def _make_tdoc_xlsx_bytes() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["TDoc", "Title"])
    sheet.append(["R5s260001", "Example TDoc"])

    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_tdoc_repository_upsert_and_list(sqlite_env) -> None:
    create_schema()
    repo = SQLAlchemyTDocRepository()

    repo.upsert(TDoc(tdoc_id="R1-000001", title="First", meeting="RAN1#100", url="https://x/1"))
    repo.upsert(TDoc(tdoc_id="R1-000002", title="Second"))
    repo.upsert(TDoc(tdoc_id="R1-000001", title="First updated", meeting="RAN1#100", url="https://x/1a"))

    rows = repo.list(limit=10)

    assert len(rows) == 2
    by_id = {r.tdoc_id: r for r in rows}
    assert by_id["R1-000001"].title == "First updated"
    assert by_id["R1-000002"].title == "Second"


def test_tdoc_service_save_and_list(sqlite_env) -> None:
    create_schema()
    service = TDocService(SQLAlchemyTDocRepository())

    service.save(TDoc(tdoc_id="R2-000010", title="Agenda"))
    service.save(TDoc(tdoc_id="R2-000011", title="CR pack", meeting="RAN2#130"))

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
        meeting="R5#74",
    )
    assert count == 1

    rows = SQLAlchemyTDocRepository().list(limit=10)
    assert len(rows) == 1
    assert rows[0].tdoc_id == "R5s260001"
    assert rows[0].title == "Example TDoc"
    assert rows[0].meeting == "R5#74"


def test_cli_tdoc_sync_from_meeting_ftp(monkeypatch, sqlite_env) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0

    def fake_sync_from_meeting_ftp(self, ftp_url: str, meeting: str | None = None) -> int:
        assert ftp_url == "tsg_ran/WG5_Test_ex-T1/Workshop/TSGR5_Workshop_2026/docs/"
        assert meeting == "R5#74"
        return 1

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.TDocService.sync_from_meeting_ftp",
        fake_sync_from_meeting_ftp,
    )

    meeting_repo = SQLAlchemyMeetingRepository()
    from datetime import date

    meeting = Meeting(
        meeting_id=85434,
        name="R5--TTCN Workshop#74",
        title="3GPPRAN5-TTCN Workshop#74",
        location="Online",
        start_date=date(2026, 7, 2),
        end_date=date(2026, 7, 2),
        ftp_url="tsg_ran/WG5_Test_ex-T1/Workshop/TSGR5_Workshop_2026/docs/",
    )
    # Use raw session insert via repository to keep test compatible with current repo API.
    meeting_repo.upsert_many([meeting])

    result = runner.invoke(
        app,
        [
            "tdoc",
            "sync",
            "--meeting-id",
            "85434",
            "--meeting",
            "R5#74",
        ],
    )
    assert result.exit_code == 0
    assert "TDoc sync complete: 1 records stored" in result.stdout
