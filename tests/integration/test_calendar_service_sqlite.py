from __future__ import annotations

from pathlib import Path

from doc3gpp.services.calendar_service import CalendarService
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository


def test_calendar_service_persists_to_sqlite(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "calendar.db"
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    get_settings.cache_clear()
    get_engine.cache_clear()

    fixture = Path("tests/fixtures/sample_pages/meetings_r5_sample.html")
    html = fixture.read_text(encoding="utf-8")

    def fake_fetch_calendar(_: str):
        from doc3gpp.parsers.calendar_parser import parse_3gpp_calendar

        return parse_3gpp_calendar(html)

    import doc3gpp.services.calendar_service as calendar_service_module

    monkeypatch.setattr(calendar_service_module, "fetch_calendar", fake_fetch_calendar)

    create_schema()
    service = CalendarService(SQLAlchemyMeetingRepository())
    inserted = service.sync("https://example.invalid", max_year_closed=10, max_year_future=2)
    rows = service.list_recent(limit=10)

    assert inserted == 1
    assert len(rows) == 1
    assert rows[0].meeting_id == 85434
