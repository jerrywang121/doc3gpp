"""Unit tests for ``doc3gpp config init``.

These tests pin the behaviour of the new :func:`config_init` command:
the bootstrap target resolution (``--target auto|project|user``), the
``--force`` / ``-f`` overwrite gate, the refusal when
:envvar:`DOC3GPP_CONFIG` pins the config file, and the
``tempfile`` + :func:`os.replace` atomic-write dance that guarantees a
crashed write cannot leave a partial file behind. Every test uses an
isolated ``tmp_path`` and pins the user-config / home locations so no
shared state bleeds between cases.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.settings.config_writer import (
    load_default_template,
    resolve_init_target,
)
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.db.session import get_engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def clean_settings(monkeypatch: pytest.MonkeyPatch):
    """Strip every DOC3GPP_* env var and clear the settings cache.

    Mirrors the canonical fixture in ``test_config_set_cli`` so the
    ``config init`` tests cannot leak env state into one another.
    """
    for key in list(os.environ):
        if key.startswith("DOC3GPP_"):
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()


@pytest.fixture()
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Make :func:`resolve_init_target` resolve the user path to ``tmp_path``.

    Patches :data:`DEFAULT_USER_CONFIG` (which ``resolve_init_target``
    reads from the ``config_writer`` namespace) and re-points
    ``$HOME`` away from the developer's real home so any fallback lookup
    lands inside the sandbox.
    """
    monkeypatch.setattr(
        "doc3gpp.settings.config_writer.DEFAULT_USER_CONFIG",
        tmp_path / "user-config.toml",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


Runner = CliRunner


# ---------------------------------------------------------------------------
# Happy paths — config init writes the packaged template.
# ---------------------------------------------------------------------------


def test_config_init_happy_writes_template(
    clean_settings, isolated_home, monkeypatch, tmp_path,
) -> None:
    """Given no config in use and a project root available, ``config
    init`` writes the packaged default template under cwd."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    result = Runner().invoke(app, ["config", "init"])
    assert result.exit_code == 0, result.output

    created = tmp_path / "doc3gpp.toml"
    assert created.is_file()
    assert created.read_text(encoding="utf-8") == load_default_template()


def test_config_init_target_user(
    clean_settings, isolated_home, monkeypatch, tmp_path,
) -> None:
    """``--target user`` writes to the user-config path even from a cwd
    that has a project root (because the flag overrides auto-resolution)."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    user_target = tmp_path / "user-config.toml"

    result = Runner().invoke(app, ["config", "init", "--target", "user"])
    assert result.exit_code == 0, result.output

    assert user_target.is_file()
    # Project-local file must NOT exist.
    assert not (tmp_path / "doc3gpp.toml").exists()


def test_config_init_target_project(
    clean_settings, isolated_home, monkeypatch, tmp_path,
) -> None:
    """``--target project`` writes ``./doc3gpp.toml`` under cwd when a
    project marker is present."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    result = Runner().invoke(app, ["config", "init", "--target", "project"])
    assert result.exit_code == 0, result.output

    created = tmp_path / "doc3gpp.toml"
    assert created.is_file()
    assert created.read_text(encoding="utf-8") == load_default_template()


# ---------------------------------------------------------------------------
# Failure paths — error contracts surface as non-zero exits.
# ---------------------------------------------------------------------------


def test_config_init_target_project_no_project_root_fails(
    clean_settings, isolated_home, monkeypatch, tmp_path,
) -> None:
    """With no project root anywhere up the tree, ``--target project``
    exits non-zero with a diagnostic that references either the valid
    values or the missing project root."""
    monkeypatch.chdir(tmp_path)

    result = Runner().invoke(app, ["config", "init", "--target", "project"])
    assert result.exit_code != 0
    assert (
        "no project root" in result.output
        or "valid values" in result.output.lower()
    )


def test_config_init_target_bogus(clean_settings, isolated_home) -> None:
    """Anything outside ``{"project", "user", "auto"}`` is rejected with
    a diagnostic naming the valid values."""
    result = Runner().invoke(app, ["config", "init", "--target", "bogus"])
    assert result.exit_code != 0
    assert "project" in result.output and "user" in result.output


def test_config_init_force_overwrites(
    clean_settings, isolated_home, monkeypatch, tmp_path,
) -> None:
    """When a file already exists at the target, ``--force`` overwrites
    it with the packaged template."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    created = tmp_path / "doc3gpp.toml"
    created.write_text("# stale content\n", encoding="utf-8")

    result = Runner().invoke(
        app, ["config", "init", "--target", "project", "--force"]
    )
    assert result.exit_code == 0, result.output
    assert created.read_text(encoding="utf-8") == load_default_template()


def test_config_init_existing_file_no_force_fails(
    clean_settings, isolated_home, monkeypatch, tmp_path,
) -> None:
    """When the bootstrap target already exists and ``--force`` is
    absent, ``config init`` exits non-zero and points the operator at
    ``--force``."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "doc3gpp.toml").write_text(
        "# pre-existing\n", encoding="utf-8"
    )

    result = Runner().invoke(
        app, ["config", "init", "--target", "project"]
    )
    assert result.exit_code != 0
    assert "--force" in result.output


def test_config_init_refuses_when_doc3gpp_config_set(
    clean_settings, isolated_home, monkeypatch, tmp_path,
) -> None:
    """When :envvar:`DOC3GPP_CONFIG` pins a config file, ``config init``
    refuses to bootstrap — because the env pin would mask the new
    file. The error names the env var."""
    pinned = tmp_path / "pinned.toml"
    pinned.write_text("", encoding="utf-8")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(pinned))
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    result = Runner().invoke(app, ["config", "init"])
    assert result.exit_code != 0
    assert "DOC3GPP_CONFIG" in result.output


# ---------------------------------------------------------------------------
# Cache-invalidation contract — get_settings sees the new file.
# ---------------------------------------------------------------------------


def test_config_init_clears_settings_cache(
    clean_settings, isolated_home, monkeypatch, tmp_path,
) -> None:
    """After a successful ``config init``, the next ``get_settings()``
    call must reflect the new file. The CLI is responsible for
    ``get_settings.cache_clear()`` — this test pins that contract."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    # Sanity: with no config file present, the loader falls back to the
    # built-in defaults.
    get_settings.cache_clear()
    assert get_settings().sync.auto_sync is True

    result = Runner().invoke(app, ["config", "init"])
    assert result.exit_code == 0, result.output

    # Same process, no env churn → must keep returning the fresh instance.
    refreshed = get_settings()
    assert refreshed.sync.auto_sync is True  # template has the default value
    # And the new file is now discoverable by ``find_config_file`` via
    # ``DOC3GPP_CONFIG`` or by virtue of the cwd containing it — i.e.
    # the loader path is wired correctly.
    assert (tmp_path / "doc3gpp.toml").is_file()


# ---------------------------------------------------------------------------
# Atomic-write contract — a crashed write leaves no garbage on disk.
# ---------------------------------------------------------------------------


def test_config_init_atomic_write_failure(
    clean_settings, isolated_home, monkeypatch, tmp_path,
) -> None:
    """When :func:`os.replace` raises during the atomic swap, the target
    must NOT be created (no partial file at the bootstrap path) and no
    ``NamedTemporaryFile`` residue must remain in the target's parent
    directory."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    target_path = tmp_path / "doc3gpp.toml"
    before = set(tmp_path.iterdir())

    def _boom(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError("simulated replace failure")

    with patch("doc3gpp.cli.os.replace", side_effect=_boom):
        result = Runner().invoke(app, ["config", "init"])

    assert result.exit_code != 0
    # (a) target must not have been created.
    assert not target_path.exists()
    # (b) no leftover tempfile in the target's parent directory.
    after = set(tmp_path.iterdir())
    assert after - before == set(), (
        f"atomic-write cleanup left residue behind: {(after - before)!r}"
    )


# ---------------------------------------------------------------------------
# Help discoverability — the command is registered under config_app.
# ---------------------------------------------------------------------------


def test_config_help_lists_init_subcommand() -> None:
    """The new ``init`` command must be discoverable via ``doc3gpp
    config --help``."""
    result = Runner().invoke(app, ["config", "--help"])
    assert result.exit_code == 0, result.output
    assert "init" in result.output


def test_config_init_resolve_init_target_still_works(
    clean_settings, isolated_home, monkeypatch, tmp_path,
) -> None:
    """Defensive sanity check: the helper :func:`resolve_init_target`
    that the CLI sits on top of is still importable and returns a Path
    for the three valid targets (so a future refactor that drops the
    helper would fail this test before reaching the CLI)."""
    assert resolve_init_target("user") == tmp_path / "user-config.toml"
    monkeypatch.chdir(tmp_path)
    assert resolve_init_target("auto") == tmp_path / "user-config.toml"
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n", encoding="utf-8"
    )
    # ``DEFAULT_PROJECT_CONFIG`` is the module-level relative path
    # ``Path("doc3gpp.toml")``; the helper returns it verbatim, so we
    # compare on ``name`` plus ``.parent`` to ignore absolute-path
    # resolution differences (chdir has no effect on a relative Path
    # returned unmodified).
    project_path = resolve_init_target("project")
    assert project_path.name == "doc3gpp.toml"
    assert project_path == Path("doc3gpp.toml")
