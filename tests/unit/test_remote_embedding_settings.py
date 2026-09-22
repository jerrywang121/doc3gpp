from __future__ import annotations


def test_remote_embedding_settings_defaults():
    from doc3gpp.settings.schema import SemanticSearchSettings
    s = SemanticSearchSettings()
    assert s.embedding_model == "nomic-embed-text"
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
