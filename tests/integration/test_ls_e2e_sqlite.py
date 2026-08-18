"""End-to-end LS: service-level parse → CLI `tdoc show` returns the ls block.

The plan's original brief invoked ``doc3gpp tdoc parse --from-path
tests/fixtures/ls/LS_sample_r5_240001.md`` via subprocess; the CLI has no
``--config`` flag, ``--from-path`` rejects ``.md``, and the parse must
not write the ``tdocs`` table (rows come via auto-sync). Per the
adjudicated Task 15 rulings the parse step runs at the service layer in
raw-markdown mode (auto-sync outcome simulated by seeding the tdocs
row) and only the show step goes through the CLI.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.tdoc import TDoc
from doc3gpp.services.factory import build_tdoc_cr_service
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.tdoc_cr_ls_sql import SQLAlchemyLSParserRepository
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

Runner = CliRunner()

_FIXTURE = Path("tests/fixtures/ls/LS_sample_r5_240001.md")
_FIXTURE_URL = "tsg/ls/R5-240001.doc"


def test_end_to_end_ls_parse_and_show(sqlite_env, monkeypatch) -> None:
    # Auto-sync stays disabled so the CLI show step never hits the
    # network (the tdocs row is seeded directly, simulating the
    # auto-sync outcome — see the Blocker-5 ruling).
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "false")
    from doc3gpp.settings.loader import get_settings

    get_settings.cache_clear()

    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(
            tdoc_id="R5-240001",
            type="LS",
            ftp_url=_FIXTURE_URL,
            title="LS on 5G_eHealth WI status update",
            source="3GPP TSG RAN WG2",
        )
    )

    result = build_tdoc_cr_service().extract_from_bytes(
        _FIXTURE.read_bytes(),
        tdoc_id="R5-240001",
        ftp_url=_FIXTURE_URL,
        tdoc_type="LS",
        source="3GPP TSG RAN WG2",
    )
    assert result.details is None  # LS rows have no CR details

    ls_row = SQLAlchemyLSParserRepository().get_by_url(_FIXTURE_URL)
    assert ls_row is not None
    assert ls_row.variant == "3gpp"
    assert ls_row.title == "LS on 5G_eHealth WI status update"

    show = Runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5-240001", "--format", "json"]
    )
    assert show.exit_code == 0, show.output
    assert '"ls"' in show.output
    assert '"5G_eHealth"' in show.output


def test_ls_show_omitted_when_sidecar_missing(sqlite_env, monkeypatch) -> None:
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "false")
    from doc3gpp.settings.loader import get_settings

    get_settings.cache_clear()

    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(
            tdoc_id="R5-240001",
            type="LS",
            ftp_url=_FIXTURE_URL,
            title="LS on 5G_eHealth WI status update",
        )
    )

    show = Runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5-240001", "--format", "json"]
    )
    assert show.exit_code == 0, show.output
    assert '"ls"' not in show.output
