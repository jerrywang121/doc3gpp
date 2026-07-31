"""CLI flag-parsing tests for the ``search sem`` sub-command."""

from __future__ import annotations

from typer.testing import CliRunner

from doc3gpp.cli import app

runner = CliRunner()


def test_search_sem_rejects_negative_limit():
    result = runner.invoke(app, ["search", "sem", "q", "--limit", "-1"])
    assert result.exit_code != 0


def test_search_sem_rejects_vector_weight_out_of_range():
    result = runner.invoke(app, ["search", "sem", "q", "--vector-weight", "1.5"])
    assert result.exit_code != 0
    result = runner.invoke(app, ["search", "sem", "q", "--vector-weight", "-0.1"])
    assert result.exit_code != 0


def test_search_sem_unavailable_when_disabled(monkeypatch):
    from doc3gpp.services import factory
    monkeypatch.setattr(factory, "build_semantic_search_service", lambda *a, **kw: None)
    result = runner.invoke(app, ["search", "sem", "q"])
    assert "unavailable" in result.output.lower() or result.exit_code == 1


def test_search_sem_query_error_exit_2(monkeypatch):
    from unittest.mock import MagicMock
    from doc3gpp.models.semantic_search import SemanticSearchQueryError
    svc = MagicMock()
    svc.search.side_effect = SemanticSearchQueryError("empty after strip")
    from doc3gpp.services import factory
    monkeypatch.setattr(factory, "build_semantic_search_service", lambda *a, **kw: svc)
    result = runner.invoke(app, ["search", "sem", "   "])
    assert result.exit_code == 2
