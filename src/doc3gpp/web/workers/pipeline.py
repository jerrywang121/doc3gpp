"""Shared preparation and serialized persistence executors for queued jobs."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import BoundedSemaphore
from typing import Callable, Generic, TypeVar


T = TypeVar("T")
R = TypeVar("R")


class _BoundedExecutor:
    """Thread-pool adapter that bounds submitted, unfinished futures."""

    def __init__(self, *, max_workers: int, max_pending: int) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._slots = BoundedSemaphore(max_pending)

    def submit(self, fn: Callable[..., T], *args: object, **kwargs: object) -> Future[T]:
        self._slots.acquire()
        try:
            future = self._executor.submit(fn, *args, **kwargs)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _future: self._slots.release())
        return future

    def shutdown(self, *, wait: bool, cancel_futures: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)


class PipelineRuntime:
    """Process-wide runtime for parallel preparation and serialized writes.

    Preparation and persistence have separate bounded pools. The persistence
    pool deliberately has exactly one worker, so all callers share one write
    lane without placing a database mutex around search reads.
    """

    def __init__(self, prepare_workers: int, max_pending: int | None = None) -> None:
        if prepare_workers < 1:
            raise ValueError("prepare_workers must be at least 1")
        pending = max_pending if max_pending is not None else prepare_workers * 2
        if pending < 1:
            raise ValueError("max_pending must be at least 1")
        self._prepare = _BoundedExecutor(
            max_workers=prepare_workers,
            max_pending=pending,
        )
        self._persist = _BoundedExecutor(max_workers=1, max_pending=pending)

    def submit_prepare(
        self,
        fn: Callable[..., T],
        *args: object,
        **kwargs: object,
    ) -> Future[T]:
        """Submit network/conversion/parser work to the preparation pool."""
        return self._prepare.submit(fn, *args, **kwargs)

    def submit_persist(self, fn: Callable[[T], R], prepared: T) -> Future[R]:
        """Submit one database mutation command to the single writer."""
        return self._persist.submit(fn, prepared)

    def shutdown(
        self,
        *,
        wait: bool = True,
        cancel_pending_preparation: bool = False,
    ) -> None:
        """Stop preparation first, then drain the serialized writer."""
        self._prepare.shutdown(
            wait=wait,
            cancel_futures=cancel_pending_preparation,
        )
        self._persist.shutdown(wait=wait)


__all__ = ["PipelineRuntime"]
