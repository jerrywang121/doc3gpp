"""Unit tests for :mod:`doc3gpp.web.filters`.

The HTTP query-param filter parser is a thin adapter on top of the
shared CLI helpers — these tests lock in its contract: the raw
query-string value is converted to the shape the existing helpers
expect, delegated to, and any ``ValueError`` is re-raised as
:class:`doc3gpp.web.errors.InvalidFilterError`.
"""
from __future__ import annotations

import pytest

from doc3gpp.web.errors import InvalidFilterError
from doc3gpp.web.filters import (
    parse_bool_query,
    parse_date_query,
    parse_int_query,
    parse_text_query,
    parse_tdoc_id_query,
)


class TestParseTextQuery:
    """``parse_text_query`` is a passthrough — the SQL helper does the work."""

    @pytest.mark.parametrize("value", [None, "!foo", "null", "not-null", "%R5%", "R5-260013"])
    def test_passthrough(self, value: str | None) -> None:
        assert parse_text_query(value) is value


class TestParseDateQuery:
    """``parse_date_query`` validates via :func:`validate_date_filter`."""

    def test_valid_operator_round_trip(self) -> None:
        assert parse_date_query(">= '2026-01-01'") == ">= '2026-01-01'"
        assert parse_date_query("<= '2026-01-01'") == "<= '2026-01-01'"
        assert parse_date_query("= '2026-01-01'") == "= '2026-01-01'"
        assert parse_date_query("!= '2026-01-01'") == "!= '2026-01-01'"
        assert parse_date_query("> '2026-01-01'") == "> '2026-01-01'"
        assert parse_date_query("< '2026-01-01'") == "< '2026-01-01'"

    def test_null_token_round_trip(self) -> None:
        assert parse_date_query("null") == "null"
        assert parse_date_query("not-null") == "not-null"

    def test_none_passthrough(self) -> None:
        assert parse_date_query(None) is None

    def test_invalid_raises(self) -> None:
        with pytest.raises(InvalidFilterError):
            parse_date_query("oops")


class TestParseBoolQuery:
    """``parse_bool_query`` accepts only the strict ``"true"`` / ``"false"`` literals."""

    def test_true(self) -> None:
        assert parse_bool_query("true") is True

    def test_false(self) -> None:
        assert parse_bool_query("false") is False

    def test_none(self) -> None:
        assert parse_bool_query(None) is None

    @pytest.mark.parametrize("value", ["maybe", "", "True", "FALSE", "1", "0"])
    def test_invalid_raises(self, value: str) -> None:
        with pytest.raises(InvalidFilterError):
            parse_bool_query(value)


class TestParseIntQuery:
    """``parse_int_query`` parses base-10 integers with optional bounds."""

    def test_basic_parse(self) -> None:
        assert parse_int_query("42") == 42
        assert parse_int_query("-1") == -1

    def test_none_passthrough(self) -> None:
        assert parse_int_query(None) is None

    def test_no_bounds_accepts_any_int(self) -> None:
        assert parse_int_query("0") == 0
        assert parse_int_query("9999") == 9999

    def test_within_bounds(self) -> None:
        assert parse_int_query("42", min=10, max=50) == 42
        assert parse_int_query("10", min=10, max=50) == 10
        assert parse_int_query("50", min=10, max=50) == 50

    def test_below_min_raises(self) -> None:
        with pytest.raises(InvalidFilterError) as exc:
            parse_int_query("5", min=10, max=50)
        assert "out of range" in str(exc.value)
        assert "10" in str(exc.value)
        assert "50" in str(exc.value)

    def test_above_max_raises(self) -> None:
        with pytest.raises(InvalidFilterError) as exc:
            parse_int_query("60", min=10, max=50)
        assert "out of range" in str(exc.value)

    def test_min_only(self) -> None:
        assert parse_int_query("100", min=10) == 100
        with pytest.raises(InvalidFilterError):
            parse_int_query("1", min=10)

    def test_max_only(self) -> None:
        assert parse_int_query("1", max=50) == 1
        with pytest.raises(InvalidFilterError):
            parse_int_query("100", max=50)

    def test_non_integer_raises(self) -> None:
        with pytest.raises(InvalidFilterError) as exc:
            parse_int_query("not-a-number")
        assert "expected integer" in str(exc.value)


class TestParseTdocIdQuery:
    """``parse_tdoc_id_query`` wraps :func:`parse_tdoc_id`."""

    def test_canonical_id(self) -> None:
        assert parse_tdoc_id_query("R5-123456") == ("R5-", 123456)
        assert parse_tdoc_id_query("R5-260013") == ("R5-", 260013)

    def test_ttcn_shape(self) -> None:
        assert parse_tdoc_id_query("R5s260009") == ("R5s", 260009)

    def test_workshop_shape(self) -> None:
        assert parse_tdoc_id_query("R5w260013") == ("R5w", 260013)

    def test_seven_digit_ran4(self) -> None:
        assert parse_tdoc_id_query("R4-2607922") == ("R4-", 2607922)

    def test_bad_input_raises(self) -> None:
        with pytest.raises(InvalidFilterError):
            parse_tdoc_id_query("not-a-tdoc-id")
