"""Unit tests for :mod:`doc3gpp.cli_filters`.

The CLI exposes a single, stable grammar for filter values:
``null`` / ``not-null`` for nullability, plain strings as ``LIKE``
patterns, and the ``"<op> 'YYYY-MM-DD'"`` form for date columns. The
helpers here are the contract surface — any drift here would change
the behaviour of ``tdoc parse --meeting-id`` and the other commands
that consume the same syntax.
"""

from __future__ import annotations

import pytest

from doc3gpp.cli_filters import (
    DATE_FILTER_RE,
    NOT_LIKE_PREFIX,
    NOT_NULL_TOKEN,
    NULL_TOKEN,
    TDOC_ID_RE,
    is_not_null_token,
    is_null_token,
    parse_tdoc_id,
    split_not_like_prefix,
    validate_date_filter,
    validate_tdoc_id,
)


class TestNullTokens:
    """``null`` / ``not-null`` are case-insensitive on stripped input."""

    @pytest.mark.parametrize("value", ["null", "NULL", "Null", "  null  "])
    def test_is_null_token_matches(self, value: str) -> None:
        assert is_null_token(value)

    @pytest.mark.parametrize("value", ["not-null", "NOT-NULL", "  Not-Null  "])
    def test_is_not_null_token_matches(self, value: str) -> None:
        assert is_not_null_token(value)

    @pytest.mark.parametrize(
        "value", ["", " nul ", "none", "nil", "not_null", "0", "%null%"]
    )
    def test_is_null_token_rejects(self, value: str) -> None:
        assert not is_null_token(value)

    @pytest.mark.parametrize(
        "value", ["", " not null ", "notnull", "null-not", "%not-null%"]
    )
    def test_is_not_null_token_rejects(self, value: str) -> None:
        assert not is_not_null_token(value)


class TestValidateDateFilter:
    """``validate_date_filter`` is the boundary guard for ``--uploaded-date``."""

    @pytest.mark.parametrize(
        "value",
        [
            "null",
            "NULL",
            "  Null  ",
            "not-null",
            "NOT-NULL",
            "  Not-Null  ",
            "= '2026-02-31'",
            "!= '2026-02-31'",
            "< '2026-02-31'",
            "<= '2026-02-31'",
            "> '2026-02-31'",
            ">= '2026-02-31'",
            ">=  '2026-02-31'",
            "  >= '2026-02-31'  ",
            ">= '2026-12-31'",
        ],
    )
    def test_accepts_valid_inputs(self, value: str) -> None:
        validate_date_filter(value)

    @pytest.mark.parametrize(
        "value",
        [
            # Not a recognised form.
            "",
            "yesterday",
            "2026-02-31",
            # Wrong shape: bare value.
            "'2026-02-31'",
            # Wrong operator.
            "== '2026-02-31'",
            "<> '2026-02-31'",
            # Wrong date shape — month / day truncated.
            ">= '2026-2-31'",
            ">= '26-02-31'",
            ">= '2026/02/31'",
            # Trailing junk.
            ">= '2026-02-31' OR 1=1",
            ">= '2026-02-31'; DROP TABLE tdocs",
            # Missing quotes.
            ">= 2026-02-31",
        ],
    )
    def test_rejects_invalid_inputs(self, value: str) -> None:
        with pytest.raises(ValueError, match="Invalid date filter"):
            validate_date_filter(value)

    def test_error_message_lists_accepted_forms(self) -> None:
        """The error message must mention every accepted form so the
        operator can fix the typo without reading the docs."""
        with pytest.raises(ValueError) as excinfo:
            validate_date_filter("yesterday")
        msg = str(excinfo.value)
        assert "null" in msg
        assert "not-null" in msg
        assert "YYYY-MM-DD" in msg


class TestDateFilterRegex:
    """The regex is part of the public surface — pin its shape so a
    silent edit cannot break the CLI grammar."""

    def test_captures_operator_and_date(self) -> None:
        match = DATE_FILTER_RE.match(">= '2026-02-31'")
        assert match is not None
        assert match["op"] == ">="
        assert match["date"] == "2026-02-31"

    @pytest.mark.parametrize(
        "op", ["=", "!=", "<", "<=", ">", ">="]
    )
    def test_all_supported_operators(self, op: str) -> None:
        match = DATE_FILTER_RE.match(f"{op} '2026-02-31'")
        assert match is not None
        assert match["op"] == op

    def test_rejects_double_equals(self) -> None:
        assert DATE_FILTER_RE.match("== '2026-02-31'") is None

    def test_token_constants(self) -> None:
        """The token constants are part of the surface used by the
        repository layer — accidental rename would be a breaking change."""
        assert NULL_TOKEN == "null"
        assert NOT_NULL_TOKEN == "not-null"


class TestNotLikePrefix:
    """``!X`` is the marker for a negated ``LIKE`` filter; the bang is
    consumed and the remainder is bound as the pattern."""

    def test_prefix_constant(self) -> None:
        assert NOT_LIKE_PREFIX == "!"

    @pytest.mark.parametrize(
        "value,pattern",
        [
            ("!%RAN5%", "%RAN5%"),
            ("!foo", "foo"),
            ("!tsg_ran/WG5%", "tsg_ran/WG5%"),
            ("!_underscore", "_underscore"),
            ("!", ""),  # bare bang → empty pattern (matches everything)
        ],
    )
    def test_bang_is_consumed(self, value: str, pattern: str) -> None:
        negated, actual = split_not_like_prefix(value)
        assert negated is True
        assert actual == pattern

    @pytest.mark.parametrize(
        "value",
        [
            "%RAN5%",
            "foo",
            "tsg_ran/WG5%",
            "",  # empty value is not negated
            "  !foo",  # leading whitespace is not stripped
            "  foo",
            "null",  # nullability tokens are NOT consumed by this helper
            "not-null",
        ],
    )
    def test_non_negated_values_pass_through(self, value: str) -> None:
        negated, actual = split_not_like_prefix(value)
        assert negated is False
        assert actual == value


class TestParseTdocId:
    """``parse_tdoc_id`` is the boundary guard for ``meeting list --tdoc``."""

    @pytest.mark.parametrize(
        "value,prefix,number",
        [
            ("R5-260013", "R5-", 260013),
            ("R5s260009", "R5s", 260009),
            ("R5w260013", "R5w", 260013),
            ("C6-250028", "C6-", 250028),
            ("S2-150000", "S2-", 150000),
            ("r5-260013", "r5-", 260013),
            ("  R5-260013  ", "R5-", 260013),
        ],
    )
    def test_accepts_valid_shapes(self, value: str, prefix: str, number: int) -> None:
        assert parse_tdoc_id(value) == (prefix, number)

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "R5-26001",
            "R5-2600133",
            "A5-260013",
            "X5-260013",
            "R0-260013",
            "R5x260013",
            "R5.260013",
            "R5-abcdef",
            "R5-260013r1",
            "LS-260001",
        ],
    )
    def test_rejects_invalid_shapes(self, value: str) -> None:
        with pytest.raises(ValueError, match="Invalid TDoc id"):
            parse_tdoc_id(value)

    def test_error_message_lists_expected_shape(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            parse_tdoc_id("bogus")
        msg = str(excinfo.value)
        assert "R5-260013" in msg
        assert "R5s260009" in msg
        assert "R5w260013" in msg

    def test_validate_tdoc_id_is_parse_tdoc_id_without_return(self) -> None:
        validate_tdoc_id("R5-260013")
        with pytest.raises(ValueError, match="Invalid TDoc id"):
            validate_tdoc_id("not-a-tdoc")

    def test_tdoc_id_regex_is_full_shape(self) -> None:
        assert TDOC_ID_RE.fullmatch("R5-260013") is not None
        assert TDOC_ID_RE.fullmatch(" R5-260013") is None
        assert TDOC_ID_RE.fullmatch("R5-260013 ") is None
        assert TDOC_ID_RE.fullmatch("R5-260013X") is None