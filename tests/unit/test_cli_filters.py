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
    NOT_NULL_TOKEN,
    NULL_TOKEN,
    is_not_null_token,
    is_null_token,
    validate_date_filter,
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