"""Tests for the FTS5 query normalization helper."""

from __future__ import annotations

import pytest

from doc3gpp.cli_filters import (
    SearchQueryBuilder,
    parse_date_filter,
    parse_release_filter,
    parse_spec_filter,
)
from doc3gpp.models.search import SearchQueryError


def test_parse_date_filter_accepts_iso() -> None:
    parse_date_filter("2026-01-02")  # no raise


def test_parse_date_filter_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_date_filter("2026/01/02")


def test_parse_release_filter_rejects_empty() -> None:
    with pytest.raises(ValueError):
        parse_release_filter("")


def test_parse_spec_filter_accepts_dotted() -> None:
    parse_spec_filter("38.300")
    parse_spec_filter("38.300-1")


def test_parse_spec_filter_accepts_rich_grammar() -> None:
    """``--spec`` accepts the rich-filter grammar like the MCP tools."""
    parse_spec_filter("38.3%")
    parse_spec_filter("!38.3%")
    parse_spec_filter("null")
    parse_spec_filter("not-null")


def test_parse_spec_filter_rejects_bare_digits() -> None:
    with pytest.raises(ValueError):
        parse_spec_filter("38300")


def test_query_builder_passes_operators_through() -> None:
    """``NB-IoT`` contains a bare ``-`` which FTS5 would parse as a
    column separator (``no such column: IoT``); the operator-
    passthrough branch now wraps each hyphenated operand in
    quotes so the FTS5 expression is safe. ``"5G NR"`` is
    already quoted by the user so the builder passes it through.
    """
    assert (
        SearchQueryBuilder('NB-IoT AND "5G NR"').build()
        == '"NB-IoT" AND "5G NR"'
    )


def test_query_builder_quotes_plain_text() -> None:
    assert SearchQueryBuilder("scheduling NR").build() == '"scheduling NR"'


def test_query_builder_escapes_specials() -> None:
    assert (
        SearchQueryBuilder("foo(bar)").build() == '"foo\\(bar\\)"'
    )


def test_query_builder_rejects_empty() -> None:
    with pytest.raises(SearchQueryError):
        SearchQueryBuilder("").build()


def test_query_builder_rejects_stopwords_only() -> None:
    with pytest.raises(SearchQueryError):
        SearchQueryBuilder("the a").build()


def test_query_builder_normalizes_tdoc_id() -> None:
    """User input ``R5-1234567r2`` is normalized the same way the
    index normalized it (base + full id both searchable)."""
    # Operator passthrough branch: query containing AND/OR/etc. is
    # returned as-is. For ``R5-1234567r2`` alone (no operator), the
    # builder quotes it AND runs normalize_query on it.
    result = SearchQueryBuilder("R5-1234567r2").build()
    assert result == '"R5-1234567 R5-1234567r2"'


def test_query_builder_normalizes_spec_id() -> None:
    """User input ``38.300`` becomes ``38_300`` in the quoted FTS5
    expression (matching the index-time normalization)."""
    assert SearchQueryBuilder("38.300").build() == '"38_300"'


def test_query_builder_passthrough_normalizes_too() -> None:
    """Even operator-passthrough queries get normalize_query applied
    to each token so the user can write ``R5-1234567 AND 38.300``
    and the FTS5 expression matches the indexed text.

    ``R5-1234567`` (after TDoc-id normalization: ``R5-1234567
    R5-1234567``) is also wrapped per-operand in quotes because
    each half still contains a ``-`` — FTS5 would otherwise parse
    the first as ``R5``-column / ``1234567``-operand and crash
    with ``no such column: 1234567``. The quoted form is a safe
    FTS5 phrase.
    """
    assert (
        SearchQueryBuilder("R5-1234567 AND 38.300").build()
        == '"R5-1234567" "R5-1234567" AND 38_300'
    )


def test_query_builder_quotes_hyphenated_jargon_in_operator_query() -> None:
    """User reports: ``nb-iot AND ims`` crashes FTS5 with
    ``no such column: iot`` because FTS5 treats ``-`` as a column
    separator. The fix: hyphenated jargon (``NB-IoT``, ``5G NR``
    would not apply since it has no hyphen, but ``38.300`` IS
    already handled by spec-id normalization) must be quoted when
    it appears in an operator-passthrough query so the column
    separator is suppressed.

    After the fix:
    - Bare hyphenated jargon is quoted (``nb-iot`` → ``"nb-iot"``)
    - Bare spec-id jargon is left alone (already handled by
      ``normalize_query``: ``38.300`` → ``38_300``)
    - Operators (``AND``/``OR``/``NOT``/``NEAR``) are not quoted.
    - Already-quoted phrases are passed through.
    """
    # Primary user-reported bug.
    assert (
        SearchQueryBuilder("nb-iot AND ims").build()
        == '"nb-iot" AND ims'
    )
    # Capitalised variant (matches index-time casing).
    assert (
        SearchQueryBuilder('NB-IoT AND "5G NR"').build()
        == '"NB-IoT" AND "5G NR"'
    )
    # Three-term case.
    assert (
        SearchQueryBuilder("nb-iot OR nb-iot AND ims").build()
        == '"nb-iot" OR "nb-iot" AND ims'
    )
    # Hyphenated jargon without operator still gets the quote-wrap
    # from the existing branch (no regression).
    assert SearchQueryBuilder("nb-iot").build() == '"nb-iot"'
