from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.models.meeting import Meeting


def test_cli_fields_and_filters(monkeypatch):
    runner = CliRunner()

    sample = [
        Meeting(
            meeting_id=10,
            name="R5-200",
            title="Title",
            location="City",
            start_date=None,
            end_date=None,
            ftp_url=None,
            start_doc=None,
            end_doc=None,
        ),
    ]

    def fake_list_recent(self, limit=20, offset=0, tsg=None, name_like=None, location_like=None, year=None):
        return sample

    monkeypatch.setattr(MeetingService, "list_recent", fake_list_recent)

    result = runner.invoke(app, ["meeting", "list", "--fields", "meeting_id,name"])
    assert result.exit_code == 0
    # expect a single line with meeting_id and name separated by tab
    lines = [line for line in result.output.splitlines() if line and not line.startswith("Listing")]
    assert lines
    parts = lines[0].split("\t")
    assert parts[0].strip() == "10"
    assert parts[1].strip() == "R5-200"
