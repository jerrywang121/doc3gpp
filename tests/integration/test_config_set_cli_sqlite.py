"""Integration tests for ``doc3gpp config set <key> <value>``.

End-to-end coverage for the cross-layer round-trip: real subprocess
(:class:`typer.testing.CliRunner`), real on-disk TOML, real
:class:`doc3gpp.settings.loader.get_settings` cache, and the
pydantic-settings precedence rules in
:class:`doc3gpp.settings.schema.Settings`. Unit-level behaviour
(parser, writer, ``--dry-run``) is locked down in
``tests/unit/test_config_set_cli.py``; the tests in this file focus on
the cross-layer round-trip and the env-var precedence contract — not
on writer internals. The bootstrap path (``config init`` /
``--force`` / ``DOC3GPP_CONFIG`` refusal) lives in
``tests/unit/test_config_init_cli.py`` and
``tests/integration/test_config_init_cli_sqlite.py``.

These tests are sqlite-only by construction: they reuse the
``sqlite_env`` fixture from :mod:`tests.conftest`, exercise no
network or non-sqlite backend, and carry no ``online`` marker.
"""

from __future__ import annotations

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
# Scenario 1 — Persisted value is visible to get_settings() in this process.
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
# Scenario 2 — Env var still wins after a file-level write.
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
