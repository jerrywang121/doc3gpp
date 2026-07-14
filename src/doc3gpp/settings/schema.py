"""Application configuration models.

Two layers of settings are exposed:

* :class:`Settings` is the top-level :class:`pydantic_settings.BaseSettings`.
  It reads ``DOC3GPP_*`` environment variables (and the optional ``.env``)
  for backward compatibility with the original CLI surface.
* Nested sub-models (:class:`OutputSettings`, :class:`OutputFieldsSettings`,
  :class:`CacheSettings`, :class:`TDocParseSettings`) carry values that are
  most naturally configured from a TOML file rather than env vars. They can
  still be overridden by env vars via the ``__`` delimiter
  (``DOC3GPP_OUTPUT__FORMAT=json``).

Precedence (highest wins)::

    CLI flags  >  environment variables  >  config file (TOML)  >  defaults

The CLI layer (``doc3gpp.cli``) reads from ``get_settings()`` for its
non-flag defaults, so the same precedence applies transparently.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


_HUMAN_DELTA_RE = re.compile(r"^(?P<value>[+-]?\d+(?:\.\d+)?)(?P<unit>[smhd])$", re.IGNORECASE)


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

    The flat fields at the root (``database_url``, ``db_echo``, ...) are
    populated from ``DOC3GPP_*`` env vars to preserve backward
    compatibility. Nested sub-models (``output``, ``cache``,
    ``tdoc_parse``) are populated from a TOML config file via
    :func:`doc3gpp.settings.loader.get_settings`; the ``env_nested_delimiter``
    in :attr:`model_config` lets env vars override nested values too
    (``DOC3GPP_OUTPUT__FORMAT=json``).
    """

    database_url: str = Field(
        default_factory=lambda: f"sqlite+pysqlite:///{Path.home()}/.local/share/doc3gpp/doc3gpp.db",
        validation_alias="DOC3GPP_DATABASE_URL",
    )
    db_echo: bool = Field(default=False, validation_alias="DOC3GPP_DB_ECHO")
    db_pool_size: int = Field(default=5, validation_alias="DOC3GPP_DB_POOL_SIZE")
    db_auto_migrate: bool = Field(default=True, validation_alias="DOC3GPP_DB_AUTO_MIGRATE")
    log_level: str = Field(default="INFO", validation_alias="DOC3GPP_LOG_LEVEL")
    http_verify: bool = Field(default=False, validation_alias="DOC3GPP_HTTP_VERIFY")
    http_max_retries: int = Field(
        default=3, ge=0, validation_alias="DOC3GPP_HTTP_MAX_RETRIES"
    )
    http_retry_backoff: float = Field(
        default=0.5, ge=0.0, validation_alias="DOC3GPP_HTTP_RETRY_BACKOFF"
    )

    # Nested config (config-file-driven, env-overridable via '__' delimiter).
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
        """Reorder sources so env vars beat the TOML config file.

        pydantic-settings' default priority is ``init_args > env > .env >
        secrets > defaults``. We feed TOML data through ``init_settings``
        (in :func:`doc3gpp.settings.loader.get_settings`), so without this
        override the config file would shadow the env vars and silently
        break the documented ``CLI > env > file > defaults`` chain.
        Returning ``(env, init, dotenv, secret)`` flips init below env.
        """
        return (
            env_settings,
            init_settings,
            dotenv_settings,
            file_secret_settings,
        )