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
from doc3gpp.settings.schema import (
    MeetingSyncSettings,
    OutputFieldsSettings,
    OutputSettings,
    Settings,
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
    assert s.meeting_sync.closed_years == 2
    assert s.meeting_sync.future_years == 1
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


def test_meeting_sync_validation_bounds(clean_settings) -> None:
    """``closed_years`` and ``future_years`` are bounded; out-of-range
    values must raise rather than silently truncating.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MeetingSyncSettings(closed_years=99)
    with pytest.raises(ValidationError):
        MeetingSyncSettings(future_years=99)


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
    assert isinstance(s.meeting_sync, MeetingSyncSettings)
    assert isinstance(s.output, OutputSettings)
    assert isinstance(s.output.fields, OutputFieldsSettings)
    assert len(s.output.fields.tdoc) > 0
    assert len(s.output.fields.wi) > 0


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
    cfg = write_toml("custom.toml", "[meeting_sync]\nclosed_years = 7\n")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    assert find_config_file() == cfg


def test_find_explicit_env_var_accepts_directory(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """Passing a *directory* resolves to ``<dir>/config.toml`` so users
    can choose between pointing at the file directly or at its folder.
    """
    cfg = write_toml("config.toml", "[meeting_sync]\nclosed_years = 7\n")
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
    project_cfg = write_toml("doc3gpp.toml", "[meeting_sync]\nclosed_years = 4\n")
    user_dir = tmp_path / "user-cfg"
    user_dir.mkdir()
    (user_dir / "config.toml").write_text(
        "[meeting_sync]\nclosed_years = 9\n", encoding="utf-8"
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
    user_cfg = write_toml("config.toml", "[meeting_sync]\nclosed_years = 9\n")
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
        "[meeting_sync]\nclosed_years = 4\n[output]\nformat = 'json'\n",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    path, data = load_config_data()
    assert path == cfg
    assert data == {
        "meeting_sync": {"closed_years": 4},
        "output": {"format": "json"},
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
        meeting_sync={"closed_years": 4},
        unknown_section={"foo": "bar"},
    )
    assert s.meeting_sync.closed_years == 4


# ---------------------------------------------------------------------------
# Loader precedence (env > file > defaults)
# ---------------------------------------------------------------------------


def test_toml_overrides_defaults(clean_settings, write_toml, monkeypatch) -> None:
    cfg = write_toml("c.toml", "[meeting_sync]\nclosed_years = 7\n")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    get_settings.cache_clear()
    s = get_settings()
    assert s.meeting_sync.closed_years == 7
    # Future years not set in TOML -> still default 1.
    assert s.meeting_sync.future_years == 1


def test_env_overrides_toml(clean_settings, write_toml, monkeypatch) -> None:
    cfg = write_toml(
        "c.toml",
        "[meeting_sync]\nclosed_years = 7\nfuture_years = 4\n",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setenv("DOC3GPP_MEETING_SYNC__CLOSED_YEARS", "15")
    get_settings.cache_clear()
    s = get_settings()
    assert s.meeting_sync.closed_years == 15  # env wins
    assert s.meeting_sync.future_years == 4   # TOML still wins over default


def test_env_overrides_toml_for_output_format(
    clean_settings, write_toml, monkeypatch,
) -> None:
    cfg = write_toml("c.toml", '[output]\nformat = "json"\n')
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setenv("DOC3GPP_OUTPUT__FORMAT", "markdown")
    get_settings.cache_clear()
    s = get_settings()
    assert s.output.format == "markdown"  # env wins


def test_tdoc_parse_max_ftp_depth_overrides(clean_settings, write_toml, monkeypatch) -> None:
    """TOML and env both override ``max_ftp_depth`` with the usual precedence."""
    cfg = write_toml("c.toml", "[tdoc_parse]\nmax_ftp_depth = 5\n")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    get_settings.cache_clear()
    s = get_settings()
    assert s.tdoc_parse.max_ftp_depth == 5

    monkeypatch.setenv("DOC3GPP_TDOC_PARSE__MAX_FTP_DEPTH", "3")
    get_settings.cache_clear()
    s = get_settings()
    assert s.tdoc_parse.max_ftp_depth == 3  # env wins
    assert s.tdoc_parse.max_batch == 100


def test_env_overrides_toml_for_flat_fields(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """Backward compat: existing DOC3GPP_* flat env vars still win over
    any TOML value for the same field.
    """
    cfg = write_toml("c.toml", 'db_pool_size = 1\nlog_level = "WARNING"\n')
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setenv("DOC3GPP_DB_POOL_SIZE", "50")
    monkeypatch.setenv("DOC3GPP_LOG_LEVEL", "ERROR")
    get_settings.cache_clear()
    s = get_settings()
    assert s.db_pool_size == 50
    assert s.log_level == "ERROR"


def test_env_overrides_only_set_keys(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """A partial TOML + a partial env together produce a merged result
    where each key is resolved independently against its highest source.
    """
    cfg = write_toml(
        "c.toml",
        "[meeting_sync]\nclosed_years = 7\nfuture_years = 4\n[output]\nformat = 'json'\n",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setenv("DOC3GPP_MEETING_SYNC__FUTURE_YEARS", "9")
    get_settings.cache_clear()
    s = get_settings()
    assert s.meeting_sync.closed_years == 7   # only TOML set
    assert s.meeting_sync.future_years == 9   # env wins
    assert s.output.format == "json"          # only TOML set


def test_cache_clear_picks_up_new_env(clean_settings, monkeypatch) -> None:
    """The ``lru_cache`` wrapper must yield fresh values once a test
    mutates the env — the canonical pattern documented for
    ``sqlite_env``.
    """
    s1 = get_settings()
    assert s1.db_pool_size == 5
    monkeypatch.setenv("DOC3GPP_DB_POOL_SIZE", "42")
    # Without cache_clear the cached instance keeps the old value.
    assert get_settings().db_pool_size == 5
    get_settings.cache_clear()
    assert get_settings().db_pool_size == 42


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_meeting_sync_uses_settings_defaults(
    clean_settings, write_toml, monkeypatch,
) -> None:
    """When neither ``--closed-years`` nor ``--future-years`` is given,
    the values come from ``settings.meeting_sync.*``.
    """
    cfg = write_toml(
        "c.toml",
        "[meeting_sync]\nclosed_years = 5\nfuture_years = 3\n",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    get_settings.cache_clear()

    captured: dict = {}

    def fake_sync(self, meetings_url, max_year_closed, max_year_future, today=None, tsg=None):
        captured["closed"] = max_year_closed
        captured["future"] = max_year_future
        return 0

    from doc3gpp.services import meetings_service

    monkeypatch.setattr(meetings_service.MeetingService, "sync", fake_sync)
    # Auto-seed TSG table on demand.
    from doc3gpp.services.tsg_service import TsgService

    monkeypatch.setattr(TsgService, "count", lambda self: 16)
    monkeypatch.setattr(TsgService, "seed_defaults", lambda self: 16)
    monkeypatch.setattr(TsgService, "is_known_short_name", lambda self, name: True)
    monkeypatch.setattr(TsgService, "known_short_names", lambda self: ["R5"])

    result = Runner().invoke(app, ["meeting", "sync", "--tsg", "R5"])
    assert result.exit_code == 0, result.output
    assert captured == {"closed": 5, "future": 3}


def test_meeting_sync_cli_flag_overrides_settings(
    clean_settings, write_toml, monkeypatch,
) -> None:
    cfg = write_toml("c.toml", "[meeting_sync]\nclosed_years = 5\n")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(cfg))
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    get_settings.cache_clear()

    captured: dict = {}

    def fake_sync(self, meetings_url, max_year_closed, max_year_future, today=None, tsg=None):
        captured["closed"] = max_year_closed
        captured["future"] = max_year_future
        return 0

    from doc3gpp.services import meetings_service
    from doc3gpp.services.tsg_service import TsgService

    monkeypatch.setattr(meetings_service.MeetingService, "sync", fake_sync)
    monkeypatch.setattr(TsgService, "count", lambda self: 16)
    monkeypatch.setattr(TsgService, "is_known_short_name", lambda self, name: True)

    result = Runner().invoke(
        app, ["meeting", "sync", "--tsg", "R5", "--closed-years", "1"]
    )
    assert result.exit_code == 0, result.output
    assert captured["closed"] == 1        # CLI flag wins over TOML 5
    assert captured["future"] == 1       # default (TOML only set closed_years)


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
    assert body["meeting_sync"]["closed_years"] == 2
    assert body["output"]["format"] == "table"


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