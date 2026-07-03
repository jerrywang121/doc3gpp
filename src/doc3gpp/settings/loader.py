"""Cached loader for :class:`doc3gpp.settings.schema.Settings`.

The loader merges two layers:

1. **Config file (TOML)** — discovered by
   :func:`doc3gpp.settings.config_source.load_config_data`. Its keys are
   passed as ``init`` kwargs to ``Settings``.
2. **Environment variables / .env** — read directly by pydantic-settings.
   pydantic-settings v2 treats env vars as *higher priority* than init
   kwargs, so the resulting object follows the documented precedence
   (env > file > defaults) without any extra plumbing.

The whole result is cached with ``functools.lru_cache`` so repeated
``get_settings()`` calls inside one process share one instance. Tests
that change environment variables must call ``get_settings.cache_clear()``
to see the new values; the ``sqlite_env`` fixture in
``tests/conftest.py`` is the canonical example.
"""

from __future__ import annotations

from functools import lru_cache

from doc3gpp.settings.config_source import load_config_data
from doc3gpp.settings.schema import Settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""

    _path, toml_data = load_config_data()
    return Settings(**toml_data)
