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


def test_parse_spec_filter_rejects_bare_digits() -> None:
    with pytest.raises(ValueError):
        parse_spec_filter("38300")


def test_query_builder_passes_operators_through() -> None:
    assert (
        SearchQueryBuilder('NB-IoT AND "5G NR"').build() == 'NB-IoT AND "5G NR"'
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
    and the FTS5 expression matches the indexed text."""
    assert (
        SearchQueryBuilder("R5-1234567 AND 38.300").build()
        == "R5-1234567 R5-1234567 AND 38_300"
    )
