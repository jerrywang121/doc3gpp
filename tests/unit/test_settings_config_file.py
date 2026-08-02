"""Unit tests for the TOML config-file layer of ``doc3gpp.settings``.

These tests do not touch the database. They cover:

* ``doc3gpp.settings.config_source`` — discovery order (env var >
  project-local > XDG default) and TOML parsing.
* ``doc3gpp.settings.schema`` — built-in defaults and validation rules.
* ``doc3gpp.settings.loader`` — precedence chain (env > file > defaults)
  and cache invalidation across ``monkeypatch`` boundaries.
* ``doc3gpp.cli`` — ``meeting sync`` year-window defaults,
  ``* list`` default fields and ``--format`` defaults, plus the
  ``doc3gpp config path`` / ``doc3gpp config show`` debug commands.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.settings.config_source import (
    DEFAULT_PROJECT_CONFIG,
    DEFAULT_USER_CONFIG,
    find_config_file,
    load_config_data,
)
from doc3gpp.settings.loader import get_settings
from doc3gpp.models.sync import SyncOutcome
from doc3gpp.settings.schema import (
    OutputFieldsSettings,
    OutputSettings,
    Settings,
    SyncSettings,
    TDocParseSettings,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def clean_settings(monkeypatch: pytest.MonkeyPatch):
    """Strip every DOC3GPP_* env var and clear the settings cache.

    Tests that need a specific env state should ``monkeypatch.setenv`` the
    keys they want *before* calling ``get_settings()``. The fixture
    enforces the canonical pattern documented in ``tests/conftest.py``.
    """
    for key in list(os.environ):
        if key.startswith("DOC3GPP_"):
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def write_toml(tmp_path: Path):
    """Factory fixture returning a ``(filename, content) -> Path`` writer."""

    def _write(name: str, content: str) -> Path:
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        return path

    return _write


Runner = CliRunner


# ---------------------------------------------------------------------------
# Defaults / schema
# ---------------------------------------------------------------------------


def test_built_in_defaults_match_previously_hardcoded_values(
    clean_settings,
) -> None:
    """The schema defaults must equal the values that previously lived as
    inline literals inside ``doc3gpp.cli`` — otherwise users who never
    create a config file would see different output than before.
    """
    s = get_settings()
    assert s.output.format == "table"
    assert s.output.fields.meeting == [
        "meeting_id",
        "name",
        "location",
        "start_date",
        "end_date",
        "ftp_url",
        "start_doc",
        "end_doc",
    ]
    assert s.output.fields.tdoc == [
        "tdoc_id",
        "meeting_name",
        "title",
        "source",
        "type",
        "status",
        "cr_cat",
        "spec",
        "version",
        "related_wis",
    ]
    assert s.output.fields.tsg == ["tsg_name", "short_name", "description"]
    assert s.output.fields.wi == ["wi_id", "acronym", "release", "name"]


def test_tdoc_parse_defaults_and_bounds(clean_settings) -> None:
    """``tdoc_parse`` defaults and validation rules are enforced."""
    from pydantic import ValidationError

    s = get_settings()
    assert s.tdoc_parse.max_batch == 100
    assert s.tdoc_parse.max_ftp_depth == 2

    TDocParseSettings(max_ftp_depth=0)
    TDocParseSettings(max_ftp_depth=10)
    with pytest.raises(ValidationError):
        TDocParseSettings(max_ftp_depth=-1)
    with pytest.raises(ValidationError):
        TDocParseSettings(max_ftp_depth=11)


def test_output_format_is_literal(clean_settings) -> None:
    """``format`` accepts only ``table``, ``json``, ``markdown``."""
    from pydantic import ValidationError

    OutputSettings(format="json")  # no raise
    OutputSettings(format="markdown")  # no raise
    with pytest.raises(ValidationError):
        OutputSettings(format="yaml")  # noqa: S604 - intentionally invalid


def test_settings_nested_default_factories(clean_settings) -> None:
    """Sub-models without a TOML entry must instantiate via their default
    factories so every list-command still has a ``default_fields`` list.
    """
    s = get_settings()
    assert isinstance(s.output, OutputSettings)
    assert isinstance(s.output.fields, OutputFieldsSettings)
    assert isinstance(s.tdoc_parse, TDocParseSettings)
    assert isinstance(s.sync, SyncSettings)
    assert len(s.output.fields.tdoc) > 0
    assert len(s.output.fields.wi) > 0


def test_sync_settings_defaults(clean_settings) -> None:
    """Default sync intervals match the documented values."""
    from datetime import timedelta

    s = get_settings()
    assert s.sync.meeting_sync_interval == timedelta(hours=24)
    assert s.sync.tdoc_list_sync_interval == timedelta(minutes=30)
    assert s.sync.tdoc_list_closed_window == timedelta(days=90)


def test_sync_settings_accepts_human_durations(clean_settings) -> None:
    """Durations may be supplied as human strings in code or config."""
    from datetime import timedelta

    settings = SyncSettings(
        meeting_sync_interval="12h",
        tdoc_list_sync_interval="45m",
        tdoc_list_closed_window="120d",
    )
    assert settings.meeting_sync_interval == timedelta(hours=12)
    assert settings.tdoc_list_sync_interval == timedelta(minutes=45)
    assert settings.tdoc_list_closed_window == timedelta(days=120)


def test_sync_settings_accepts_iso_durations(clean_settings) -> None:
    """Durations may be supplied as ISO 8601 strings."""
    from datetime import timedelta

    settings = SyncSettings(
        meeting_sync_interval="P1D",
        tdoc_list_sync_interval="PT30M",
        tdoc_list_closed_window="P90D",
    )
    assert settings.meeting_sync_interval == timedelta(days=1)
    assert settings.tdoc_list_sync_interval == timedelta(minutes=30)
    assert settings.tdoc_list_closed_window == timedelta(days=90)


def test_sync_settings_rejects_bad_durations(clean_settings) -> None:
    """Invalid duration strings raise a clear validation error."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SyncSettings(meeting_sync_interval="banana")
    with pytest.raises(ValidationError):
        SyncSettings(tdoc_list_sync_interval="-1h")


def test_sync_outcome_equality() -> None:
    """SyncOutcome is a frozen value object."""
    a = SyncOutcome(status="synced", reason="ok", synced_count=5)
    b = SyncOutcome(status="synced", reason="ok", synced_count=5)
    c = SyncOutcome(status="skipped", reason="too soon")
    assert a == b
    assert a != c


# ---------------------------------------------------------------------------
# TOML discovery
# ---------------------------------------------------------------------------


def test_find_returns_none_when_no_config_exists(
    clean_settings, tmp_path, monkeypatch,
) -> None:
    """With ``DOC3GPP_CONFIG`` unset and no project-local file, the
    XDG default is also absent because we point ``HOME`` at an empty
    tmp dir.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # Change into a directory with no doc3gpp.toml so project-local lookup misses.
    monkeypatch.chdir(tmp_path)
    assert find_config_file() is None


def test_find_returns_explicit_env_var(
    clean_settings, write_toml, monkeypatch,
) -> None:
    cfg = write_toml("custom.toml", "[output]\\nformat = \"json\"\\n")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    assert find_config_file() == cfg


def test_find_explicit_env_var_accepts_directory(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """Passing a *directory* resolves to ``<dir>/config.toml`` so users
    can choose between pointing at the file directly or at its folder.
    """
    cfg = write_toml("config.toml", "[output]\\nformat = \"json\"\\n")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg.parent))
    assert find_config_file() == cfg


def test_find_explicit_env_var_missing_path_raises(
    clean_settings, monkeypatch, tmp_path,
) -> None:
    """A bogus ``DOC3GPP_CONFIG`` value must surface as ``FileNotFoundError``
    so typos are loud, not silent.
    """
    monkeypatch.setenv("DOC3GPP_CONFIG", str(tmp_path / "does-not-exist.toml"))
    with pytest.raises(FileNotFoundError):
        find_config_file()


def test_find_project_local_beats_xdg(
    clean_settings, write_toml, monkeypatch, tmp_path,
) -> None:
    """``./doc3gpp.toml`` wins over the XDG user-wide file when both
    exist — project-local defaults should override user preferences.
    """
    project_cfg = write_toml("doc3gpp.toml", "[output]\\nformat = \"json\"\\n")
    user_dir = tmp_path / "user-cfg"
    user_dir.mkdir()
    (user_dir / "config.toml").write_text(
        "[output]\\nformat = \"json\"\\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(user_dir.parent))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user_dir.parent))
    monkeypatch.chdir(project_cfg.parent)
    found = find_config_file()
    assert found is not None
    assert found.name == "doc3gpp.toml"
    assert found == project_cfg.resolve()


def test_find_xdg_default_when_no_project_local(
    clean_settings, write_toml, monkeypatch, tmp_path,
) -> None:
    user_cfg = write_toml("config.toml", "[output]\\nformat = \"json\"\\n")
    # Layout the file at the canonical XDG location.
    xdg_root = tmp_path / "xdg"
    xdg_doc = xdg_root / "doc3gpp"
    xdg_doc.mkdir(parents=True)
    xdg_doc.joinpath("config.toml").write_text(
        user_cfg.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_root))
    monkeypatch.chdir(tmp_path)  # project-local lookup misses here
    found = find_config_file()
    assert found is not None
    assert found == xdg_doc / "config.toml"


def test_load_returns_empty_when_no_config(clean_settings, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    path, data = load_config_data()
    assert path is None
    assert data == {}


def test_load_parses_top_level_table(clean_settings, write_toml, monkeypatch) -> None:
    cfg = write_toml(
        "c.toml",
        "[output]\nformat = 'json'\n[cache]\nsize_limit_mb = 512\n",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    path, data = load_config_data()
    assert path == cfg
    assert data == {
        "output": {"format": "json"},
        "cache": {"size_limit_mb": 512},
    }


def test_load_malformed_toml_raises_with_path(
    clean_settings, write_toml, monkeypatch,
) -> None:
    cfg = write_toml("bad.toml", "this is = not = valid toml ===")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    with pytest.raises(ValueError, match=str(cfg)):
        load_config_data()


def test_settings_drops_unknown_top_level_keys(
    clean_settings,
) -> None:
    """``extra='ignore'`` is intentional: users sometimes keep unrelated
    metadata in the same file. Unknown keys must be dropped, not raised.
    """
    s = Settings(
        output={"format": "json"},
        unknown_section={"foo": "bar"},
    )
    assert s.output.format == "json"


# ---------------------------------------------------------------------------
# Loader precedence (env > file > defaults)
# ---------------------------------------------------------------------------


def test_env_overrides_toml_for_cache_dir(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """``DOC3GPP_CACHE__DIR`` is allowlisted and therefore overrides the
    TOML value at runtime — same precedence chain as before, just for
    the much smaller set of allowlisted env vars."""
    cfg = write_toml("c.toml", '[cache]\ndir = "/tmp/from-toml"\n')
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setenv("DOC3GPP_CACHE__DIR", "/tmp/from-env")
    get_settings.cache_clear()
    s = get_settings()
    assert str(s.cache.dir) == "/tmp/from-env"  # env wins


def test_tdoc_parse_max_ftp_depth_is_toml_only(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """``DOC3GPP_TDOC_PARSE__MAX_FTP_DEPTH`` is **not** allowlisted, so
    setting it has no effect — the TOML value (or default) wins."""
    cfg = write_toml("c.toml", "[tdoc_parse]\nmax_ftp_depth = 5\n")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setenv("DOC3GPP_TDOC_PARSE__MAX_FTP_DEPTH", "3")
    get_settings.cache_clear()
    s = get_settings()
    assert s.tdoc_parse.max_ftp_depth == 5
    assert s.tdoc_parse.max_batch == 100


def test_env_overrides_toml_for_flat_fields(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """Allowlisted flat env vars (``DOC3GPP_DATABASE_URL``,
    ``DOC3GPP_LOG_LEVEL``) still beat TOML at runtime."""
    cfg = write_toml(
        "c.toml",
        'database_url = "sqlite+pysqlite:////tmp/from-toml.db"\n'
        'log_level = "WARNING"\n',
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setenv(
        "DOC3GPP_DATABASE_URL", "sqlite+pysqlite:////tmp/from-env.db"
    )
    monkeypatch.setenv("DOC3GPP_LOG_LEVEL", "ERROR")
    get_settings.cache_clear()
    s = get_settings()
    assert s.database_url == "sqlite+pysqlite:////tmp/from-env.db"
    assert s.log_level == "ERROR"


def test_cache_clear_picks_up_new_env(clean_settings, monkeypatch) -> None:
    """The ``lru_cache`` wrapper must yield fresh values once a test
    mutates an allowlisted env var — the canonical pattern documented
    for ``sqlite_env``.
    """
    s1 = get_settings()
    assert str(s1.cache.dir) != "/tmp/from-cache-clear-env"
    monkeypatch.setenv("DOC3GPP_CACHE__DIR", "/tmp/from-cache-clear-env")
    # Without cache_clear the cached instance keeps the old value.
    assert str(get_settings().cache.dir) != "/tmp/from-cache-clear-env"
    get_settings.cache_clear()
    assert str(get_settings().cache.dir) == "/tmp/from-cache-clear-env"


def test_non_allowlisted_env_vars_are_silently_ignored(
    clean_settings, monkeypatch,
) -> None:
    """The closed :data:`ALLOWED_ENV_VARS` allowlist means setting a
    ``DOC3GPP_*`` env var outside the allowset has no effect — the
    field falls back to its default. This locks in the new policy so
    regressions back to "any DOC3GPP_* works" are caught."""
    from doc3gpp.settings.schema import ALLOWED_ENV_VARS

    # Sanity check: every var below is outside the allowlist.
    assert "DOC3GPP_OUTPUT__FORMAT" not in ALLOWED_ENV_VARS
    assert "DOC3GPP_TDOC_PARSE__MAX_BATCH" not in ALLOWED_ENV_VARS

    monkeypatch.setenv("DOC3GPP_OUTPUT__FORMAT", "json")
    monkeypatch.setenv("DOC3GPP_TDOC_PARSE__MAX_BATCH", "500")
    monkeypatch.setenv("DOC3GPP_HTTP_MAX_RETRIES", "7")
    monkeypatch.setenv("DOC3GPP_CACHE__SIZE_LIMIT_MB", "999")
    monkeypatch.setenv("DOC3GPP_CACHE__PURGE_CONFIRM", "false")
    monkeypatch.setenv("DOC3GPP_DB_AUTO_MIGRATE", "false")
    monkeypatch.setenv("DOC3GPP_HTTP_RETRY_BACKOFF", "2.5")

    get_settings.cache_clear()
    s = get_settings()
    assert s.output.format == "table"
    assert s.tdoc_parse.max_batch == 100
    assert s.http_max_retries == 3
    assert s.cache.size_limit_mb == 1024
    assert s.cache.purge_confirm is True
    assert s.db_auto_migrate is True
    assert s.http_retry_backoff == 0.5


def test_allowlisted_env_vars_override_toml(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """All six allowlisted vars take precedence over the TOML file
    when both are set."""
    from doc3gpp.settings.schema import ALLOWED_ENV_VARS

    cfg = write_toml(
        "c.toml",
        'database_url = "sqlite+pysqlite:////tmp/toml.db"\n'
        'db_echo = false\n'
        'log_level = "INFO"\n'
        'http_verify = false\n'
        '[cache]\n'
        'dir = "/tmp/toml-cache"\n'
        '[sync]\n'
        'auto_sync = false\n',
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setenv(
        "DOC3GPP_DATABASE_URL", "sqlite+pysqlite:////tmp/env.db"
    )
    monkeypatch.setenv("DOC3GPP_DB_ECHO", "true")
    monkeypatch.setenv("DOC3GPP_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("DOC3GPP_HTTP_VERIFY", "true")
    monkeypatch.setenv("DOC3GPP_CACHE__DIR", "/tmp/env-cache")
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "true")

    get_settings.cache_clear()
    s = get_settings()
    assert len(ALLOWED_ENV_VARS) == 6
    assert s.database_url == "sqlite+pysqlite:////tmp/env.db"
    assert s.db_echo is True
    assert s.log_level == "DEBUG"
    assert s.http_verify is True
    assert str(s.cache.dir) == "/tmp/env-cache"
    assert s.sync.auto_sync is True


def test_tdoc_parse_max_tdoc_size_kb_env_var_is_ignored(monkeypatch) -> None:
    """``DOC3GPP_TDOC_PARSE__MAX_TDOC_SIZE_KB`` is outside the env-var
    allowlist, so it is silently dropped and the default applies —
    mirrors the existing :func:`test_tdoc_parse_max_batch_env_var_is_ignored`
    policy for the sibling knob.
    """
    from doc3gpp.settings.schema import ALLOWED_ENV_VARS

    assert "DOC3GPP_TDOC_PARSE__MAX_TDOC_SIZE_KB" not in ALLOWED_ENV_VARS
    monkeypatch.delenv("DOC3GPP_CONFIG", raising=False)
    monkeypatch.setenv("DOC3GPP_TDOC_PARSE__MAX_TDOC_SIZE_KB", "256")
    get_settings.cache_clear()
    try:
        assert get_settings().tdoc_parse.max_tdoc_size_kb == 1000
    finally:
        get_settings.cache_clear()


def test_output_compact_env_var_is_ignored(monkeypatch) -> None:
    """``DOC3GPP_OUTPUT__COMPACT`` is outside the env-var allowlist and
    must be silently dropped — the TOML/default value wins. Mirrors
    ``test_tdoc_parse_max_tdoc_size_kb_env_var_is_ignored``."""
    from doc3gpp.settings.schema import ALLOWED_ENV_VARS

    assert "DOC3GPP_OUTPUT__COMPACT" not in ALLOWED_ENV_VARS
    monkeypatch.delenv("DOC3GPP_CONFIG", raising=False)
    monkeypatch.setenv("DOC3GPP_OUTPUT__COMPACT", "true")
    get_settings.cache_clear()
    try:
        assert get_settings().output.compact is False
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_meeting_list_uses_settings_default_fields(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """``meeting list`` columns come from ``settings.output.fields.meeting``."""
    cfg = write_toml(
        "c.toml",
        '[output.fields]\nmeeting = ["meeting_id", "name"]\n',
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    get_settings.cache_clear()

    from doc3gpp.models.meeting import Meeting
    from doc3gpp.services import meetings_service

    fake_meeting = Meeting(
        meeting_id=1, name="R5#1", title="t", location="loc",
        start_date=None, end_date=None,
    )
    monkeypatch.setattr(
        meetings_service.MeetingService,
        "list_recent",
        lambda self, **_: [fake_meeting],
    )

    result = Runner().invoke(app, ["meeting", "list", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == [{"meeting_id": "1", "name": "R5#1"}]


def test_meeting_list_format_defaults_from_settings(
    clean_settings, write_toml, monkeypatch,
) -> None:
    cfg = write_toml("c.toml", '[output]\nformat = "json"\n')
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    get_settings.cache_clear()

    from doc3gpp.models.meeting import Meeting
    from doc3gpp.services import meetings_service

    monkeypatch.setattr(
        meetings_service.MeetingService,
        "list_recent",
        lambda self, **_: [Meeting(meeting_id=1, name="R5#1", title="t", location="l")],
    )

    result = Runner().invoke(app, ["meeting", "list"])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert isinstance(parsed, list)


def test_cli_format_flag_overrides_settings(
    clean_settings, write_toml, monkeypatch,
) -> None:
    cfg = write_toml("c.toml", '[output]\nformat = "json"\n')
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    get_settings.cache_clear()

    from doc3gpp.models.meeting import Meeting
    from doc3gpp.services import meetings_service

    monkeypatch.setattr(
        meetings_service.MeetingService,
        "list_recent",
        lambda self, **_: [Meeting(meeting_id=1, name="R5#1", title="t", location="l")],
    )

    result = Runner().invoke(app, ["meeting", "list", "--format", "markdown"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("| meeting_id |")


def test_wi_list_uses_settings_default_fields(
    clean_settings, write_toml, monkeypatch,
) -> None:
    cfg = write_toml(
        "c.toml",
        '[output.fields]\nwi = ["wi_id", "name"]\n',
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    get_settings.cache_clear()

    from doc3gpp.models.wi import Wi
    from doc3gpp.services import wi_service

    monkeypatch.setattr(
        wi_service.WiService,
        "list_recent",
        lambda self, **_: [Wi(wi_id=1, acronym="ACR", release="Rel-19", name="y", tsg_short="R5")],
    )

    result = Runner().invoke(app, ["wi", "list", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == [{"wi_id": "1", "name": "y"}]


def test_config_path_with_no_file(
    clean_settings, monkeypatch, tmp_path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    result = Runner().invoke(app, ["config", "path"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "(no config file found)"


def test_config_path_with_env_var(clean_settings, write_toml, monkeypatch) -> None:
    cfg = write_toml("pinned.toml", "")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    result = Runner().invoke(app, ["config", "path"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == str(cfg)


def test_config_show_emits_json(clean_settings, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    result = Runner().invoke(app, ["config", "show"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("# config source:")
    body = json.loads("\n".join(lines[1:]))
    assert body["output"]["format"] == "table"
    assert body["tdoc_parse"]["max_batch"] == 100


# ---------------------------------------------------------------------------
# Sanity: the two public default config paths are well-formed and unique.
# ---------------------------------------------------------------------------


def test_default_paths_are_distinct() -> None:
    """Both fallbacks must point to different files; otherwise a user
    with a stale project-local file would never see their XDG override.
    """
    assert DEFAULT_PROJECT_CONFIG != DEFAULT_USER_CONFIG
    assert DEFAULT_PROJECT_CONFIG.name == "doc3gpp.toml"
    assert DEFAULT_USER_CONFIG.name == "config.toml"