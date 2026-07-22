"""Read-modify-write helpers for the active TOML config file.

This module is the writer-side twin of :mod:`doc3gpp.settings.config_source`.
It exposes the pure functions the ``doc3gpp config set`` and
``doc3gpp config init`` commands need to mutate one TOML file without
clobbering the rest of its contents:

* :func:`read_toml` / :func:`write_toml` \u2014 file \u2194 dict.
* :func:`parse_dotted_key` \u2014 split ``"a.b.c"`` into segments.
* :func:`patch_dotted` \u2014 merge a single leaf value into a dict without
  mutating the input, creating intermediate tables as needed.
* :func:`prune_empty_tables` \u2014 bottom-up, only inside the patched
  sub-tree, drop dicts whose children are themselves empty.
* :func:`walk_known_dotted_keys` \u2014 enumerate every reachable dotted
  path on a pydantic model so the CLI can reject unknown keys.
* :func:`validate_against_settings` \u2014 hand a dict to :class:`Settings`
  and surface pydantic errors as :class:`ConfigValidationError`.
* :func:`resolve_echo_subtree` \u2014 dump the minimal containing dict
  for a dotted key, for ``doc3gpp config get`` style echo output.
* :func:`resolve_init_target` \u2014 ``"user" | "project" | "auto"`` \u2192 path.

The dotted-key merge is non-mutating: every helper that returns a dict
returns a *new* dict. Empty-table pruning is bottom-up and scoped to
the patched sub-tree \u2014 sibling tables are never inspected.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel, ValidationError

if sys.version_info >= (3, 11):
    import tomllib  # noqa: F401 - intentional stdlib fallback
else:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef, no-redef]

from doc3gpp.settings.config_source import (
    DEFAULT_PROJECT_CONFIG,
    DEFAULT_USER_CONFIG,
)
from doc3gpp.settings.schema import Settings


#: Markers that identify a project root when walking parents for
#: ``resolve_init_target``. The first hit on any of these wins.
_PROJECT_MARKERS = ("pyproject.toml", ".git", "doc3gpp.toml.example")


class ConfigValidationError(Exception):
    """Wrapper around :class:`pydantic.ValidationError` from the writer side.

    The CLI catches this and re-emits a one-line friendly message per failing
    field. ``original`` carries the full pydantic exception for callers that
    want the detail.
    """

    def __init__(self, original: ValidationError) -> None:
        super().__init__(str(original))
        self.original = original


def read_toml(path: Path) -> dict[str, Any]:
    """Parse ``path`` and return its contents as a plain dict.

    Re-raises :class:`tomllib.TOMLDecodeError` unmodified so the CLI can
    render the parser's own error message verbatim.
    """
    with path.open("rb") as fh:
        return tomllib.load(fh)


def write_toml(path: Path, data: dict[str, Any]) -> None:
    """Serialise ``data`` as TOML to ``path``, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        tomli_w.dump(data, fh)


def parse_dotted_key(key: str) -> list[str]:
    """Split ``"a.b.c"`` into ``["a", "b", "c"]`` after stripping whitespace.

    Raises:
        ValueError: if ``key`` is empty or whitespace-only.
    """
    parts = key.strip().split(".")
    if not parts or parts == [""]:
        raise ValueError("dotted key is empty")
    return parts


def patch_dotted(data: dict[str, Any], key: str, value: str) -> dict[str, Any]:
    """Return a new dict with ``key`` set to ``value``.

    Intermediate tables are created on demand. The input dict is not
    mutated; the raw ``value`` string is stored at the leaf so coercion
    happens later in :func:`validate_against_settings`.
    """
    parts = parse_dotted_key(key)
    out = copy.deepcopy(data)
    cursor: dict[str, Any] = out
    for segment in parts[:-1]:
        nxt = cursor.get(segment)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[segment] = nxt
        cursor = nxt
    cursor[parts[-1]] = value
    return out


def _prune_subtree(node: dict[str, Any]) -> dict[str, Any] | None:
    """Recursively prune empty dicts in ``node``; return ``None`` if it
    becomes empty so the caller knows to drop it."""
    kept: dict[str, Any] = {}
    for child_key, child_val in node.items():
        if isinstance(child_val, dict):
            cleaned = _prune_subtree(child_val)
            if cleaned is not None:
                kept[child_key] = cleaned
        else:
            kept[child_key] = child_val
    return kept or None


def prune_empty_tables(data: dict[str, Any], key: str) -> dict[str, Any]:
    """Return a new dict with empty tables removed under ``key``.

    Walks the sub-tree rooted at ``key`` bottom-up, dropping dicts whose
    only children are themselves empty dicts. Sibling sub-trees are left
    untouched \u2014 only the branch the operator is patching is inspected.

    If the leaf at ``key`` is not a dict (i.e. ``key`` points to a scalar
    field), the function returns ``out`` unchanged \u2014 there is nothing to
    prune and the table that owns the leaf is preserved.
    """
    parts = parse_dotted_key(key)
    out = copy.deepcopy(data)
    parents: list[tuple[dict[str, Any], str]] = []
    cursor: Any = out
    for segment in parts:
        if not isinstance(cursor, dict) or segment not in cursor:
            return out
        parents.append((cursor, segment))
        cursor = cursor[segment]
    if not isinstance(cursor, dict):
        return out
    pruned: dict[str, Any] | None = _prune_subtree(cursor)
    for parent, segment in reversed(parents):
        if pruned is None:
            parent.pop(segment, None)
            pruned = None if not any(parent.values()) else parent
        else:
            parent[segment] = pruned
            return out
    return out


def walk_known_dotted_keys(model: type[BaseModel]) -> set[str]:
    """Return every reachable dotted path on ``model`` and its nested models.

    Field names come back exactly as declared (the schema uses lowercased
    names already; ``Settings.model_config.case_sensitive=False`` makes
    input matching case-insensitive).
    """
    paths: set[str] = set()

    def _recurse(cls: type[BaseModel], prefix: str) -> None:
        for name, field in cls.model_fields.items():
            dotted = f"{prefix}.{name}" if prefix else name
            annotation = field.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                _recurse(annotation, dotted)
            else:
                paths.add(dotted)

    _recurse(model, "")
    return paths


def validate_against_settings(data: dict[str, Any]) -> Settings:
    """Validate ``data`` against :class:`Settings` and return the instance.

    Raises:
        ConfigValidationError: wrapping the original pydantic error.
    """
    try:
        return Settings(**data)
    except ValidationError as exc:
        raise ConfigValidationError(exc) from exc


def resolve_echo_subtree(settings: Settings, key: str) -> dict[str, Any]:
    """Dump ``settings`` and return the minimal containing dict for ``key``."""
    parts = parse_dotted_key(key)
    full = settings.model_dump(mode="json")
    cursor: Any = full
    for segment in parts:
        if not isinstance(cursor, dict) or segment not in cursor:
            # The path doesn't exist on the dumped model; return the full dump
            # so the CLI can still show something useful.
            return full
        cursor = cursor[segment]
    # Re-wrap in the segment path so the caller sees the containing shape.
    out: dict[str, Any] = {}
    nested: dict[str, Any] = out
    for segment in parts[:-1]:
        nested[segment] = {}
        nested = nested[segment]
    nested[parts[-1]] = cursor
    return out


def _find_project_root(start: Path) -> Path | None:
    """Walk parents from ``start`` looking for project markers; return the
    first directory that contains one, or ``None``."""
    for candidate in (start, *start.parents):
        if any((candidate / marker).exists() for marker in _PROJECT_MARKERS):
            return candidate
    return None


def resolve_init_target(target: str) -> Path:
    """Resolve the ``--target`` flag from ``doc3gpp config init``.

    * ``"user"`` \u2192 :data:`doc3gpp.settings.config_source.DEFAULT_USER_CONFIG`.
    * ``"project"`` \u2192 :data:`DEFAULT_PROJECT_CONFIG` (``./doc3gpp.toml``) if a
      project root can be found, else raise :class:`FileNotFoundError`.
    * ``"auto"`` \u2192 project if a root is found, else user.
    """
    if target == "user":
        return DEFAULT_USER_CONFIG
    if target not in {"project", "auto"}:
        valid = "'project', 'user', 'auto'"
        raise ValueError(f"unknown init target {target!r}; expected one of {valid}")
    root = _find_project_root(Path.cwd())
    if root is not None:
        return DEFAULT_PROJECT_CONFIG
    if target == "project":
        raise FileNotFoundError(
            "no project root found (looked for pyproject.toml, .git/, or doc3gpp.toml.example "
            f"in {Path.cwd()} and its parents)"
        )
    return DEFAULT_USER_CONFIG


def load_default_template(path: Path | None = None) -> str:
    """Read the packaged default TOML template as utf-8 text.

    Resolution order:

    1. If ``path`` is supplied, read that file directly. Intended for
       tests that need to inject a fixture; callers outside the test
       suite should leave it as ``None``.
    2. :func:`importlib.resources.files` on the ``doc3gpp`` package,
       joined with ``data/doc3gpp.toml.example``. This is the canonical
       path for wheel installs — ``pyproject.toml`` force-includes the
       file at ``doc3gpp/data/doc3gpp.toml.example`` so it ships inside
       the package.
    3. Walk ``Path(__file__).parents`` looking for
       ``<ancestor>/doc3gpp/data/doc3gpp.toml.example``. This catches
       editable installs and source-tree runs where the file lives at
       ``src/doc3gpp/data/doc3gpp.toml.example`` and
       :mod:`importlib.resources` either isn't on ``sys.path`` or the
       spec resolver can't locate the data file.

    Returns:
        The full template text (utf-8).

    Raises:
        FileNotFoundError: if the template cannot be located via any of
            the strategies above; the message lists every candidate path
            that was probed.
    """
    if path is not None:
        return path.read_text(encoding="utf-8")

    searched: list[str] = []

    # Strategy 1: wheel-installed package data via importlib.resources.
    try:
        from importlib.resources import files

        traversable = files("doc3gpp").joinpath("data/doc3gpp.toml.example")
        return traversable.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, NotImplementedError) as exc:
        searched.append(
            "importlib.resources.files('doc3gpp').joinpath("
            f"'data/doc3gpp.toml.example') ({type(exc).__name__}: {exc})"
        )

    # Strategy 2: walk parents of this file looking for the source-tree
    # location. The first hit wins.
    here = Path(__file__).resolve()
    for ancestor in (here.parent, *here.parents):
        candidate = ancestor / "doc3gpp" / "data" / "doc3gpp.toml.example"
        searched.append(str(candidate))
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")

    raise FileNotFoundError(
        "could not locate doc3gpp/data/doc3gpp.toml.example; "
        f"searched: {', '.join(searched)}"
    )


__all__ = [
    "ConfigValidationError",
    "DEFAULT_PROJECT_CONFIG",
    "DEFAULT_USER_CONFIG",
    "load_default_template",
    "parse_dotted_key",
    "patch_dotted",
    "prune_empty_tables",
    "read_toml",
    "resolve_echo_subtree",
    "resolve_init_target",
    "tomllib",
    "validate_against_settings",
    "walk_known_dotted_keys",
    "write_toml",
]
