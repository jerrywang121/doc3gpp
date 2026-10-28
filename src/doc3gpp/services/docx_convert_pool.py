"""Bounded process pool for DOCX member conversion.

The pool runs the :data:`doc3gpp.parsers.docx_converter.DOCX_WORKERS` entry
points either in-process (``workers <= 1``) or in spawned worker processes.
Workers use the ``spawn`` context because conversion is started from threads,
and forking a threaded process is unsafe.
"""

from __future__ import annotations

import multiprocessing
import threading
from collections import deque
from collections.abc import Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from itertools import islice

from typing_extensions import Self

from doc3gpp.parsers.docx_converter import (
    DOCX_WORKERS,
    DocxConvertPoolError,
    InlineDocxPool,
    MemberKind,
)


def _run_member(kind: MemberKind, name: str, payload: bytes) -> object:
    return DOCX_WORKERS[kind](payload, name)


class DocxConvertPool:
    """Converts DOCX members with at most ``workers`` spawned processes.

    Implements :class:`doc3gpp.parsers.docx_converter.DocxMemberPool`. A member
    exception is that member's outcome. A dead worker is reported as
    :class:`DocxConvertPoolError`, and the next call starts a fresh executor.
    """

    def __init__(self, workers: int) -> None:
        if workers < 0:
            raise ValueError(f"workers must be >= 0, got {workers}")
        self._workers = workers
        self._inline = InlineDocxPool()
        self._lock = threading.Lock()
        self._executor: ProcessPoolExecutor | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def convert_members(
        self, kind: MemberKind, members: Sequence[tuple[str, bytes]]
    ) -> list[object]:
        if self._workers <= 1:
            return self._inline.convert_members(kind, members)
        window = 2 * self._workers
        queued = iter(members)
        outstanding: deque[tuple[str, ProcessPoolExecutor, Future]] = deque()
        outcomes: list[object] = []
        while True:
            for name, payload in islice(queued, window - len(outstanding)):
                outstanding.append((name, *self._submit(kind, name, payload)))
            if not outstanding:
                return outcomes
            name, executor, future = outstanding.popleft()
            outcomes.append(self._outcome(name, executor, future))

    def close(self) -> None:
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True)

    def _get_executor(self) -> ProcessPoolExecutor:
        # Called with ``self._lock`` held.
        if self._executor is None:
            self._executor = ProcessPoolExecutor(
                max_workers=self._workers,
                mp_context=multiprocessing.get_context("spawn"),
            )
        return self._executor

    def _submit(
        self, kind: MemberKind, name: str, payload: bytes
    ) -> tuple[ProcessPoolExecutor, Future]:
        with self._lock:
            executor = self._get_executor()
            try:
                return executor, executor.submit(_run_member, kind, name, payload)
            except BrokenProcessPool as exc:
                submit_error = exc
                retired = self._detach_locked(executor)
        if retired:
            executor.shutdown(wait=True)
        failed: Future = Future()
        failed.set_exception(submit_error)
        return executor, failed

    def _outcome(self, name: str, executor: ProcessPoolExecutor, future: Future) -> object:
        try:
            return future.result()
        except BrokenProcessPool as exc:
            self._retire(executor)
            message = f"DOCX conversion pool broke while converting {name}: {exc}"
            return DocxConvertPoolError(message)
        except Exception as exc:  # noqa: BLE001 - per-member failures are outcomes
            return exc

    def _retire(self, executor: ProcessPoolExecutor) -> None:
        with self._lock:
            retired = self._detach_locked(executor)
        if retired:
            executor.shutdown(wait=True)

    def _detach_locked(self, executor: ProcessPoolExecutor) -> bool:
        # Called with ``self._lock`` held. Returns True only for the caller that
        # detached ``executor``, so exactly one caller shuts it down.
        if self._executor is not executor:
            return False
        self._executor = None
        return True
