"""Graceful degradation when the semantic stack is disabled or unavailable.

Three scenarios, each must end with the factory returning ``None`` —
never raising, never surfacing an internal traceback:

1. ``Settings.semantic_search.enabled = False`` →
   :func:`build_semantic_search_service` returns ``None``.
2. :class:`SQLAlchemyVectorIndexRepository` raises
   :class:`VectorIndexUnavailableError` (simulates the sqlite-vec
   extension being absent) → factory returns ``None``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from doc3gpp.models.semantic_search import VectorIndexUnavailableError
from doc3gpp.services.factory import build_semantic_search_service
from doc3gpp.settings.schema import SemanticSearchSettings, Settings


pytestmark = pytest.mark.semantic


def test_semantic_disabled_in_settings(sqlite_env) -> None:
    settings = Settings(semantic_search=SemanticSearchSettings(enabled=False))
    assert build_semantic_search_service(settings) is None


def test_semantic_returns_none_when_sqlite_vec_missing(sqlite_env) -> None:
    settings = Settings()
    with patch(
        "doc3gpp.storage.repositories.vector_sql.SQLAlchemyVectorIndexRepository",
        side_effect=VectorIndexUnavailableError("no sqlite-vec"),
    ):
        assert build_semantic_search_service(settings) is None


# Guard against the brief's "monkeypatch.setitem(sys.modules, 'sqlite_vec', None)"
# formula: assigning ``None`` into sys.modules makes ``import sqlite_vec``
# raise ImportError as if the package were uninstalled, which yields
# ``ModuleNotFoundError`` → ``SQLAlchemyVectorIndexRepository`` raises
# ``VectorIndexUnavailableError`` (the exact branch the factory catches).
@pytest.mark.parametrize("missing_target", ["sqlite_vec"])
def test_semantic_returns_none_when_sqlite_vec_moduled_missing(
    sqlite_env, monkeypatch, missing_target: str,
) -> None:
    monkeypatch.setitem(__import__("sys").modules, missing_target, None)
    settings = Settings()
    assert build_semantic_search_service(settings) is None
