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
    result = CliRunner().invoke(app, ["search", "index"])
    assert result.exit_code == 0, result.output
    assert "Vector model:" in result.output
    assert "old-model" in result.output
    assert "Vector dim:" in result.output


def test_sem_explain_prints_embedding_and_stored_model(monkeypatch):
    """`search sem --explain` surfaces configured + stored model/dim.

    Operators diagnosing a dim/model mismatch need both sides without
    triggering a network probe: configured values from settings, stored
    values from the repo snapshot. The API key must never appear.
    """
    from unittest.mock import MagicMock
    from typer.testing import CliRunner
    from doc3gpp.cli import app
    from doc3gpp.services import factory

    svc = MagicMock()
    svc.search.return_value = []
    svc._settings.semantic_search.rrf_k = 60
    svc._settings.semantic_search.fanout_multiplier = 4
    svc._vec._stored_model = "old-model"
    svc._vec._dim = 384
    monkeypatch.setattr(
        factory, "build_semantic_search_service", lambda *a, **kw: svc,
    )
    result = CliRunner().invoke(app, ["search", "sem", "q", "--explain"])
    assert result.exit_code == 0, result.output
    assert "embedding_model:" in result.output
    assert "embedding_host:" in result.output
    assert "stored_model:" in result.output
    assert "old-model" in result.output
    assert "stored_dim:" in result.output
    assert "384" in result.output


def test_sem_explain_strips_url_userinfo(monkeypatch):
    """`search sem --explain` prints host only, never URL userinfo.

    Operators sometimes embed credentials in the base URL
    (``https://token:sekret@host/v1``) — ``urlsplit().netloc``
    would leak them to stderr; ``hostname`` (+ port) does not.
    """
    from unittest.mock import MagicMock
    from typer.testing import CliRunner
    from doc3gpp.cli import app
    from doc3gpp.services import factory
    from doc3gpp.settings.loader import get_settings
    from doc3gpp.settings.schema import SemanticSearchSettings, Settings

    settings = Settings(semantic_search=SemanticSearchSettings(
        embedding_base_url="https://token:sekret@emb.example.com:8443/v1",
    ))
    svc = MagicMock()
    svc.search.return_value = []
    svc._vec._stored_model = None
    svc._vec._dim = None
    monkeypatch.setattr(
        factory, "build_semantic_search_service", lambda *a, **kw: svc,
    )
    monkeypatch.setattr(
        "doc3gpp.settings.loader.get_settings", lambda: settings,
    )
    try:
        result = CliRunner().invoke(app, ["search", "sem", "q", "--explain"])
        assert result.exit_code == 0, result.output
        assert "sekret" not in result.output
        assert "emb.example.com" in result.output
    finally:
        get_settings.cache_clear()


def test_search_sem_surfaces_rebuild_hint_on_model_mismatch(sqlite_env):
    """A swapped model must surface the rebuild hint, not the URL hint.

    The factory returns None on dim/model mismatch, so without a
    vec_meta check `search sem` would misdirect to the
    "set embedding_base_url" message even though the URL is set.
    """
    from sqlalchemy import text
    from typer.testing import CliRunner
    from doc3gpp.cli import app
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine

    create_schema()
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO vec_meta (key, value) VALUES ('embedding_model', 'old-model') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            ),
        )
    result = CliRunner().invoke(app, ["search", "sem", "q"])
    assert result.exit_code == 1, result.output
    assert "model mismatch" in result.output.lower()
    assert "rebuild-embeddings" in result.output
