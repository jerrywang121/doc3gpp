"""Shared bounded preparation and serialized persistence runtime."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import BoundedSemaphore, Lock
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


class PipelinePermit:
    """A single unit of capacity held by one pipeline item.

    A permit is acquired before preparation is submitted and remains held
    until the caller has either handed the prepared value to persistence or
    handled an already-complete preparation result.  Release is deliberately
    idempotent so callers can safely use both callbacks and ``finally``
    blocks for cleanup.
    """

    def __init__(self, capacity: BoundedSemaphore) -> None:
        self._capacity = capacity
        self._lock = Lock()
        self._released = False

    def release(self) -> None:
        """Return this permit's capacity at most once."""
        with self._lock:
            if self._released:
                return
            self._released = True
        self._capacity.release()


class PipelineRuntime:
    """Run preparation concurrently and persistence on one shared lane.

    The capacity semaphore is acquired explicitly by callers through
    :meth:`reserve`.  Both executors consume the same permit, so handing a
    prepared value from the preparation pool to the persistence pool does not
    create another pending-work slot or allow the prepared-result queue to
    grow without bound.
    """

    def __init__(self, prepare_workers: int, max_pending: int | None = None) -> None:
        if prepare_workers < 1:
            raise ValueError("prepare_workers must be at least 1")
        pending = max_pending if max_pending is not None else prepare_workers * 2
        if pending < 1:
            raise ValueError("max_pending must be at least 1")

        self._capacity = BoundedSemaphore(pending)
        self._prepare = ThreadPoolExecutor(max_workers=prepare_workers)
        self._persist = ThreadPoolExecutor(max_workers=1)

    def reserve(self) -> PipelinePermit:
        """Block the calling thread until one pipeline slot is available."""
        self._capacity.acquire()
        return PipelinePermit(self._capacity)

    async def reserve_async(self) -> PipelinePermit:
        """Wait for capacity without blocking the asyncio event loop."""
        return await asyncio.to_thread(self.reserve)

    def submit_prepare(
        self,
        permit: PipelinePermit,
        fn: Callable[..., T],
        *args: object,
        **kwargs: object,
    ) -> Future[T]:
        """Submit network/conversion/parser work to the preparation pool."""
        del permit  # Capacity is acquired by reserve(), never by submission.
        return self._prepare.submit(fn, *args, **kwargs)

    def submit_persist(
        self,
        permit: PipelinePermit,
        fn: Callable[[T], R],
        prepared: T,
    ) -> Future[R]:
        """Submit one database mutation to the single persistence worker."""
        del permit  # The preparation permit spans both executor phases.
        return self._persist.submit(fn, prepared)

    def shutdown(
        self,
        *,
        wait: bool = True,
        cancel_pending_preparation: bool = False,
    ) -> None:
        """Stop preparation first, then drain the serialized persistence lane."""
        self._prepare.shutdown(
            wait=wait,
            cancel_futures=cancel_pending_preparation,
        )
        self._persist.shutdown(wait=wait)

    async def shutdown_async(self, *, cancel_pending_preparation: bool = False) -> None:
        """Drain both executors without blocking the asyncio event loop."""
        await asyncio.to_thread(
            self.shutdown,
            wait=True,
            cancel_pending_preparation=cancel_pending_preparation,
        )


__all__ = ["PipelinePermit", "PipelineRuntime"]
