"""CLI-level tests for the ``wi`` Typer subcommand group."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from doc3gpp.cli import app

FIXTURE = Path("tests/fixtures/wi_pages/R5.html")


def test_wi_sync_validates_tsg(sqlite_env, monkeypatch) -> None:
    """``wi sync --tsg bogus`` should fail with ``typer.BadParameter`` for an unknown TSG."""
    runner = CliRunner()

    # Avoid any actual HTTP call by stubbing the scraper inside the service module.
    import doc3gpp.services.wi_service as wi_service_module
    monkeypatch.setattr(wi_service_module, "fetch_wis", lambda tsg: FIXTURE.read_text(encoding="utf-8"))

    # First seed the TSG reference data.
    init = runner.invoke(app, ["db", "init"])
    assert init.exit_code == 0

    # Unknown short name -> BadParameter.
    bad = runner.invoke(app, ["wi", "sync", "--tsg", "r99"])
    assert bad.exit_code != 0
    assert "Unknown TSG short name 'r99'" in bad.output


def test_wi_sync_stores_rows_and_lists_with_default_fields(sqlite_env, monkeypatch) -> None:
    runner = CliRunner()

    import doc3gpp.services.wi_service as wi_service_module
    monkeypatch.setattr(wi_service_module, "fetch_wis", lambda tsg: FIXTURE.read_text(encoding="utf-8"))

    init = runner.invoke(app, ["db", "init"])
    assert init.exit_code == 0

    sync = runner.invoke(app, ["wi", "sync", "--tsg", "R5"])
    assert sync.exit_code == 0, sync.output
    assert "WI sync complete:" in sync.stdout
    assert " WI rows stored for R5" in sync.stdout

    listing = runner.invoke(app, ["wi", "list", "--limit", "100"])
    assert listing.exit_code == 0, listing.output
    # Default output columns: wi_id, acronym, release, name (tab-separated).
    data_lines = [
        line for line in listing.stdout.splitlines()
        if line and not line.startswith(("Listing", "Stored"))
    ]
    assert data_lines, listing.stdout
    for line in data_lines:
        cells = line.split("\t")
        assert len(cells) == 4, f"expected 4 columns, got {cells!r}"


def test_wi_list_with_tsg_filter(sqlite_env, monkeypatch) -> None:
    runner = CliRunner()

    import doc3gpp.services.wi_service as wi_service_module

    def fake_fetch(tsg: str) -> str:
        # Only R5 has data; S2 is missing.
        if tsg.upper() == "R5":
            return FIXTURE.read_text(encoding="utf-8")
        return "<html><body>No WIs</body></html>"

    monkeypatch.setattr(wi_service_module, "fetch_wis", fake_fetch)

    init = runner.invoke(app, ["db", "init"])
    assert init.exit_code == 0

    sync_r5 = runner.invoke(app, ["wi", "sync", "--tsg", "r5"])
    assert sync_r5.exit_code == 0

    sync_s2 = runner.invoke(app, ["wi", "sync", "--tsg", "S2"])
    assert sync_s2.exit_code == 0

    # Filter by TSG should only return R5 rows.
    listing = runner.invoke(app, ["wi", "list", "--tsg", "r5"])
    assert listing.exit_code == 0
    rows = [line for line in listing.stdout.splitlines() if line and not line.startswith("Listing")]
    assert rows, listing.stdout
    # Each row's 2nd column (acronym) is text; nothing here indicates which TSG, but
    # the listing should not be empty when filtering for a TSG that has WIs.


def test_wi_list_with_acronym_filter(sqlite_env, monkeypatch) -> None:
    runner = CliRunner()

    import doc3gpp.services.wi_service as wi_service_module
    monkeypatch.setattr(wi_service_module, "fetch_wis", lambda tsg: FIXTURE.read_text(encoding="utf-8"))

    assert runner.invoke(app, ["db", "init"]).exit_code == 0
    assert runner.invoke(app, ["wi", "sync", "--tsg", "r5"]).exit_code == 0

    # The fixture's first real WI has acronym NTShar; filter to it.
    res = runner.invoke(app, ["wi", "list", "--acronym", "NTShar"])
    assert res.exit_code == 0
    rows = [line for line in res.stdout.splitlines() if line and not line.startswith("Listing")]
    assert rows
    # All returned rows must carry NTShar in their 2nd column (acronym).
    for line in rows:
        cells = line.split("\t")
        assert "NTShar" == cells[1]


def test_wi_list_with_release_filter(sqlite_env, monkeypatch) -> None:
    runner = CliRunner()

    import doc3gpp.services.wi_service as wi_service_module
    monkeypatch.setattr(wi_service_module, "fetch_wis", lambda tsg: FIXTURE.read_text(encoding="utf-8"))

    assert runner.invoke(app, ["db", "init"]).exit_code == 0
    assert runner.invoke(app, ["wi", "sync", "--tsg", "r5"]).exit_code == 0

    # The fixture includes two Rel-6 WI rows; filtering by "Rel-6" must return at least one.
    res = runner.invoke(app, ["wi", "list", "--release", "Rel-6", "--limit", "5"])
    assert res.exit_code == 0
    rows = [line for line in res.stdout.splitlines() if line and not line.startswith("Listing")]
    assert rows
    for line in rows:
        cells = line.split("\t")
        assert cells[2] == "Rel-6"


def test_wi_list_empty_returns_message(sqlite_env, monkeypatch) -> None:
    runner = CliRunner()

    import doc3gpp.services.wi_service as wi_service_module
    monkeypatch.setattr(wi_service_module, "fetch_wis", lambda tsg: "<html><body>empty</body></html>")

    assert runner.invoke(app, ["db", "init"]).exit_code == 0

    listing = runner.invoke(app, ["wi", "list"])
    assert listing.exit_code == 0
    assert "No WIs found" in listing.stdout
