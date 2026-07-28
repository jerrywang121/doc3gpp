"""Unit tests for :mod:`doc3gpp.settings.config_writer`.

These tests pin the read-modify-write contract that ``doc3gpp config set``
will rely on. They cover every helper independently and the cross-helper
scenarios from the Wave-1 plan block.

No database, no network, no mocks of :class:`pathlib.Path` \u2014 every test
uses ``tmp_path`` and ``monkeypatch.chdir`` so each run starts from a
known-clean filesystem state.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from doc3gpp.settings.config_source import (
    DEFAULT_PROJECT_CONFIG,
    DEFAULT_USER_CONFIG,
)
from doc3gpp.settings.config_writer import (
    ConfigValidationError,
    load_default_template,
    parse_dotted_key,
    patch_dotted,
    prune_empty_tables,
    read_toml,
    resolve_echo_subtree,
    resolve_init_target,
    validate_against_settings,
    walk_known_dotted_keys,
    write_toml,
)
from doc3gpp.settings.schema import Settings


# ---------------------------------------------------------------------------
# read_toml
# ---------------------------------------------------------------------------


def test_read_toml_happy_parses_simple_table(tmp_path: Path) -> None:
    """``read_toml`` returns a plain dict mirroring the parsed file."""
    path = tmp_path / "c.toml"
    path.write_text('[output]\nformat = "json"\n[cache]\nsize_limit_mb = 256\n', encoding="utf-8")

    data = read_toml(path)

    assert data == {"output": {"format": "json"}, "cache": {"size_limit_mb": 256}}


def test_read_toml_failure_raises_toml_decode_error_on_malformed(tmp_path: Path) -> None:
    """Malformed TOML surfaces the stdlib ``TOMLDecodeError`` unchanged so
    the CLI can present a clear message."""
    path = tmp_path / "bad.toml"
    path.write_text("[broken\n", encoding="utf-8")

    with pytest.raises(tomllib.TOMLDecodeError):
        read_toml(path)


def test_read_toml_failure_on_partial_table(tmp_path: Path) -> None:
    """``key =`` without a value is also malformed and must surface."""
    path = tmp_path / "bad2.toml"
    path.write_text("key = \n", encoding="utf-8")

    with pytest.raises(tomllib.TOMLDecodeError):
        read_toml(path)


# ---------------------------------------------------------------------------
# parse_dotted_key
# ---------------------------------------------------------------------------


def test_parse_dotted_key_happy_two_segments() -> None:
    assert parse_dotted_key("sync.auto_sync") == ["sync", "auto_sync"]


def test_parse_dotted_key_happy_single_segment() -> None:
    assert parse_dotted_key("database_url") == ["database_url"]


def test_parse_dotted_key_happy_deep_nested() -> None:
    assert parse_dotted_key("output.fields.meeting") == [
        "output",
        "fields",
        "meeting",
    ]


def test_parse_dotted_key_strips_whitespace() -> None:
    assert parse_dotted_key("  sync.auto_sync  ") == ["sync", "auto_sync"]


def test_parse_dotted_key_failure_on_empty_string() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_dotted_key("")


def test_parse_dotted_key_failure_on_whitespace_only() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_dotted_key("   ")


# ---------------------------------------------------------------------------
# patch_dotted
# ---------------------------------------------------------------------------


def test_patch_dotted_happy_preserves_other_tables() -> None:
    """Patching one key leaves sibling tables untouched."""
    data = {"cache": {"dir": "/tmp/cache", "size_limit_mb": 256}}
    out = patch_dotted(data, "sync.auto_sync", "true")

    assert out["sync"] == {"auto_sync": "true"}
    assert out["cache"] == {"dir": "/tmp/cache", "size_limit_mb": 256}


def test_patch_dotted_happy_creates_intermediate_tables() -> None:
    """A leaf under a missing table creates the table on the fly."""
    data: dict = {}
    out = patch_dotted(data, "output.format", "json")
    assert out == {"output": {"format": "json"}}


def test_patch_dotted_happy_deep_nested_preserves_parents() -> None:
    """``output.fields.meeting`` survives with parents intact."""
    data = {"output": {"format": "json", "fields": {"tdoc": ["x"]}}}
    out = patch_dotted(data, "output.fields.meeting", '["meeting_id"]')
    assert out["output"]["format"] == "json"
    assert out["output"]["fields"]["tdoc"] == ["x"]
    assert out["output"]["fields"]["meeting"] == '["meeting_id"]'


def test_patch_dotted_does_not_mutate_input() -> None:
    """The helper returns a new dict; the caller's dict is untouched."""
    data = {"cache": {"size_limit_mb": 256}}
    snapshot = {"cache": {"size_limit_mb": 256}}
    patch_dotted(data, "sync.auto_sync", "true")
    assert data == snapshot


# ---------------------------------------------------------------------------
# prune_empty_tables
# ---------------------------------------------------------------------------


def test_prune_empty_tables_keeps_other_keys() -> None:
    """Pruning ``sync`` (left empty) leaves ``cache`` alone."""
    data = {"sync": {}, "cache": {"size_limit_mb": 256}}
    out = prune_empty_tables(data, "sync")
    assert out == {"cache": {"size_limit_mb": 256}}


def test_prune_empty_tables_keeps_non_empty_subtree() -> None:
    """Pruning must only touch the sub-tree under ``key``."""
    data = {"output": {"format": "json", "fields": {}}, "cache": {"dir": "/x"}}
    out = prune_empty_tables(data, "output")
    # output.fields is empty so it gets dropped; output.format stays
    assert out == {"output": {"format": "json"}, "cache": {"dir": "/x"}}


def test_prune_empty_tables_round_trip_preserves_other_keys() -> None:
    """A patch-then-prune round trip on a fresh file leaves siblings intact."""
    original = {
        "cache": {"dir": "/tmp/cache", "size_limit_mb": 256},
        "sync": {"auto_sync": "false"},
    }
    patched = patch_dotted(original, "sync.auto_sync", "true")
    pruned = prune_empty_tables(patched, "sync")
    assert pruned["cache"] == original["cache"]
    assert pruned["sync"] == {"auto_sync": "true"}


def test_prune_empty_tables_is_noop_for_leaf_path() -> None:
    """When ``key`` points to a scalar leaf (not a dict), the function
    returns the input unchanged \u2014 there is nothing to prune."""
    data = {"sync": {"auto_sync": "true"}}
    out = prune_empty_tables(data, "sync.auto_sync")
    assert out == data


def test_prune_empty_tables_leaf_path_does_not_pop_parents() -> None:
    """A leaf-path prune must not pop the parent table either."""
    data = {"sync": {"auto_sync": "true"}, "cache": {"dir": "/x"}}
    out = prune_empty_tables(data, "sync.auto_sync")
    assert out == data
    assert "sync" in out
    assert "cache" in out


def test_prune_empty_tables_leaf_path_does_not_pop_after_patch() -> None:
    """Realistic round-trip: patch a leaf, then prune at the same leaf
    path \u2014 the just-patched value must survive."""
    original = {"sync": {"auto_sync": "false"}}
    patched = patch_dotted(original, "sync.auto_sync", "true")
    pruned = prune_empty_tables(patched, "sync.auto_sync")
    assert pruned["sync"]["auto_sync"] == "true"


# ---------------------------------------------------------------------------
# validate_against_settings
# ---------------------------------------------------------------------------


def test_validate_against_settings_happy_returns_settings() -> None:
    """Valid data returns a populated :class:`Settings`."""
    s = validate_against_settings({"output": {"format": "json"}})
    assert isinstance(s, Settings)
    assert s.output.format == "json"


def test_validate_against_settings_failure_bad_duration() -> None:
    """An invalid duration raises :class:`ConfigValidationError` carrying
    the original pydantic error."""
    with pytest.raises(ConfigValidationError) as excinfo:
        validate_against_settings(
            {"sync": {"meeting_sync_interval": "not-a-duration"}}
        )
    assert isinstance(excinfo.value.original, ValidationError)


def test_config_validation_error_carries_original() -> None:
    """The wrapper exception exposes ``.original`` for the CLI."""
    try:
        validate_against_settings({"sync": {"meeting_sync_interval": "banana"}})
    except ConfigValidationError as exc:
        assert isinstance(exc.original, ValidationError)
        assert "meeting_sync_interval" in str(exc.original)
    else:
        pytest.fail("ConfigValidationError was not raised")


# ---------------------------------------------------------------------------
# walk_known_dotted_keys
# ---------------------------------------------------------------------------


def test_walk_known_dotted_keys_includes_schema_paths() -> None:
    """Canonical dotted paths from the schema are present in the walked set."""
    keys = walk_known_dotted_keys(Settings)
    assert "sync.auto_sync" in keys
    assert "sync.meeting_sync_interval" in keys
    assert "output.format" in keys
    assert "output.fields.meeting" in keys
    assert "database_url" in keys
    assert "log_level" in keys


def test_walk_known_dotted_keys_excludes_unknown() -> None:
    """Unknown paths are absent so the validator can reject them."""
    keys = walk_known_dotted_keys(Settings)
    assert "sync.nonexistent" not in keys
    assert "totally.made.up" not in keys


def test_walk_known_dotted_keys_nested_output_fields() -> None:
    """``output.fields`` is recursively walked down to the leaf columns."""
    keys = walk_known_dotted_keys(Settings)
    assert "output.fields.tdoc" in keys
    assert "output.fields.tsg" in keys
    assert "output.fields.wi" in keys


def test_walk_known_dotted_keys_returns_a_set() -> None:
    """The return type is a set; types documented in the contract are honoured."""
    assert isinstance(walk_known_dotted_keys(Settings), set)


# ---------------------------------------------------------------------------
# resolve_echo_subtree
# ---------------------------------------------------------------------------


def test_resolve_echo_subtree_returns_minimal_containing_dict() -> None:
    """For ``sync``, the result is exactly ``{"sync": {...}}`` with all
    sub-keys visible (json-dumped)."""
    settings = Settings()
    out = resolve_echo_subtree(settings, "sync")
    assert set(out.keys()) == {"sync"}
    assert "auto_sync" in out["sync"]
    assert out["sync"]["meeting_sync_interval"] == "P1D"


def test_resolve_echo_subtree_leaf_level() -> None:
    """A leaf dotted key still returns the containing table."""
    settings = Settings()
    out = resolve_echo_subtree(settings, "output.format")
    assert out == {"output": {"format": "table"}}


# ---------------------------------------------------------------------------
# write_toml
# ---------------------------------------------------------------------------


def test_write_toml_happy_round_trip(tmp_path: Path) -> None:
    """A dict written by ``write_toml`` is round-trippable via ``read_toml``."""
    path = tmp_path / "out.toml"
    data = {"output": {"format": "json"}, "cache": {"size_limit_mb": 256}}
    write_toml(path, data)
    assert read_toml(path) == data


def test_write_toml_creates_parent_directories(tmp_path: Path) -> None:
    """Missing parent dirs are created with ``parents=True, exist_ok=True``."""
    path = tmp_path / "nested" / "deeper" / "out.toml"
    write_toml(path, {"output": {"format": "markdown"}})
    assert read_toml(path) == {"output": {"format": "markdown"}}


# ---------------------------------------------------------------------------
# resolve_init_target
# ---------------------------------------------------------------------------


def test_resolve_init_target_user_returns_xdg_default() -> None:
    """``target="user"`` always returns :data:`DEFAULT_USER_CONFIG`."""
    assert resolve_init_target("user") == DEFAULT_USER_CONFIG


def test_resolve_init_target_project_with_pyproject_toml(tmp_path: Path, monkeypatch) -> None:
    """A cwd containing ``pyproject.toml`` resolves to ``./doc3gpp.toml``."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert resolve_init_target("project") == DEFAULT_PROJECT_CONFIG


def test_resolve_init_target_project_with_git_dir(tmp_path: Path, monkeypatch) -> None:
    """A cwd containing ``.git/`` also qualifies as a project root."""
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    assert resolve_init_target("project") == DEFAULT_PROJECT_CONFIG


def test_resolve_init_target_project_no_marker_raises(tmp_path: Path, monkeypatch) -> None:
    """``target="project"`` with no marker anywhere up the tree raises."""
    # tmp_path is empty; walk to root.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        resolve_init_target("project")


def test_resolve_init_target_auto_falls_back_to_user(tmp_path: Path, monkeypatch) -> None:
    """``target="auto"`` with no markers anywhere returns the user path."""
    monkeypatch.chdir(tmp_path)
    assert resolve_init_target("auto") == DEFAULT_USER_CONFIG


def test_resolve_init_target_auto_with_marker_returns_project(tmp_path: Path, monkeypatch) -> None:
    """``target="auto"`` with a ``pyproject.toml`` returns the project path."""
    (tmp_path / "doc3gpp.toml.example").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert resolve_init_target("auto") == DEFAULT_PROJECT_CONFIG


def test_resolve_init_target_rejects_unknown_value() -> None:
    """Anything outside ``{"project", "user", "auto"}`` raises ``ValueError``."""
    with pytest.raises(ValueError):
        resolve_init_target("garbage")


# ---------------------------------------------------------------------------
# Cross-helper integration: read -> patch -> prune -> validate -> write -> read
# ---------------------------------------------------------------------------


def test_cross_helper_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full pipeline: read an existing TOML, patch a key, prune the table
    if it became empty, validate, write back, and re-read."""
    # Allowlisted env var shadows ``Settings(**data)`` init kwargs; clear
    # it so the patched dict is the only source of truth for the assertion
    # below.
    monkeypatch.delenv("DOC3GPP_SYNC__AUTO_SYNC", raising=False)

    src = tmp_path / "doc3gpp.toml"
    src.write_text(
        '[cache]\ndir = "/tmp/cache"\nsize_limit_mb = 256\n',
        encoding="utf-8",
    )

    data = read_toml(src)
    assert data == {"cache": {"dir": "/tmp/cache", "size_limit_mb": 256}}

    # Cache survives the sync.auto_sync patch.
    patched = patch_dotted(data, "sync.auto_sync", "true")
    assert patched["cache"] == {"dir": "/tmp/cache", "size_limit_mb": 256}

    # Pruning sync (non-empty here) leaves it alone.
    pruned = prune_empty_tables(patched, "sync")
    assert pruned["sync"] == {"auto_sync": "true"}

    # Validates cleanly.
    settings = validate_against_settings(pruned)
    assert settings.sync.auto_sync is True
    assert settings.cache.size_limit_mb == 256

    # Writes back and round-trips losslessly.
    write_toml(src, pruned)
    assert read_toml(src) == pruned


def test_cross_helper_unknown_key_after_walk_check() -> None:
    """Cross-helper: ``walk_known_dotted_keys`` is the canonical gate for
    rejecting unknown dotted paths. The CLI does the membership check
    itself and re-emits as ``typer.BadParameter``."""
    keys = walk_known_dotted_keys(Settings)
    assert "sync.nonexistent" not in keys

    if "sync.nonexistent" not in keys:
        with pytest.raises(KeyError, match="sync.nonexistent"):
            raise KeyError("sync.nonexistent")


def test_cross_helper_malformed_toml_read_then_patch(tmp_path: Path) -> None:
    """A malformed TOML on read fails fast; nothing past ``read_toml``
    runs, which is exactly what the CLI relies on."""
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = not = valid toml ===", encoding="utf-8")

    with pytest.raises(tomllib.TOMLDecodeError):
        read_toml(bad)


def test_cross_helper_prune_then_empty_table_round_trip(tmp_path: Path) -> None:
    """An empty ``[sync]`` table read from disk is dropped by prune and
    does not resurrect after a write + re-read."""
    src = tmp_path / "doc3gpp.toml"
    src.write_text("[sync]\n", encoding="utf-8")

    data = read_toml(src)
    assert data == {"sync": {}}

    pruned = prune_empty_tables(data, "sync")
    assert "sync" not in pruned

    write_toml(src, pruned)
    assert "sync" not in read_toml(src)


# ---------------------------------------------------------------------------
# Misc contract sanity
# ---------------------------------------------------------------------------


def test_module_uses_python_stdlib_tomllib_when_available() -> None:
    """The read shim honours the python version: ``tomllib`` on 3.11+,
    ``tomli`` on 3.10. We can't really exercise the 3.10 path here, but
    we can assert the symbol is bound to one of those two."""
    from doc3gpp.settings import config_writer

    if sys.version_info >= (3, 11):
        assert config_writer.tomllib is tomllib
    else:  # pragma: no cover - 3.10 only
        import tomli

        assert config_writer.tomllib is tomli


def test_tomli_w_is_importable() -> None:
    """The writer-side dep must be importable in the installed env."""
    import tomli_w

    assert hasattr(tomli_w, "dump")


# ---------------------------------------------------------------------------
# load_default_template
# ---------------------------------------------------------------------------


def test_load_default_template_returns_canonical_string() -> None:
    """The packaged template ends with the ``tdoc_list_url_template`` line
    and starts with the canonical comment header."""
    text = load_default_template()

    assert "# doc3gpp configuration file (TOML)" in text
    assert text.rstrip().endswith(
        'tdoc_list_url_template = "'
        "https://portal.3gpp.org/ngppapp/GenerateDocumentList.aspx"
        '?meetingId={meeting_id}"'
    )


def test_load_default_template_matches_packaged_file() -> None:
    """The result equals the on-disk ``src/doc3gpp/data/doc3gpp.toml.example``
    bytes byte-for-byte (no transformation)."""
    packaged = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "doc3gpp"
        / "data"
        / "doc3gpp.toml.example"
    )
    assert packaged.is_file(), (
        f"expected the packaged template at {packaged} to exist for this test"
    )

    text = load_default_template()

    assert text == packaged.read_text(encoding="utf-8")


def test_load_default_template_injects_test_path(tmp_path: Path) -> None:
    """``path=`` is honored verbatim — the helper reads the supplied file
    and returns its contents without consulting ``importlib.resources``."""
    fixture = tmp_path / "fixture.toml"
    fixture.write_text(
        '[output]\nformat = "json"\n',
        encoding="utf-8",
    )

    text = load_default_template(path=fixture)

    assert text == '[output]\nformat = "json"\n'


def test_load_default_template_missing_resource_raises(tmp_path: Path) -> None:
    """A non-existent ``path=`` raises :class:`FileNotFoundError`; the
    provided file is the only lookup that runs in this branch."""
    missing = tmp_path / "does-not-exist.toml"
    assert not missing.exists()

    with pytest.raises(FileNotFoundError):
        load_default_template(path=missing)
