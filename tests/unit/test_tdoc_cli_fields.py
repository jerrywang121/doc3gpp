from typer.testing import CliRunner

from doc3gpp.cli import app


def test_cli_tdoc_list_fields_and_filters(monkeypatch):
    runner = CliRunner()

    class FakeTDoc:
        def __init__(self, tdoc_id: str, title: str, url: str | None = None):
            self.tdoc_id = tdoc_id
            self.title = title
            self.url = url

    sample = [FakeTDoc(tdoc_id="R5s260001", title="Example A", url="https://x/1")]

    def fake_list_recent(self, limit=20, tsg=None, meeting_like=None, year=None):
        return sample

    monkeypatch.setattr("doc3gpp.services.tdoc_service.TDocService.list_recent", fake_list_recent)

    result = runner.invoke(app, ["tdoc", "list", "--fields", "tdoc_id,title,url"])
    assert result.exit_code == 0
    lines = [l for l in result.output.splitlines() if l and not l.startswith("Listing")]
    assert lines
    parts = lines[0].split("\t")
    assert parts == ["R5s260001", "Example A", "https://x/1"]


def test_cli_tdoc_list_invalid_fields_error():
    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "list", "--fields", "badfield,foo"])
    assert result.exit_code != 0
    assert "Unknown field(s): badfield, foo" in result.output
    assert "Valid fields:" in result.output
    assert "tdoc_id" in result.output
    assert "meeting_id" in result.output


def test_cli_tdoc_list_meeting_name_field_is_invalid():
    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "list", "--fields", "tdoc_id,meeting_name"])
    assert result.exit_code != 0
    assert "Unknown field(s): meeting_name" in result.output
