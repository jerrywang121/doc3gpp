from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.services.meetings_service import MeetingService


def test_cli_passes_location_filter(monkeypatch):
    runner = CliRunner()
    captured = {}

    def fake_list_recent(self, limit=20, offset=0, tsg=None, name_like=None, location_like=None, year=None):
        captured['location_like'] = location_like
        return []

    monkeypatch.setattr(MeetingService, "list_recent", fake_list_recent)

    result = runner.invoke(app, ["meeting", "list", "--location", "%Online%"])
    assert result.exit_code == 0
    assert captured['location_like'] == '%Online%'
