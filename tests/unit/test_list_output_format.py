"""Output-format tests for every ``* list`` CLI command.

Exercises the ``--format`` (table/json/markdown) and ``-o/--output``
(redirect to file) flags added uniformly to ``meeting list``, ``tdoc
list``, ``tsg list``, and ``wi list``.

The service layer is patched so each test owns its fixtures and runs
without touching sqlite, mirroring the strategy used by
``test_meeting_cli.py``.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.meeting import Meeting
from doc3gpp.models.tdoc import TDoc, TDocWithMeeting
from doc3gpp.models.tsg import Tsg
from doc3gpp.models.wi import Wi
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.tdoc_service import TDocService
from doc3gpp.services.tsg_service import TsgService
from doc3gpp.services.wi_service import WiService

Runner = CliRunner


SAMPLE_MEETING = Meeting(
    meeting_id=10,
    name="R5-200",
    title="Title",
    location="City",
    start_date=None,
    end_date=None,
    ftp_url=None,
    start_doc=None,
    end_doc=None,
    updated_at=None,
)

SAMPLE_TDOC = TDoc(
    tdoc_id="R5s260001",
    title="Example A",
    url="https://x/1",
    cr_pack="RP-000123",
    source="Qualcomm",
    type="CR",
    status="Agreed",
    cr_cat="F",
    spec="38.331",
    version="18.1.0",
    related_wis="NR_ext",
)
SAMPLE_TDOC_ROW = TDocWithMeeting(tdoc=SAMPLE_TDOC, meeting_name="RAN5#111")

SAMPLE_TSGS = [
    Tsg(tsg_name="RAN WG1", short_name="R1", description="Radio", url="u1"),
    Tsg(tsg_name="RAN WG2", short_name="R2", description="Layer 2", url="u2"),
]

SAMPLE_WI = Wi(
    wi_id="42",
    tsg_short="R5",
    acronym="NTShar",
    release="Rel-19",
    name="NTM sharing",
    updated_at=None,
)


def _patch_simple(monkeypatch, cls, attr: str, return_value: object) -> None:
    """Swap ``cls.attr`` for a callable that ignores args and returns ``return_value``."""
    monkeypatch.setattr(cls, attr, lambda *args, **kwargs: return_value)


# ---------- meeting list ----------

def test_meeting_list_format_json_stdout(monkeypatch) -> None:
    _patch_simple(monkeypatch, MeetingService, "list_recent", [SAMPLE_MEETING])

    result = Runner().invoke(
        app,
        ["meeting", "list", "--format", "json", "--fields", "meeting_id,name"],
    )
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert payload == [{"meeting_id": "10", "name": "R5-200"}]


def test_meeting_list_format_markdown_stdout(monkeypatch) -> None:
    _patch_simple(monkeypatch, MeetingService, "list_recent", [SAMPLE_MEETING])

    result = Runner().invoke(
        app,
        ["meeting", "list", "--format", "markdown", "--fields", "meeting_id,name"],
    )
    assert result.exit_code == 0, result.output

    lines = [line for line in result.output.splitlines() if line]
    assert lines == [
        "| meeting_id | name |",
        "|---|---|",
        "| 10 | R5-200 |",
    ]


def test_meeting_list_format_json_output_file(monkeypatch, tmp_path) -> None:
    _patch_simple(monkeypatch, MeetingService, "list_recent", [SAMPLE_MEETING])

    target = tmp_path / "meetings.json"
    result = Runner().invoke(
        app,
        [
            "meeting", "list",
            "--format", "json",
            "--fields", "meeting_id,name",
            "-o", str(target),
        ],
    )
    assert result.exit_code == 0, result.output
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8")) == [
        {"meeting_id": "10", "name": "R5-200"}
    ]


def test_meeting_list_format_markdown_output_file(monkeypatch, tmp_path) -> None:
    _patch_simple(monkeypatch, MeetingService, "list_recent", [SAMPLE_MEETING])

    target = tmp_path / "meetings.md"
    result = Runner().invoke(
        app,
        [
            "meeting", "list",
            "--format", "markdown",
            "--fields", "meeting_id,name",
            "-o", str(target),
        ],
    )
    assert result.exit_code == 0, result.output
    text = target.read_text(encoding="utf-8")
    assert text.splitlines() == [
        "| meeting_id | name |",
        "|---|---|",
        "| 10 | R5-200 |",
    ]


def test_meeting_list_empty_json_writes_array(monkeypatch) -> None:
    _patch_simple(monkeypatch, MeetingService, "list_recent", [])

    result = Runner().invoke(app, ["meeting", "list", "--format", "json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == []


def test_meeting_list_empty_markdown_writes_header(monkeypatch) -> None:
    _patch_simple(monkeypatch, MeetingService, "list_recent", [])

    result = Runner().invoke(
        app, ["meeting", "list", "--format", "markdown", "--fields", "meeting_id,name"]
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line]
    assert lines == ["| meeting_id | name |", "|---|---|"]


def test_meeting_list_empty_table_keeps_legacy_message(monkeypatch) -> None:
    _patch_simple(monkeypatch, MeetingService, "list_recent", [])

    result = Runner().invoke(app, ["meeting", "list"])
    assert result.exit_code == 0
    assert "No meetings found" in result.output


def test_meeting_list_invalid_format_rejected(monkeypatch) -> None:
    _patch_simple(monkeypatch, MeetingService, "list_recent", [SAMPLE_MEETING])

    result = Runner().invoke(app, ["meeting", "list", "--format", "yaml"])
    assert result.exit_code != 0
    assert "Unknown format 'yaml'" in result.output
    assert "table" in result.output
    assert "json" in result.output
    assert "markdown" in result.output


# ---------- tdoc list ----------

def test_tdoc_list_format_json_stdout(monkeypatch) -> None:
    def fake(self, **kwargs) -> list[TDocWithMeeting]:
        return [SAMPLE_TDOC_ROW]

    monkeypatch.setattr(TDocService, "list_recent_with_meeting", fake)

    result = Runner().invoke(
        app,
        ["tdoc", "list", "--format", "json", "--fields", "tdoc_id,meeting_name,spec"],
    )
    assert result.exit_code == 0, result.output

    assert json.loads(result.output) == [
        {"tdoc_id": "R5s260001", "meeting_name": "RAN5#111", "spec": "38.331"}
    ]


def test_tdoc_list_format_markdown_stdout(monkeypatch) -> None:
    def fake(self, **kwargs) -> list[TDocWithMeeting]:
        return [SAMPLE_TDOC_ROW]

    monkeypatch.setattr(TDocService, "list_recent_with_meeting", fake)

    result = Runner().invoke(
        app,
        ["tdoc", "list", "--format", "markdown", "--fields", "tdoc_id,meeting_name"],
    )
    assert result.exit_code == 0, result.output

    lines = [line for line in result.output.splitlines() if line]
    assert lines == [
        "| tdoc_id | meeting_name |",
        "|---|---|",
        "| R5s260001 | RAN5#111 |",
    ]


def test_tdoc_list_format_json_output_file(monkeypatch, tmp_path) -> None:
    def fake(self, **kwargs) -> list[TDocWithMeeting]:
        return [SAMPLE_TDOC_ROW]

    monkeypatch.setattr(TDocService, "list_recent_with_meeting", fake)

    target = tmp_path / "tdocs.json"
    result = Runner().invoke(
        app,
        ["tdoc", "list", "--format", "json", "-o", str(target),
         "--fields", "tdoc_id,title"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(target.read_text(encoding="utf-8")) == [
        {"tdoc_id": "R5s260001", "title": "Example A"}
    ]


def test_tdoc_list_empty_legacy_message(monkeypatch) -> None:
    def fake(self, **kwargs) -> list[TDocWithMeeting]:
        return []

    monkeypatch.setattr(TDocService, "list_recent_with_meeting", fake)

    result = Runner().invoke(app, ["tdoc", "list"])
    assert result.exit_code == 0
    assert "No TDocs found" in result.output


def test_tdoc_list_invalid_format_rejected(monkeypatch) -> None:
    def fake(self, **kwargs) -> list[TDocWithMeeting]:
        return []

    monkeypatch.setattr(TDocService, "list_recent_with_meeting", fake)

    result = Runner().invoke(app, ["tdoc", "list", "--format", "csv"])
    assert result.exit_code != 0
    assert "Unknown format 'csv'" in result.output


# ---------- tsg list ----------

def test_tsg_list_format_json_stdout(monkeypatch) -> None:
    monkeypatch.setattr(TsgService, "list_all", lambda self: SAMPLE_TSGS)

    result = Runner().invoke(app, ["tsg", "list", "--format", "json", "--fields", "all"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert payload[0]["short_name"] == "R1"
    assert payload[1]["tsg_name"] == "RAN WG2"


def test_tsg_list_format_markdown_output_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(TsgService, "list_all", lambda self: SAMPLE_TSGS)

    target = tmp_path / "tsgs.md"
    result = Runner().invoke(
        app,
        ["tsg", "list", "--format", "markdown", "-o", str(target),
         "--fields", "short_name,tsg_name"],
    )
    assert result.exit_code == 0, result.output
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "| short_name | tsg_name |"
    assert lines[1] == "|---|---|"
    assert lines[2] == "| R1 | RAN WG1 |"
    assert lines[3] == "| R2 | RAN WG2 |"


def test_tsg_list_empty_markdown_writes_only_header(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(TsgService, "list_all", lambda self: [])

    target = tmp_path / "tsgs.md"
    result = Runner().invoke(
        app, ["tsg", "list", "--format", "markdown", "-o", str(target)]
    )
    assert result.exit_code == 0, result.output
    lines = target.read_text(encoding="utf-8").splitlines()
    # Only header + separator when empty.
    assert lines == ["| tsg_name | short_name | description |", "|---|---|---|"]


def test_tsg_list_empty_table_keeps_legacy_message(monkeypatch) -> None:
    monkeypatch.setattr(TsgService, "list_all", lambda self: [])

    result = Runner().invoke(app, ["tsg", "list"])
    assert result.exit_code == 0
    assert "No TSG records found" in result.output


# ---------- wi list ----------

def test_wi_list_format_json_output_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(WiService, "list_recent", lambda self, **kw: [SAMPLE_WI])

    target = tmp_path / "wis.json"
    result = Runner().invoke(
        app, ["wi", "list", "--format", "json", "-o", str(target)]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload == [
        {"wi_id": "42", "acronym": "NTShar", "release": "Rel-19", "name": "NTM sharing"}
    ]


def test_wi_list_format_markdown_stdout(monkeypatch) -> None:
    monkeypatch.setattr(WiService, "list_recent", lambda self, **kw: [SAMPLE_WI])

    result = Runner().invoke(app, ["wi", "list", "--format", "markdown"])
    assert result.exit_code == 0, result.output

    lines = [line for line in result.output.splitlines() if line]
    assert lines == [
        "| wi_id | acronym | release | name |",
        "|---|---|---|---|",
        "| 42 | NTShar | Rel-19 | NTM sharing |",
    ]


def test_wi_list_format_is_case_insensitive(monkeypatch) -> None:
    monkeypatch.setattr(WiService, "list_recent", lambda self, **kw: [SAMPLE_WI])

    result = Runner().invoke(app, ["wi", "list", "--format", "JSON"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["wi_id"] == "42"


def test_wi_list_empty_legacy_message(monkeypatch) -> None:
    monkeypatch.setattr(WiService, "list_recent", lambda self, **kw: [])

    result = Runner().invoke(app, ["wi", "list"])
    assert result.exit_code == 0
    assert "No WIs found" in result.output


def test_wi_list_empty_json_writes_array(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(WiService, "list_recent", lambda self, **kw: [])

    target = tmp_path / "wis.json"
    result = Runner().invoke(
        app, ["wi", "list", "--format", "json", "-o", str(target)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(target.read_text(encoding="utf-8")) == []


# ---------- negative flow shared across commands ----------

@pytest.mark.parametrize(
    "argv",
    [
        ["meeting", "list", "--format", "parquet"],
        ["tdoc", "list", "--format", "parquet"],
        ["tsg", "list", "--format", "parquet"],
        ["wi", "list", "--format", "parquet"],
    ],
)
def test_invalid_format_is_consistent_across_commands(
    argv: list[str], monkeypatch
) -> None:
    """Every ``* list`` command surfaces the same ``--format`` validation error."""
    monkeypatch.setattr(MeetingService, "list_recent", lambda self, **kw: [])
    monkeypatch.setattr(
        TDocService, "list_recent_with_meeting", lambda self, **kw: []
    )
    monkeypatch.setattr(TsgService, "list_all", lambda self: [])
    monkeypatch.setattr(WiService, "list_recent", lambda self, **kw: [])

    result = Runner().invoke(app, argv)
    assert result.exit_code != 0
    assert "Unknown format 'parquet'" in result.output
    # All three valid tokens are listed in the error message so users can recover.
    for token in ("table", "json", "markdown"):
        assert token in result.output
