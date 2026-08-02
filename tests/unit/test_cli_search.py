"""CLI flag-parsing tests for the ``search`` sub-app."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

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
        "--limit", "--format", "--compact", "--sem-query",
        "--snippet-tokens", "--explain", "--quiet",
    ):
        assert flag in result.output, f"missing flag {flag} in search help"
    # The old --rerank flag is gone; the new --sem-query is the
    # only semantic-rerank switch.
    assert "--rerank" not in result.output, (
        "--rerank must be removed from search query help; use --sem-query"
    )


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
        "doc3gpp.services.factory.build_search_service", lambda *a, **kw: stub,
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
        "doc3gpp.services.factory.build_search_service", lambda *a, **kw: fts5_stub,
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
        "doc3gpp.services.factory.build_search_service", lambda *a, **kw: fts5_stub,
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


# ----------------------------------------------------------------------
# Task 6: --sem-query fanout wiring + removed --rerank flag
# ----------------------------------------------------------------------


def test_search_query_no_sem_query_does_not_invoke_reranker(monkeypatch) -> None:
    """Without ``--sem-query`` the CLI bypasses the reranker (today's behaviour)."""
    from doc3gpp.cli import search_app

    runner = CliRunner()
    fake_svc = MagicMock()
    fake_svc._repo.search.return_value = []  # type: ignore[attr-defined]
    fake_svc.status.return_value = MagicMock(is_stale=False)
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_search_service",
        lambda *a, **kw: fake_svc,
    )
    result = runner.invoke(search_app, ["query", "anything"])
    assert result.exit_code == 0, result.output
    fake_svc._reranker.rerank.assert_not_called()  # type: ignore[attr-defined]


def test_search_query_sem_query_invokes_reranker_with_fanout(monkeypatch) -> None:
    """``--sem-query`` triggers fanout filters and a rerank call."""
    from doc3gpp.cli import search_app
    from doc3gpp.models.search import SearchFilters

    class _FakeSvc:
        """Service double with a real ``_quiet`` attribute."""

        def __init__(self) -> None:
            self._reranker = MagicMock()
            self._repo = MagicMock()
            self._repo.search.return_value = []
            self.status = MagicMock(return_value=MagicMock(is_stale=False))
            self._quiet = False

    fake_svc = _FakeSvc()

    captured: dict = {}

    def _capture_rerank(semantic_query, hits, final_limit=None, quiet=False):
        captured["semantic_query"] = semantic_query
        captured["hits"] = hits
        captured["final_limit"] = final_limit
        captured["quiet"] = quiet
        return []

    fake_svc._reranker.rerank.side_effect = _capture_rerank  # type: ignore[attr-defined]

    def _factory(quiet: bool = False):
        fake_svc._quiet = quiet
        return fake_svc

    fake_settings = MagicMock()
    fake_settings.search.search_fanout_factor = 4
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_search_service", _factory,
    )
    monkeypatch.setattr(
        "doc3gpp.cli.get_settings", lambda: fake_settings,
    )

    runner = CliRunner()
    result = runner.invoke(
        search_app, ["query", "R5-1", "--sem-query", "TTCN handover"],
    )
    assert result.exit_code == 0, result.output
    assert captured["semantic_query"] == "TTCN handover"
    assert captured["final_limit"] == 20  # default --limit
    # Default --quiet=False: the rerank must see quiet=False so the
    # empty-vector warning can fire if it ever trips.
    assert captured["quiet"] is False
    # The repo was called with filters whose limit == 20 * 4 == 80.
    call_args = fake_svc._repo.search.call_args  # type: ignore[attr-defined]
    filters = call_args[0][1]
    assert isinstance(filters, SearchFilters)
    assert filters.limit == 80


def test_search_query_quiet_flag_reaches_reranker(monkeypatch) -> None:
    """``--quiet`` on ``search query`` must reach the reranker as ``quiet=True``.

    Pins the CLI → factory → reranker plumbing for the empty-vector
    warning gate introduced in the final review. Without ``--quiet``
    the default is ``False``; with ``--quiet`` the reranker sees
    ``quiet=True`` and skips the one-shot WARNING.
    """
    from doc3gpp.cli import search_app

    class _FakeSvc:
        """Bare-bones service double with real ``_quiet`` attribute.

        A :class:`MagicMock` is unsuitable because the CLI reads
        ``svc._quiet`` (a real ``bool``), not a method call; on a
        Mock the attribute would be another child Mock and would
        compare truthy regardless of the input flag.
        """

        def __init__(self) -> None:
            self._reranker = MagicMock()
            self._repo = MagicMock()
            self._repo.search.return_value = []
            self.status = MagicMock(return_value=MagicMock(is_stale=False))
            self._quiet = False  # mutated per-test

    fake_svc = _FakeSvc()

    captured: dict = {}

    def _capture_rerank(
        semantic_query, hits, final_limit=None, quiet=False,
    ):
        captured["semantic_query"] = semantic_query
        captured["final_limit"] = final_limit
        captured["quiet"] = quiet
        return []

    fake_svc._reranker.rerank.side_effect = _capture_rerank  # type: ignore[attr-defined]

    def _factory(quiet: bool = False):
        fake_svc._quiet = quiet
        return fake_svc

    fake_settings = MagicMock()
    fake_settings.search.search_fanout_factor = 4
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_search_service", _factory,
    )
    monkeypatch.setattr(
        "doc3gpp.cli.get_settings", lambda: fake_settings,
    )

    result = CliRunner().invoke(
        search_app,
        ["query", "R5-1", "--sem-query", "TTCN", "--quiet"],
    )
    assert result.exit_code == 0, result.output
    assert captured["quiet"] is True

    # The default (no --quiet) must thread quiet=False so existing
    # callers see no behaviour change.
    captured.clear()
    result_default = CliRunner().invoke(
        search_app, ["query", "R5-1", "--sem-query", "TTCN"],
    )
    assert result_default.exit_code == 0, result_default.output
    assert captured["quiet"] is False


def test_search_query_sem_query_empty_string_treated_as_none(monkeypatch) -> None:
    """``--sem-query ''`` is a no-op (no rerank, no embedder call)."""
    from doc3gpp.cli import search_app

    fake_svc = MagicMock()
    fake_svc._repo.search.return_value = []  # type: ignore[attr-defined]
    fake_svc.status.return_value = MagicMock(is_stale=False)
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_search_service",
        lambda *a, **kw: fake_svc,
    )
    runner = CliRunner()
    result = runner.invoke(search_app, ["query", "R5-1", "--sem-query", ""])
    assert result.exit_code == 0, result.output
    fake_svc._reranker.rerank.assert_not_called()  # type: ignore[attr-defined]


def test_search_query_rerank_flag_raises_bad_parameter() -> None:
    """The removed ``--rerank`` flag must raise a clear migration error."""
    from doc3gpp.cli import search_app

    runner = CliRunner()
    result = runner.invoke(search_app, ["query", "R5-1", "--rerank"])
    assert result.exit_code != 0
    assert "--rerank" in (result.output + (result.stderr or ""))
