"""Unit tests for ``doc3gpp config set <key> <value>``.

These tests pin the behaviour of the new :func:`config_set` command:
operator ergonomics (one key + one value per invocation), TOML emission
shape (top-level vs nested table), pydantic coercion for non-string
fields, the ``--init`` / ``--init-target`` / ``--init-force`` /
``--dry-run`` flag surface, and the cache-invalidation contract that
makes the new value visible to :func:`get_settings` in the same
process. Every test uses an isolated ``tmp_path`` and pins the config
location via ``DOC3GPP_CONFIG`` so no shared state bleeds between
cases.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import _configure_logging, app
from doc3gpp.settings.config_source import (
    DEFAULT_PROJECT_CONFIG,
    DEFAULT_USER_CONFIG,
)
from doc3gpp.settings.config_writer import read_toml
from doc3gpp.settings.loader import get_settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def clean_settings(monkeypatch: pytest.MonkeyPatch):
    """Strip every DOC3GPP_* env var and clear the settings cache.

    Mirrors the canonical fixture in ``test_settings_config_file`` so
    the ``config set`` tests cannot leak env state into one another.
    """
    for key in list(os.environ):
        if key.startswith("DOC3GPP_"):
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def write_toml(tmp_path: Path):
    """Factory fixture returning a ``(name, content) -> Path`` writer."""

    def _write(name: str, content: str) -> Path:
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        return path

    return _write


Runner = CliRunner


# ---------------------------------------------------------------------------
# Happy paths — value lands on disk in the expected TOML shape.
# ---------------------------------------------------------------------------


def test_config_set_writes_nested_value(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """Given a pinned empty TOML, when the operator sets ``sync.auto_sync``,
    then ``read_toml`` shows the key as a nested string under ``[sync]``."""
    cfg = write_toml("pinned.toml", "")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))

    result = Runner().invoke(
        app, ["config", "set", "sync.auto_sync", "true"]
    )
    assert result.exit_code == 0, result.output

    data = read_toml(cfg)
    assert data == {"sync": {"auto_sync": "true"}}


def test_config_set_writes_nested_table_for_output_format(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """Given an empty TOML, setting ``output.format`` emits a nested table."""
    cfg = write_toml("pinned.toml", "")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))

    result = Runner().invoke(app, ["config", "set", "output.format", "json"])
    assert result.exit_code == 0, result.output

    data = read_toml(cfg)
    assert data == {"output": {"format": "json"}}
    # The TOML on disk must include a [output] section header.
    assert "[output]" in cfg.read_text(encoding="utf-8")


def test_config_set_writes_top_level_key(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """Setting a flat key like ``database_url`` lands at the TOML root,
    with no table wrapper."""
    cfg = write_toml("pinned.toml", "")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))

    result = Runner().invoke(
        app, ["config", "set", "database_url", "sqlite:///foo"]
    )
    assert result.exit_code == 0, result.output

    data = read_toml(cfg)
    assert data == {"database_url": "sqlite:///foo"}


def test_config_set_coerces_timedelta_and_renders_p1d(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """Pydantic coerces the ``value`` string ``"24h"`` to
    ``timedelta(days=1)`` for the ``sync.meeting_sync_interval`` field.
    The CLI's success message echoes the resolved subtree in JSON form,
    so ``"P1D"`` must appear in stdout."""
    cfg = write_toml("pinned.toml", "")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))

    result = Runner().invoke(
        app, ["config", "set", "sync.meeting_sync_interval", "24h"]
    )
    assert result.exit_code == 0, result.output
    assert "P1D" in result.output


# ---------------------------------------------------------------------------
# Happy paths — --init / --init-target / --init-force / --dry-run
# ---------------------------------------------------------------------------


def test_config_set_init_user_target_creates_file(
    clean_settings, monkeypatch, tmp_path,
) -> None:
    """Given no config in use, ``--init --init-target user`` creates the
    user config and writes the key. We override ``DEFAULT_USER_CONFIG``
    because it is bound at import time from ``Path.home()`` and because
    :func:`resolve_init_target` reads it from the ``config_writer``
    namespace, not the original ``config_source`` binding."""
    # Isolate HOME/XDG so no real fallback is found.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    user_target = tmp_path / "user-config.toml"
    monkeypatch.setattr(
        "doc3gpp.settings.config_writer.DEFAULT_USER_CONFIG", user_target
    )

    result = Runner().invoke(
        app, ["config", "set", "--init", "--init-target", "user", "sync.auto_sync", "true"]
    )
    assert result.exit_code == 0, result.output
    assert user_target.is_file()
    data = read_toml(user_target)
    assert data == {"sync": {"auto_sync": "true"}}


def test_config_set_init_project_target_creates_file(
    clean_settings, monkeypatch, tmp_path,
) -> None:
    """Given a cwd containing ``pyproject.toml``, ``--init --init-target
    project`` writes ``./doc3gpp.toml`` (project-local) under cwd."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(tmp_path)

    result = Runner().invoke(
        app, ["config", "set", "--init", "--init-target", "project", "sync.auto_sync", "true"]
    )
    assert result.exit_code == 0, result.output

    created = tmp_path / "doc3gpp.toml"
    assert created.is_file()
    data = read_toml(created)
    assert data == {"sync": {"auto_sync": "true"}}


def test_config_set_dry_run_leaves_file_unchanged(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """``--dry-run`` validates the patch and echoes what *would* be
    written, but the file on disk must be byte-for-byte unchanged."""
    cfg = write_toml("pinned.toml", "")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    before = cfg.read_bytes()

    result = Runner().invoke(
        app, ["config", "set", "--dry-run", "sync.auto_sync", "true"]
    )
    assert result.exit_code == 0, result.output
    assert "# dry-run:" in result.output
    assert cfg.read_bytes() == before


# ---------------------------------------------------------------------------
# Failure paths — operator errors surface as non-zero exits.
# ---------------------------------------------------------------------------


def test_config_set_without_file_and_no_init_fails(
    clean_settings, monkeypatch, tmp_path,
) -> None:
    """With no config in use and no ``--init``, the command must exit
    non-zero and point the operator at ``doc3gpp config path``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("DOC3GPP_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)

    result = Runner().invoke(
        app, ["config", "set", "sync.auto_sync", "true"]
    )
    assert result.exit_code != 0
    assert "config path" in result.output


def test_config_set_invalid_value_for_bool_fails(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """Passing ``"notabool"`` for a boolean field surfaces pydantic's
    error message containing ``bool``."""
    cfg = write_toml("pinned.toml", "")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))

    result = Runner().invoke(
        app, ["config", "set", "sync.auto_sync", "notabool"]
    )
    assert result.exit_code != 0
    assert "bool" in result.output.lower()


def test_config_set_unknown_key_fails(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """``sync.unknown`` is not a valid field; the CLI rejects it with
    the canonical "Unknown config key:" message."""
    cfg = write_toml("pinned.toml", "")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))

    result = Runner().invoke(
        app, ["config", "set", "sync.unknown", "true"]
    )
    assert result.exit_code != 0
    assert "Unknown config key: sync.unknown" in result.output


def test_config_set_against_malformed_toml_fails(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """A pinned file containing ``[broken`` must surface a "malformed"
    diagnostic rather than blow up with a traceback."""
    cfg = write_toml("pinned.toml", "[broken")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))

    result = Runner().invoke(
        app, ["config", "set", "sync.auto_sync", "true"]
    )
    assert result.exit_code != 0
    assert "malformed" in result.output.lower()


def test_config_set_init_refuses_when_env_var_pins_config(
    clean_settings, write_toml, monkeypatch, tmp_path,
) -> None:
    """When ``DOC3GPP_CONFIG`` is set, ``--init`` refuses and tells the
    operator to unset the explicit source — because the env pin takes
    precedence over the bootstrap target."""
    cfg = write_toml("pinned.toml", "")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))

    result = Runner().invoke(
        app, ["config", "set", "--init", "sync.auto_sync", "true"]
    )
    assert result.exit_code != 0
    assert "DOC3GPP_CONFIG" in result.output


def test_config_set_init_user_target_existing_file_needs_force(
    clean_settings, monkeypatch, tmp_path,
) -> None:
    """When the bootstrap target already exists, ``--init`` refuses and
    points the operator at ``--init-force``."""
    user_target = tmp_path / "user-config.toml"
    user_target.write_text("# pre-existing\n", encoding="utf-8")
    monkeypatch.setattr(
        "doc3gpp.settings.config_writer.DEFAULT_USER_CONFIG", user_target
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DOC3GPP_CONFIG", raising=False)

    result = Runner().invoke(
        app, ["config", "set", "--init", "--init-target", "user", "sync.auto_sync", "true"]
    )
    assert result.exit_code != 0
    assert "--init-force" in result.output


# ---------------------------------------------------------------------------
# Cache-invalidation contract — written values are visible immediately.
# ---------------------------------------------------------------------------


def test_config_set_clears_settings_cache(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """After a successful ``config set``, the next ``get_settings()``
    call must return the freshly written value. The CLI is responsible
    for ``get_settings.cache_clear()`` — the test pins that contract."""
    cfg = write_toml("pinned.toml", "")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", "sqlite+pysqlite:///:memory:")

    # Sanity: pre-set state shows the default.
    get_settings.cache_clear()
    assert get_settings().sync.auto_sync is False

    result = Runner().invoke(
        app, ["config", "set", "sync.auto_sync", "true"]
    )
    assert result.exit_code == 0, result.output

    # Same process, no monkeypatch env churn → must see the new value.
    refreshed = get_settings()
    assert refreshed.sync.auto_sync is True


# ---------------------------------------------------------------------------
# Helpers that are tested at the import boundary.
# ---------------------------------------------------------------------------


def test_config_help_lists_set_subcommand() -> None:
    """The new ``set`` command must be discoverable via ``doc3gpp config
    --help``; the contract forbids hiding it behind a different group."""
    result = Runner().invoke(app, ["config", "--help"])
    assert result.exit_code == 0, result.output
    assert "set" in result.output


def test_default_user_config_path_is_distinct_from_project(
    clean_settings,
) -> None:
    """Defensive sanity check that keeps ``--init --init-target user``
    from accidentally colliding with the project-local path in any
    future refactor."""
    assert DEFAULT_USER_CONFIG != DEFAULT_PROJECT_CONFIG
    assert DEFAULT_USER_CONFIG.name == "config.toml"
    assert DEFAULT_PROJECT_CONFIG.name == "doc3gpp.toml"


# ---------------------------------------------------------------------------
# Logging resilience — narrow catch on tomllib.TOMLDecodeError
# ---------------------------------------------------------------------------


def test_malformed_toml_logs_warning_falls_back_to_info(
    clean_settings, monkeypatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """When the active TOML is malformed, ``_configure_logging`` must
    emit a warning and fall back to the INFO level instead of
    propagating the exception. This locks in the tightened catch so a
    future regression back to a broad ``except ValueError`` (or
    exception swallowing) is caught by the test."""
    get_settings.cache_clear()

    def _raise():
        raise tomllib.TOMLDecodeError("from test", 0)

    monkeypatch.setattr(
        "doc3gpp.cli.get_settings", _raise, raising=True
    )

    with caplog.at_level(logging.DEBUG, logger="doc3gpp.cli"):
        _configure_logging()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("malformed" in r.getMessage() for r in warnings), caplog.text
    # The INFO fallback is asserted implicitly: the call must not raise.