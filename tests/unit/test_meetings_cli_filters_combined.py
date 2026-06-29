from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.services.meetings_service import MeetingService


def test_cli_passes_combined_filters(monkeypatch):
    runner = CliRunner()

    captured = {}

    def fake_list_recent(self, limit=20, tsg=None, name_like=None, year=None):
        captured['limit'] = limit
        captured['tsg'] = tsg
        captured['name_like'] = name_like
        captured['year'] = year
        return []

    monkeypatch.setattr(MeetingService, "list_recent", fake_list_recent)

    result = runner.invoke(app, ["meetings", "list", "--tsg", "r5", "--name", "%TTCN%", "--year", "2026"])
    assert result.exit_code == 0
    assert captured['tsg'] == 'r5'
    assert captured['name_like'] == '%TTCN%'
    assert captured['year'] == 2026
