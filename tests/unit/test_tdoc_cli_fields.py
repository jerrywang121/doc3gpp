from typer.testing import CliRunner

from doc3gpp.cli import app


def test_cli_tdoc_list_fields_and_filters(monkeypatch):
    runner = CliRunner()

    class FakeTDoc:
        def __init__(self, tdoc_id: str, title: str, url: str | None = None, cr_pack: str | None = None, meeting_name: str | None = None, source: str | None = None, type: str | None = None, status: str | None = None, cr_cat: str | None = None, spec: str | None = None, version: str | None = None, related_wis: str | None = None):
            self.tdoc_id = tdoc_id
            self.title = title
            self.url = url
            self.cr_pack = cr_pack
            self.meeting_name = meeting_name
            self.source = source
            self.type = type
            self.status = status
            self.cr_cat = cr_cat
            self.spec = spec
            self.version = version
            self.related_wis = related_wis

    sample = [FakeTDoc(
        tdoc_id="R5s260001",
        title="Example A",
        url="https://x/1",
        cr_pack="RP-000123",
        meeting_name="RAN5#111",
        source="Qualcomm",
        type="CR",
        status="Agreed",
        cr_cat="F",
        spec="38.331",
        version="18.1.0",
        related_wis="NR_ext"
    )]

    observed_filters = {}

    def fake_list_recent(self, limit=20, tsg=None, meeting_like=None, year=None, source_like=None, spec_like=None, wi_like=None, title_like=None, cat_like=None, status_like=None, type_like=None):
        observed_filters.update({
            "limit": limit,
            "tsg": tsg,
            "meeting_like": meeting_like,
            "year": year,
            "source_like": source_like,
            "spec_like": spec_like,
            "wi_like": wi_like,
            "title_like": title_like,
            "cat_like": cat_like,
            "status_like": status_like,
            "type_like": type_like,
        })
        return sample

    monkeypatch.setattr("doc3gpp.services.tdoc_service.TDocService.list_recent", fake_list_recent)

    # test explicit fields including meeting_name and cr_pack
    result = runner.invoke(app, ["tdoc", "list", "--fields", "tdoc_id,title,meeting_name,cr_pack"])
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line and not line.startswith("Listing")]
    assert lines
    parts = lines[0].split("\t")
    assert parts == ["R5s260001", "Example A", "RAN5#111", "RP-000123"]

    # test default fields (now more detailed)
    result = runner.invoke(app, ["tdoc", "list"])
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line and not line.startswith("Listing")]
    assert lines
    parts = lines[0].split("\t")
    # Expected default: tdoc_id, meeting_name, title, source, type, status, cr_cat, spec, version, related_wis
    assert parts == ["R5s260001", "RAN5#111", "Example A", "Qualcomm", "CR", "Agreed", "F", "38.331", "18.1.0", "NR_ext"]

    # test all new filters
    runner.invoke(app, [
        "tdoc", "list",
        "--source", "Q%",
        "--spec", "38.331",
        "--wi", "NR%",
        "--title", "RedCap%",
        "--cat", "F",
        "--status", "Agreed",
        "--type", "CR"
    ])
    assert observed_filters["source_like"] == "Q%"
    assert observed_filters["spec_like"] == "38.331"
    assert observed_filters["wi_like"] == "NR%"
    assert observed_filters["title_like"] == "RedCap%"
    assert observed_filters["cat_like"] == "F"
    assert observed_filters["status_like"] == "Agreed"
    assert observed_filters["type_like"] == "CR"


def test_cli_tdoc_list_invalid_fields_error():
    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "list", "--fields", "badfield,foo"])
    assert result.exit_code != 0
    assert "Unknown field(s): badfield, foo" in result.output
    assert "Valid fields:" in result.output
    assert "tdoc_id" in result.output
    assert "meeting_id" in result.output
    assert "meeting_name" in result.output
