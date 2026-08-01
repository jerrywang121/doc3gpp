"""Integration tests for ``doc3gpp config init``.

End-to-end coverage for the bootstrap path: real subprocess
(:class:`typer.testing.CliRunner`), real on-disk TOML, real
:class:`doc3gpp.settings.loader.get_settings` cache, and the
pydantic-settings precedence rules in
:class:`doc3gpp.settings.schema.Settings`. Unit-level behaviour
(``--target`` resolution, ``--force`` overwrite, atomic-write failure,
``DOC3GPP_CONFIG`` refusal) is locked down in
``tests/unit/test_config_init_cli.py``; the tests in this file focus on
the cross-layer round-trip — process state, on-disk file, settings
cache, and the ``config init`` → ``config set`` → ``config show`` flow.

These tests are sqlite-only by construction: they reuse the
``sqlite_env`` fixture from :mod:`tests.conftest`, exercise no network
or non-sqlite backend, and carry no ``online``/``mysql`` marker.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.settings.config_source import load_config_data
from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import Settings


Runner = CliRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_packaged_template() -> str:
    """Return the packaged default TOML template as utf-8 text.

    Reads the package-data file via :mod:`importlib.resources` (stdlib),
    *not* via :func:`doc3gpp.settings.config_writer.load_default_template`,
    so the test stays on the public surface and does not duplicate the
    writer's resolution logic.
    """
    return files("doc3gpp").joinpath("data/doc3gpp.toml.example").read_text(
        encoding="utf-8"
    )


def _seed_project_root(tmp_path: Path, monkeypatch) -> None:
    """Anchor ``tmp_path`` as a project root and pin the bootstrap target.

    ``config init --target auto`` looks for a project marker
    (``pyproject.toml`` etc.) up the cwd tree; dropping one in
    ``tmp_path`` plus ``monkeypatch.chdir(tmp_path)`` makes the auto
    target resolve to ``./doc3gpp.toml``. Redirecting ``$HOME`` and
    dropping ``$XDG_CONFIG_HOME`` keeps the user-wide fallback from
    pointing at the developer's real ``~/.config/doc3gpp/config.toml``.
    The ``DOC3GPP_SYNC__AUTO_SYNC`` clear is the allowlist twin of the
    same intent — without it, a developer shell that exports the var
    shadows the TOML write and ``config set sync.auto_sync true``
    would look like a no-op when ``config show`` runs.
    """
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DOC3GPP_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("DOC3GPP_SYNC__AUTO_SYNC", raising=False)
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Scenario 1 — `config init` then `config show` round-trip.
# ---------------------------------------------------------------------------


def test_config_init_creates_file_then_show(sqlite_env, tmp_path, monkeypatch) -> None:
    """Given a fresh project root with ``pyproject.toml``, ``config init``
    creates ``./doc3gpp.toml`` populated with the packaged template;
    ``load_config_data()`` parses it cleanly (every commented-out table
    disappears into the dict; the active ``[search]`` block surfaces
    with its default values); and ``config show`` then dumps JSON that
    matches ``Settings().model_dump(mode="json")`` — proving the cache
    cleared and the file is wired into the loader.
    """
    _seed_project_root(tmp_path, monkeypatch)

    # When: config init writes the project-local template.
    init_result = Runner().invoke(app, ["config", "init"])
    assert init_result.exit_code == 0, init_result.output
    project_cfg = tmp_path / "doc3gpp.toml"
    assert project_cfg.is_file()

    # And: the file parses cleanly via the public loader.
    path, data = load_config_data()
    assert path is not None and path.name == "doc3gpp.toml"
    # The packaged template is all comments except the active [search]
    # block, so the parsed dict surfaces search defaults — every other
    # table is commented out and disappears into the dict.
    assert data == {
        "search": {
            "enabled": True,
            "auto_index_on_parse": True,
            "rebuild_batch_size": 100,
            "snippet_tokens": 8,
            "bm25_weights": [5.0, 0.0, 0.0, 1.0, 5.0, 5.0, 5.0, 5.0],
        }
    }

    # Then: config show reflects the settings cache after init cleared it.
    show_result = Runner().invoke(app, ["config", "show"])
    assert show_result.exit_code == 0, show_result.output

    # First line is the "# config source:" header; everything after is JSON.
    payload = show_result.output.split("\n", 1)[1]
    shown = json.loads(payload)
    expected = Settings().model_dump(mode="json")
    assert shown == expected


# ---------------------------------------------------------------------------
# Scenario 2 — `config init` then `config set` persists across the cache.
# ---------------------------------------------------------------------------


def test_config_init_then_set_persists(sqlite_env, tmp_path, monkeypatch) -> None:
    """With a bootstrapped ``./doc3gpp.toml`` and no env override, the
    end-to-end ``config init`` → ``config set sync.auto_sync true`` →
    ``config show`` chain must surface the persisted boolean (not the
    string ``"true"``). Proves the writer's cache-clear is wired into
    the cross-process boundary.
    """
    _seed_project_root(tmp_path, monkeypatch)

    init_result = Runner().invoke(app, ["config", "init"])
    assert init_result.exit_code == 0, init_result.output

    set_result = Runner().invoke(
        app, ["config", "set", "sync.auto_sync", "true"]
    )
    assert set_result.exit_code == 0, set_result.output

    show_result = Runner().invoke(app, ["config", "show"])
    assert show_result.exit_code == 0, show_result.output
    payload = show_result.output.split("\n", 1)[1]
    shown = json.loads(payload)
    assert shown["sync"]["auto_sync"] is True


# ---------------------------------------------------------------------------
# Scenario 3 — `config init` refuses when DOC3GPP_CONFIG pins a file.
# ---------------------------------------------------------------------------


def test_config_init_refuses_when_doc3gpp_config_set(
    sqlite_env, tmp_path, monkeypatch
) -> None:
    """With ``DOC3GPP_CONFIG`` set, ``config init`` must short-circuit
    and tell the operator to unset the explicit pin — the env var is
    the authoritative bootstrap source so ``config init`` cannot
    redirect the write elsewhere. Non-zero exit + diagnostic that
    names the env var.
    """
    # Given: a project root that *would* be a valid init target, plus
    # DOC3GPP_CONFIG pointing elsewhere. The pin must override.
    pinned = tmp_path / "pinned.toml"
    pinned.write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\n", encoding="utf-8"
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(pinned))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    get_settings.cache_clear()

    # When: config init is invoked despite the pin.
    result = Runner().invoke(app, ["config", "init"])

    # Then: non-zero exit and the diagnostic names DOC3GPP_CONFIG.
    assert result.exit_code != 0
    assert "DOC3GPP_CONFIG" in result.output


# ---------------------------------------------------------------------------
# Scenario 4 — `config init --force` overwrites a stale file.
# ---------------------------------------------------------------------------


def test_config_init_force_overwrites(sqlite_env, tmp_path, monkeypatch) -> None:
    """Pre-populate ``./doc3gpp.toml`` with garbage; ``config init
    --force`` must overwrite it with the packaged template. After the
    rewrite, ``load_config_data()`` parses cleanly and the on-disk
    bytes equal the packaged template.
    """
    _seed_project_root(tmp_path, monkeypatch)

    project_cfg = tmp_path / "doc3gpp.toml"
    garbage = "this is not valid toml ::: => [[["
    project_cfg.write_text(garbage, encoding="utf-8")

    template = _read_packaged_template()

    # When: --force bypasses the "file exists" refusal.
    result = Runner().invoke(
        app, ["config", "init", "--force"]
    )
    assert result.exit_code == 0, result.output

    # Then: the garbage is gone and the file matches the template exactly.
    on_disk = project_cfg.read_text(encoding="utf-8")
    assert garbage not in on_disk
    assert on_disk == template

    # And: the loader parses the rewritten file cleanly.
    path, data = load_config_data()
    assert path is not None and path.name == "doc3gpp.toml"
    assert data == {
        "search": {
            "enabled": True,
            "auto_index_on_parse": True,
            "rebuild_batch_size": 100,
            "snippet_tokens": 8,
            "bm25_weights": [5.0, 0.0, 0.0, 1.0, 5.0, 5.0, 5.0, 5.0],
        }
    }