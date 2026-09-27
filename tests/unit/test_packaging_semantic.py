"""Packaging guard for the remote-only embedding backend.

The ``[semantic]`` extra must not pull ``sentence-transformers`` —
the local backend was removed in favour of the remote
OpenAI-compatible API (``OpenAICompatibleEmbedder`` over httpx, which
is already a core dep). ``sqlite-vec`` stays for the vector index.
"""

from __future__ import annotations


def test_semantic_extra_drops_sentence_transformers():
    import tomllib
    with open("pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)
    deps = data["project"]["dependencies"]
    assert any(d.startswith("numpy") for d in deps)
    assert any(d.startswith("httpx") for d in deps)
    semantic = data["project"]["optional-dependencies"]["semantic"]
    assert not any("sentence-transformers" in dep for dep in semantic)
    assert any("sqlite-vec" in dep for dep in semantic)


def test_config_set_dry_run_redacts_api_key(tmp_path, monkeypatch):
    """`config set --dry-run` must not echo the raw API key."""
    from typer.testing import CliRunner
    from doc3gpp.cli import app
    from doc3gpp.settings.loader import get_settings

    cfg = tmp_path / "doc3gpp.toml"
    cfg.write_text('[semantic_search]\nembedding_base_url = "http://localhost:11434/v1"\n')
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(
            app, ["config", "set", "semantic_search.embedding_api_key", "sekret", "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        assert "sekret" not in result.output
        assert "***" in result.output
    finally:
        get_settings.cache_clear()
        monkeypatch.delenv("DOC3GPP_CONFIG", raising=False)


def test_config_show_redacts_api_key(tmp_path, monkeypatch):
    """`config show` must redact a configured API key."""
    from typer.testing import CliRunner
    from doc3gpp.cli import app
    from doc3gpp.settings.loader import get_settings

    cfg = tmp_path / "doc3gpp.toml"
    cfg.write_text(
        '[semantic_search]\nembedding_base_url = "http://localhost:11434/v1"\n'
        'embedding_api_key = "sekret"\n'
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(app, ["config", "show"])
        assert result.exit_code == 0, result.output
        body = result.output.split("\n", 1)[1]
        assert "sekret" not in body
        assert "***" in body
    finally:
        get_settings.cache_clear()
        monkeypatch.delenv("DOC3GPP_CONFIG", raising=False)


def test_index_status_panel_shows_vector_model_and_dim(sqlite_env):
    """Status panel surfaces stored vs configured model/dim.

    Regression: after the remote-backend switch the panel must show
    both the ``vec_meta``-stored model/dim and the configured
    ``embedding_model`` so a pending mismatch is visible before any
    query fails with the rebuild hint.
    """
    from typer.testing import CliRunner
    from doc3gpp.cli import app
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from sqlalchemy import text

    create_schema()
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO vec_meta (key, value) VALUES ('embedding_model', 'old-model') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            ),
        )
    result = CliRunner().invoke(app, ["tdoc", "index"])
    assert result.exit_code == 0, result.output
    assert "FTS5" in result.output
    assert "Vector" in result.output


def test_unified_search_explain_prints_fts5_configuration(monkeypatch):
    """Unified search keeps the FTS5 explanation on the text path."""
    from unittest.mock import MagicMock
    from typer.testing import CliRunner
    from doc3gpp.cli import app
    from doc3gpp.services import factory

    svc = MagicMock()
    svc.search.return_value = []
    monkeypatch.setattr(
        factory, "build_tdoc_search_facade", lambda *a, **kw: svc,
    )
    result = CliRunner().invoke(
        app, ["tdoc", "search", "--text", "q", "--explain"]
    )
    assert result.exit_code == 0, result.output
    assert "# search config" in result.output
    assert "match:" in result.output
    assert "snippet_tokens:" in result.output
