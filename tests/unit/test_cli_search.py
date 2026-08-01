"""CLI flag-parsing tests for the ``search`` sub-app."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from typer.testing import CliRunner

from doc3gpp.cli import app


def test_search_help_lists_filters() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["search", "query", "--help"])
    assert result.exit_code == 0
    for flag in (
        "--tsg", "--meeting", "--meeting-id", "--tdoc-id",
        "--release", "--spec", "--since", "--until",
        "--limit", "--format", "--compact", "--rerank",
        "--snippet-tokens", "--explain", "--quiet",
    ):
        assert flag in result.output, f"missing flag {flag} in search help"


def test_index_help_lists_rebuild_flags() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["search", "index", "--help"])
    assert result.exit_code == 0
    for flag in ("--rebuild", "--batch", "--resume", "--stale-only", "--quiet"):
        assert flag in result.output, f"missing flag {flag} in index help"


# ----------------------------------------------------------------------
# Task 3: --explain wire-up + per-call snippet_tokens override
# ----------------------------------------------------------------------


class _CapturingRepo:
    """In-memory SearchIndexRepository double for the explain test.

    The repo records the args passed to :meth:`search` so the test can
    assert the per-call ``snippet_tokens`` override. The real
    FTS5-backed repo would require a sqlite + FTS5 build; the explain
    test only cares about the CLI's stderr block, not the actual
    query plan, so the mock is sufficient.
    """

    def __init__(self) -> None:
        self.search_calls: list[dict[str, Any]] = []
        # The CLI's ``_emit_explain`` reads the cached config the
        # repo will use at search-time. Mirror the real repo's
        # attributes so the explain block is populated.
        self._weights = (5.0, 0.0, 0.0, 1.0, 5.0, 5.0, 5.0, 5.0)

    def search(self, query: str, filters, snippet_tokens: int | None = None):
        self.search_calls.append(
            {"query": query, "filters": filters, "snippet_tokens": snippet_tokens}
        )
        return []


class _StubService:
    """SearchService double that exposes the same internal surface as the real one."""

    def __init__(self) -> None:
        self._repo = _CapturingRepo()
        from doc3gpp.services.search_service import PassthroughReranker
        self._reranker = PassthroughReranker()
        from doc3gpp.models.search import SearchIndexStatus
        self._status = SearchIndexStatus(
            enabled=True,
            row_count=0,
            last_rebuild_at=None,
            last_indexed_uploaded_date=None,
            latest_tdocs_uploaded_date=None,
            is_stale=False,
        )

    def status(self):
        return self._status


def test_explain_prints_match_and_weights(monkeypatch) -> None:
    """``--explain`` prints the resolved MATCH + weight vector to stderr.

    The block is also visible on stdout (CliRunner mixes streams by
    default) so the test asserts against ``result.output``. The
    expected format is the one in the perf spec:

    ::

        # search config
        match:           "alpha"
        snippet_tokens:  8
        bm25_weights:    [5.0, 0.0, 0.0, 1.0, 5.0, 5.0, 5.0, 5.0]
    """
    runner = CliRunner()
    stub = _StubService()
    # The CLI imports ``build_search_service`` lazily inside
    # ``search_command``; monkeypatch the resolved import site.
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_search_service", lambda: stub,
    )
    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda: None)

    result = runner.invoke(
        app, ["search", "query", "alpha", "--explain", "--format", "json"]
    )
    assert result.exit_code == 0, result.output

    assert "# search config" in result.output, (
        f"missing explain header; output was:\n{result.output}"
    )
    assert "bm25_weights:" in result.output
    # Resolved MATCH expression: the SearchQueryBuilder wraps plain
    # text in double quotes after normalizing.
    assert 'match:           "alpha"' in result.output
    assert "snippet_tokens:  8" in result.output
    assert "[5.0, 0.0, 0.0, 1.0, 5.0, 5.0, 5.0, 5.0]" in result.output


def test_snippet_tokens_overrides_setting(
    tmp_path: Path, monkeypatch, sqlite_env,
) -> None:
    """``--snippet-tokens 4`` overrides ``Settings.search.snippet_tokens=16``.

    Captures the SQL via a ``before_cursor_execute`` event hook and
    asserts the bound ``:tok`` parameter is ``4`` (not ``16``). The
    override must flow from the CLI through the repo's per-call
    parameter; the cached setting value on ``self._snippet_tokens``
    is bypassed.
    """
    from doc3gpp.settings.loader import get_settings
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.search_sql import (
        SQLAlchemySearchIndexRepository,
    )

    # Set Settings.search.snippet_tokens=16 via TOML (env vars can't
    # override search.* keys because they're not in ALLOWED_ENV_VARS).
    config_path = tmp_path / "doc3gpp.toml"
    config_path.write_text(
        "[search]\nsnippet_tokens = 16\n", encoding="utf-8"
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    get_settings.cache_clear()
    try:
        assert get_settings().search.snippet_tokens == 16

        # Seed the FTS5 index so the repo's search() actually runs.
        create_schema()
        with get_engine().begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tsgs (tsg_name, short_name, description) "
                    "VALUES ('TSG RAN', 'RAN', 'Radio Access Network')"
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO meetings (
                        meeting_id, name, title, location, tsg, start_date,
                        end_date, ftp_url, tdoc_list_last_sync
                    ) VALUES (
                        1, 'RAN#1', 'RAN#1 plenary', 'Online', 'RAN',
                        '2026-01-01', '2026-01-05',
                        'https://www.3gpp.org/ftp/meetings/RAN_1',
                        '2026-01-05T00:00:00'
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO tdocs (
                        tdoc_id, meeting_id, title, ftp_url, type, source,
                        uploaded_date, release, spec
                    ) VALUES (
                        'R5-1000001', 1, 'alpha body', 'https://x/A.zip',
                        'CR', 'TSG', '2026-01-02T00:00:00', 'Rel-17', '38.300'
                    )
                    """
                )
            )
        repo = SQLAlchemySearchIndexRepository()
        repo.upsert("R5-1000001")
        # Sanity: the cached setting value is 16.
        assert repo._snippet_tokens == 16

        captured: list[tuple[str, tuple]] = []
        engine = get_engine()

        def _before_cursor_execute(
            conn, cursor, statement, parameters, context, executemany,
        ):
            captured.append((statement, parameters))

        event.listen(engine, "before_cursor_execute", _before_cursor_execute)
        try:
            result = CliRunner().invoke(
                app,
                [
                    "search", "query", "alpha",
                    "--snippet-tokens", "4",
                    "--format", "json",
                ],
            )
        finally:
            event.remove(engine, "before_cursor_execute", _before_cursor_execute)

        assert result.exit_code == 0, result.output
        matched = [
            (stmt, params) for stmt, params in captured
            if "snippet(" in stmt and "MATCH" in stmt
        ]
        assert matched, (
            f"expected a SELECT against tdoc_search with snippet(); "
            f"saw {len(captured)} cursor events\nall events:\n{captured}"
        )
        stmt, params = matched[-1]
        # The :tok bound param is the 10th positional parameter
        # (w0..w7, col_idx, tok). The override is 4, the cached
        # setting value was 16 — the override must have flowed
        # through to the SQL.
        assert "snippet(tdoc_search, " in stmt
        assert params[9] == 4, (
            f"expected snippet_tokens override=4 at params[9]; "
            f"got {params[9]!r} (cached setting was 16)"
        )
    finally:
        get_settings.cache_clear()


# ----------------------------------------------------------------------
# Status panel: `doc3gpp search index` (no args) must surface the
# vector row count when the semantic service is available, and
# must clearly label the existing FTS5 row count.
# ----------------------------------------------------------------------


def test_search_index_status_panel_includes_vector_rows(monkeypatch) -> None:
    """When the semantic service is available, the status panel
    must include the vector row count alongside the FTS5 row count.
    Otherwise operators see "Rows indexed: 13,693" and assume it
    covers both indexes — but vec_tdoc_embeddings can be empty
    after a fresh schema.
    """
    from dataclasses import replace

    runner = CliRunner()
    fts5_stub = _StubService()
    fts5_stub._status = replace(
        fts5_stub.status(),
        row_count=13_693,
        last_rebuild_at="2026-07-31 23:01:53",
        is_stale=False,
    )

    class _VecStub:
        def __init__(self) -> None:
            from doc3gpp.models.search import SearchIndexStatus
            self._status = SearchIndexStatus(
                enabled=True,
                row_count=3_842,
                last_rebuild_at="2026-07-31 22:50:15",
                last_indexed_uploaded_date="2026-05-08",
                latest_tdocs_uploaded_date="2026-07-22",
                is_stale=True,
            )

        def status(self):
            return self._status

    sem_stub = _VecStub()

    monkeypatch.setattr(
        "doc3gpp.services.factory.build_search_service", lambda: fts5_stub,
    )
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_semantic_search_service",
        lambda: sem_stub,
    )
    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda: None)

    result = runner.invoke(app, ["search", "index"])
    assert result.exit_code == 0, result.output
    assert "FTS5 rows:" in result.output, (
        f"missing FTS5 row label in panel; output was:\n{result.output}"
    )
    assert "13,693" in result.output
    assert "Vector rows:" in result.output, (
        f"missing vector row label in panel; output was:\n{result.output}"
    )
    assert "3,842" in result.output


def test_search_index_status_panel_omits_vector_when_service_none(monkeypatch) -> None:
    """When the semantic service is unavailable (no [semantic]
    extra installed, sqlite-vec missing, etc.), the panel must
    not print a Vector rows line. FTS5 still prints.
    """
    from dataclasses import replace

    runner = CliRunner()
    fts5_stub = _StubService()
    fts5_stub._status = replace(fts5_stub.status(), row_count=100)

    monkeypatch.setattr(
        "doc3gpp.services.factory.build_search_service", lambda: fts5_stub,
    )
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_semantic_search_service",
        lambda: None,
    )
    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda: None)

    result = runner.invoke(app, ["search", "index"])
    assert result.exit_code == 0, result.output
    assert "FTS5 rows:" in result.output
    assert "100" in result.output
    assert "Vector rows" not in result.output
