"""Disk cache for TDoc extraction artifacts.

Phase 1 primitive: stores zip/markdown bytes under a two-level directory
(``zips/`` + ``markdown/``), with FIFO size-based eviction keyed on
``st_ctime`` and an explicit purge. Knows nothing about 3GPP, the
database, or HTTP — the rest of the pipeline composes on top of this.

The cache is deliberately byte-agnostic: any blob that can be expressed
as ``(key, bytes, subdir)`` slots in. ``zips/`` holds raw 3GPP zip
downloads keyed by the lower-cased TDoc id; ``markdown/`` holds the
markitdown output keyed by the content hash of the source docx so that
edits to the upstream document invalidate the right way.

Eviction order is insertion order. Each put writes through a tempfile
+ ``os.replace`` and then trims until the cache is at or below the
configured limit. Reads use :meth:`pathlib.Path.read_bytes` — they
deliberately do **not** touch ``st_mtime`` (which would silently invert
the eviction order).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


Subdir = Literal["zips", "markdown"]

# Keys are sanitised to this alphabet so a malicious TDoc id can never
# escape the cache root via path traversal (no ``..``, no separators, no
# control characters).
_KEY_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")
_VALID_SUBDIRS: tuple[str, ...] = ("zips", "markdown")


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
        └── markdown/  # markitdown output (keyed by content hash)

    The cache owns the directory layout and key sanitisation; callers
    pass only the data. Concurrent access from multiple processes is
    not supported — each invocation of the CLI owns the cache for its
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
        self._root.mkdir(parents=True, exist_ok=True)
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
        """Write ``payload`` to ``<root>/<subdir>/<key>`` atomically.

        The key is sanitised; zero-byte payloads are rejected (they're
        a sign of a truncated download and would otherwise pollute the
        cache). The write goes through a tempfile + ``os.replace`` so a
        reader never sees a half-written file. After a successful write
        the size limit is enforced (FIFO eviction by ``st_ctime``).

        Returns:
            The :class:`pathlib.Path` of the written file.

        Raises:
            ValueError: bad key, bad ``subdir``, or empty payload.
        """
        self._validate_key(key)
        self._validate_subdir(subdir)
        if len(payload) == 0:
            raise ValueError("Refusing to write zero-byte payload to cache")

        target_dir = self._root / subdir
        target_path = target_dir / key
        # mkstemp in the target subdir keeps the rename on the same
        # filesystem (atomic). The leading dot makes leftover temp
        # files easy to spot and lets status() / enforce_size_limit()
        # skip them if a writer died mid-write.
        fd, tmp_name = tempfile.mkstemp(
            prefix=".tmp.",
            suffix=".partial",
            dir=target_dir,
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, target_path)
        except BaseException:
            # Best-effort cleanup if os.replace never ran.
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
        self.enforce_size_limit()
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
        if self._size_limit_bytes == 0:
            return 0

        candidates: list[tuple[float, int, Path]] = []
        for subdir in _VALID_SUBDIRS:
            for entry in (self._root / subdir).iterdir():
                if not entry.is_file() or entry.name.startswith("."):
                    continue
                try:
                    st = entry.stat()
                except FileNotFoundError:
                    continue
                candidates.append((st.st_ctime, st.st_size, entry))

        total = sum(size for _, size, _ in candidates)
        if total <= self._size_limit_bytes:
            return 0

        # Oldest first; tie-break on path string for deterministic
        # behaviour when two files share a ctime (common on coarse
        # filesystem clocks).
        candidates.sort(key=lambda c: (c[0], str(c[2])))
        deleted = 0
        for ctime, size, path in candidates:
            if total <= self._size_limit_bytes:
                break
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            total -= size
            deleted += 1
            logger.debug(
                "Evicted %s (size=%d, ctime=%s) — over size limit",
                path,
                size,
                ctime,
            )
        return deleted

    def purge(self) -> int:
        """Recursively delete ``zips/`` and ``markdown/`` and recreate them empty.

        The cache root itself is preserved. Files are counted before
        deletion so the returned count reflects everything that lived
        under the two subdirs (including any sub-subdirs — currently
        none, but the helper is forward-compatible).

        Returns:
            Number of files deleted.
        """
        deleted = 0
        for subdir in _VALID_SUBDIRS:
            path = self._root / subdir
            if path.exists():
                for entry in path.rglob("*"):
                    if entry.is_file():
                        deleted += 1
                shutil.rmtree(path)
            path.mkdir(parents=True, exist_ok=True)
        logger.info("Purged %d cache files under %s", deleted, self._root)
        return deleted

    def purge_subdir(self, subdir: Subdir) -> int:
        """Delete every file under ``<root>/<subdir>/`` and recreate it empty.

        Sibling subtree is left untouched, so this is the method to
        call when re-extracting every TDoc (force the zip download
        again while preserving the rendered-markdown sidecar) or when
        wiping the markdown cache alone. Files with a leading ``.``
        (leftover ``.tmp.*.partial`` blobs from an interrupted write)
        are skipped.

        Args:
            subdir: Which subtree to wipe. Must be one of
                ``"zips"`` / ``"markdown"``.

        Returns:
            Number of files deleted. ``0`` when the subdir was already
            empty (or never existed).

        Raises:
            ValueError: ``subdir`` is not a recognised cache subtree.
        """
        self._validate_subdir(subdir)
        path = self._root / subdir
        deleted = 0
        if path.exists():
            for entry in path.rglob("*"):
                if not entry.is_file() or entry.name.startswith("."):
                    continue
                try:
                    entry.unlink()
                    deleted += 1
                except FileNotFoundError:
                    continue
        logger.info(
            "Purged %d files from %s/%s", deleted, self._root, subdir
        )
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
        """Return ``(file_count, total_bytes)`` for one cache subtree."""
        assert subdir in _VALID_SUBDIRS  # internal precondition
        count = 0
        total = 0
        for entry in (self._root / subdir).iterdir():
            if not entry.is_file() or entry.name.startswith("."):
                continue
            try:
                st = entry.stat()
            except FileNotFoundError:
                continue
            count += 1
            total += st.st_size
        return count, total

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or not _KEY_PATTERN.fullmatch(key):
            raise ValueError(
                f"Invalid cache key {key!r}: must match {_KEY_PATTERN.pattern} "
                f"(1-128 chars from [A-Za-z0-9._-])"
            )

    @staticmethod
    def _validate_subdir(subdir: str) -> None:
        if subdir not in _VALID_SUBDIRS:
            raise ValueError(
                f"Invalid subdir {subdir!r}: must be one of {_VALID_SUBDIRS}"
            )