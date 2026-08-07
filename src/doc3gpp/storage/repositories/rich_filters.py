"""Shared rich-filter grammar helpers.

The "rich filter" syntax (also called ``cli_filters`` grammar) lets a
single user-supplied string express three predicates against a text
column:

- ``null`` / ``not-null``  — match the column's nullability;
- ``!pattern``             — ``NOT LIKE``; the ``!`` is consumed;
- any other value          — ``LIKE`` pattern (``%`` / ``_`` wildcards;
  a plain value with no wildcard degenerates to equality).

This module provides the same semantics for both query styles used
across the repository layer:

- :func:`apply_text_filter` builds a SQLAlchemy ``Select`` expression;
- :func:`build_text_filter_sql` builds a raw-SQL fragment + bound param
  for repos that assemble ``text()`` statements (search / vector).

Both delegate token parsing to :mod:`doc3gpp.cli_filters` so the
behaviour is identical everywhere.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, Select

from doc3gpp.cli_filters import (
    is_not_null_token,
    is_null_token,
    split_not_like_prefix,
)


def apply_text_filter(
    stmt: Select, column: ColumnElement, value: str | None
) -> Select:
    """Return ``stmt`` filtered by the rich grammar against ``column``.

    ``None`` is a pass-through. ``null`` / ``not-null`` match the
    column's nullability. A leading ``!`` flips the comparison to
    ``NOT LIKE``; the ``!`` is consumed and the remainder is bound as
    the pattern. Any other value is bound as a ``LIKE`` pattern.
    """
    if value is None:
        return stmt
    if is_null_token(value):
        return stmt.where(column.is_(None))
    if is_not_null_token(value):
        return stmt.where(column.is_not(None))
    negated, pattern = split_not_like_prefix(value)
    if negated:
        return stmt.where(column.notlike(pattern))
    return stmt.where(column.like(pattern))


def build_text_filter_sql(
    expr: str, value: str | None, param: str = "p"
) -> tuple[str, object] | None:
    """Build a raw-SQL fragment + bound param for the rich grammar.

    ``expr`` is the SQL column expression to test (e.g. ``"t.release"``).
    Returns ``None`` when ``value`` is ``None`` (pass-through), else a
    ``(fragment, bound_value)`` pair. ``fragment`` references the named
    parameter ``:param`` exactly once, and ``bound_value`` is the value
    to bind for it. For a negated ``!`` pattern the fragment is
    ``NOT (expr LIKE :param)``.
    """
    if value is None:
        return None
    if is_null_token(value):
        return f"{expr} IS NULL", None
    if is_not_null_token(value):
        return f"{expr} IS NOT NULL", None
    negated, pattern = split_not_like_prefix(value)
    if negated:
        return f"NOT ({expr} LIKE :{param})", pattern
    return f"{expr} LIKE :{param}", pattern


def build_text_filter_or_params(
    exprs: tuple[str, ...], value: str | None, param: str = "p"
) -> tuple[str, dict[str, object]]:
    """Build an OR-combined raw-SQL fragment across several columns.

    Applies the rich grammar to ``exprs`` as a group and ORs the
    resulting clauses together, binding each with a distinct
    ``:param_<i>`` named parameter. Returns ``("", {})`` when ``value``
    is ``None`` (pass-through).

    For a negated ``!`` pattern the negation wraps the *whole* OR group
    (``NOT (a LIKE OR b LIKE)``), so a row is kept only when none of
    the columns match — not ``NOT(a) OR NOT(b)``. Used for compound
    filters such as the search ``meeting`` match over ``name OR title``.
    """
    if value is None:
        return "", {}
    negated, pattern = split_not_like_prefix(value)
    like_frag: str
    bound: dict[str, object] = {}
    if is_null_token(value):
        like_frag = "(" + " OR ".join(f"{e} IS NULL" for e in exprs) + ")"
        return like_frag, bound
    if is_not_null_token(value):
        like_frag = "(" + " OR ".join(f"{e} IS NOT NULL" for e in exprs) + ")"
        return like_frag, bound
    clauses: list[str] = []
    for i, expr in enumerate(exprs):
        clauses.append(f"{expr} LIKE :{param}_{i}")
        bound[f"{param}_{i}"] = pattern
    joined = " OR ".join(clauses)
    if negated:
        return f"NOT ({joined})", bound
    return f"({joined})", bound
