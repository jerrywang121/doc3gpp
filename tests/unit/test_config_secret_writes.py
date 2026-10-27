"""Tests for keeping embedding bearer tokens out of project TOML files.

``config set semantic_search.embedding_api_key`` stores a secret. Project
files can be committed, so by default the command refuses to write the
secret into any config file other than the user config. The explicit
``--allow-plaintext-secret`` flag overrides that. Whenever the user config
is written, its file mode is restricted to ``0600``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.settings.config_writer import read_toml
from doc3gpp.settings.loader import get_settings

PROJECT_BODY = '[semantic_search]\nembedding_base_url = "http://localhost:11434/v1"\n'


@pytest.fixture()
def clean_settings(monkeypatch: pytest.MonkeyPatch):
    for key in list(os.environ):
        if key.startswith("DOC3GPP_"):
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_config_set_refuses_api_key_in_project_file(clean_settings, tmp_path, monkeypatch):
    cfg = tmp_path / "doc3gpp.toml"
    cfg.write_text(PROJECT_BODY, encoding="utf-8")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))

    result = CliRunner().invoke(
        app, ["config", "set", "semantic_search.embedding_api_key", "sekret"],
    )

    assert result.exit_code != 0
    assert "DOC3GPP_SEMANTIC_SEARCH__EMBEDDING_API_KEY" in result.output
    assert "sekret" not in result.output
    assert "embedding_api_key" not in cfg.read_text(encoding="utf-8")


def test_config_set_api_key_in_project_file_with_flag_warns_and_restricts(
    clean_settings, tmp_path, monkeypatch,
):
    cfg = tmp_path / "doc3gpp.toml"
    cfg.write_text(PROJECT_BODY, encoding="utf-8")
    cfg.chmod(0o644)
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))

    result = CliRunner().invoke(
        app,
        [
            "config", "set", "--allow-plaintext-secret",
            "semantic_search.embedding_api_key", "sekret",
        ],
    )

    assert result.exit_code == 0, result.output
    assert read_toml(cfg)["semantic_search"]["embedding_api_key"] == "sekret"
    assert "plain text" in result.output
    assert "sekret" not in result.output
    assert _mode(cfg) == 0o600


def test_config_set_api_key_in_user_config_warns_and_restricts(
    clean_settings, tmp_path, monkeypatch,
):
    cfg = tmp_path / "config.toml"
    cfg.write_text(PROJECT_BODY, encoding="utf-8")
    cfg.chmod(0o644)
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setattr("doc3gpp.cli.DEFAULT_USER_CONFIG", cfg)

    result = CliRunner().invoke(
        app, ["config", "set", "semantic_search.embedding_api_key", "sekret"],
    )

    assert result.exit_code == 0, result.output
    assert "plain text" in result.output
    assert "sekret" not in result.output
    assert _mode(cfg) == 0o600


def test_config_set_restricts_user_config_permissions(clean_settings, tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text("", encoding="utf-8")
    cfg.chmod(0o644)
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setattr("doc3gpp.cli.DEFAULT_USER_CONFIG", cfg)

    result = CliRunner().invoke(app, ["config", "set", "sync.auto_sync", "true"])

    assert result.exit_code == 0, result.output
    assert _mode(cfg) == 0o600
