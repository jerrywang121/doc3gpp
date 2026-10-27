"""Tests for atomic, permission-aware writes of the active config file.

``write_toml`` replaces the file through a same-directory temp file, so a
failed serialisation leaves the original untouched. Callers can pass an
explicit ``mode``; without one an existing file keeps its permission bits.
``config init`` always writes its file as ``0600``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.settings.config_writer import write_toml


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch):
    for key in list(os.environ):
        if key.startswith("DOC3GPP_"):
            monkeypatch.delenv(key, raising=False)


def test_write_toml_failure_keeps_original_file(tmp_path):
    path = tmp_path / "doc3gpp.toml"
    original = "[sync]\nauto_sync = true\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(TypeError):
        write_toml(path, {"sync": {"bad": object()}})

    assert path.read_text(encoding="utf-8") == original
    assert [p.name for p in tmp_path.iterdir()] == ["doc3gpp.toml"]


def test_write_toml_keeps_existing_mode_by_default(tmp_path):
    path = tmp_path / "doc3gpp.toml"
    path.write_text("", encoding="utf-8")
    path.chmod(0o644)

    write_toml(path, {"sync": {"auto_sync": True}})

    assert _mode(path) == 0o644


def test_write_toml_applies_explicit_mode(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    path.chmod(0o644)

    write_toml(path, {"sync": {"auto_sync": True}}, mode=0o600)

    assert _mode(path) == 0o600


def test_config_init_user_target_is_private(clean_env, tmp_path, monkeypatch):
    target = tmp_path / "home" / "config.toml"
    monkeypatch.setattr("doc3gpp.settings.config_writer.DEFAULT_USER_CONFIG", target)

    result = CliRunner().invoke(app, ["config", "init", "--target", "user"])

    assert result.exit_code == 0, result.output
    assert _mode(target) == 0o600


def test_config_init_force_overwrite_restricts_mode(clean_env, tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_text("old = true\n", encoding="utf-8")
    target.chmod(0o644)
    monkeypatch.setattr("doc3gpp.settings.config_writer.DEFAULT_USER_CONFIG", target)

    result = CliRunner().invoke(app, ["config", "init", "--target", "user", "--force"])

    assert result.exit_code == 0, result.output
    assert _mode(target) == 0o600


def test_config_set_keeps_project_file_mode(clean_env, tmp_path, monkeypatch):
    cfg = tmp_path / "doc3gpp.toml"
    cfg.write_text("", encoding="utf-8")
    cfg.chmod(0o644)
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))

    result = CliRunner().invoke(app, ["config", "set", "sync.auto_sync", "true"])

    assert result.exit_code == 0, result.output
    assert _mode(cfg) == 0o644
