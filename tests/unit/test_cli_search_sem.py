"""CLI flag-parsing tests for the ``search sem`` sub-command."""

from __future__ import annotations

from typer.testing import CliRunner

from doc3gpp.cli import app

runner = CliRunner()


def test_search_sem_rejects_negative_limit():
    result = runner.invoke(app, ["search", "sem", "q", "--limit", "-1"])
    assert result.exit_code != 0


def test_search_sem_rejects_fts5_weight_out_of_range():
    result = runner.invoke(app, ["search", "sem", "q", "--fts5-weight", "1.5"])
    assert result.exit_code != 0
    result = runner.invoke(app, ["search", "sem", "q", "--fts5-weight", "-0.1"])
    assert result.exit_code != 0


def test_search_sem_accepts_fts5_query_flag():
    """--fts5-query is optional; the CLI must pass it through to the service."""
    from unittest.mock import MagicMock
    import pytest

    from doc3gpp.services import factory

    svc = MagicMock()
    svc.search.return_value = []
    mp = pytest.MonkeyPatch()
    mp.setattr(factory, "build_semantic_search_service", lambda *a, **kw: svc)
    try:
        runner.invoke(
            app,
            ["search", "sem", "natural prose", "--fts5-query", "tsg:RP spec:38.300"],
        )
        assert svc.search.called
        call_kwargs = svc.search.call_args.kwargs
        assert call_kwargs["fts5_query"] == "tsg:RP spec:38.300"
        assert call_kwargs["fts5_weight"] == 0.5
    finally:
        mp.undo()


def test_search_sem_defaults_fts5_query_to_none():
    """When --fts5-query is omitted, the CLI passes None to the service."""
    from unittest.mock import MagicMock
    import pytest

    from doc3gpp.services import factory

    svc = MagicMock()
    svc.search.return_value = []
    mp = pytest.MonkeyPatch()
    mp.setattr(factory, "build_semantic_search_service", lambda *a, **kw: svc)
    try:
        runner.invoke(app, ["search", "sem", "natural prose"])
        assert svc.search.called
        assert svc.search.call_args.kwargs["fts5_query"] is None
    finally:
        mp.undo()


def test_search_sem_unavailable_when_disabled(monkeypatch):
    from doc3gpp.services import factory
    monkeypatch.setattr(factory, "build_semantic_search_service", lambda *a, **kw: None)
    result = runner.invoke(app, ["search", "sem", "q"])
    assert "unavailable" in result.output.lower() or result.exit_code == 1


def test_search_sem_query_error_exit_2(monkeypatch):
    from unittest.mock import MagicMock
    from doc3gpp.models.semantic_search import SemanticSearchQueryError
    svc = MagicMock()
    svc.search.side_effect = SemanticSearchQueryError("query empty")
    from doc3gpp.services import factory
    monkeypatch.setattr(factory, "build_semantic_search_service", lambda *a, **kw: svc)
    result = runner.invoke(app, ["search", "sem", "   "])
    assert result.exit_code == 2
