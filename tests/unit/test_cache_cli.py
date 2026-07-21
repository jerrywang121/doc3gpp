"""Unit tests for the ``cache status`` and ``cache purge`` CLI commands.

The tests use pytest's ``tmp_path`` for the cache directory and a
patched :func:`doc3gpp.settings.loader.get_settings` to point
``settings.cache.dir`` and ``settings.cache.size_limit_mb`` at the
test root. The ``sqlite_env`` fixture is not needed because these
commands never touch the database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import _build_cache, app
from doc3gpp.scraping.cache import TDocCache
from doc3gpp.settings.loader import get_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def cache_env(tmp_path, monkeypatch) -> Iterator[Path]:
    """Point the settings' cache config at ``tmp_path/cache``.

    Yields the cache root so tests can inspect the on-disk state after
    each command runs. The settings + engine caches are cleared in the
    surrounding ``sqlite_env``-style teardown (here, manually, since
    these tests do not need a database).

    Uses a pinned ``DOC3GPP_CONFIG`` TOML so the cache directory and
    size limit are configurable (these fields are TOML-only — see
    :data:`doc3gpp.settings.schema.ALLOWED_ENV_VARS`).
    """
    cache_root = tmp_path / "cache"
    config_path = tmp_path / "cache-config.toml"
    config_path.write_text(
        f'[cache]\n'
        f'dir = "{cache_root}"\n'
        f'size_limit_mb = 16\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(cache_root))
    get_settings.cache_clear()
    yield cache_root
    get_settings.cache_clear()


def _populate_cache(root: Path, *, zips: int = 0, markdown: int = 0) -> None:
    """Drop ``zips`` + ``markdown`` fixtures into the cache directory.

    Uses :class:`TDocCache.put_bytes` so the on-disk layout matches
    exactly what ``TDocCrService`` would produce. Avoids hand-crafting
    files (which would skip the key sanitiser and the subdir creation).
    """
    cache = TDocCache(root=root, size_limit_bytes=0)
    for i in range(zips):
        cache.put_bytes(f"tdoc{i}.zip", b"z" * 32, "zips")
    for i in range(markdown):
        cache.put_bytes(f"hash{i}.md", b"m" * 64, "markdown")


# ---------------------------------------------------------------------------
# cache status
# ---------------------------------------------------------------------------


def test_cache_status_empty_dir(cache_env) -> None:
    """A fresh cache dir prints zeros on every metric."""
    runner = CliRunner()
    result = runner.invoke(app, ["cache", "status"])
    assert result.exit_code == 0, result.output
    assert "file_count:  0" in result.output
    assert "total_bytes: 0 B" in result.output
    # The default 16 MB ceiling from cache_env renders as 16.0 MB.
    assert "limit_bytes: 16.0 MB" in result.output
    assert "zips:        0" in result.output
    assert "markdown:    0" in result.output


def test_cache_status_with_files(cache_env) -> None:
    """Two zips + one markdown file produce the expected per-subdir counts."""
    _populate_cache(cache_env, zips=2, markdown=1)

    runner = CliRunner()
    result = runner.invoke(app, ["cache", "status"])
    assert result.exit_code == 0, result.output
    assert "file_count:  3" in result.output
    assert "zips:        2" in result.output
    assert "markdown:    1" in result.output
    # 2 zips × 32 + 1 markdown × 64 = 128 bytes.
    assert "total_bytes: 128 B" in result.output


def test_cache_status_unlimited_limit(cache_env, monkeypatch, tmp_path) -> None:
    """``size_limit_mb=0`` (unlimited) renders the friendly ``unlimited`` token."""
    config_path = tmp_path / "unlimited-config.toml"
    config_path.write_text(
        f'[cache]\n'
        f'dir = "{tmp_path / "cache"}"\n'
        f'size_limit_mb = 0\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    get_settings.cache_clear()

    runner = CliRunner()
    result = runner.invoke(app, ["cache", "status"])
    assert result.exit_code == 0, result.output
    assert "limit_bytes: unlimited" in result.output


# ---------------------------------------------------------------------------
# cache purge
# ---------------------------------------------------------------------------


def test_cache_purge_with_yes(cache_env) -> None:
    """``--yes`` bypasses the prompt and deletes every cached file."""
    _populate_cache(cache_env, zips=2, markdown=1)

    runner = CliRunner()
    result = runner.invoke(app, ["cache", "purge", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Deleted 3 files from cache." in result.output

    # The subdirs are recreated empty (so subsequent ``tdoc parse``
    # calls still work).
    assert (cache_env / "zips").is_dir()
    assert (cache_env / "markdown").is_dir()
    assert list((cache_env / "zips").iterdir()) == []
    assert list((cache_env / "markdown").iterdir()) == []


def test_cache_purge_without_yes_aborts_when_confirm_enabled(
    cache_env, monkeypatch, tmp_path,
) -> None:
    """With ``purge_confirm=true`` (via TOML) and no ``--yes``,
    ``typer.confirm`` raises ``Abort`` in the non-interactive CliRunner.
    The output contains the abort marker and exit code is non-zero;
    no files are deleted."""
    config_path = tmp_path / "confirm-on.toml"
    config_path.write_text(
        f'[cache]\n'
        f'dir = "{tmp_path / "cache"}"\n'
        f'size_limit_mb = 16\n'
        f'purge_confirm = true\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    get_settings.cache_clear()
    _populate_cache(cache_env, zips=2, markdown=1)

    runner = CliRunner()
    result = runner.invoke(app, ["cache", "purge"])
    assert result.exit_code != 0
    assert "Aborted" in result.output

    # Nothing was deleted.
    zips_files = list((cache_env / "zips").iterdir())
    md_files = list((cache_env / "markdown").iterdir())
    assert len(zips_files) == 2
    assert len(md_files) == 1


def test_cache_purge_toml_overrides_confirm(
    cache_env, monkeypatch, tmp_path,
) -> None:
    """Setting ``purge_confirm = false`` in the TOML config makes the
    CLI skip the prompt and proceed straight to deletion."""
    config_path = tmp_path / "confirm-off.toml"
    config_path.write_text(
        f'[cache]\n'
        f'dir = "{tmp_path / "cache"}"\n'
        f'size_limit_mb = 16\n'
        f'purge_confirm = false\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    get_settings.cache_clear()
    _populate_cache(cache_env, zips=2, markdown=1)

    runner = CliRunner()
    result = runner.invoke(app, ["cache", "purge"])
    assert result.exit_code == 0, result.output
    assert "Deleted 3 files from cache." in result.output
    assert list((cache_env / "zips").iterdir()) == []


def test_cache_purge_size_limit_bytes_calculation(cache_env) -> None:
    """The ``_build_cache`` helper must translate ``size_limit_mb`` to bytes.

    The test fixture pins ``cache.size_limit_mb = 16`` via the TOML
    config; the resulting :class:`TDocCache` must report
    ``size_limit_bytes == 16 * 1024 * 1024``. This guards against a
    future unit drift (e.g. someone changing the helper to
    ``* 1000 * 1000``).
    """
    cache = _build_cache()
    assert cache.size_limit_bytes == 16 * 1024 * 1024
    assert cache.root == cache_env


def test_cache_purge_short_form_yes_alias(cache_env) -> None:
    """``-y`` is the short alias for ``--yes`` and behaves identically."""
    _populate_cache(cache_env, zips=1, markdown=1)

    runner = CliRunner()
    result = runner.invoke(app, ["cache", "purge", "-y"])
    assert result.exit_code == 0, result.output
    assert "Deleted 2 files from cache." in result.output
