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

import multiprocessing
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

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


def test_concurrent_puts_serialize_install_and_eviction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent revisions remain complete and size-bounded.

    Eviction is deliberately slowed so an unguarded writer can overlap
    another writer's accounting pass; the cache must serialize those
    critical sections while retaining atomic, revision-specific files.
    """
    worker_count = 12
    keys = [f"R5s2600{i:04d}.zip" for i in range(worker_count)]
    payloads = {
        key: (key.encode() + b"|complete|") * 32
        for key in keys
    }
    size_limit = max(map(len, payloads.values())) * 2
    cache = TDocCache(tmp_path / "cache", size_limit)

    original_enforce = cache.enforce_size_limit
    probe_lock = threading.Lock()
    start_barrier = threading.Barrier(worker_count + 1)
    active_enforcers = 0
    peak_enforcers = 0

    def probed_enforce() -> int:
        nonlocal active_enforcers, peak_enforcers
        with probe_lock:
            active_enforcers += 1
            peak_enforcers = max(peak_enforcers, active_enforcers)
        time.sleep(0.02)
        try:
            return original_enforce()
        finally:
            with probe_lock:
                active_enforcers -= 1

    monkeypatch.setattr(cache, "enforce_size_limit", probed_enforce)

    def write_revision(key: str) -> Path:
        start_barrier.wait()
        return cache.put_bytes(key, payloads[key], "zips")

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(write_revision, key) for key in keys]
        start_barrier.wait()
        paths = [future.result() for future in futures]

    assert peak_enforcers == 1
    assert paths == [cache.path_for(key, "zips") for key in keys]

    files = sorted((cache.root / "zips").iterdir())
    assert files
    for path in files:
        assert path.read_bytes() == payloads[path.name]

    status = cache.status()
    actual_bytes = sum(path.stat().st_size for path in files)
    assert status.total_bytes == actual_bytes
    assert status.total_bytes <= size_limit
    assert status.file_count == len(files)



def test_zip_provenance_round_trip_and_cache_accounting(tmp_path: Path) -> None:
    cache = TDocCache(tmp_path / "cache", 10_000)
    payload = b"PK\x03\x04zip snapshot"

    path = cache.put_zip_with_provenance(
        "R5s260009.zip", payload, "https://example.test/R5s260009.zip"
    )

    assert path == cache.path_for("R5s260009.zip", "zips")
    assert cache.get_zip_with_provenance("R5s260009.zip") == (
        payload,
        "https://example.test/R5s260009.zip",
    )
    status = cache.status()
    assert status.zips == 2
    assert status.file_count == 2
    assert status.total_bytes > len(payload)


def test_plain_zip_write_invalidates_existing_provenance(tmp_path: Path) -> None:
    cache = TDocCache(tmp_path / "cache", 10_000)
    cache.put_zip_with_provenance("R5s260009.zip", b"verified", "https://example.test/a.zip")

    cache.put_bytes("R5s260009.zip", b"plain replacement", "zips")

    assert cache.get_bytes("R5s260009.zip", "zips") == b"plain replacement"
    assert cache.get_zip_with_provenance("R5s260009.zip") is None


def test_zip_provenance_sidecars_participate_in_purge_and_eviction(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    cache = TDocCache(root, 10_000)
    cache.put_zip_with_provenance("R5s260009.zip", b"verified", "https://example.test/a.zip")

    assert cache.purge_subdir("zips") == 2
    assert cache.get_bytes("R5s260009.zip", "zips") is None
    assert cache.get_zip_with_provenance("R5s260009.zip") is None

    cache.put_zip_with_provenance("R5s260009.zip", b"verified", "https://example.test/a.zip")
    tighter = TDocCache(root, 1)
    assert tighter.enforce_size_limit() == 2
    assert tighter.get_zip_with_provenance("R5s260009.zip") is None
    assert tighter.status().zips == 0


def test_interrupted_or_orphaned_provenance_is_never_trusted(tmp_path: Path) -> None:
    cache = TDocCache(tmp_path / "cache", 10_000)
    cache.put_zip_with_provenance("R5s260009.zip", b"verified", "https://example.test/a.zip")
    sidecar_dir = cache.root / "zips" / ".provenance"
    sidecar = next(sidecar_dir.iterdir())

    sidecar.write_bytes(b'{"version":')
    assert cache.get_zip_with_provenance("R5s260009.zip") is None

    cache.put_zip_with_provenance("R5s260009.zip", b"verified", "https://example.test/a.zip")
    cache.path_for("R5s260009.zip", "zips").unlink()
    assert cache.get_zip_with_provenance("R5s260009.zip") is None


def _cache_process_put_read(root: str, payload: bytes, source_url: str, results) -> None:
    cache = TDocCache(Path(root), 0)
    expected = {
        b"PK\x03\x04first payload": "https://example.test/first.zip",
        b"PK\x03\x04second payload": "https://example.test/second.zip",
    }
    for _ in range(20):
        cache.put_zip_with_provenance("shared.zip", payload, source_url)
        pair = cache.get_zip_with_provenance("shared.zip")
        if pair is None or expected.get(pair[0]) != pair[1]:
            results.put(("mismatch", pair))
            return
    results.put(("ok", source_url))


def test_concurrent_cache_processes_never_pair_bytes_with_wrong_url(tmp_path: Path) -> None:
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    root = str(tmp_path / "cache")
    writers = [
        context.Process(
            target=_cache_process_put_read,
            args=(root, b"PK\x03\x04first payload", "https://example.test/first.zip", results),
        ),
        context.Process(
            target=_cache_process_put_read,
            args=(root, b"PK\x03\x04second payload", "https://example.test/second.zip", results),
        ),
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(timeout=20)
    try:
        assert all(not writer.is_alive() for writer in writers)
        assert [writer.exitcode for writer in writers] == [0, 0]
        assert [results.get(timeout=2)[0] for _ in writers] == ["ok", "ok"]
    finally:
        for writer in writers:
            if writer.is_alive():
                writer.terminate()
                writer.join(timeout=2)
        results.close()
        results.join_thread()


def _write_zip_while_paused(
    root: str,
    staged: Any,
    resume: Any,
    results: Any,
    *,
    with_provenance: bool,
) -> None:
    cache = TDocCache(Path(root), 10_000)
    write_temp_file = cache._write_temp_file

    def pause_after_zip_temp(directory: Path, payload: bytes) -> str:
        temp_name = write_temp_file(directory, payload)
        if directory == cache.root / "zips":
            staged.set()
            if not resume.wait(timeout=15):
                raise TimeoutError("test did not release staged ZIP write")
        return temp_name

    cache._write_temp_file = pause_after_zip_temp
    try:
        if with_provenance:
            path = cache.put_zip_with_provenance(
                "new.zip", b"ZIP payload", "https://example.test/new.zip"
            )
        else:
            path = cache.put_bytes("new.zip", b"ZIP payload", "zips")
    except Exception as exc:  # noqa: BLE001 - report child failure to the parent test
        results.put(("error", type(exc).__name__, str(exc)))
    else:
        results.put(("ok", path.name))


@pytest.mark.parametrize("purge_scope", ["all", "zips"], ids=["full", "zip-subdir"])
@pytest.mark.parametrize("with_provenance", [False, True], ids=["plain-zip", "provenance-zip"])
def test_zip_write_and_purge_are_serialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    purge_scope: str,
    with_provenance: bool,
) -> None:
    """Purges wait for staged writes and never remove their temporary files."""
    context = multiprocessing.get_context("spawn")
    root = tmp_path / "cache"
    cache = TDocCache(root, 10_000)
    staged = context.Event()
    resume = context.Event()
    write_results = context.Queue()
    writer = context.Process(
        target=_write_zip_while_paused,
        args=(str(root), staged, resume, write_results),
        kwargs={"with_provenance": with_provenance},
    )
    notifications: queue.Queue[tuple[str, ...]] = queue.Queue()
    purge_outcome: list[tuple[str, ...]] = []
    purger: threading.Thread | None = None

    writer.start()
    try:
        assert staged.wait(timeout=15), "writer did not stage a ZIP temporary file"

        if os.name == "nt":
            import msvcrt

            original_locking = msvcrt.locking

            def track_locking(fd: int, mode: int, nbytes: int) -> None:
                if (
                    threading.current_thread().name == "cache-purger"
                    and mode == msvcrt.LK_LOCK
                ):
                    notifications.put(("lock-attempt",))
                original_locking(fd, mode, nbytes)

            monkeypatch.setattr(msvcrt, "locking", track_locking)
        else:
            import fcntl

            original_flock = fcntl.flock

            def track_flock(fd: int, operation: int) -> None:
                if (
                    threading.current_thread().name == "cache-purger"
                    and operation & fcntl.LOCK_EX
                ):
                    notifications.put(("lock-attempt",))
                original_flock(fd, operation)

            monkeypatch.setattr(fcntl, "flock", track_flock)

        def purge() -> None:
            try:
                deleted = (
                    cache.purge()
                    if purge_scope == "all"
                    else cache.purge_subdir("zips")
                )
            except Exception as exc:  # noqa: BLE001 - report thread failure to the parent test
                purge_outcome.append(("error", type(exc).__name__, str(exc)))
                notifications.put(("purge-error",))
            else:
                purge_outcome.append(("ok", str(deleted)))
                notifications.put(("purge-complete",))

        purger = threading.Thread(target=purge, name="cache-purger")
        purger.start()
        first_notice = notifications.get(timeout=15)
        assert first_notice[0] in {"lock-attempt", "purge-complete"}, first_notice

        # For the fixed protocol the purger is waiting on the interprocess barrier;
        # before it existed, the purge completed while the writer's temp was staged.
        resume.set()
        writer.join(timeout=15)
        purger.join(timeout=15)

        assert not writer.is_alive()
        assert not purger.is_alive()
        assert writer.exitcode == 0
        write_result = write_results.get(timeout=2)
        assert write_result == ("ok", "new.zip"), write_result
        assert purge_outcome and purge_outcome[0][0] == "ok", purge_outcome
        assert cache.get_bytes("new.zip", "zips") is None
        assert cache.get_zip_with_provenance("new.zip") is None
        assert cache.status().zips == 0
    finally:
        resume.set()
        if writer.is_alive():
            writer.terminate()
        writer.join(timeout=5)
        if purger is not None:
            purger.join(timeout=5)
        write_results.close()
        write_results.join_thread()
