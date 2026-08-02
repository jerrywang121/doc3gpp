"""HTTP query-parameter filter parsers.

Thin adapter that converts raw query-string values to the same grammar
the CLI uses, then hands off to the existing helpers in
:mod:`doc3gpp.cli_filters` and :mod:`doc3gpp.storage.repositories.tdoc_sql`.

Every helper is a single, short function: convert the raw input shape
to the shape the existing helper expects, then delegate. Any
:class:`ValueError` raised by the underlying helper is re-raised as
:class:`doc3gpp.web.errors.InvalidFilterError` so the FastAPI error
handler maps it to HTTP 400.
"""
from __future__ import annotations

from doc3gpp.cli_filters import parse_tdoc_id, validate_date_filter
from doc3gpp.web.errors import InvalidFilterError


def parse_text_query(raw: str | None) -> str | None:
    """Pass ``raw`` through to ``_apply_text_filter`` semantics.

    ``None`` is a pass-through; ``"null"`` / ``"not-null"`` / ``"!foo"``
    are recognised by the underlying SQL helper. The HTTP layer just
    hands the string over.
    """
    return raw


def parse_date_query(raw: str | None) -> str | None:
    """Validate ``raw`` against :func:`validate_date_filter` and return it.

    ``None`` is a pass-through. Any non-``None`` value is checked with
    :func:`validate_date_filter`; a :class:`ValueError` is re-raised as
    :class:`InvalidFilterError` so the FastAPI error handler maps it
    to HTTP 400. On success, ``raw`` is returned unchanged.
    """
    if raw is None:
        return None
    try:
        validate_date_filter(raw)
    except ValueError as exc:
        raise InvalidFilterError(str(exc)) from exc
    return raw


def parse_bool_query(raw: str | None) -> bool | None:
    """Parse ``raw`` as a strict ``"true"`` / ``"false"`` string.

    ``None`` is a pass-through. Anything other than the literal
    ``"true"`` or ``"false"`` (including ``"1"`` / ``"0"`` /
    ``"True"``) raises :class:`InvalidFilterError`.
    """
    if raw is None:
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise InvalidFilterError(f"expected 'true' or 'false', got: {raw}")


def parse_int_query(
    raw: str | None,
    *,
    min: int | None = None,
    max: int | None = None,
) -> int | None:
    """Parse ``raw`` as a base-10 integer, optionally bounded.

    ``None`` is a pass-through. Non-integer strings raise
    :class:`InvalidFilterError`. When ``min`` or ``max`` are supplied,
    out-of-range values raise :class:`InvalidFilterError` with the
    accepted range in the message.
    """
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise InvalidFilterError(f"expected integer, got: {raw}") from exc
    if min is not None and value < min:
        raise InvalidFilterError(f"value {value} out of range [{min}, {max}]")
    if max is not None and value > max:
        raise InvalidFilterError(f"value {value} out of range [{min}, {max}]")
    return value


def parse_tdoc_id_query(raw: str) -> tuple[str, int]:
    """Wrap :func:`doc3gpp.cli_filters.parse_tdoc_id` and remap its ``ValueError``."""
    try:
        return parse_tdoc_id(raw)
    except ValueError as exc:
        raise InvalidFilterError(str(exc)) from exc


__all__ = [
    "parse_text_query",
    "parse_date_query",
    "parse_bool_query",
    "parse_int_query",
    "parse_tdoc_id_query",
]
