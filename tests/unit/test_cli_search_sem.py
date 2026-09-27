"""CLI surface tests for the unified semantic search option."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.unified_search import SearchMode, TDocSearchResult

runner = CliRunner()


def _result() -> TDocSearchResult:
    return TDocSearchResult(
        tdoc_id="R5-1",
        score=0.12,
        search_mode=SearchMode.SEMANTIC,
        previews=None,
        title="handover",
        meeting=None,
        tsg=None,
        uploaded_date=None,
        ftp_url=None,
        wis=None,
        type="CR",
        status="Agreed",
        best_chunk_id="R5-1#0",
    )


def test_unified_tdoc_search_accepts_semantic_option(monkeypatch):
    facade = MagicMock()
    facade.search.return_value = [_result()]
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: SimpleNamespace(
        log_level="INFO",
        output=SimpleNamespace(format="json", compact=False),
        search=SimpleNamespace(enabled=True, bm25_weights=()),
        semantic_search=SimpleNamespace(fts5_weight=0.5),
    ))
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_tdoc_search_facade",
        lambda: facade,
    )

    result = runner.invoke(
        app,
        ["tdoc", "search", "--semantic", "handover", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    assert facade.search.call_args.kwargs["semantic"] == "handover"
    assert facade.search.call_args.kwargs["text"] is None


def test_removed_semantic_flags_are_rejected():
    for args in (
        ["tdoc", "search", "--fts5-query", "handover"],
        ["tdoc", "search", "--fts5-weight", "0.5"],
        ["tdoc", "search", "--sem-query", "handover"],
        ["tdoc", "search", "sem", "handover"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code != 0, (args, result.output)


def test_whitespace_semantic_input_is_allowed_as_filter_mode(monkeypatch):
    facade = MagicMock()
    facade.search.return_value = []
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: SimpleNamespace(
        log_level="INFO",
        output=SimpleNamespace(format="json", compact=False),
        search=SimpleNamespace(enabled=True, bm25_weights=()),
        semantic_search=SimpleNamespace(fts5_weight=0.5),
    ))
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_tdoc_search_facade",
        lambda: facade,
    )

    result = runner.invoke(
        app,
        ["tdoc", "search", "--semantic", "   ", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    assert facade.search.call_args.kwargs["semantic"] == "   "
