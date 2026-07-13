from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from doc3gpp.models.tsg import Tsg
from doc3gpp.services import meetings_service as meetings_service_module
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import TsgORM
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository


def test_calendar_service_persists_to_sqlite(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "meetings.db"
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    get_settings.cache_clear()
    get_engine.cache_clear()

    fixture = Path("tests/fixtures/sample_pages/3GPP-meeting-R5.html")
    html = fixture.read_text(encoding="utf-8")

    def fake_fetch_calendar(_: str):
        from doc3gpp.parsers.calendar_parser import parse_3gpp_calendar

        return parse_3gpp_calendar(html)

    import doc3gpp.services.meetings_service as meetings_service_module

    monkeypatch.setattr(meetings_service_module, "fetch_calendar", fake_fetch_calendar)

    create_schema()
    service = MeetingService(SQLAlchemyMeetingRepository())
    outcome = service.sync("https://example.invalid")
    rows = service.list_recent(limit=10)

    assert outcome.status == "synced"
    assert outcome.synced_count == 6
    assert len(rows) == 6
    # SQL repo orders by start_date DESC, meeting_id DESC; R5-121 (2028) is
    # therefore the first row in the listing.
    assert rows[0].meeting_id == 85637
    assert {m.meeting_id for m in rows} == {85637, 82711, 85434, 60240, 18788, 11017}


def test_sync_populates_tsg_fk_and_list_filters_by_it(tmp_path, monkeypatch) -> None:
    """End-to-end: sync(tsg=...) stamps the FK and list(tsg=...) filters by it.

    Reproduces the CLI flow ``meeting sync --tsg r5`` followed by
    ``meeting list --tsg r5`` against a real SQLite file. The parent
    tsgs row must exist or the FK constraint rejects the upsert.
    """
    db_path = tmp_path / "meetings.db"
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    get_settings.cache_clear()
    get_engine.cache_clear()

    fixture = Path("tests/fixtures/sample_pages/3GPP-meeting-R5.html")
    html = fixture.read_text(encoding="utf-8")

    def fake_fetch_calendar(_: str):
        from doc3gpp.parsers.calendar_parser import parse_3gpp_calendar

        return parse_3gpp_calendar(html)

    import doc3gpp.services.meetings_service as meetings_service_module

    monkeypatch.setattr(meetings_service_module, "fetch_calendar", fake_fetch_calendar)

    create_schema()

    with get_engine().begin() as conn:
        conn.execute(
            TsgORM.__table__.insert().values(
                tsg_name="RAN WG5",
                short_name="R5",
                description="Mobile terminal conformance testing",
                url=None,
            )
        )

    service = MeetingService(SQLAlchemyMeetingRepository())
    outcome = service.sync("https://example.invalid", tsg="r5")
    assert outcome.status == "synced"
    assert outcome.synced_count == 6

    all_rows = service.list_recent(limit=10)
    assert {m.meeting_id for m in all_rows} == {85637, 82711, 85434, 60240, 18788, 11017}
    assert all(m.tsg == "R5" for m in all_rows)

    r5_rows = service.list_recent(limit=10, tsg="r5")
    assert {m.meeting_id for m in r5_rows} == {85637, 82711, 85434, 60240, 18788, 11017}

    s2_rows = service.list_recent(limit=10, tsg="s2")
    assert s2_rows == []


def test_meeting_sync_skips_within_interval(tmp_path, monkeypatch) -> None:
    """A second sync within the sync interval is skipped."""
    db_path = tmp_path / "meetings.db"
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    get_settings.cache_clear()
    get_engine.cache_clear()

    fixture = Path("tests/fixtures/sample_pages/3GPP-meeting-R5.html")
    html = fixture.read_text(encoding="utf-8")

    def fake_fetch_calendar(_: str):
        from doc3gpp.parsers.calendar_parser import parse_3gpp_calendar

        return parse_3gpp_calendar(html)

    monkeypatch.setattr(meetings_service_module, "fetch_calendar", fake_fetch_calendar)

    create_schema()
    tsg_repo = SQLAlchemyTsgRepository()
    tsg_repo.upsert_many([
        Tsg(
            tsg_name="RAN WG5",
            short_name="R5",
            description="x",
            url=None,
            meeting_last_sync=datetime.now(timezone.utc) - timedelta(hours=1),
        ),
    ])

    service = MeetingService(
        SQLAlchemyMeetingRepository(),
        tsg_repo,
        sync_interval=timedelta(hours=24),
    )
    outcome = service.sync("https://example.invalid", tsg="r5")

    assert outcome.status == "skipped"
    assert "last sync" in outcome.reason


def test_meeting_sync_force_bypasses_interval(tmp_path, monkeypatch) -> None:
    """``force=True`` runs the sync even inside the interval window."""
    db_path = tmp_path / "meetings.db"
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    get_settings.cache_clear()
    get_engine.cache_clear()

    fixture = Path("tests/fixtures/sample_pages/3GPP-meeting-R5.html")
    html = fixture.read_text(encoding="utf-8")

    def fake_fetch_calendar(_: str):
        from doc3gpp.parsers.calendar_parser import parse_3gpp_calendar

        return parse_3gpp_calendar(html)

    monkeypatch.setattr(meetings_service_module, "fetch_calendar", fake_fetch_calendar)

    create_schema()
    tsg_repo = SQLAlchemyTsgRepository()
    tsg_repo.upsert_many([
        Tsg(
            tsg_name="RAN WG5",
            short_name="R5",
            description="x",
            url=None,
            meeting_last_sync=datetime.now(timezone.utc) - timedelta(hours=1),
        ),
    ])

    service = MeetingService(
        SQLAlchemyMeetingRepository(),
        tsg_repo,
        sync_interval=timedelta(hours=24),
    )
    outcome = service.sync("https://example.invalid", tsg="r5", force=True)

    assert outcome.status == "synced"
    assert outcome.synced_count == 6