"""Index-time query normalizer contract.

This pins every recognition rule documented in
``docs/superpowers/specs/2026-07-29-fts5-search-design.md``
§"What `normalize_query` adds on top". The corpus IS the contract —
if a rule changes, this file changes with it, never one without
the other.
"""

from __future__ import annotations

import pytest

from doc3gpp.storage.db.fts5_query import normalize_query


@pytest.mark.parametrize(
    "raw,normalized",
    [
        # TDoc IDs — base + full id both searchable
        ("R5-1234567r2",  "R5-1234567 R5-1234567r2"),
        ("R5-123456r1",   "R5-123456 R5-123456r1"),
        ("RP-2200456r10", "RP-2200456 RP-2200456r10"),
        # TDoc IDs — no revision, both forms written
        ("R5-1234567",    "R5-1234567 R5-1234567"),
        ("RP-2200456",    "RP-2200456 RP-2200456"),  # plenary, no rev
        ("SP-2100123",    "SP-2100123 SP-2100123"),
        ("S2-1987654",    "S2-1987654 S2-1987654"),
        # Case-insensitive
        ("r5-1234567R2",  "R5-1234567 R5-1234567r2"),
        # Spec numbers — dot replaced with underscore so they stay one token
        ("38.300",        "38_300"),
        ("38.300-1",      "38_300-1"),
        # Hyphenated jargon — passthrough (unicode61 already splits it)
        ("NB-IoT",        "NB-IoT"),
        ("5G NR scheduling", "5G NR scheduling"),
        ("eNB/gNB",       "eNB/gNB"),
        # Plain words — passthrough
        ("scheduling",    "scheduling"),
        # Empty input
        ("",              ""),
    ],
)
def test_normalize_query(raw: str, normalized: str) -> None:
    assert normalize_query(raw) == normalized


@pytest.mark.parametrize(
    "raw,normalized",
    [
        ("abc123.45xyz", "abc123_45xyz"),
        ("1.2.3.4", "1_2.3_4"),
        ("1.2-3.4", "1_2-3.4"),
        ("١٢.٣٤-٥", "١٢_٣٤-٥"),
        ("123.x 456.", "123.x 456."),
    ],
)
def test_normalize_query_preserves_spec_matching(raw: str, normalized: str) -> None:
    assert normalize_query(raw) == normalized


def test_normalize_query_handles_long_digit_runs_promptly() -> None:
    import os
    import subprocess
    import sys

    long_digits = "0" * 250_000
    raw = long_digits + ".x 38.300-1 " + long_digits + ".1"
    expected = long_digits + ".x 38_300-1 " + long_digits + "_1"
    code = (
        "import sys; "
        "from doc3gpp.storage.db.fts5_query import normalize_query; "
        "sys.stdout.write(normalize_query(sys.stdin.read()))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            input=raw,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            timeout=5,
            check=True,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("normalizing long digit runs took more than five seconds")
    assert result.stdout == expected
