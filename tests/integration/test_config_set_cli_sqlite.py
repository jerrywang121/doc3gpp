"""Integration tests for ``doc3gpp config set <key> <value>``.

End-to-end coverage for the cross-layer round-trip: real subprocess
(:class:`typer.testing.CliRunner`), real on-disk TOML, real
:class:`doc3gpp.settings.loader.get_settings` cache, and the
pydantic-settings precedence rules in
:class:`doc3gpp.settings.schema.Settings`. Unit-level behaviour
(parser, writer, ``--dry-run``, ``--force``, ``--target``)
is locked down in ``tests/unit/test_config_set_cli.py``; the tests in
this file focus on the cross-layer round-trip and the env-var
precedence contract — not on writer internals.

These tests are sqlite-only by construction: they reuse the
``sqlite_env`` fixture from :mod:`tests.conftest`, exercise no
network or non-sqlite backend, and carry no ``online``/``mysql``
marker.
"""

from __future__ import annotations

import json
import tomllib
from datetime import timedelta
from pathlib import Path

from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.settings.loader import get_settings


Runner = CliRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_toml(path: Path) -> dict:
    """Parse the on-disk TOML via the stdlib (no ``config_writer`` import).

    The integration boundary forbids reaching into the writer module
    directly, so the file's bytes are inspected through
    :mod:`tomllib` only — keeps the test honest about what actually
    landed on disk.
    """
    return tomllib.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Scenario 1 — `config set --init` then `config show` round-trip.
# ---------------------------------------------------------------------------


def test_config_set_init_then_show(sqlite_env, tmp_path, monkeypatch) -> None:
    """Given a fresh project root with ``pyproject.toml``, ``config set
    --init sync.auto_sync true`` creates ``./doc3gpp.toml`` and writes
    the key; ``config show`` then surfaces the resolved value with
    pydantic coercion (``is True``, not the raw string ``"true"``).
    """
    # Given: a project root with a project marker (so --init targets project).
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    # Wipe any DOC3GPP_CONFIG pin and HOME-side fallback so --init targets project.
    monkeypatch.delenv("DOC3GPP_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    get_settings.cache_clear()

    # When: config set --init creates the project config and writes the key.
    set_result = Runner().invoke(
        app, ["config", "set", "--init", "sync.auto_sync", "true"]
    )
    assert set_result.exit_code == 0, set_result.output
    project_cfg = tmp_path / "doc3gpp.toml"
    assert project_cfg.is_file()

    # Then: config show surfaces the value as a real boolean (not "true").
    show_result = Runner().invoke(app, ["config", "show"])
    assert show_result.exit_code == 0, show_result.output

    # The first line of `config show` is the "# config source:" header;
    # everything after it is the JSON dump.
    payload = show_result.output.split("\n", 1)[1]
    data = json.loads(payload)
    assert data["sync"]["auto_sync"] is True


# ---------------------------------------------------------------------------
# Scenario 2 — Persisted value is visible to get_settings() in this process.
# ---------------------------------------------------------------------------


def test_persisted_value_visible_to_get_settings(
    sqlite_env, tmp_path, monkeypatch
) -> None:
    """``config set sync.meeting_sync_interval 12h`` writes a string
    ``"12h"`` to disk; the CLI clears the settings cache; the next
    :func:`get_settings` call must hand back ``timedelta(hours=12)``.
    This proves the cross-layer round-trip: subprocess → file →
    pydantic coercion → typed Settings.
    """
    # Given: a pinned, non-empty TOML so config set has somewhere to write.
    cfg = tmp_path / "pinned.toml"
    cfg.write_text(
        'database_url = "sqlite+pysqlite:////unused"\n', encoding="utf-8"
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    get_settings.cache_clear()

    # When: config set the timedelta field as a string.
    set_result = Runner().invoke(
        app, ["config", "set", "sync.meeting_sync_interval", "12h"]
    )
    assert set_result.exit_code == 0, set_result.output

    # Then: the file carries the literal string and get_settings coerces it.
    on_disk = _parse_toml(cfg)
    assert on_disk["sync"]["meeting_sync_interval"] == "12h"

    get_settings.cache_clear()
    settings = get_settings()
    assert settings.sync.meeting_sync_interval == timedelta(hours=12)


# ---------------------------------------------------------------------------
# Scenario 3 — Env var still wins after a file-level write.
# ---------------------------------------------------------------------------


def test_env_var_still_wins_after_set(sqlite_env, tmp_path, monkeypatch) -> None:
    """With ``DOC3GPP_SYNC__AUTO_SYNC=false`` in the env, ``config set
    sync.auto_sync true`` must still succeed at the file layer (the
    writer is unaware of env precedence), but :func:`get_settings`
    must return ``False`` — env > file per the source ordering in
    :class:`Settings.settings_customise_sources`.
    """
    # Given: an empty pinned TOML plus an env var that contradicts the write.
    cfg = tmp_path / "pinned.toml"
    cfg.write_text("", encoding="utf-8")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "false")
    get_settings.cache_clear()

    # When: config set writes the truthy value to disk.
    set_result = Runner().invoke(
        app, ["config", "set", "sync.auto_sync", "true"]
    )
    assert set_result.exit_code == 0, set_result.output

    # Then: the file carries the literal "true" (writer is file-scoped).
    on_disk = _parse_toml(cfg)
    assert on_disk["sync"]["auto_sync"] == "true"

    # And: the env var wins at runtime — get_settings sees False.
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.sync.auto_sync is False


# ---------------------------------------------------------------------------
# Scenario 4 — `--init` refuses when DOC3GPP_CONFIG pins an explicit source.
# ---------------------------------------------------------------------------


def test_init_refuses_when_doc3gpp_config_set(
    sqlite_env, tmp_path, monkeypatch
) -> None:
    """With ``DOC3GPP_CONFIG`` set, ``--init`` must short-circuit and
    tell the operator to unset the explicit pin — the env var is the
    authoritative bootstrap source so ``--init`` cannot redirect the
    write elsewhere.
    """
    # Given: a project root that *would* be a valid --init target, plus
    # DOC3GPP_CONFIG pointing elsewhere. The pin must override the project.
    cfg = tmp_path / "explicit.toml"
    cfg.write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\n", encoding="utf-8"
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    get_settings.cache_clear()

    # When: --init is invoked despite the pin.
    result = Runner().invoke(
        app, ["config", "set", "--init", "sync.auto_sync", "true"]
    )

    # Then: non-zero exit and the diagnostic names DOC3GPP_CONFIG.
    assert result.exit_code != 0
    assert "DOC3GPP_CONFIG" in result.output
