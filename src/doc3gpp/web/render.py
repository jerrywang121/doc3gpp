"""JSON serialisation helpers for the web / MCP layer.

The CLI already has its own compact-bytes serializer (``json.dump`` with
``separators=(",", ":")`` and ``ensure_ascii=False``) for ``tdoc show``
and the read commands. The HTTP layer needs the **same** shape so
``doc3gpp tdoc show --format json`` and ``GET /tdocs/{id}?format=json``
return byte-identical payloads — a spec violation otherwise.

This module is the single source of truth for the web-side JSON
shape. The CLI's own serializers continue to live in :mod:`doc3gpp.cli`
because they need ``_serialise_show_value`` semantics that depend on
the omit-when-null payload rule already encoded there; the read
commands (``tdoc list``, ``meeting list``, etc.) **do** delegate to
:func:`to_jsonable` so the list / detail payloads share one
encoder.
"""
from __future__ import annotations

from dataclasses import fields as dataclass_fields
from datetime import date, datetime
from enum import Enum
from typing import Any

from doc3gpp.models.jobs import JSONValue


def to_jsonable(value: Any) -> JSONValue:
    """Recursively coerce ``value`` into a JSON-serialisable shape.

    Handles:

    * ``datetime`` / ``date`` — ISO-8601 strings (UTC-naive ``datetime``
      stays naive, matching the CLI's ``_serialise_show_value`` rule).
    * ``Enum`` — the string name (``Enum.value`` is the more common
      convention; the CLI uses ``str(exc)`` which for ``Enum`` returns
      ``"ClassName.NAME"``. The web renderer always emits the
      ``.value`` so ``?format=json`` stays shape-stable across the
      whole response).
    * ``None`` / ``bool`` / ``int`` / ``float`` / ``str`` — pass-through.
    * ``list`` / ``tuple`` — recursively mapped.
    * ``dict`` — recursively mapped; keys are coerced to ``str``.
    * Dataclass instances — walked via :func:`dataclasses.fields`. The
      outermost dataclass is serialised with the **omit-when-null**
      convention (any field whose value is ``None`` is dropped); nested
      dataclasses are serialised with every field included so the
      CLI's nested-object shape (e.g. ``cover.spec = null``) is
      preserved.
    * Other objects — fall back to ``str(value)`` so the encoder never
      raises on a domain object a future endpoint adds.
    """
    return _to_jsonable(value, is_top=True)


def _to_jsonable(value: Any, *, is_top: bool) -> JSONValue:
    """Recursive walker for :func:`to_jsonable`. ``is_top`` flips the omit rule."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v, is_top=False) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v, is_top=False) for k, v in value.items()}
    if _is_dataclass_instance(value):
        return _dataclass_to_dict(value, is_top=is_top)
    return str(value)


def _dataclass_to_dict(value: Any, *, is_top: bool) -> dict[str, JSONValue]:
    """Serialise ``value`` into a dict, optionally omitting ``None`` fields.

    The CLI's :func:`_build_show_payload` uses per-field conditionals
    to drop absent values at the top of a ``TDocShowRecord`` payload.
    :func:`to_jsonable` reproduces that rule generically for the
    outermost dataclass (``is_top=True``) — ``None`` fields and empty
    containers are dropped so HTTP and CLI JSON envelopes stay
    byte-equivalent. Nested dataclasses (``is_top=False``) keep every
    field (including ``None`` values) so the nested-object shape the
    CLI emits is preserved.
    """
    out: dict[str, JSONValue] = {}
    for f in dataclass_fields(value):
        raw = getattr(value, f.name)
        if is_top:
            if raw is None:
                continue
            if isinstance(raw, (list, tuple)) and not raw:
                continue
        out[f.name] = _to_jsonable(raw, is_top=False)
    return out


def _is_dataclass_instance(value: Any) -> bool:
    """Return ``True`` when ``value`` is a dataclass instance.

    Avoids the heavyweight ``dataclasses.is_dataclass`` import while
    preserving the dataclass check semantics — checks both the
    ``__dataclass_fields__`` attribute (set on dataclass instances)
    and that it's not a class itself.
    """
    return not isinstance(value, type) and hasattr(value, "__dataclass_fields__")


__all__ = ["to_jsonable"]