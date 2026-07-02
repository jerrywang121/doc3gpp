from __future__ import annotations

from datetime import date
from pathlib import Path

from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository


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
    inserted = service.sync(
        "https://example.invalid",
        max_year_closed=10,
        max_year_future=2,
        today=date(2026, 7, 2),
    )
    rows = service.list_recent(limit=10)

    assert inserted == 4
    assert len(rows) == 4
    # SQL repo orders by start_date DESC, meeting_id DESC; R5-116 (2027) is
    # therefore the first row in the listing.
    assert rows[0].meeting_id == 82711
    assert {m.meeting_id for m in rows} == {82711, 85434, 60240, 18788}