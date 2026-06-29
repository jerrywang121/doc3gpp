from typer.testing import CliRunner

from doc3gpp.cli import app


def test_invalid_fields_error_message():
    runner = CliRunner()
    result = runner.invoke(app, ["meetings", "list", "--fields", "badfield,foo"])
    assert result.exit_code != 0
    # Typer reports the error; ensure invalid names are mentioned
    assert "Unknown field(s): badfield, foo" in result.output
    # Ensure the output lists valid fields
    assert "Valid fields:" in result.output
    # Check a couple of known valid fields are present in the message
    assert "meeting_id" in result.output
    assert "start_date" in result.output
