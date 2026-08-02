from __future__ import annotations

import pytest

from doc3gpp.settings.schema import SemanticSearchSettings, Settings


def test_defaults():
    s = SemanticSearchSettings()
    assert s.enabled is True
    assert s.auto_embed_on_parse is True
    assert s.embedding_model == "sentence-transformers/all-MiniLM-L6-v2"
    assert s.chunk_size == 200
    assert s.chunk_overlap == 20
    assert s.rrf_k == 60
    assert s.fts5_weight == 0.5
    assert s.fanout_multiplier == 4
    assert s.max_chunks_per_tdoc == 8


def test_chunk_size_must_be_positive():
    with pytest.raises(Exception):
        SemanticSearchSettings(chunk_size=0)
    with pytest.raises(Exception):
        SemanticSearchSettings(chunk_size=-1)


def test_chunk_overlap_must_be_less_than_size():
    with pytest.raises(Exception):
        SemanticSearchSettings(chunk_size=800, chunk_overlap=800)
    with pytest.raises(Exception):
        SemanticSearchSettings(chunk_size=800, chunk_overlap=801)


def test_fts5_weight_range():
    SemanticSearchSettings(fts5_weight=0.0)
    SemanticSearchSettings(fts5_weight=1.0)
    with pytest.raises(Exception):
        SemanticSearchSettings(fts5_weight=-0.1)
    with pytest.raises(Exception):
        SemanticSearchSettings(fts5_weight=1.5)


def test_fanout_multiplier_at_least_one():
    SemanticSearchSettings(fanout_multiplier=1)
    with pytest.raises(Exception):
        SemanticSearchSettings(fanout_multiplier=0)


def test_rrf_k_positive():
    with pytest.raises(Exception):
        SemanticSearchSettings(rrf_k=0)


def test_settings_has_semantic_search_field():
    s = Settings()
    assert isinstance(s.semantic_search, SemanticSearchSettings)
