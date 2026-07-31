"""Tests for the FTS5 search settings (``SearchSettings``)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from doc3gpp.settings.schema import SearchSettings


def test_bm25_weights_default_is_eight_floats() -> None:
    """``bm25_weights`` defaults to 8 floats matching the 8 FTS5 columns."""
    settings = SearchSettings()
    assert settings.bm25_weights == (5.0, 0.0, 0.0, 1.0, 5.0, 5.0, 5.0, 5.0)
    assert len(settings.bm25_weights) == 8
    assert all(isinstance(w, float) for w in settings.bm25_weights)


def test_bm25_weights_rejects_wrong_length() -> None:
    """``bm25_weights`` must have exactly 8 elements (one per FTS5 column)."""
    with pytest.raises(ValidationError):
        SearchSettings(bm25_weights=(1.0, 2.0, 3.0))
    with pytest.raises(ValidationError):
        SearchSettings(
            bm25_weights=(1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0)
        )
