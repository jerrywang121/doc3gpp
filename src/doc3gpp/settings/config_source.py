"""TOML config file discovery and loading for ``doc3gpp``.

The TOML layer sits below env vars in the precedence chain (see
:mod:`doc3gpp.settings.schema`). Search order:

1. The path named by ``DOC3GPP_CONFIG`` (absolute or relative). This is
   meant for tests and for users who want to pin a specific file.
2. ``./doc3gpp.toml`` (the current working directory). Lets a team check
   project-local defaults into git.
3. ``$XDG_CONFIG_HOME/doc3gpp/config.toml`` (or the XDG default
   ``~/.config/doc3gpp/config.toml`` when the env var is unset). User-wide
   overrides that survive across projects.

The first existing file wins; later candidates are ignored. A missing
file is *not* an error — the loader returns an empty dict and the
settings fall back to their built-in defaults. Malformed TOML *is* an
error: it surfaces with the file path attached so the user knows where
to look.

Stdlib ``tomllib`` is used on Python 3.11+; ``tomli`` is the fallback
for 3.10 (declared in ``pyproject.toml``).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib  # noqa: F401 - intentional stdlib fallback
else:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

#: Canonical default config path under XDG. Public so the CLI can echo it.
DEFAULT_USER_CONFIG = Path.home() / ".config" / "doc3gpp" / "config.toml"

#: Project-local candidate, checked after the explicit env var but before
#: the XDG default.
DEFAULT_PROJECT_CONFIG = Path("doc3gpp.toml")


def _explicit_config_path() -> Path | None:
    """Return the path named by ``DOC3GPP_CONFIG`` if it points to a file.

    The env var may name a directory; in that case we append the canonical
    filename so users can pass either ``~/.config/doc3gpp`` or
    ``~/.config/doc3gpp/config.toml``. A path that points at neither a
    file nor a directory is treated as an error so typos surface early.
    """
    raw = os.environ.get("DOC3GPP_CONFIG")
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.is_file():
        return path
    if path.is_dir():
        candidate = path / "config.toml"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"DOC3GPP_CONFIG points to {raw!r}, but no config.toml exists there."
    )


def find_config_file() -> Path | None:
    """Return the first existing config file in the search order, or ``None``.

    See the module docstring for the search order. The return value is
    also exposed through ``doc3gpp config path`` so users can see which
    file (if any) is in effect.
    """
    explicit = _explicit_config_path()
    if explicit is not None:
        logger.debug("Using config from DOC3GPP_CONFIG: %s", explicit)
        return explicit

    if DEFAULT_PROJECT_CONFIG.is_file():
        logger.debug("Using project-local config: %s", DEFAULT_PROJECT_CONFIG.resolve())
        return DEFAULT_PROJECT_CONFIG.resolve()

    xdg_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    user_config = xdg_root / "doc3gpp" / "config.toml"
    if user_config.is_file():
        logger.debug("Using XDG config: %s", user_config)
        return user_config

    return None


def load_config_data() -> tuple[Path | None, dict[str, Any]]:
    """Find and parse the active config file.

    Returns:
        A ``(path, data)`` tuple. ``path`` is ``None`` when no file was
        found (callers should treat ``data`` as the empty defaults).
        ``data`` is the dict that should be unpacked into
        :class:`doc3gpp.settings.schema.Settings`.
    """
    path = find_config_file()
    if path is None:
        return None, {}
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Failed to parse TOML config at {path}: {exc}") from exc
    logger.debug("Loaded %d top-level keys from %s", len(data), path)
    return path, data