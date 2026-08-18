"""Regression test for ``tdoc parse --from-url`` against an uninitialized DB.

Pins the ``OperationalError`` guard around the ``--from-url`` direct-parse
auto-sync call. The ``--from-url`` path fires
``trigger_auto_sync(tdoc_ids=candidates)`` before delegating to
``_tdoc_parse_direct``; on a fresh sqlite file with no schema, the
candidate resolution (which calls ``meeting_service.list_recent`` /
``get_by_id``) raises ``sqlalchemy.exc.OperationalError`` because the
``meetings`` / ``tdocs`` tables don't exist yet.

The CLI must NOT propagate that as a Python traceback. Per the Blocker-7
contract applied to ``_resolve_direct_tdoc_type`` (see
``test_cli_direct_mode_ls_dispatch.py::test_direct_type_db_absent_assumes_cr``),
the parse helper should warn and continue — the operator gets a clean
exit (0 on success, 1 on parse/network failure) with no raw ``OperationalError``
in the rendered output, and the DB file is left untouched (not corrupted).
"""

from __future__ import annotations

from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.config import get_settings
from doc3gpp.storage.db.session import get_engine


def test_tdoc_parse_from_url_does_not_crash_on_uninitialized_db(
    sqlite_env, monkeypatch
) -> None:
    """``--from-url`` with auto_sync must not surface an ``OperationalError``.

    No ``create_schema()`` is called — the engine points at an empty
    sqlite file. ``Settings.sync.auto_sync`` is enabled so the auto-sync
    branch fires. The 3GPP-zip URL yields a single tdoc_id candidate,
    which drives ``trigger_auto_sync`` into the uninitialised-DB path.
    """
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "true")
    get_settings.cache_clear()

    # Sanity: the test env should NOT have a schema on disk. The fixture
    # provides a fresh sqlite file; if a prior test ran ``create_schema()``,
    # that would mask the bug we're guarding against.
    from sqlalchemy import inspect

    with get_engine().connect() as conn:
        inspector = inspect(conn)
        assert "meetings" not in inspector.get_table_names()
        assert "tdocs" not in inspector.get_table_names()

    # Stub the network-touching service so the test never sees the internet
    # and never has to wait for a real download / parse failure.
    from doc3gpp.models.tdoc_cr import DirectParseResult, TDocCRDetails

    class _StubCrService:
        def extract_from_url(self, *args, **kwargs) -> DirectParseResult:
            return DirectParseResult(
                source_kind="url-3gpp",
                markdown="",
                details=TDocCRDetails(tdoc_id="R5-240001"),
                extract_meta=None,
                from_cache=False,
                persisted=False,
                tdoc_id="R5-240001",
                tdoc_id_in_tdocs=False,
                source_url="https://www.3gpp.org/ftp/tsg_sa/TSG_SA/TSGS_99/SA_99/LS_IN/LS_R5-240001.zip",
            )

        def collect_3gpp_file_urls(self, *args, **kwargs) -> list[str]:
            return []

    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_cr_service",
        lambda **kwargs: _StubCrService(),
    )

    result = CliRunner().invoke(
        app,
        [
            "tdoc", "parse",
            "--from-url",
            "https://www.3gpp.org/ftp/tsg_sa/TSG_SA/TSGS_99/SA_99/LS_IN/LS_R5-240001.zip",
        ],
    )

    assert result.exit_code in (0, 1), (
        f"expected exit 0 or 1 (no Python traceback); got {result.exit_code}.\n"
        f"output: {result.output}"
    )
    assert "Traceback" not in result.output
    assert "OperationalError" not in result.output

    # The DB file must not be corrupted. ``sqlite3.connect`` is the cheapest
    # integrity probe — a corrupted file raises ``sqlite3.DatabaseError``.
    import sqlite3

    con = sqlite3.connect(sqlite_env)
    try:
        cur = con.execute("PRAGMA integrity_check")
        row = cur.fetchone()
        assert row and row[0] == "ok", f"sqlite integrity_check failed: {row!r}"
    finally:
        con.close()
