from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.services.meetings_service import MeetingService


def test_cli_passes_combined_filters(monkeypatch):
    runner = CliRunner()

    captured = {}

    def fake_list_recent(self, limit=20, offset=0, tsg=None, name_like=None, location_like=None, year=None, tdoc_id=None):
        captured['limit'] = limit
        captured['offset'] = offset
        captured['tsg'] = tsg
        captured['name_like'] = name_like
        captured['location_like'] = location_like
        captured['year'] = year
        return []

    monkeypatch.setattr(MeetingService, "list_recent", fake_list_recent)

    result = runner.invoke(app, ["meeting", "list", "--tsg", "r5", "--name", "%TTCN%", "--year", "2026"])
    assert result.exit_code == 0
    assert captured['tsg'] == 'R5'
    assert captured['name_like'] == '%TTCN%'
    assert captured['year'] == 2026
