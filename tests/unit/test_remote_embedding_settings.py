from __future__ import annotations


def test_remote_embedding_settings_defaults():
    from doc3gpp.settings.schema import SemanticSearchSettings
    s = SemanticSearchSettings()
    assert s.embedding_model == "embeddinggemma:300m"
    assert s.embedding_base_url is None
    assert s.embedding_api_key is None
    assert s.embedding_timeout_s == 30.0
    assert s.embedding_batch_size == 32


def test_remote_embedding_settings_validation():
    import pytest
    from doc3gpp.settings.schema import SemanticSearchSettings
    SemanticSearchSettings(embedding_timeout_s=1.0, embedding_batch_size=1)
    with pytest.raises(Exception):
        SemanticSearchSettings(embedding_timeout_s=0.0)
    with pytest.raises(Exception):
        SemanticSearchSettings(embedding_timeout_s=301.0)
    with pytest.raises(Exception):
        SemanticSearchSettings(embedding_batch_size=0)
    with pytest.raises(Exception):
        SemanticSearchSettings(embedding_batch_size=513)


def test_embedding_api_key_env_is_allowlisted():
    from doc3gpp.settings.schema import ALLOWED_ENV_VARS, env_var_for_dotted_key
    assert "DOC3GPP_SEMANTIC_SEARCH__EMBEDDING_API_KEY" in ALLOWED_ENV_VARS
    assert env_var_for_dotted_key("semantic_search.embedding_api_key") == (
        "DOC3GPP_SEMANTIC_SEARCH__EMBEDDING_API_KEY"
    )


def test_embedding_api_key_env_populates_settings(monkeypatch):
    from doc3gpp.settings.loader import get_settings

    monkeypatch.setenv("DOC3GPP_SEMANTIC_SEARCH__EMBEDDING_API_KEY", "env-sekret")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.semantic_search.embedding_api_key == "env-sekret"
    finally:
        get_settings.cache_clear()
        monkeypatch.delenv("DOC3GPP_SEMANTIC_SEARCH__EMBEDDING_API_KEY", raising=False)


def test_embedding_api_key_env_beats_toml(tmp_path, monkeypatch):
    from doc3gpp.settings.loader import get_settings

    cfg = tmp_path / "doc3gpp.toml"
    cfg.write_text(
        '[semantic_search]\nembedding_base_url = "http://localhost:11434/v1"\n'
        'embedding_api_key = "toml-sekret"\n'
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setenv("DOC3GPP_SEMANTIC_SEARCH__EMBEDDING_API_KEY", "env-sekret")
    get_settings.cache_clear()
    try:
        assert get_settings().semantic_search.embedding_api_key == "env-sekret"
    finally:
        get_settings.cache_clear()
        monkeypatch.delenv("DOC3GPP_CONFIG", raising=False)
        monkeypatch.delenv("DOC3GPP_SEMANTIC_SEARCH__EMBEDDING_API_KEY", raising=False)
