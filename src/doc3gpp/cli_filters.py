"""Filter-value parsing helpers shared by ``tdoc`` CLI commands.

The ``tdoc parse --meeting-id`` selector accepts per-field filter
strings whose semantics differ by column type:

- **Text columns** (status, cr_cat, spec, related_wis, title, ftp_url,
  source, type, is_revision_of, revised_to, release, version, cr_num,
  cr_pack):
    - the literal token ``null`` or ``not-null`` selects rows whose
      column is ``NULL`` or not ``NULL`` respectively;
    - a leading ``!`` flips the comparison to ``NOT LIKE`` (e.g.
      ``!%RAN5%`` excludes rows whose column matches ``%RAN5%``);
      the ``!`` itself is consumed and the remainder is bound as the
      ``LIKE`` pattern;
    - any other value is treated as a SQL ``LIKE`` pattern (the user
      is responsible for ``%`` / ``_`` wildcards).

- **Date columns** (uploaded_date):
    - ``null`` / ``not-null`` as above;
    - ``"<op> 'YYYY-MM-DD'"`` with ``<op>`` in ``=`` / ``!=`` / ``<`` /
      ``<=`` / ``>`` / ``>=`` produces a parameterized column
      comparison. The date literal is bound by SQLAlchemy (no string
      interpolation into the SQL), so injection is impossible.

Anything else on a date field is rejected at the CLI boundary with a
:class:`ValueError` carrying the exact list of accepted forms. The
text columns do not raise — a typo'd ``status`` falls through to
``LIKE`` and matches nothing, which is the same behaviour as
``tdoc list``.

Keeping the validation centralised here lets the CLI surface a single
error path (``typer.BadParameter``) and keeps the repository layer
trusting the format contract documented above.
"""

from __future__ import annotations

import re


# Token sentinels for nullable columns. Stored lower-case; matching is
# done case-insensitively on a stripped copy of the input.
NULL_TOKEN = "null"
NOT_NULL_TOKEN = "not-null"

# Syntactic marker for a negated ``LIKE`` pattern. ``!%foo%`` binds
# as ``column NOT LIKE '%foo%'``; the ``!`` is consumed first.
NOT_LIKE_PREFIX = "!"


# ``OP 'YYYY-MM-DD'`` with optional whitespace; OP is one of
# {=, !=, <, <=, >, >=}. The named groups let callers unpack the
# operator and the date string without re-parsing.
DATE_FILTER_RE = re.compile(
    r"""^\s*
        (?P<op>>=|<=|!=|>|<|=)\s*
        '(?P<date>\d{4}-\d{2}-\d{2})'
        \s*$""",
    re.VERBOSE,
)


def is_null_token(value: str) -> bool:
    """Return ``True`` when ``value`` (stripped, lower-cased) is ``"null"``."""
    return value.strip().lower() == NULL_TOKEN


def is_not_null_token(value: str) -> bool:
    """Return ``True`` when ``value`` (stripped, lower-cased) is ``"not-null"``."""
    return value.strip().lower() == NOT_NULL_TOKEN


def split_not_like_prefix(value: str) -> tuple[bool, str]:
    """Split ``value`` into ``(is_negated, pattern)``.

    A leading ``!`` (the :data:`NOT_LIKE_PREFIX`) flips the comparison
    to ``NOT LIKE``; the ``!`` is consumed and the remainder is the
    pattern. Without a leading ``!``, ``is_negated`` is ``False`` and
    ``pattern`` is the original value.
    """
    if value.startswith(NOT_LIKE_PREFIX):
        return True, value[len(NOT_LIKE_PREFIX):]
    return False, value


def validate_date_filter(value: str) -> None:
    """Raise :class:`ValueError` if ``value`` is not a valid date filter.

    Accepted forms:

    - ``null`` / ``not-null`` — match the NULL / NOT NULL rows;
    - ``"<op> 'YYYY-MM-DD'"`` — operator comparison against a literal
      date, where ``<op>`` is one of ``=`` / ``!=`` / ``<`` / ``<=`` /
      ``>`` / ``>=``.

    The error message lists every accepted form so the operator can
    fix the typo without reading the docs.
    """
    v = value.strip()
    if v.lower() in (NULL_TOKEN, NOT_NULL_TOKEN):
        return
    if DATE_FILTER_RE.match(v):
        return
    raise ValueError(
        f"Invalid date filter {value!r}. Expected 'null', 'not-null', "
        f"or an expression like \">= 'YYYY-MM-DD'\" with one of "
        f"=, !=, <, <=, >, >=."
    )