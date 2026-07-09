from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.tdoc import TDoc, TDocWithMeeting


def test_cli_tdoc_list_fields_and_filters(monkeypatch):
    runner = CliRunner()

    sample_tdoc = TDoc(
        tdoc_id="R5s260001",
        title="Example A",
        ftp_url="x/1",
        cr_pack="RP-000123",
        source="Qualcomm",
        type="CR",
        status="Agreed",
        cr_cat="F",
        spec="38.331",
        version="18.1.0",
        related_wis="NR_ext",
    )
    sample = [TDocWithMeeting(tdoc=sample_tdoc, meeting_name="RAN5#111")]

    observed_filters = {}

    def fake_list_recent_with_meeting(
        self, limit=20, tsg=None, meeting_like=None, meeting_id=None, year=None,
        source=None, spec=None, wi=None, title=None, cr_cat=None,
        status=None, tdoc_type=None,
        revision_of=None, revised_to=None, ftp_url=None, uploaded_date=None,
        **_kwargs,
    ):
        observed_filters.update({
            "limit": limit,
            "tsg": tsg,
            "meeting_like": meeting_like,
            "meeting_id": meeting_id,
            "year": year,
            "source": source,
            "spec": spec,
            "wi": wi,
            "title": title,
            "cr_cat": cr_cat,
            "status": status,
            "tdoc_type": tdoc_type,
            "revision_of": revision_of,
            "revised_to": revised_to,
            "ftp_url": ftp_url,
            "uploaded_date": uploaded_date,
        })
        return sample

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.TDocService.list_recent_with_meeting",
        fake_list_recent_with_meeting,
    )

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

    # Text filters route through the un-suffixed (rich-filter) repo params so
    # the CLI surface is consistent with `tdoc parse --meeting-id`.
    runner.invoke(app, [
        "tdoc", "list",
        "--source", "Q%",
        "--spec", "38.331",
        "--wi", "NR%",
        "--title", "RedCap%",
        "--cat", "F",
        "--status", "Agreed",
        "--type", "CR",
    ])
    assert observed_filters["source"] == "Q%"
    assert observed_filters["spec"] == "38.331"
    assert observed_filters["wi"] == "NR%"
    assert observed_filters["title"] == "RedCap%"
    assert observed_filters["cr_cat"] == "F"
    assert observed_filters["status"] == "Agreed"
    assert observed_filters["tdoc_type"] == "CR"
    # `_like` variants stay None — the CLI no longer routes through them.
    assert observed_filters["revision_of"] is None
    assert observed_filters["revised_to"] is None
    assert observed_filters["ftp_url"] is None
    assert observed_filters["uploaded_date"] is None

    # New filters added for parity with `tdoc parse --meeting-id`.
    runner.invoke(app, [
        "tdoc", "list",
        "--revision-of", "R5s260000",
        "--revised-to", "R5s260100",
        "--ftp-url", "tsg_ran/%",
        "--uploaded-date", ">= '2026-01-01'",
    ])
    assert observed_filters["revision_of"] == "R5s260000"
    assert observed_filters["revised_to"] == "R5s260100"
    assert observed_filters["ftp_url"] == "tsg_ran/%"
    assert observed_filters["uploaded_date"] == ">= '2026-01-01'"


def test_cli_tdoc_list_passes_not_like_prefix_unchanged(monkeypatch):
    """`-prefixed values are forwarded to the repo verbatim; the bang
    is consumed by the repository's ``_apply_text_filter`` to emit
    ``NOT LIKE``. The CLI does not interpret the bang — it must
    survive the trip through Typer / Click untouched."""
    runner = CliRunner()
    observed: dict = {}

    def fake_list_recent_with_meeting(self, **_kwargs):
        observed.update(_kwargs)
        return []

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.TDocService.list_recent_with_meeting",
        fake_list_recent_with_meeting,
    )

    result = runner.invoke(app, [
        "tdoc", "list",
        "--title", "!%Sidelink%",
        "--source", "!Qualcomm",
        "--cat", "!F",
    ])
    assert result.exit_code == 0, result.output
    assert observed["title"] == "!%Sidelink%"
    assert observed["source"] == "!Qualcomm"
    assert observed["cr_cat"] == "!F"


def test_cli_tdoc_list_passes_null_and_not_null_tokens(monkeypatch):
    """`null` / `not-null` literals flow through to the repo verbatim so
    the rich-filter grammar is consistent with `tdoc parse`."""
    runner = CliRunner()
    observed: dict = {}

    def fake_list_recent_with_meeting(self, **_kwargs):
        observed.update(_kwargs)
        return []

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.TDocService.list_recent_with_meeting",
        fake_list_recent_with_meeting,
    )

    runner.invoke(app, [
        "tdoc", "list",
        "--cat", "null",
        "--source", "not-null",
        "--spec", "38.331",
    ])
    assert observed["cr_cat"] == "null"
    assert observed["source"] == "not-null"
    assert observed["spec"] == "38.331"


def test_cli_tdoc_list_rejects_invalid_uploaded_date(monkeypatch):
    """The CLI mirrors `tdoc parse` and rejects malformed --uploaded-date
    values before the service is touched."""
    runner = CliRunner()
    called = {"count": 0}

    def fake_list_recent_with_meeting(self, **_kwargs):
        called["count"] += 1
        return []

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.TDocService.list_recent_with_meeting",
        fake_list_recent_with_meeting,
    )

    result = runner.invoke(
        app,
        ["tdoc", "list", "--uploaded-date", "yesterday"],
    )
    assert result.exit_code != 0
    assert "Invalid date filter" in result.output
    assert called["count"] == 0

    result = runner.invoke(
        app,
        ["tdoc", "list", "--uploaded-date", "== '2026-02-31'"],
    )
    assert result.exit_code != 0
    assert "Invalid date filter" in result.output
    assert called["count"] == 0


def test_cli_tdoc_list_auto_wraps_meeting_filter(monkeypatch):
    runner = CliRunner()

    captured: dict = {}

    def fake_list_recent_with_meeting(self, meeting_like=None, **_kwargs):
        captured["meeting_like"] = meeting_like
        return []

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.TDocService.list_recent_with_meeting",
        fake_list_recent_with_meeting,
    )

    result = runner.invoke(app, ["tdoc", "list", "--meeting", "RAN5#111"])
    assert result.exit_code == 0
    assert captured["meeting_like"] == "%RAN5#111%"

    result = runner.invoke(app, ["tdoc", "list", "--meeting", "R5s%"])
    assert result.exit_code == 0
    assert captured["meeting_like"] == "R5s%"


def test_cli_tdoc_list_invalid_fields_error():
    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "list", "--fields", "badfield,foo"])
    assert result.exit_code != 0
    assert "Unknown field(s): badfield, foo" in result.output
    assert "Valid fields:" in result.output
    assert "tdoc_id" in result.output
    assert "meeting_id" in result.output
    assert "meeting_name" in result.output