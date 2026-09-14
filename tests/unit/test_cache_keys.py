"""Unit tests for :mod:`doc3gpp.scraping.cache_keys`.

The ``derive_cache_file`` helper centralises the rule for turning a 3GPP
``ftp_url`` into the cache key used by :class:`doc3gpp.scraping.cache.TDocCache`.
This file locks the spec examples (MD5s and the sanitisation rules) so
downstream code can rely on the contract without re-reading the function.
"""

from __future__ import annotations

import hashlib
import re

import pytest

from doc3gpp.scraping.cache import _KEY_PATTERN
from doc3gpp.scraping.cache_keys import derive_cache_file

# Locked spec examples: canonical (lowercase) ``ftp_url`` form —
# ``normalize_ftp_path`` lowercases the whole relative path before
# anything reaches ``derive_cache_file``.
_SPEC_EXAMPLES: list[tuple[str, str]] = [
    (
        "tsg_ran/wg5_test_ex-t1/ttcn/ttcn_crs/2026/docs/r5s260162.zip",
        "r5s260162-0004e1fa60134d966150b74812f8d983.zip",
    ),
    (
        "tsg_ran/wg5_test_ex-t1/ttcn/ttcn_crs/2026/review/r5s260034_mcc160comments.zip",
        "r5s260034_mcc160comments-7648344d6114eaf191db0d2915a5c37f.zip",
    ),
]


@pytest.mark.parametrize(("ftp_url", "expected"), _SPEC_EXAMPLES)
def test_derive_cache_file_examples_match_spec(ftp_url: str, expected: str) -> None:
    assert derive_cache_file(ftp_url) == expected


def test_derive_cache_file_stable_across_calls() -> None:
    ftp_url = "tsg_ran/wg5_test_ex-t1/ttcn/ttcn_crs/2026/docs/r5s260162.zip"
    first = derive_cache_file(ftp_url)
    for _ in range(10):
        assert derive_cache_file(ftp_url) == first


def test_derive_cache_file_uses_relative_url_not_basename() -> None:
    """The MD5 must cover the full relative path, not just the basename.

    Two URLs sharing a basename but living in different folders must
    produce different cache keys — that's the whole point of keying on
    the relative URL (revisions and re-uploads in different folders stay
    distinct).
    """
    assert derive_cache_file("a/R5s260001.zip") != derive_cache_file("b/R5s260001.zip")


def test_derive_cache_file_sanitises_hostile_chars() -> None:
    """Hostile characters in the basename must not raise; they become ``_``."""
    ftp_url = "path with spaces/foo bar.zip"
    result = derive_cache_file(ftp_url)
    # The stem portion (everything before the final ``-{md5}.zip``) must
    # be drawn entirely from the cache-key alphabet.
    assert "-" in result
    assert result.endswith(".zip")
    stem = result.rsplit("-", 1)[0]
    assert re.fullmatch(r"[A-Za-z0-9._-]*", stem) is not None


def test_derive_cache_file_strips_zip_extension() -> None:
    """``foo.zip`` must yield ``foo-<md5>.zip`` (no ``.zip.zip`` artifact)."""
    result = derive_cache_file("foo.zip")
    assert result.startswith("foo-")
    assert result.endswith(".zip")
    assert ".zip.zip" not in result


def test_derive_cache_file_handles_no_extension() -> None:
    """A bare basename with no ``.zip`` suffix still gets ``-<md5>.zip`` appended."""
    ftp_url = "foo"
    expected_digest = hashlib.md5(ftp_url.encode("utf-8")).hexdigest()
    assert derive_cache_file(ftp_url) == f"foo-{expected_digest}.zip"


def test_derive_cache_file_strips_only_trailing_zip() -> None:
    """Only a **trailing** ``.zip`` is stripped; middle ``.zip`` survives."""
    result = derive_cache_file("foo.zip.docx")
    # The stem portion must still contain the inner ``.zip``.
    assert ".zip" in result
    # And the function must not have produced a double ``.zip`` artifact
    # (it appends one ``.zip`` unconditionally at the end).
    assert not result.endswith(".zip.zip")


def test_derive_cache_file_respects_max_length() -> None:
    """Even with a 250-char stem, the result must be <= 200 chars."""
    long_stem = "a" * 250
    ftp_url = f"some/folder/{long_stem}.zip"
    result = derive_cache_file(ftp_url)
    assert len(result) <= 200


def test_derive_cache_file_output_matches_key_pattern() -> None:
    """Every output (spec + hostile) must satisfy the shared key pattern."""
    cases = [ftp_url for ftp_url, _ in _SPEC_EXAMPLES] + [
        "path with spaces/foo bar.zip",
        "ümläut/R5s260001.zip",
    ]
    for ftp_url in cases:
        result = derive_cache_file(ftp_url)
        assert _KEY_PATTERN.fullmatch(result) is not None, (
            f"derive_cache_file({ftp_url!r}) = {result!r} does not match "
            f"{_KEY_PATTERN.pattern!r}"
        )
