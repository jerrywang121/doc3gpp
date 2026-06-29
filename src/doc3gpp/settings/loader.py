from __future__ import annotations

from functools import lru_cache

from doc3gpp.settings.schema import Settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""

    return Settings()
