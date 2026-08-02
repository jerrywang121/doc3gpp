"""Graceful degradation when FTS5 is missing or disabled.

Two scenarios, each must end with the system behaving as if search
were unavailable — never crashing, never surfacing an internal
traceback:

1. ``Settings.search.enabled = False`` → ``build_search_service``
   returns ``None``.
2. Build-but-don't-register flow → repo raises
   ``SearchUnavailableError`` which the factory converts to ``None``.
"""

from __future__ import annotations

from unittest.mock import patch

from doc3gpp.models.search import SearchUnavailableError
from doc3gpp.services.factory import build_search_service
from doc3gpp.settings.schema import SearchSettings, Settings


def test_disabled_returns_none() -> None:
    settings = Settings(search=SearchSettings(enabled=False))
    assert build_search_service(settings) is None


def test_unavailable_error_returns_none() -> None:
    settings = Settings()
    with patch(
        "doc3gpp.services.factory.SQLAlchemySearchIndexRepository",
        side_effect=SearchUnavailableError("nope"),
    ):
        assert build_search_service(settings) is None
