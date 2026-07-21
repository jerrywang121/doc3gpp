"""Application configuration models.

Two layers of settings are exposed:

* :class:`Settings` is the top-level :class:`pydantic_settings.BaseSettings`.
  It reads environment variables from a **closed allowlist** —
  :data:`ALLOWED_ENV_VARS` — plus the optional ``.env`` file. Anything
  outside the allowlist is ignored, even when the framework's
  ``env_prefix`` / ``env_nested_delimiter`` would otherwise match it.
* Nested sub-models (:class:`OutputSettings`, :class:`OutputFieldsSettings`,
  :class:`CacheSettings`, :class:`TDocParseSettings`,
  :class:`SyncSettings`) carry values that are most naturally configured
  from a TOML file. They are **not** env-overridable; the TOML config
  file is the single source of truth for them.

The TOML config file discovery layer lives in
:mod:`doc3gpp.settings.config_source`; the ``$DOC3GPP_CONFIG`` env var
pinning that file is independent of :data:`ALLOWED_ENV_VARS` and remains
the only way to override the config file location from the environment.

Precedence (highest wins)::

    CLI flags  >  environment variables  >  config file (TOML)  >  defaults

The CLI layer (``doc3gpp.cli``) reads from ``get_settings()`` for its
non-flag defaults, so the same precedence applies transparently.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


_HUMAN_DELTA_RE = re.compile(r"^(?P<value>[+-]?\d+(?:\.\d+)?)(?P<unit>[smhd])$", re.IGNORECASE)


#: Closed allowlist of ``DOC3GPP_*`` environment variables that
#: :class:`Settings` will honour. Anything outside this set is silently
#: ignored by the env source — operators must use the TOML config file
#: (``doc3gpp config set ...`` or a hand-edited ``doc3gpp.toml``) for
#: the remaining fields.
#:
#: ``DOC3GPP_CONFIG`` (TOML config file location pin) and
#: ``DOC3GPP_TEST_MYSQL_URL`` (test-only MySQL URL) are read directly
#: by :mod:`doc3gpp.settings.config_source` and the test fixtures,
#: respectively, and are intentionally **not** part of this allowlist.
ALLOWED_ENV_VARS: frozenset[str] = frozenset(
    {
        "DOC3GPP_DATABASE_URL",
        "DOC3GPP_DB_ECHO",
        "DOC3GPP_LOG_LEVEL",
        "DOC3GPP_HTTP_VERIFY",
        "DOC3GPP_CACHE__DIR",
        "DOC3GPP_SYNC__AUTO_SYNC",
    }
)


class FilteredEnvSettingsSource(EnvSettingsSource):
    """:class:`EnvSettingsSource` that only honours allowlisted env vars.

    The base class already filters by ``env_prefix`` (so only
    ``DOC3GPP_*`` is considered) and then maps each field to its
    env-var name via ``env_nested_delimiter`` (``__``). On top of that,
    this subclass drops every key that is not in
    :data:`ALLOWED_ENV_VARS`. The result: any ``DOC3GPP_*`` env var that
    is not on the allowlist is read as if it were unset, and the field
    falls back to whatever the TOML config file (or default factory)
    supplies.

    Keys in :attr:`env_vars` are lowercase (the parent class applies
    ``case_sensitive=False``), so the filter compares against
    :data:`ALLOWED_ENV_VARS` in lowercase form.
    """

    def __init__(self, settings_cls: type[BaseSettings], **kwargs: Any) -> None:
        super().__init__(settings_cls, **kwargs)
        # Parent populates ``self.env_vars`` with lowercase keys when
        # ``case_sensitive=False``; normalise both sides for comparison.
        allowed_lower = frozenset(name.lower() for name in ALLOWED_ENV_VARS)
        self.env_vars = {
            key: value for key, value in self.env_vars.items() if key in allowed_lower
        }


def env_var_for_dotted_key(key: str) -> str | None:
    """Render the ``DOC3GPP_*`` env-var name for a dotted ``key``.

    Returns ``None`` when ``key`` does not correspond to an allowlisted
    binding, so callers can detect "TOML-only" keys and skip the
    env-override hint. Shared between :mod:`doc3gpp.cli` and tests.
    """
    parts = key.split(".")
    if len(parts) == 1:
        name = f"DOC3GPP_{parts[0].upper()}"
    else:
        head = parts[0].upper()
        tail = "_".join(parts[1:]).upper()
        name = f"DOC3GPP_{head}__{tail}"
    return name if name in ALLOWED_ENV_VARS else None


def _parse_timedelta(value: object) -> timedelta:
    """Parse a non-negative timedelta from a human or ISO 8601 duration.

    Accepts:
        - human: ``24h``, ``30m``, ``90d``, ``15s`` (case-insensitive)
        - ISO 8601: ``P1D``, ``PT24H``, ``PT30M`` (also accepts lower-case ``pt24h``)
        - ``timedelta`` instances pass through unchanged

    Negative durations are rejected because these fields represent intervals
    and closed windows that must be non-negative.
    """
    parsed: timedelta
    if isinstance(value, timedelta):
        parsed = value
    elif not isinstance(value, str):
        raise TypeError(f"expected str or timedelta, got {type(value).__name__}")
    else:
        text = value.strip()
        if not text:
            raise ValueError("duration must not be empty")
        match = _HUMAN_DELTA_RE.match(text)
        if match:
            numeric = float(match.group("value"))
            unit = match.group("unit").lower()
            multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
            parsed = timedelta(seconds=numeric * multipliers[unit])
        else:
            # Pydantic v2 already accepts ISO 8601 durations for timedelta
            # fields, but normalize through its parser for consistent error
            # messages.
            from pydantic import TypeAdapter

            try:
                parsed = TypeAdapter(timedelta).validate_strings(text)
            except ValueError as exc:
                raise ValueError(
                    f"invalid duration {value!r}; expected a human duration "
                    f"(e.g. 24h, 30m, 90d), an ISO 8601 duration (e.g. PT24H, P90D), "
                    f"or a timedelta object"
                ) from exc
    if parsed.total_seconds() < 0:
        raise ValueError(f"duration {value!r} must not be negative")
    return parsed

# Output formats accepted by every ``* list`` command. Mirrored in
# ``doc3gpp.cli.VALID_FORMATS`` so a typo in one place fails fast.
OutputFormat = Literal["table", "json", "markdown"]


class OutputFieldsSettings(BaseModel):
    """Default column lists for each ``* list`` command.

    Each list mirrors the hardcoded defaults that previously lived inside
    ``doc3gpp.cli``. Centralising them here lets users customise the
    default columns via the config file without touching code.
    """

    meeting: list[str] = Field(
        default_factory=lambda: [
            "meeting_id",
            "name",
            "location",
            "start_date",
            "end_date",
            "ftp_url",
            "start_doc",
            "end_doc",
        ]
    )
    tdoc: list[str] = Field(
        default_factory=lambda: [
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
    )
    tsg: list[str] = Field(
        default_factory=lambda: ["tsg_name", "short_name", "description"]
    )
    wi: list[str] = Field(
        default_factory=lambda: ["wi_id", "acronym", "release", "name"]
    )


class OutputSettings(BaseModel):
    """Output defaults for every ``* list`` command."""

    format: OutputFormat = "table"
    fields: OutputFieldsSettings = Field(default_factory=OutputFieldsSettings)


class TDocParseSettings(BaseModel):
    """Knobs for ``doc3gpp tdoc parse``.

    :attr:`max_batch` caps how many TDocs a single ``tdoc parse``
    invocation will actually process. Filters narrow the candidate
    set; matches above this ceiling are truncated and reported as
    ``remaining`` in the completion summary so the operator can
    re-run the same command (without ``--force``) to continue where
    the previous invocation stopped.

    The default of 100 is conservative — 3GPP meetings rarely exceed
    that for CR-type TDocs and most ad-hoc runs are well under it.
    Operators can raise it for big-batch sweeps via the TOML
    ``[tdoc_parse] max_batch`` key or the
    ``DOC3GPP_TDOC_PARSE__MAX_BATCH`` env var.

    :attr:`max_ftp_depth` controls how many folder levels
    ``tdoc parse --from-url <3gpp-folder> --recursive`` will descend.
    A value of ``0`` means only the root folder is scanned; the
    default of ``2`` scans the root plus two levels of subfolders.
    """

    max_batch: int = Field(default=100, ge=1)
    max_ftp_depth: int = Field(default=2, ge=0, le=10)


class SyncSettings(BaseModel):
    """Sync intervals, skip-rule windows, and auto-sync behavior.

    These values gate the ``meeting sync`` and ``tdoc sync`` commands
    so repeated invocations do not re-scrape unchanged upstream data.
    Durations may be written in TOML/env as human strings (``24h``,
    ``30m``, ``90d``) or ISO 8601 durations (``P1D``, ``PT30M``).

    When ``auto_sync`` is enabled, the read commands ``meeting list``,
    ``tdoc list``, ``tdoc show``, and DB-mode ``tdoc parse`` will
    internally trigger meeting and TDoc list syncs before querying.
    The same skip rules still apply.
    """

    auto_sync: bool = Field(
        default=False,
        description=(
            "Automatically sync meeting calendars and TDoc lists when "
            "running list/show/parse commands."
        ),
    )
    meeting_sync_interval: timedelta = Field(
        default=timedelta(hours=24),
        description="Minimum time between meeting calendar syncs for the same TSG.",
    )
    tdoc_list_sync_interval: timedelta = Field(
        default=timedelta(minutes=30),
        description="Minimum time between TDoc list syncs for the same meeting.",
    )
    tdoc_list_closed_window: timedelta = Field(
        default=timedelta(days=90),
        description="Meetings whose end date is older than this window are skipped.",
    )
    tdoc_list_url_template: str = Field(
        default="https://portal.3gpp.org/ngppapp/GenerateDocumentList.aspx?meetingId={meeting_id}",
        description=(
            "Portal URL template used to download a meeting's TDoc list XLSX. "
            "Must contain the '{meeting_id}' placeholder."
        ),
    )

    @field_validator("meeting_sync_interval", "tdoc_list_sync_interval", "tdoc_list_closed_window", mode="before")
    @classmethod
    def _validate_durations(cls, value: object) -> timedelta:
        return _parse_timedelta(value)


class CacheSettings(BaseModel):
    """Disk cache configuration for TDoc extraction artifacts.

    Two subtrees live under :attr:`dir`: ``zips/`` holds the raw 3GPP zip
    downloads and ``markdown/`` holds the python-docx output. The cache
    module evicts files in insertion order (oldest by ``st_ctime`` first)
    whenever the combined size of both subtrees exceeds
    :attr:`size_limit_mb` megabytes; ``0`` means unlimited. The
    :attr:`purge_confirm` flag is read by the CLI's ``cache purge``
    command to gate the destructive operation with an interactive prompt
    (skip the prompt with ``--yes`` or by setting
    ``DOC3GPP_CACHE__PURGE_CONFIRM=false``).
    """

    dir: Path = Field(
        default_factory=lambda: Path.home() / ".cache" / "doc3gpp" / "tdocs"
    )
    size_limit_mb: int = Field(default=1024, ge=0)  # 0 = unlimited
    purge_confirm: bool = Field(default=True)  # CLI guard for `cache purge`


class Settings(BaseSettings):
    """Application configuration loaded from environment variables or .env.

    The flat fields at the root (``database_url``, ``db_echo``,
    ``log_level``, ``http_verify``) are populated from the
    :data:`ALLOWED_ENV_VARS` subset of ``DOC3GPP_*`` env vars.
    Nested sub-models (``output``, ``cache``, ``tdoc_parse``,
    ``sync``) come exclusively from the TOML config file via
    :func:`doc3gpp.settings.loader.get_settings`; the only nested
    env-override is ``DOC3GPP_CACHE__DIR`` (allowed) and
    ``DOC3GPP_SYNC__AUTO_SYNC`` (allowed). All other nested fields
    are TOML-only.
    """

    database_url: str = Field(
        default_factory=lambda: f"sqlite+pysqlite:///{Path.home()}/.local/share/doc3gpp/doc3gpp.db",
        validation_alias="DOC3GPP_DATABASE_URL",
    )
    db_echo: bool = Field(default=False, validation_alias="DOC3GPP_DB_ECHO")
    db_pool_size: int = Field(default=5)
    db_auto_migrate: bool = Field(default=True)
    log_level: str = Field(default="INFO", validation_alias="DOC3GPP_LOG_LEVEL")
    http_verify: bool = Field(default=False, validation_alias="DOC3GPP_HTTP_VERIFY")
    http_max_retries: int = Field(default=3, ge=0)
    http_retry_backoff: float = Field(default=0.5, ge=0.0)

    output: OutputSettings = Field(default_factory=OutputSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    tdoc_parse: TDocParseSettings = Field(default_factory=TDocParseSettings)
    sync: SyncSettings = Field(default_factory=SyncSettings)

    model_config = SettingsConfigDict(
        env_prefix="DOC3GPP_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Reorder sources so allowlisted env vars beat the TOML config file.

        pydantic-settings' default priority is ``init_args > env > .env >
        secrets > defaults``. We feed TOML data through ``init_settings``
        (in :func:`doc3gpp.settings.loader.get_settings`), so without this
        override the config file would shadow the env vars and silently
        break the documented ``CLI > env > file > defaults`` chain.
        Returning ``(filtered_env, init, dotenv, secret)`` flips init
        below env.

        The ``env_settings`` argument here is the framework-provided
        :class:`EnvSettingsSource`; we wrap it in
        :class:`FilteredEnvSettingsSource` so the allowlist
        (:data:`ALLOWED_ENV_VARS`) is the single source of truth for
        which env vars are honoured.
        """
        _ = env_settings  # noqa: F841 - replaced by the filtered source below
        filtered_env = FilteredEnvSettingsSource(
            settings_cls,
            case_sensitive=cls.model_config.get("case_sensitive"),
            env_prefix=cls.model_config.get("env_prefix"),
            env_nested_delimiter=cls.model_config.get("env_nested_delimiter"),
            env_ignore_empty=cls.model_config.get("env_ignore_empty"),
            env_parse_none_str=cls.model_config.get("env_parse_none_str"),
        )
        return (
            filtered_env,
            init_settings,
            dotenv_settings,
            file_secret_settings,
        )