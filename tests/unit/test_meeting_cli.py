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

    def fake_list_recent(self, limit=20, offset=0, tsg=None, name_like=None, location_like=None, year=None, tdoc_id=None):
        return sample

    monkeypatch.setattr(MeetingService, "list_recent", fake_list_recent)

    result = runner.invoke(app, ["meeting", "list", "--fields", "meeting_id,name"])
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line and not line.startswith("Listing")]
    assert lines
    parts = lines[0].split("\t")
    assert parts[0].strip() == "10"
    assert parts[1].strip() == "R5-200"


def test_cli_tdoc_filter_forwards_parsed_tuple(monkeypatch):
    """A well-formed --tdoc value must be parsed into (prefix, number) and
    forwarded as ``tdoc_id`` to ``MeetingService.list_recent``."""
    runner = CliRunner()

    captured: dict = {}

    def fake_list_recent(self, limit=20, offset=0, tsg=None, name_like=None, location_like=None, year=None, tdoc_id=None):
        captured["tdoc_id"] = tdoc_id
        return []

    monkeypatch.setattr(MeetingService, "list_recent", fake_list_recent)

    result = runner.invoke(app, ["meeting", "list", "--tdoc", "R5-260013"])
    assert result.exit_code == 0, result.output
    assert captured["tdoc_id"] == ("R5-", 260013)


def test_cli_tdoc_filter_accepts_ttcn_form(monkeypatch):
    """``R5s260009`` (TTCN) and ``R5w260013`` (workshop) share the CR-shape regex."""
    runner = CliRunner()
    captured: dict = {}

    def fake_list_recent(self, limit=20, offset=0, tsg=None, name_like=None, location_like=None, year=None, tdoc_id=None):
        captured["tdoc_id"] = tdoc_id
        return []

    monkeypatch.setattr(MeetingService, "list_recent", fake_list_recent)

    result = runner.invoke(app, ["meeting", "list", "--tdoc", "R5s260009"])
    assert result.exit_code == 0, result.output
    assert captured["tdoc_id"] == ("R5s", 260009)


def test_cli_tdoc_filter_rejects_malformed_value(monkeypatch):
    """Non-CR-shape --tdoc values must raise ``BadParameter`` before the
    database is touched."""
    runner = CliRunner()

    def fake_list_recent(self, **kwargs):
        raise AssertionError("list_recent must not be called for malformed --tdoc")

    monkeypatch.setattr(MeetingService, "list_recent", fake_list_recent)

    result = runner.invoke(app, ["meeting", "list", "--tdoc", "not-a-tdoc"])
    assert result.exit_code != 0
    assert "Invalid TDoc id" in result.output


def test_cli_tdoc_filter_not_passed_when_absent(monkeypatch):
    """Without ``--tdoc``, the service must receive ``tdoc_id=None``."""
    runner = CliRunner()
    captured: dict = {}

    def fake_list_recent(self, limit=20, offset=0, tsg=None, name_like=None, location_like=None, year=None, tdoc_id=None):
        captured["tdoc_id"] = tdoc_id
        return []

    monkeypatch.setattr(MeetingService, "list_recent", fake_list_recent)

    result = runner.invoke(app, ["meeting", "list"])
    assert result.exit_code == 0, result.output
    assert captured["tdoc_id"] is None


def test_cli_tsg_filter_uppercases_like_pattern(monkeypatch):
    """``--tsg`` is treated as a SQL LIKE pattern and upper-cased."""
    runner = CliRunner()
    captured: dict = {}

    def fake_list_recent(self, limit=20, offset=0, tsg=None, name_like=None, location_like=None, year=None, tdoc_id=None):
        captured["tsg"] = tsg
        return []

    monkeypatch.setattr(MeetingService, "list_recent", fake_list_recent)

    result = runner.invoke(app, ["meeting", "list", "--tsg", "r%"])
    assert result.exit_code == 0, result.output
    assert captured["tsg"] == "R%"


def test_cli_tsg_filter_accepts_unknown_pattern_without_validation_error(monkeypatch):
    """Wildcard patterns must not be rejected by the TSG reference lookup."""
    runner = CliRunner()
    captured: dict = {}

    def fake_list_recent(self, limit=20, offset=0, tsg=None, name_like=None, location_like=None, year=None, tdoc_id=None):
        captured["tsg"] = tsg
        return []

    monkeypatch.setattr(MeetingService, "list_recent", fake_list_recent)

    result = runner.invoke(app, ["meeting", "list", "--tsg", "XYZ%"])
    assert result.exit_code == 0, result.output
    assert captured["tsg"] == "XYZ%"
