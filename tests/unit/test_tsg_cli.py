from dataclasses import fields

from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.tsg import Tsg
from doc3gpp.services.tsg_service import TsgService


# Sample TSG records used by the CLI tests.
_SAMPLE_TSGS = [
    Tsg(
        tsg_name="RAN WG1",
        short_name="R1",
        description="Radio Layer 1",
        url="https://www.3gpp.org/3gpp-groups/radio-access-networks-ran/ran-wg1",
    ),
    Tsg(
        tsg_name="RAN AH1",
        short_name="RT",
        description="ITU-R Ad Hoc",
        url="https://www.3gpp.org/3gpp-groups/radio-access-networks-ran/ran-ah1",
    ),
    Tsg(
        tsg_name="CT WG1",
        short_name="C1",
        description="UE-CN protocols",
        url="https://www.3gpp.org/3gpp-groups/core-network-terminals-ct/ct-wg1",
    ),
]


def _sorted_sample() -> list[Tsg]:
    return sorted(_SAMPLE_TSGS, key=lambda t: t.tsg_name)


def test_tsg_list_default_fields(monkeypatch) -> None:
    runner = CliRunner()

    def fake_list_all(self):
        return _sorted_sample()

    monkeypatch.setattr(TsgService, "list_all", fake_list_all)

    result = runner.invoke(app, ["tsg", "list"])
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line and not line.startswith("Listing")]
    assert lines == [
        "CT WG1\tC1\tUE-CN protocols",
        "RAN AH1\tRT\tITU-R Ad Hoc",
        "RAN WG1\tR1\tRadio Layer 1",
    ]


def test_tsg_list_with_all_fields(monkeypatch) -> None:
    runner = CliRunner()

    def fake_list_all(self):
        return _sorted_sample()

    monkeypatch.setattr(TsgService, "list_all", fake_list_all)

    result = runner.invoke(app, ["tsg", "list", "--fields", "all"])
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line and not line.startswith("Listing")]
    assert len(lines) == 3
    # ``--fields all`` emits one tab-separated column per dataclass
    # field; ``Tsg`` gained ``meeting_last_sync`` and ``spec_last_sync``
    # after this test was first written (sync timestamp tracking), so
    # compute the expected count from the model rather than hardcoding
    # it.
    expected_columns = len(fields(Tsg))
    for line in lines:
        assert len(line.split("\t")) == expected_columns
    assert "ran-ah1" in lines[1]


def test_tsg_list_specific_fields(monkeypatch) -> None:
    runner = CliRunner()

    def fake_list_all(self):
        return _sorted_sample()

    monkeypatch.setattr(TsgService, "list_all", fake_list_all)

    result = runner.invoke(app, ["tsg", "list", "--fields", "short_name,tsg_name"])
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line and not line.startswith("Listing")]
    assert lines == [
        "C1\tCT WG1",
        "RT\tRAN AH1",
        "R1\tRAN WG1",
    ]


def test_tsg_list_invalid_field(monkeypatch) -> None:
    runner = CliRunner()

    def fake_list_all(self):
        return []

    monkeypatch.setattr(TsgService, "list_all", fake_list_all)

    result = runner.invoke(app, ["tsg", "list", "--fields", "bogus"])
    assert result.exit_code != 0
    assert "Unknown field(s)" in result.output


def test_tsg_list_empty_table_hint(monkeypatch) -> None:
    runner = CliRunner()

    def fake_list_all(self):
        return []

    monkeypatch.setattr(TsgService, "list_all", fake_list_all)

    result = runner.invoke(app, ["tsg", "list"])
    assert result.exit_code == 0
    assert "No TSG records found" in result.output
    assert "db init" in result.output


def test_tsg_show_by_short_name(monkeypatch) -> None:
    runner = CliRunner()

    def fake_get_by_short_name(self, short_name):
        return next(
            (t for t in _SAMPLE_TSGS if t.short_name.lower() == short_name.lower()),
            None,
        )

    def fake_get_by_tsg_name(self, tsg_name):
        return next(
            (t for t in _SAMPLE_TSGS if t.tsg_name.lower() == tsg_name.lower()),
            None,
        )

    def fake_known_short_names(self):
        return [t.short_name for t in _sorted_sample()]

    monkeypatch.setattr(TsgService, "get_by_short_name", fake_get_by_short_name)
    monkeypatch.setattr(TsgService, "get_by_tsg_name", fake_get_by_tsg_name)
    monkeypatch.setattr(TsgService, "known_short_names", fake_known_short_names)

    result = runner.invoke(app, ["tsg", "show", "--tsg", "r99"])
    assert result.exit_code != 0
    assert "Unknown TSG 'r99'" in result.output

    result = runner.invoke(app, ["tsg", "show", "--tsg", "rt"])
    assert result.exit_code == 0, result.output
    assert "RAN AH1" in result.output
    assert "ran-ah1" in result.output


def test_tsg_show_by_tsg_name(monkeypatch) -> None:
    runner = CliRunner()

    def fake_get_by_short_name(self, short_name):
        return next(
            (t for t in _SAMPLE_TSGS if t.short_name.lower() == short_name.lower()),
            None,
        )

    def fake_get_by_tsg_name(self, tsg_name):
        return next(
            (t for t in _SAMPLE_TSGS if t.tsg_name.lower() == tsg_name.lower()),
            None,
        )

    monkeypatch.setattr(TsgService, "get_by_short_name", fake_get_by_short_name)
    monkeypatch.setattr(TsgService, "get_by_tsg_name", fake_get_by_tsg_name)

    result = runner.invoke(app, ["tsg", "show", "--tsg", "RAN WG1"])
    assert result.exit_code == 0
    assert "RAN WG1" in result.output
    assert "R1" in result.output
    assert "ran-wg1" in result.output


def test_tsg_seed_calls_service(monkeypatch) -> None:
    runner = CliRunner()
    calls = {"upsert_many": 0}

    class _FakeRepo:
        def upsert_many(self, tsgs):
            calls["upsert_many"] += len(tsgs)
            return len(tsgs)

    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda: None)
    monkeypatch.setattr(
        "doc3gpp.cli.build_tsg_service", lambda: TsgService(_FakeRepo())
    )

    result = runner.invoke(app, ["tsg", "seed"])
    assert result.exit_code == 0
    assert "Seeded 19" in result.output
    assert calls["upsert_many"] == 19
