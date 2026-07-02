from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables or .env."""

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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )