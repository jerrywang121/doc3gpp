"""Disk cache for TDoc extraction artifacts.

Phase 1 primitive: stores zip/markdown bytes under a two-level directory
(``zips/`` + ``markdown/``), with FIFO size-based eviction keyed on
``st_ctime`` and an explicit purge. Knows nothing about 3GPP, the
database, or HTTP — the rest of the pipeline composes on top of this.

The cache is deliberately byte-agnostic: any blob that can be expressed
as ``(key, bytes, subdir)`` slots in. ``zips/`` holds raw 3GPP zip
downloads keyed by the lower-cased TDoc id; ``markdown/`` holds the
python-docx output keyed by the content hash of the source docx so that
edits to the upstream document invalidate the right way.

Eviction order is insertion order. Each put writes through a tempfile
+ ``os.replace`` and then trims until the cache is at or below the
configured limit. Reads use :meth:`pathlib.Path.read_bytes` — they
deliberately do **not** touch ``st_mtime`` (which would silently invert
the eviction order).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


Subdir = Literal["zips", "markdown"]

# Keys are sanitised to this alphabet so a malicious TDoc id can never
# escape the cache root via path traversal (no ``..``, no separators, no
# control characters).
_KEY_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,200}")
_VALID_SUBDIRS: tuple[str, ...] = ("zips", "markdown")
_PROVENANCE_DIR = ".provenance"
_PROVENANCE_VERSION = 1

_PROCESS_FILE_LOCKS: dict[str, object] = {}
_PROCESS_FILE_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCK_DEPTHS = threading.local()


@dataclass(slots=True, frozen=True)
class CacheStatus:
    """Snapshot of cache size and composition.

    Returned by :meth:`TDocCache.status`; pure read — never writes or
    deletes. ``file_count`` is the combined total of ``zips`` and
    ``markdown``; ``total_bytes`` is their combined byte size;
    ``limit_bytes`` echoes the configured ceiling (``0`` = unlimited).
    """

    file_count: int
    total_bytes: int
    limit_bytes: int
    zips: int
    markdown: int


class TDocCache:
    """Two-subtree on-disk cache with FIFO size-based eviction.

    Layout::

        <root>/
        ├── zips/      # raw 3GPP zip downloads
        └── markdown/  # python-docx output (keyed by content hash)

    The cache owns the directory layout and key sanitisation; callers
    pass only the data. Installs and eviction are serialized for threads
    sharing one cache instance; concurrent access from multiple processes
    is not supported — each invocation of the CLI owns the cache for its
    lifetime.
    """

    def __init__(self, root: Path, size_limit_bytes: int) -> None:
        """Create (or open) the cache rooted at ``root``.

        Creates ``root``, ``root/zips`` and ``root/markdown`` if missing.
        ``size_limit_bytes`` must be non-negative; ``0`` means unlimited.

        Raises:
            ValueError: ``size_limit_bytes`` is negative. We use
                ``ValueError`` rather than ``AssertionError`` so a CLI
                flag that parses to a negative value surfaces as the
                same error type that :class:`CacheSettings` raises via
                pydantic validation (``ge=0``), keeping the failure
                surface uniform for end users.
        """
        if size_limit_bytes < 0:
            raise ValueError(
                f"size_limit_bytes must be >= 0, got {size_limit_bytes}"
            )
        self._root = Path(root)
        self._size_limit_bytes = size_limit_bytes
        self._lock = threading.RLock()
        self._root.mkdir(parents=True, exist_ok=True)
        with self._lock_cache_tree():
            for subdir in _VALID_SUBDIRS:
                (self._root / subdir).mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # Properties (read-only handles for service-layer callers).
    # ------------------------------------------------------------------

    @property
    def root(self) -> Path:
        """The cache root directory."""
        return self._root

    @property
    def size_limit_bytes(self) -> int:
        """Configured size limit in bytes (``0`` = unlimited)."""
        return self._size_limit_bytes

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def put_bytes(
        self,
        key: str,
        payload: bytes,
        subdir: Subdir,
    ) -> Path:
        """Atomically cache bytes; plain ZIP writes invalidate known provenance."""
        self._validate_key(key)
        self._validate_subdir(subdir)
        if len(payload) == 0:
            raise ValueError("Refusing to write zero-byte payload to cache")

        target_dir = self._root / subdir
        target_path = target_dir / key
        tmp_name: str | None = None
        try:
            with self._lock_cache_tree(), self._lock:
                tmp_name = self._write_temp_file(target_dir, payload)
                if subdir == "zips":
                    with self._lock_key(key):
                        sidecar = self._provenance_path(key)
                        try:
                            sidecar.unlink()
                        except FileNotFoundError:
                            pass
                        else:
                            self._fsync_directory(sidecar.parent)
                        os.replace(tmp_name, target_path)
                else:
                    os.replace(tmp_name, target_path)
                self.enforce_size_limit()
        except BaseException:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
            raise

        logger.debug(
            "Cached %d bytes at %s (key=%s, subdir=%s)",
            len(payload),
            target_path,
            key,
            subdir,
        )
        return target_path

    def get_bytes(self, key: str, subdir: Subdir) -> bytes | None:
        """Return the cached bytes for ``key``/``subdir`` or ``None``.

        Uses :meth:`pathlib.Path.read_bytes` so ``st_mtime`` and
        ``st_ctime`` are not touched — the brief requires that
        insertion order drives eviction, and touching ``mtime`` would
        silently invert it.

        Raises:
            ValueError: bad key or bad ``subdir``.
        """
        self._validate_key(key)
        self._validate_subdir(subdir)
        path = self._root / subdir / key
        if not path.is_file():
            return None
        return path.read_bytes()


    def get_zip_with_provenance(self, key: str) -> tuple[bytes, str] | None:
        """Return a digest-verified ZIP byte snapshot and its recorded source URL."""
        self._validate_key(key)
        zip_path = self._root / "zips" / key
        sidecar_path = self._provenance_path(key)
        with self._lock_key(key):
            if not zip_path.is_file() or not sidecar_path.is_file():
                return None
            try:
                payload = zip_path.read_bytes()
                record = json.loads(sidecar_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return None

            if (
                not isinstance(record, dict)
                or type(record.get("version")) is not int
                or record["version"] != _PROVENANCE_VERSION
            ):
                return None
            source_url = record.get("url")
            digest = record.get("sha256")
            if (
                not isinstance(source_url, str)
                or not source_url.strip()
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                return None
            if hashlib.sha256(payload).hexdigest() != digest:
                return None
            return payload, source_url

    def put_zip_with_provenance(
        self,
        key: str,
        data: bytes,
        source_url: str,
    ) -> Path:
        """Atomically replace a ZIP and a versioned digest/source sidecar."""
        self._validate_key(key)
        if not data:
            raise ValueError("Refusing to write zero-byte payload to cache")
        if not isinstance(source_url, str) or not source_url.strip():
            raise ValueError("source_url must be a non-empty URL")

        zip_dir = self._root / "zips"
        sidecar_path = self._provenance_path(key)
        record = {
            "version": _PROVENANCE_VERSION,
            "url": source_url,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        metadata = json.dumps(
            record, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        zip_path = zip_dir / key
        zip_tmp: str | None = None
        sidecar_tmp: str | None = None
        try:
            with self._lock_cache_tree(), self._lock:
                sidecar_path.parent.mkdir(parents=True, exist_ok=True)
                zip_tmp = self._write_temp_file(zip_dir, data)
                sidecar_tmp = self._write_temp_file(sidecar_path.parent, metadata)
                with self._lock_key(key):
                    # Remove old authority before replacing bytes. A process
                    # interruption from here until sidecar installation is a miss.
                    try:
                        sidecar_path.unlink()
                    except FileNotFoundError:
                        pass
                    self._fsync_directory(sidecar_path.parent)
                    os.replace(zip_tmp, zip_path)
                    os.replace(sidecar_tmp, sidecar_path)
                    self._fsync_directory(zip_dir)
                    self._fsync_directory(sidecar_path.parent)
                self.enforce_size_limit()
        except BaseException:
            for temporary in (zip_tmp, sidecar_tmp):
                if temporary is None:
                    continue
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
            raise

        logger.debug(
            "Cached %d bytes at %s with source provenance (key=%s, url=%s)",
            len(data),
            zip_path,
            key,
            source_url,
        )
        return zip_path

    def path_for(self, key: str, subdir: Subdir) -> Path:
        """Return the expected cache path for ``key``/``subdir``.

        Does not check existence — useful for "about to download this?"
        checks. The path is well-defined and safe (the key is
        sanitised).

        Raises:
            ValueError: bad key or bad ``subdir``.
        """
        self._validate_key(key)
        self._validate_subdir(subdir)
        return self._root / subdir / key

    def enforce_size_limit(self) -> int:
        """Trim the cache until total size <= ``size_limit_bytes``.

        Scans both ``zips/`` and ``markdown/``, sorts every file by
        ``st_ctime`` ascending (oldest first), and deletes files until
        the cache is at or below the limit. ``size_limit_bytes == 0``
        is a no-op (unlimited). Idempotent.

        Files with a leading ``.`` (e.g. leftover ``.tmp.*.partial``
        blobs from an interrupted write) are skipped.

        Returns:
            Number of files deleted.
        """
        with self._lock_cache_tree(), self._lock:
            return self._enforce_size_limit_unlocked()

    def _enforce_size_limit_unlocked(self) -> int:
        """Trim the cache; the caller holds the instance lock."""
        if self._size_limit_bytes == 0:
            return 0

        deleted = 0
        zip_dir = self._root / "zips"
        while True:
            candidates: list[tuple[float, Path]] = []
            total = 0
            for subdir in _VALID_SUBDIRS:
                for entry in self._visible_files(subdir):
                    try:
                        stat = entry.stat()
                    except FileNotFoundError:
                        continue
                    candidates.append((stat.st_ctime, entry))
                    total += stat.st_size
            if total <= self._size_limit_bytes:
                return deleted

            candidates.sort(key=lambda candidate: (candidate[0], str(candidate[1])))
            removed = False
            for ctime, path in candidates:
                lock = (
                    self._lock_key(self._zip_key_for_path(path))
                    if path.is_relative_to(zip_dir)
                    else nullcontext()
                )
                with lock:
                    try:
                        current_size = path.stat().st_size
                    except FileNotFoundError:
                        continue
                    current_total = 0
                    for subdir in _VALID_SUBDIRS:
                        for entry in self._visible_files(subdir):
                            try:
                                current_total += entry.stat().st_size
                            except FileNotFoundError:
                                continue
                    if current_total <= self._size_limit_bytes:
                        return deleted
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        continue
                    deleted += 1
                    logger.debug(
                        "Evicted %s (size=%d, ctime=%s) — over size limit",
                        path,
                        current_size,
                        ctime,
                    )
                    removed = True
                    break
            if not removed:
                return deleted

    def purge(self) -> int:
        """Recursively delete both cache subdirs and recreate them empty."""
        deleted = 0
        with self._lock_cache_tree(), self._lock:
            for subdir in _VALID_SUBDIRS:
                path = self._root / subdir
                if path.exists():
                    files = [entry for entry in path.rglob("*") if entry.is_file()]
                    if subdir == "zips":
                        keys = sorted(
                            {
                                self._zip_key_for_path(entry)
                                for entry in files
                                if not (
                                    entry.name.startswith(".tmp.")
                                    and entry.name.endswith(".partial")
                                )
                            }
                        )
                        with ExitStack() as locks:
                            for key in keys:
                                locks.enter_context(self._lock_key(key))
                            deleted += len(files)
                            shutil.rmtree(path)
                    else:
                        deleted += len(files)
                        shutil.rmtree(path)
                path.mkdir(parents=True, exist_ok=True)
        logger.info("Purged %d cache files under %s", deleted, self._root)
        return deleted

    def purge_subdir(self, subdir: Subdir) -> int:
        """Delete visible files below one subtree, keeping the sibling subtree."""
        self._validate_subdir(subdir)
        path = self._root / subdir

        with self._lock_cache_tree(), self._lock:
            if not path.exists():
                return 0

            entries = [
                entry
                for entry in path.rglob("*")
                if entry.is_file() and not entry.name.startswith(".")
            ]
            lock_keys = (
                sorted({self._zip_key_for_path(entry) for entry in entries})
                if subdir == "zips"
                else []
            )
            deleted = 0
            with ExitStack() as locks:
                for key in lock_keys:
                    locks.enter_context(self._lock_key(key))
                for entry in entries:
                    try:
                        entry.unlink()
                        deleted += 1
                    except FileNotFoundError:
                        continue
            if subdir == "zips":
                try:
                    (path / _PROVENANCE_DIR).rmdir()
                except OSError:
                    pass
        logger.info("Purged %d files from %s/%s", deleted, self._root, subdir)
        return deleted

    def status(self) -> CacheStatus:
        """Return a snapshot of cache size and composition.

        Pure read — does **not** call :meth:`enforce_size_limit`.
        Files with a leading ``.`` are skipped (leftover temp blobs).

        Returns:
            A :class:`CacheStatus` with file counts and total bytes.
        """
        zips_count, zips_bytes = self._scan_subdir("zips")
        md_count, md_bytes = self._scan_subdir("markdown")
        return CacheStatus(
            file_count=zips_count + md_count,
            total_bytes=zips_bytes + md_bytes,
            limit_bytes=self._size_limit_bytes,
            zips=zips_count,
            markdown=md_count,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _scan_subdir(self, subdir: str) -> tuple[int, int]:
        """Return file count and total bytes for one cache subtree."""
        assert subdir in _VALID_SUBDIRS  # internal precondition
        count = 0
        total = 0
        for entry in self._visible_files(subdir):
            try:
                stat = entry.stat()
            except FileNotFoundError:
                continue
            count += 1
            total += stat.st_size
        return count, total

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or not _KEY_PATTERN.fullmatch(key):
            raise ValueError(
                f"Invalid cache key {key!r}: must match {_KEY_PATTERN.pattern} "
                f"(1-200 chars from [A-Za-z0-9._-])"
            )

    @staticmethod
    def _validate_subdir(subdir: str) -> None:
        if subdir not in _VALID_SUBDIRS:
            raise ValueError(
                f"Invalid subdir {subdir!r}: must be one of {_VALID_SUBDIRS}"
            )


    def _provenance_path(self, key: str) -> Path:
        return self._root / "zips" / _PROVENANCE_DIR / f"p_{key}.json"

    @contextmanager
    def _lock_cache_tree(self) -> Iterator[None]:
        """Serialize cache writes, purge, and eviction across processes."""
        with self._lock_file(self._root / ".locks" / "cache-tree.lock"):
            yield

    @contextmanager
    def _lock_file(self, lock_path: Path) -> Iterator[None]:
        """Lock one path across threads/processes, allowing same-thread nesting."""
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        local_key = str(lock_path.resolve())
        with _PROCESS_FILE_LOCKS_GUARD:
            local_lock = _PROCESS_FILE_LOCKS.setdefault(local_key, threading.RLock())

        with local_lock:
            lock_depths = getattr(_PROCESS_LOCK_DEPTHS, "paths", None)
            if lock_depths is None:
                lock_depths = {}
                _PROCESS_LOCK_DEPTHS.paths = lock_depths

            if lock_depths.get(local_key, 0):
                lock_depths[local_key] += 1
                try:
                    yield
                finally:
                    lock_depths[local_key] -= 1
                return

            with lock_path.open("a+b") as lock_file:
                if os.name == "nt":
                    import msvcrt

                    lock_file.seek(0, os.SEEK_END)
                    if lock_file.tell() == 0:
                        lock_file.write(b"\0")
                        lock_file.flush()
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                    try:
                        lock_depths[local_key] = 1
                        yield
                    finally:
                        lock_depths.pop(local_key, None)
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                    try:
                        lock_depths[local_key] = 1
                        yield
                    finally:
                        lock_depths.pop(local_key, None)
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _lock_key(self, key: str) -> Iterator[None]:
        """Lock one ZIP key across threads and processes; failures are fail-closed."""
        lock_name = f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.lock"
        with self._lock_file(self._root / ".locks" / lock_name):
            yield

    @staticmethod
    def _write_temp_file(directory: Path, payload: bytes) -> str:
        fd, tmp_name = tempfile.mkstemp(
            prefix=".tmp.", suffix=".partial", dir=directory
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
        return tmp_name

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _zip_key_for_path(self, path: Path) -> str:
        relative = path.relative_to(self._root / "zips")
        if relative.parts[0] == _PROVENANCE_DIR:
            return Path(relative.name).stem.removeprefix("p_")
        return relative.name

    def _visible_files(self, subdir: str) -> Iterator[Path]:
        for entry in (self._root / subdir).rglob("*"):
            if entry.is_file() and not entry.name.startswith("."):
                yield entry
