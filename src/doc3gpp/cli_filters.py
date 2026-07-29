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

- **TDoc identifiers** (used by ``meeting list --tdoc``): a 9-character
  CR-shape id matching :data:`TDOC_ID_RE`, e.g. ``R5-260013`` (canonical
  form), ``R5s260009`` (TTCN), or ``R5w260013`` (workshop). The
  helper :func:`validate_tdoc_id` parses the value into a
  ``(prefix, number)`` tuple for the repository to consume; any other
  shape is rejected at the CLI boundary with a clear error message.

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


# CR-shape TDoc identifier: ``[RSC][1-9][-sw]\d{6,7}`` — TSG group
# initial (R/S/C), non-zero TSG digit, shape marker (- / s / w for
# canonical / TTCN / workshop), 6- or 7-digit sequence number.
# 3GPP RAN4 has used 7-digit numbers since 2016 (e.g. ``R4-2607922``);
# every other working group is still on 6 digits. Do not narrow without
# re-checking the RAN4 DynaReport.
TDOC_ID_RE = re.compile(r"[RSC][1-9][-sw]\d{6,7}", re.IGNORECASE)


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


def parse_tdoc_id(value: str) -> tuple[str, int]:
    """Return ``(prefix, number)`` for a canonical CR-shape ``value``.

    ``prefix`` is the first three characters (e.g. ``R5-`` / ``R5s`` /
    ``R5w``); ``number`` is the trailing 6- or 7-digit integer
    (e.g. ``260013``, ``2607922``). Case-insensitive: ``r5-260013`` parses
    the same as ``R5-260013``.

    Raises :class:`ValueError` when ``value`` does not match
    :data:`TDOC_ID_RE`; the message lists the expected shape so the
    operator can fix the typo without reading the docs.
    """
    stripped = value.strip()
    match = TDOC_ID_RE.fullmatch(stripped)
    if match is None:
        raise ValueError(
            f"Invalid TDoc id {value!r}. Expected a 9- or 10-character "
            f"CR-shape id like 'R5-260013', 'R5s260009', 'R5w260013', or "
            f"'R4-2607922' — TSG group initial (R/S/C), non-zero TSG digit, "
            f"shape marker (-/s/w), then 6 or 7 decimal digits."
        )
    return stripped[:3], int(stripped[3:])


def validate_tdoc_id(value: str) -> None:
    """Raise :class:`ValueError` if ``value`` is not a CR-shape TDoc id.

    Thin wrapper over :func:`parse_tdoc_id` that exists so the CLI
    boundary can reject malformed ``--tdoc`` arguments without the
    caller having to unpack the parsed tuple. The error message is the
    same as the one raised by :func:`parse_tdoc_id`.
    """
    parse_tdoc_id(value)


# FTS5 stopwords we want to refuse as the only token(s). The list
# mirrors the FTS5 default stopword set; matching on it stops
# accidental "the a" queries that would return every row.
_FTS5_STOPWORDS = frozenset(
    {
        "a", "and", "are", "as", "at", "be", "but", "by", "for",
        "if", "in", "into", "is", "it", "no", "not", "of", "on",
        "or", "such", "that", "the", "their", "then", "there",
        "these", "they", "this", "to", "was", "will", "with",
    },
)

# Characters that FTS5 treats as special syntax inside a ``MATCH``
# expression. When the user's query does NOT contain an operator
# (one of AND / OR / NOT / NEAR / * / a quote) we wrap the whole
# query in double quotes after escaping any of these characters with
# a backslash — this is the documented FTS5 escape recipe.
_FTS5_SPECIAL_CHARS = frozenset({"(", ")", ":", "*", "\\"})

# FTS5 operator markers. When ANY of these substrings appear in the
# user input, we treat the query as an FTS5 expression and pass it
# through unchanged (FTS5 will reject it if it's malformed).
_FTS5_OPERATORS = (" AND ", " OR ", " NOT ", '"', "*", "NEAR")


def parse_date_filter(value: str) -> None:
    """Validate a ``YYYY-MM-DD`` date literal for ``--since`` / ``--until``.

    Accepts the literal forms ``YYYY-MM-DD``, plus the nullable
    sentinel ``null`` / ``not-null`` (matching the convention in
    :func:`validate_date_filter`). Anything else raises
    :class:`ValueError`.
    """
    v = value.strip()
    if v.lower() in (NULL_TOKEN, NOT_NULL_TOKEN):
        return
    try:
        from datetime import date
        date.fromisoformat(v)
    except ValueError as exc:
        raise ValueError(
            f"Invalid date filter {value!r}. Expected 'YYYY-MM-DD', "
            f"'null', or 'not-null'."
        ) from exc


def parse_release_filter(value: str) -> None:
    """Validate a release string for ``--release`` (e.g. ``Rel-17``)."""
    v = value.strip()
    if not v:
        raise ValueError("Release filter must not be empty.")
    if len(v) > 32:
        raise ValueError("Release filter too long (max 32 characters).")


def parse_spec_filter(value: str) -> None:
    """Validate a spec number for ``--spec`` (e.g. ``38.300``)."""
    v = value.strip()
    if not re.fullmatch(r"\d+\.\d+(?:-\d+)?", v):
        raise ValueError(
            f"Invalid spec filter {value!r}. Expected digits.digits, "
            f"optionally followed by -digits (e.g. '38.300', '38.300-1')."
        )


class SearchQueryBuilder:
    """Normalize a user query into an FTS5 ``MATCH`` expression.

    Rules (per spec §"Search query syntax"):

    * Plain text is wrapped in double quotes (``"…"``) after
      escaping FTS5 special characters.
    * Queries containing FTS5 operators (``AND``, ``OR``, ``NOT``,
      ``NEAR``, ``*``, or a ``"``) pass through unchanged — but
      each whitespace-separated token is still run through
      :func:`doc3gpp.storage.db.fts5_query.normalize_query` so
      spec-id / TDoc-id normalization is applied uniformly.
    * Empty input or stopwords-only input raises
      :class:`SearchQueryError`.
    """

    def __init__(self, query: str) -> None:
        self._query = query.strip()

    def build(self) -> str:
        if not self._query:
            from doc3gpp.models.search import SearchQueryError
            raise SearchQueryError("query required")
        if self._is_stopwords_only():
            from doc3gpp.models.search import SearchQueryError
            raise SearchQueryError("query has only stopwords")
        if self._is_operator_passthrough():
            return self._normalize_each_token(self._query)
        escaped = self._escape_specials(self._normalize_each_token(self._query))
        return f'"{escaped}"'

    def _is_operator_passthrough(self) -> bool:
        return any(op in self._query for op in _FTS5_OPERATORS)

    def _is_stopwords_only(self) -> bool:
        tokens = re.findall(r"\w+", self._query.lower())
        return bool(tokens) and all(t in _FTS5_STOPWORDS for t in tokens)

    def _normalize_each_token(self, text: str) -> str:
        """Apply ``normalize_query`` to each whitespace-separated token.

        Whole-string ``normalize_query`` would also work for plain
        text; for operator-passthrough queries we need per-token
        normalization so ``R5-1234567 AND 38.300`` becomes
        ``R5-1234567 R5-1234567 AND 38_300`` (the AND is preserved
        but each operand is normalized).
        """
        from doc3gpp.storage.db.fts5_query import normalize_query
        return " ".join(normalize_query(tok) for tok in text.split())

    def _escape_specials(self, text: str) -> str:
        out = []
        for ch in text:
            if ch in _FTS5_SPECIAL_CHARS:
                out.append("\\" + ch)
            else:
                out.append(ch)
        return "".join(out)