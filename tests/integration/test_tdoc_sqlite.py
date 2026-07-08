from __future__ import annotations

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
    meeting_repo = SQLAlchemyMeetingRepository()
    from datetime import date

    # meeting_id is an FK; satisfy it before inserting dependent TDoc rows.
    meeting_repo.upsert_many(
        [
            Meeting(
                meeting_id=100,
                name="RAN1#100",
                title="RAN1 meeting 100",
                location="Online",
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 2),
            ),
        ]
    )

    repo.upsert(
        TDoc(
            tdoc_id="R1-000001",
            title="First",
            meeting_id=100,
            ftp_url="x/1",
            source="Qualcomm",
            type="CR",
            status="Agreed",
            cr_pack="RP-000123",
        )
    )
    repo.upsert(TDoc(tdoc_id="R1-000002", title="Second"))
    repo.upsert(
        TDoc(
            tdoc_id="R1-000001",
            title="First updated",
            meeting_id=100,
            ftp_url="x/1a",
            source="Ericsson",
            cr_pack="RP-000124",
        )
    )

    rows = repo.list(limit=10)

    assert len(rows) == 2
    by_id = {r.tdoc_id: r for r in rows}
    assert by_id["R1-000001"].title == "First updated"
    assert by_id["R1-000001"].source == "Ericsson"
    assert by_id["R1-000001"].cr_pack == "RP-000124"
    assert by_id["R1-000002"].title == "Second"
    assert by_id["R1-000002"].source is None


def test_tdoc_service_sync_from_meeting_ftp(monkeypatch, sqlite_env) -> None:
    create_schema()
    service = TDocService(SQLAlchemyTDocRepository())
    meeting_repo = SQLAlchemyMeetingRepository()
    from datetime import date
    # TDocs carry meeting_id=1; seed the referenced row so FK enforcement passes.
    meeting_repo.upsert_many(
        [
            Meeting(
                meeting_id=1,
                name="RAN5#1",
                title="RAN5 meeting 1",
                location="Online",
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 2),
            ),
        ]
    )

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


def test_tdoc_sync_stores_per_tdoc_zip_url_not_xlsx_url(monkeypatch, sqlite_env) -> None:
    """``tdocs.ftp_url`` must point at the per-TDoc zip file, not the XLSX list.

    Regression: prior to the hyperlink-aware parser, every TDoc row inherited
    the XLSX file URL (e.g. ``.../TDoc_List_Meeting_RAN5#111.xlsx``) as its
    ``ftp_url`` field, which is useless for downloading the actual document.
    The real RAN5#111 fixture ships hyperlinks on column A pointing at
    each TDoc's ``.zip``; we assert those URLs round-trip through the sync
    into the database as paths relative to ``https://www.3gpp.org/ftp/``.
    """
    create_schema()
    service = TDocService(SQLAlchemyTDocRepository())
    meeting_repo = SQLAlchemyMeetingRepository()
    from datetime import date

    meeting_repo.upsert_many(
        [
            Meeting(
                meeting_id=1,
                name="RAN5#111",
                title="RAN5 meeting 111",
                location="Dalian",
                start_date=date(2026, 5, 8),
                end_date=date(2026, 5, 12),
            ),
        ]
    )

    xlsx_bytes = _make_tdoc_xlsx_bytes()
    xlsx_url = "https://www.3gpp.org/ftp/tsg_ran/WG5_Test_ex-T1/TSGR5__111_Dalian/Docs/TDoc_List_Meeting_RAN5#111.xlsx"

    class DummyClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def get_text(self, url: str) -> str:
            return f'<a href="{xlsx_url.rsplit("/", 1)[-1]}">list</a>'

        def get_bytes(self, url: str) -> bytes:
            return xlsx_bytes

    monkeypatch.setattr("doc3gpp.scraping.ftp_source.ScraperClient", DummyClient)

    count = service.sync_from_meeting_ftp(
        ftp_url="tsg_ran/WG5_Test_ex-T1/TSGR5__111_Dalian/",
        meeting_id=1,
    )
    assert count >= 1

    rows = SQLAlchemyTDocRepository().list(limit=count)
    by_id = {row.tdoc_id: row for row in rows}

    # Spot-check: both rows have column-A hyperlinks pointing at their zips.
    # ``ftp_url`` is stored as a path relative to the 3GPP FTP root.
    assert by_id["R5-261700"].ftp_url == (
        "tsg_ran/WG5_Test_ex-T1/TSGR5__111_Dalian/Docs/R5-261700.zip"
    )
    assert by_id["R5-261701"].ftp_url == (
        "tsg_ran/WG5_Test_ex-T1/TSGR5__111_Dalian/Docs/R5-261701.zip"
    )
    # Guard against regression: the XLSX list URL must never appear here.
    for row in rows:
        if row.ftp_url is not None:
            assert not row.ftp_url.endswith(".xlsx"), (
                f"TDoc {row.tdoc_id} ftp_url points at XLSX list, not zip: {row.ftp_url}"
            )


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
    res = runner.invoke(app, ["meeting", "sync", "--tsg", "r5"])
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
    res = runner.invoke(app, ["meeting", "sync", "--tsg", "r5"])
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

    repo.upsert(TDoc(tdoc_id="R5s260001", title="Example A", meeting_id=100, ftp_url="x/1"))
    repo.upsert(TDoc(tdoc_id="R5s260002", title="Example B", meeting_id=101, ftp_url="x/2"))
    repo.upsert(TDoc(tdoc_id="R6s260003", title="Example C", meeting_id=110, ftp_url="x/3"))

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
            "--fields",
            "tdoc_id,title,meeting_name,ftp_url",
        ],
    )

    assert result.exit_code == 0
    assert "R5s260001\tExample A\tRAN3#100\tx/1" in result.stdout
    assert "R5s260002\tExample B\tRAN3#101\tx/2" in result.stdout
    assert "R6s260003" not in result.stdout


def test_cli_tdoc_list_filter_by_meeting_id(sqlite_env) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0

    repo = SQLAlchemyTDocRepository()
    meeting_repo = SQLAlchemyMeetingRepository()
    from datetime import date

    m1 = Meeting(meeting_id=200, name="RAN5#200", title="RAN5 meeting 200", location="Online", start_date=date(2026,1,1), end_date=date(2026,1,2))
    m2 = Meeting(meeting_id=201, name="RAN5#201", title="RAN5 meeting 201", location="Online", start_date=date(2026,2,1), end_date=date(2026,2,2))
    meeting_repo.upsert_many([m1, m2])

    repo.upsert(TDoc(tdoc_id="R5s260001", title="Example A", meeting_id=200, ftp_url="x/1"))
    repo.upsert(TDoc(tdoc_id="R5s260002", title="Example B", meeting_id=201, ftp_url="x/2"))

    result = runner.invoke(
        app,
        [
            "tdoc",
            "list",
            "--meeting-id",
            "200",
            "--limit",
            "10",
            "--fields",
            "tdoc_id,title,meeting_name",
        ],
    )

    assert result.exit_code == 0
    assert "R5s260001\tExample A\tRAN5#200" in result.stdout
    assert "R5s260002" not in result.stdout


def test_cli_tdoc_list_meeting_id_unknown_returns_no_rows(sqlite_env) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0

    result = runner.invoke(
        app,
        ["tdoc", "list", "--meeting-id", "999999"],
    )

    assert result.exit_code == 0
    assert "No TDocs found" in result.stdout


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


def test_tdoc_repository_full_schema(sqlite_env) -> None:
    create_schema()
    repo = SQLAlchemyTDocRepository()
    meeting_repo = SQLAlchemyMeetingRepository()
    from datetime import date

    # Create a meeting to link to
    meeting_repo.upsert_many(
        [
            Meeting(
                meeting_id=123,
                name="RAN5#123",
                title="Meeting 123",
                location="Virtual",
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 5),
            )
        ]
    )

    tdoc = TDoc(
        tdoc_id="R5-260001",
        title="Full Schema Test",
        meeting_id=123,
        ftp_url="example.com/tdoc",
        source="Company A",
        type="CR",
        status="Agreed",
        reservation_date=date(2026, 6, 1),
        uploaded_date=date(2026, 6, 2),
        cr_cat="F",
        is_revision_of="R5-259999",
        revised_to="R5-260001rev1",
        release="Rel-18",
        spec="38.521-1",
        version="18.1.0",
        related_wis="NR_new_feat",
        cr_num="1234",
        cr_pack="RP-260123",
    )

    repo.upsert(tdoc)

    rows = repo.list_with_meeting(limit=1, tsg="R5")
    assert len(rows) == 1
    stored = rows[0]

    assert stored.tdoc.tdoc_id == tdoc.tdoc_id
    assert stored.tdoc.title == tdoc.title
    assert stored.tdoc.meeting_id == tdoc.meeting_id
    assert stored.meeting_name == "RAN5#123"
    assert stored.tdoc.ftp_url == tdoc.ftp_url
    assert stored.tdoc.source == tdoc.source
    assert stored.tdoc.type == tdoc.type
    assert stored.tdoc.status == tdoc.status
    assert stored.tdoc.reservation_date == tdoc.reservation_date
    assert stored.tdoc.uploaded_date == tdoc.uploaded_date
    assert stored.tdoc.cr_cat == tdoc.cr_cat
    assert stored.tdoc.is_revision_of == tdoc.is_revision_of
    assert stored.tdoc.revised_to == tdoc.revised_to
    assert stored.tdoc.release == tdoc.release
    assert stored.tdoc.spec == tdoc.spec
    assert stored.tdoc.version == tdoc.version
    assert stored.tdoc.related_wis == tdoc.related_wis
    assert stored.tdoc.cr_num == tdoc.cr_num
    assert stored.tdoc.cr_pack == tdoc.cr_pack
