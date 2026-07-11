"""Application configuration models.

Two layers of settings are exposed:

* :class:`Settings` is the top-level :class:`pydantic_settings.BaseSettings`.
  It reads ``DOC3GPP_*`` environment variables (and the optional ``.env``)
  for backward compatibility with the original CLI surface.
* Nested sub-models (:class:`MeetingSyncSettings`,
  :class:`OutputSettings`, :class:`OutputFieldsSettings`,
  :class:`CacheSettings`) carry values that are most naturally configured
  from a TOML file rather than env vars. They can still be overridden by
  env vars via the ``__`` delimiter
  (``DOC3GPP_MEETING_SYNC__CLOSED_YEARS=5``).

Precedence (highest wins)::

    CLI flags  >  environment variables  >  config file (TOML)  >  defaults

The CLI layer (``doc3gpp.cli``) reads from ``get_settings()`` for its
non-flag defaults, so the same precedence applies transparently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# Output formats accepted by every ``* list`` command. Mirrored in
# ``doc3gpp.cli.VALID_FORMATS`` so a typo in one place fails fast.
OutputFormat = Literal["table", "json", "markdown"]


class MeetingSyncSettings(BaseModel):
    """Fetch-side knobs for ``doc3gpp meeting sync``.

    Mirrors the CLI ``--closed-years`` / ``--future-years`` flags. When the
    CLI flags are not passed, these values are used as the default.
    """

    closed_years: int = Field(default=2, ge=0, le=20)
    future_years: int = Field(default=1, ge=0, le=10)


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
    compatibility. Nested sub-models (``meeting_sync``, ``output``,
    ``cache``) are populated from a TOML config file via
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
    meeting_sync: MeetingSyncSettings = Field(default_factory=MeetingSyncSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    tdoc_parse: TDocParseSettings = Field(default_factory=TDocParseSettings)

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