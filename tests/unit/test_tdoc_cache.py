"""Unit tests for the on-disk TDoc cache primitive.

These tests cover ``doc3gpp.scraping.cache.TDocCache`` only — no
network, no database, no settings overrides. Each test uses pytest's
``tmp_path`` fixture for filesystem isolation.

The cache is the foundation of the TDoc extraction pipeline: every
other layer (zip downloader, docx→markdown converter, parser, service)
composes on top of this. Keeping the test surface tight here means a
regression in any of those layers can be traced here first.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from doc3gpp.scraping.cache import CacheStatus, TDocCache


# ---------------------------------------------------------------------------
# Round-trip basics
# ---------------------------------------------------------------------------


def test_put_then_get_round_trip(tmp_path: Path) -> None:
    """Writing a payload and reading it back yields the same bytes."""
    cache = TDocCache(tmp_path / "cache", 1024)
    payload = b"x" * 100
    cache.put_bytes("R5s260009.zip", payload, "zips")

    assert cache.get_bytes("R5s260009.zip", "zips") == payload
    # A miss in the same subdir returns None (not raises).
    assert cache.get_bytes("missing.zip", "zips") is None
    # A hit in the other subdir is also a miss.
    assert cache.get_bytes("R5s260009.zip", "markdown") is None


def test_path_for_returns_expected_path_without_existence_check(
    tmp_path: Path,
) -> None:
    """``path_for`` must not touch the filesystem — useful for "are we
    about to download this?" checks before the actual fetch.
    """
    cache = TDocCache(tmp_path / "cache", 1024)
    expected = tmp_path / "cache" / "zips" / "R5s260009.zip"
    # File does not exist yet — path_for should still succeed.
    assert not expected.exists()
    assert cache.path_for("R5s260009.zip", "zips") == expected


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------


def test_enforce_size_limit_deletes_oldest_by_ctime(tmp_path: Path) -> None:
    """Three files with forced ctime order; a tighter limit must evict
    the oldest first, then the next-oldest, and stop at the limit.
    """
    # Large limit so the initial writes never auto-evict.
    cache = TDocCache(tmp_path / "cache", 10_000)
    cache.put_bytes("a", b"a" * 100, "zips")
    cache.put_bytes("b", b"b" * 100, "zips")
    cache.put_bytes("c", b"c" * 100, "zips")

    pa = cache.path_for("a", "zips")
    pb = cache.path_for("b", "zips")
    pc = cache.path_for("c", "zips")

    # Force ctime order independent of natural creation order. ext4 has
    # nanosecond ctime resolution; a 50 ms sleep gives 50 ms of headroom.
    os.utime(pa, (1_000_000, 1_000_000))
    time.sleep(0.05)
    os.utime(pb, (2_000_000, 2_000_000))
    time.sleep(0.05)
    os.utime(pc, (3_000_000, 3_000_000))

    # Fresh instance with a tighter limit (limit = 250 → must drop one
    # file to bring total from 300 to ≤ 250).
    tighter = TDocCache(tmp_path / "cache", 250)
    assert tighter.enforce_size_limit() == 1
    assert not pa.exists()
    assert pb.exists()
    assert pc.exists()

    # Tighten further: limit = 150 → must drop another file.
    tighter2 = TDocCache(tmp_path / "cache", 150)
    assert tighter2.enforce_size_limit() == 1
    assert pb.exists() is False
    assert pc.exists()


def test_enforce_size_limit_with_zero_limit_is_noop(tmp_path: Path) -> None:
    """``size_limit_bytes == 0`` means "unlimited"; enforce must be a
    no-op and must not delete anything.
    """
    cache = TDocCache(tmp_path / "cache", 0)
    cache.put_bytes("a", b"a" * 1_000, "zips")
    cache.put_bytes("b", b"b" * 1_000, "zips")
    cache.put_bytes("c", b"c" * 1_000, "zips")

    # Even though the cache holds 3 KB and the limit is "0", nothing
    # is evicted.
    assert cache.enforce_size_limit() == 0
    assert (cache.root / "zips" / "a").exists()
    assert (cache.root / "zips" / "b").exists()
    assert (cache.root / "zips" / "c").exists()


def test_enforce_size_limit_is_idempotent(tmp_path: Path) -> None:
    """Calling enforce twice in a row must not over-delete — the second
    call sees an already-trimmed cache and returns 0.
    """
    cache = TDocCache(tmp_path / "cache", 10_000)
    cache.put_bytes("a", b"a" * 100, "zips")
    cache.put_bytes("b", b"b" * 100, "zips")
    cache.put_bytes("c", b"c" * 100, "zips")

    tighter = TDocCache(tmp_path / "cache", 150)
    assert tighter.enforce_size_limit() == 2  # 300 → 150 needs 2 drops
    survivors = {p.name for p in (cache.root / "zips").iterdir()}
    assert len(survivors) == 1
    # Second pass is a no-op.
    assert tighter.enforce_size_limit() == 0


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------


def test_purge_clears_both_subdirs_and_recreates_them(tmp_path: Path) -> None:
    """purge() must remove every file under zips/ and markdown/ AND
    recreate the subdirs so subsequent writes still succeed.
    """
    cache = TDocCache(tmp_path / "cache", 1024)
    cache.put_bytes("a.zip", b"a" * 50, "zips")
    cache.put_bytes("b.zip", b"b" * 50, "zips")
    cache.put_bytes("doc.md", b"m" * 50, "markdown")

    zips_before = sorted(p.name for p in (cache.root / "zips").iterdir())
    md_before = sorted(p.name for p in (cache.root / "markdown").iterdir())
    assert zips_before == ["a.zip", "b.zip"]
    assert md_before == ["doc.md"]

    deleted = cache.purge()
    assert deleted == 3

    # Subdirs are preserved (and empty).
    assert (cache.root / "zips").is_dir()
    assert (cache.root / "markdown").is_dir()
    assert list((cache.root / "zips").iterdir()) == []
    assert list((cache.root / "markdown").iterdir()) == []

    # Status reflects zero state.
    assert cache.status() == CacheStatus(
        file_count=0, total_bytes=0, limit_bytes=1024, zips=0, markdown=0,
    )

    # And the cache is still usable after purge.
    cache.put_bytes("fresh.zip", b"hi", "zips")
    assert cache.get_bytes("fresh.zip", "zips") == b"hi"


# ---------------------------------------------------------------------------
# status — pure read
# ---------------------------------------------------------------------------


def test_status_is_non_mutating_and_reflects_changes(tmp_path: Path) -> None:
    """Two back-to-back status() calls with no intervening writes must
    be equal; adding a third file must show up in a third call.
    """
    cache = TDocCache(tmp_path / "cache", 1024)
    cache.put_bytes("a.zip", b"a" * 100, "zips")
    cache.put_bytes("b.md", b"m" * 200, "markdown")

    s1 = cache.status()
    s2 = cache.status()
    assert s1 == s2  # pure read, no side effects

    # Adding a third file must show up.
    cache.put_bytes("c.zip", b"c" * 50, "zips")
    s3 = cache.status()
    assert s3 != s1
    assert s3.file_count == 3
    assert s3.zips == 2
    assert s3.markdown == 1
    assert s3.total_bytes == 100 + 200 + 50
    assert s3.limit_bytes == 1024


def test_status_does_not_trigger_eviction(tmp_path: Path) -> None:
    """``status()`` must be a pure read — it must NOT call
    ``enforce_size_limit`` even when the cache is over the limit.
    """
    cache = TDocCache(tmp_path / "cache", 10_000)
    cache.put_bytes("a", b"a" * 100, "zips")
    cache.put_bytes("b", b"b" * 100, "zips")

    # Now "shrink" the limit via a fresh instance.
    tighter = TDocCache(tmp_path / "cache", 50)  # 200 bytes held, 50 byte limit
    s = tighter.status()
    # Both files still on disk — status did not evict them.
    assert (cache.root / "zips" / "a").exists()
    assert (cache.root / "zips" / "b").exists()
    assert s.file_count == 2
    assert s.total_bytes == 200
    assert s.limit_bytes == 50


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_key",
    [
        "../etc/passwd",
        "foo/bar",
        "foo;rm -rf",
        " ",  # space
        "",  # empty
        "key with space",
        "key\nwith\nnewline",
    ],
)
def test_path_traversal_and_invalid_keys_rejected(
    tmp_path: Path, bad_key: str,
) -> None:
    """A malicious or malformed key must never reach the filesystem —
    the sanitiser catches path separators, shell metacharacters,
    whitespace, control characters, and empty strings.
    """
    cache = TDocCache(tmp_path / "cache", 1024)
    with pytest.raises(ValueError):
        cache.put_bytes(bad_key, b"payload", "zips")
    # ``path_for`` shares the same sanitiser — also rejects.
    with pytest.raises(ValueError):
        cache.path_for(bad_key, "zips")
    # ``get_bytes`` shares the same sanitiser — also rejects.
    with pytest.raises(ValueError):
        cache.get_bytes(bad_key, "zips")


def test_zero_byte_payload_rejected(tmp_path: Path) -> None:
    """Zero-byte payloads are a sign of a truncated download; refuse
    to cache them so they can't masquerade as a successful hit later.
    """
    cache = TDocCache(tmp_path / "cache", 1024)
    with pytest.raises(ValueError):
        cache.put_bytes("r5s260009.zip", b"", "zips")


def test_bad_subdir_rejected(tmp_path: Path) -> None:
    """``subdir`` is a Literal["zips", "markdown"]; the runtime check
    ensures anything else raises ValueError (the static type is a
    hint, not a guarantee at runtime).
    """
    cache = TDocCache(tmp_path / "cache", 1024)
    with pytest.raises(ValueError):
        cache.put_bytes("r5s260009.zip", b"hi", "bogus")  # type: ignore[arg-type]


def test_negative_size_limit_rejected(tmp_path: Path) -> None:
    """A negative ``size_limit_bytes`` is meaningless and almost
    certainly a CLI parse error. The constructor raises ``ValueError``
    (rather than ``AssertionError``) so the failure surface stays
    uniform with ``CacheSettings.size_limit_mb`` which raises
    ``ValidationError`` for the same condition via pydantic.
    """
    with pytest.raises(ValueError):
        TDocCache(tmp_path / "cache", -1)


# ---------------------------------------------------------------------------
# Atomicity guarantees
# ---------------------------------------------------------------------------


def test_atomic_overwrite_replaces_payload(tmp_path: Path) -> None:
    """Writing to an existing key must replace the file, not append to
    it. The size after the second write must match the new payload
    exactly.
    """
    cache = TDocCache(tmp_path / "cache", 1024)
    original = b"original content " * 10  # 170 bytes
    replacement = b"new"  # 3 bytes

    path = cache.put_bytes("R5s260009.zip", original, "zips")
    assert path.stat().st_size == len(original)

    new_path = cache.put_bytes("R5s260009.zip", replacement, "zips")
    assert new_path == path  # same path
    assert path.stat().st_size == len(replacement)  # not appended
    assert path.read_bytes() == replacement


def test_get_bytes_does_not_touch_ctime(tmp_path: Path) -> None:
    """The brief requires that insertion order (i.e. ctime) drives
    eviction. ``Path.read_bytes`` must not silently update ``st_mtime``
    or ``st_ctime`` — doing so would invert the eviction order.
    """
    cache = TDocCache(tmp_path / "cache", 1024)
    path = cache.put_bytes("R5s260009.zip", b"hello", "zips")
    # Give the filesystem a moment so any spurious ctime change would
    # have a chance to register as a different value.
    time.sleep(0.05)
    ctime_before = os.stat(path).st_ctime
    mtime_before = os.stat(path).st_mtime

    # Multiple reads, to be sure.
    for _ in range(5):
        assert cache.get_bytes("R5s260009.zip", "zips") == b"hello"

    time.sleep(0.05)
    ctime_after = os.stat(path).st_ctime
    mtime_after = os.stat(path).st_mtime
    assert ctime_after == ctime_before
    assert mtime_after == mtime_before