"""JSON serialisation helpers for the web / MCP layer.

The CLI already has its own serializers (``json.dump`` with
``ensure_ascii=False``) for ``tdoc show`` and the read commands. The
HTTP layer needs the **same** shapes so ``doc3gpp tdoc show --format
json`` and ``GET /tdocs/{id}?format=json`` return byte-identical
payloads — a spec violation otherwise.

This module is the single source of truth for the web-side JSON
shape:

* :func:`to_jsonable` — recursive walker used by the detail routes
  (``tdoc show`` / meeting / TSG detail) and the MCP layer. It
  reproduces the CLI's ``_build_show_payload`` semantics generically:
  omit-when-null at the top level, full nested objects below, and a
  dedicated :class:`TDocCRChangeDetails` branch that emits only the
  body-derived fields (``clauses`` + ``changes``) the CLI surfaces.
* :func:`meeting_rows` / :func:`tdoc_rows` / :func:`tsg_rows` /
  :func:`wi_rows` — list-row builders that reproduce the exact
  ``* list --format json`` payload (a bare array of field-selected,
  string-coerced objects) for the list routes' ``?format=json``.
"""
from __future__ import annotations

from dataclasses import fields as dataclass_fields
from datetime import date, datetime
from enum import Enum
from typing import Any

from doc3gpp.models.jobs import JSONValue
from doc3gpp.models.tdoc import TDocWithMeeting
from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails


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
    if isinstance(value, TDocCRChangeDetails):
        return _change_details_to_dict(value)
    if _is_dataclass_instance(value):
        return _dataclass_to_dict(value, is_top=is_top)
    return str(value)


def _change_details_to_dict(value: TDocCRChangeDetails) -> dict[str, JSONValue]:
    """Serialise :class:`TDocCRChangeDetails` exactly as the CLI does.

    The CLI's ``_build_show_payload`` emits only the body-derived
    fields for this sidecar — ``clauses`` (a list of clause labels)
    and ``changes`` (one ``{clauses, text}`` block per captured
    change) — and deliberately drops the row-identity fields
    ``ftp_url`` / ``tdoc_id``. Walking every dataclass field would
    leak those identity fields into the payload, so this branch
    mirrors the CLI's per-field conditional instead.
    """
    return {
        "clauses": [str(c) for c in value.clauses],
        "changes": [
            {
                "clauses": [str(c) for c in block["clauses"]],
                "text": block["text"],
            }
            for block in value.changes
        ],
    }


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


def _coerce_cell(value: Any) -> str:
    """Coerce one list-row cell to the CLI's exact string form.

    Mirrors the per-command cell loops in :mod:`doc3gpp.cli`:
    ``None`` renders as ``"-"``, ``date`` values render as
    ``ISO-8601`` strings, and everything else falls back to
    ``str(value)``.
    """
    if value is None:
        return "-"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def meeting_rows(
    meetings: list[Any],
    fields: list[str],
) -> list[dict[str, str]]:
    """Build ``meeting list --format json``-shaped rows for ``meetings``.

    ``fields`` selects the output columns; each cell is coerced via
    :func:`_coerce_cell`, exactly like the CLI's ``meeting_list``
    print loop.
    """
    rows: list[dict[str, str]] = []
    for meeting in meetings:
        rows.append(
            {
                f: _coerce_cell(getattr(meeting, f, None))
                for f in fields
            }
        )
    return rows


def tdoc_rows(
    rows: list[Any],
    fields: list[str],
) -> list[dict[str, str]]:
    """Build ``tdoc list --format json``-shaped rows for ``TDocWithMeeting`` rows.

    ``meeting_name`` is a top-level attribute on the DTO; every other
    field lives on ``row.tdoc`` (the same routing the CLI's
    ``_tdoc_field`` helper encodes).
    """
    out: list[dict[str, str]] = []
    for item in rows:
        assert isinstance(item, TDocWithMeeting)
        out.append(
            {
                f: _coerce_cell(
                    item.meeting_name if f == "meeting_name"
                    else getattr(item.tdoc, f, None)
                )
                for f in fields
            }
        )
    return out


def tsg_rows(tsgs: list[Any], fields: list[str]) -> list[dict[str, str]]:
    """Build ``tsg list --format json``-shaped rows for ``tsgs``."""
    return [
        {f: _coerce_cell(getattr(tsg, f, None)) for f in fields}
        for tsg in tsgs
    ]


def wi_rows(wis: list[Any], fields: list[str]) -> list[dict[str, str]]:
    """Build ``wi list --format json``-shaped rows for ``wis``."""
    return [
        {f: _coerce_cell(getattr(wi, f, None)) for f in fields}
        for wi in wis
    ]


__all__ = [
    "meeting_rows",
    "tdoc_rows",
    "to_jsonable",
    "tsg_rows",
    "wi_rows",
]