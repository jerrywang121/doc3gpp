"""Ordered, bounded preparation and serialized persistence for TDocs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from concurrent.futures import (
    CancelledError as FutureCancelledError,
)
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar, cast

from doc3gpp.services.pipeline_runtime import PipelinePermit, PipelineRuntime

I = TypeVar("I")
P = TypeVar("P")
R = TypeVar("R")
T = TypeVar("T")

PipelineStatus = Literal["success", "skipped", "failed"]
PipelineErrorStatus = Literal["skipped", "failed"]


@dataclass(frozen=True, slots=True)
class PipelineItem(Generic[T]):
    """One source item and its stable, source-order identity."""

    key: str
    value: T


@dataclass(frozen=True, slots=True)
class PipelineOutcome(Generic[R]):
    """The terminal result for one pipeline item."""

    key: str
    status: PipelineStatus
    value: R | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class PipelineProgress:
    """Progress notification emitted after an item reaches a terminal state."""

    completed: int
    total: int
    key: str
    outcome: PipelineOutcome[object]


@dataclass(slots=True)
class _Pending:
    index: int
    key: str
    permit: PipelinePermit
    phase: Literal["prepare", "persist"]


def _is_cancellation(exc: BaseException) -> bool:
    return isinstance(exc, (asyncio.CancelledError, FutureCancelledError))


def classify_tdoc_exception(
    exc: Exception,
) -> tuple[PipelineErrorStatus, str]:
    """Classify one TDoc exception without importing the TDoc service eagerly.

    ``TDocNotYetOnFTPError`` lives in ``tdoc_cr_service`` while this module is
    used by that service's callers.  Resolve both TDoc-specific classes only
    when classification is requested so importing the runner cannot create a
    service-layer cycle.
    """
    if _is_cancellation(exc):
        raise exc

    from doc3gpp.services.tdoc_cr_service import (
        TDocNotYetOnFTPError,
        TDocTooLargeError,
    )

    reason = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, (TDocNotYetOnFTPError, TDocTooLargeError)):
        return "skipped", reason
    return "failed", reason


class TDocPipeline:
    """Run preparation concurrently and persistence through one writer lane."""

    def __init__(self, runtime: PipelineRuntime) -> None:
        self._runtime = runtime

    def run_sync(
        self,
        items: Sequence[PipelineItem[I]],
        prepare: Callable[[I], P | R],
        *,
        persist: Callable[[P], R] | None = None,
        is_complete: Callable[[P | R], bool] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        on_progress: Callable[[PipelineProgress], None] | None = None,
    ) -> list[PipelineOutcome[R | P]]:
        """Run a batch from a synchronous caller.

        Capacity acquisition is performed on a tiny coordinator executor so a
        full preparation/persistence queue can be observed and drained while
        the next permit waits.  Preparation and persistence themselves remain
        owned by ``PipelineRuntime``.
        """
        source = list(items)
        total = len(source)
        if not source:
            return []

        outcomes: list[PipelineOutcome[R | P] | None] = [None] * total
        pending: dict[Future[Any], _Pending] = {}
        reservation_executor = ThreadPoolExecutor(max_workers=1)
        reserve_future: Future[PipelinePermit] | None = None
        next_index = 0
        completed = 0
        cancellation: BaseException | None = None
        fatal: BaseException | None = None

        def set_cancelled(exc: BaseException | None = None) -> None:
            nonlocal cancellation
            if cancellation is None:
                cancellation = exc if exc is not None else asyncio.CancelledError()

        def check_cancelled() -> None:
            nonlocal fatal
            if cancellation is not None or fatal is not None or is_cancelled is None:
                return
            try:
                if is_cancelled():
                    set_cancelled()
            except BaseException as exc:  # noqa: BLE001
                if _is_cancellation(exc):
                    set_cancelled(exc)
                elif fatal is None:
                    fatal = exc

        def emit(outcome: PipelineOutcome[R | P]) -> None:
            nonlocal completed, fatal
            completed += 1
            if on_progress is None:
                return
            try:
                on_progress(
                    PipelineProgress(
                        completed=completed,
                        total=total,
                        key=outcome.key,
                        outcome=cast(PipelineOutcome[object], outcome),
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - cancellation must be drained
                if _is_cancellation(exc):
                    set_cancelled(exc)
                elif fatal is None:
                    fatal = exc

        def record(
            entry: _Pending,
            status: PipelineStatus,
            value: R | P | None,
            reason: str | None,
        ) -> None:
            outcome = PipelineOutcome(
                key=entry.key,
                status=status,
                value=value,
                reason=reason,
            )
            outcomes[entry.index] = outcome
            emit(outcome)

        def record_exception(entry: _Pending, exc: BaseException) -> None:
            nonlocal fatal
            entry.permit.release()
            if _is_cancellation(exc):
                set_cancelled(exc)
            elif isinstance(exc, Exception):
                status, reason = classify_tdoc_exception(exc)
                record(entry, status, None, reason)
            elif fatal is None:
                fatal = exc

        def submit_preparation(permit: PipelinePermit) -> None:
            nonlocal next_index
            item = source[next_index]
            entry = _Pending(next_index, item.key, permit, "prepare")
            next_index += 1
            try:
                future = self._runtime.submit_prepare(permit, prepare, item.value)
            except BaseException as exc:  # noqa: BLE001 - cancellation must be drained
                record_exception(entry, exc)
                return
            pending[future] = entry

        def process_preparation(entry: _Pending, prepared: P | R) -> None:
            nonlocal fatal
            check_cancelled()
            if cancellation is not None or fatal is not None:
                entry.permit.release()
                return
            if persist is None:
                entry.permit.release()
                record(entry, "success", prepared, None)
                return
            try:
                complete = is_complete(prepared) if is_complete is not None else False
            except BaseException as exc:  # noqa: BLE001 - cancellation must be drained
                record_exception(entry, exc)
                return
            if complete:
                entry.permit.release()
                record(entry, "success", prepared, None)
                return
            try:
                future = self._runtime.submit_persist(
                    entry.permit,
                    cast(Callable[[Any], Any], persist),
                    prepared,
                )
            except BaseException as exc:  # noqa: BLE001 - cancellation must be drained
                record_exception(entry, exc)
                return
            entry.phase = "persist"
            pending[future] = entry

        def process_future(future: Future[Any]) -> None:
            entry = pending.pop(future)
            if entry.phase == "prepare":
                try:
                    prepared = future.result()
                except BaseException as exc:  # noqa: BLE001 - cancellation must be drained
                    record_exception(entry, exc)
                else:
                    process_preparation(entry, prepared)
                return
            try:
                value = future.result()
            except BaseException as exc:  # noqa: BLE001 - cancellation must be drained
                record_exception(entry, exc)
            else:
                entry.permit.release()
                record(entry, "success", value, None)

        try:
            while reserve_future is not None or pending or next_index < total:
                check_cancelled()
                if (
                    reserve_future is None
                    and next_index < total
                    and cancellation is None
                    and fatal is None
                ):
                    try:
                        reserve_future = reservation_executor.submit(self._runtime.reserve)
                    except BaseException as exc:  # noqa: BLE001 - cancellation must be drained
                        fatal = exc
                        continue

                waitables: set[Future[Any]] = set(pending)
                if reserve_future is not None:
                    waitables.add(reserve_future)
                if not waitables:
                    break
                done, _ = wait(waitables, return_when="FIRST_COMPLETED")

                if reserve_future is not None and reserve_future in done:
                    completed_reservation = reserve_future
                    reserve_future = None
                    try:
                        permit = completed_reservation.result()
                    except BaseException as exc:  # noqa: BLE001 - cancellation must be drained
                        if _is_cancellation(exc):
                            set_cancelled(exc)
                        elif fatal is None:
                            fatal = exc
                    else:
                        check_cancelled()
                        if cancellation is not None or fatal is not None or next_index >= total:
                            permit.release()
                        else:
                            submit_preparation(permit)

                for future in done:
                    if future in pending:
                        process_future(future)
        finally:
            reservation_executor.shutdown(wait=True)

        if cancellation is not None:
            raise cancellation
        if fatal is not None:
            raise fatal
        return cast(list[PipelineOutcome[R | P]], outcomes)

    async def run_async(
        self,
        items: Sequence[PipelineItem[I]],
        prepare: Callable[[I], P | R],
        *,
        persist: Callable[[P], R] | None = None,
        is_complete: Callable[[P | R], bool] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        on_progress: Callable[[PipelineProgress], None] | None = None,
    ) -> list[PipelineOutcome[R | P]]:
        """Run a batch without blocking the asyncio event loop."""
        source = list(items)
        total = len(source)
        if not source:
            return []

        outcomes: list[PipelineOutcome[R | P] | None] = [None] * total
        pending: dict[asyncio.Future[Any], _Pending] = {}
        reserve_task: asyncio.Task[PipelinePermit] | None = None
        next_index = 0
        completed = 0
        cancellation: BaseException | None = None
        fatal: BaseException | None = None

        def set_cancelled(exc: BaseException | None = None) -> None:
            nonlocal cancellation
            if cancellation is None:
                cancellation = exc if exc is not None else asyncio.CancelledError()

        def stop_reservation_wait() -> None:
            if reserve_task is not None and not reserve_task.done():
                reserve_task.cancel()

        def check_cancelled() -> None:
            nonlocal fatal
            if cancellation is not None or fatal is not None or is_cancelled is None:
                return
            try:
                if is_cancelled():
                    set_cancelled()
                    stop_reservation_wait()
            except BaseException as exc:  # noqa: BLE001 - cancellation must be drained
                if _is_cancellation(exc):
                    set_cancelled(exc)
                    stop_reservation_wait()
                elif fatal is None:
                    fatal = exc

        def emit(outcome: PipelineOutcome[R | P]) -> None:
            nonlocal completed, fatal
            completed += 1
            if on_progress is None:
                return
            try:
                on_progress(
                    PipelineProgress(
                        completed=completed,
                        total=total,
                        key=outcome.key,
                        outcome=cast(PipelineOutcome[object], outcome),
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - cancellation must be drained
                if _is_cancellation(exc):
                    set_cancelled(exc)
                elif fatal is None:
                    fatal = exc

        def record(
            entry: _Pending,
            status: PipelineStatus,
            value: R | P | None,
            reason: str | None,
        ) -> None:
            outcome = PipelineOutcome(
                key=entry.key,
                status=status,
                value=value,
                reason=reason,
            )
            outcomes[entry.index] = outcome
            emit(outcome)

        def record_exception(entry: _Pending, exc: BaseException) -> None:
            nonlocal fatal
            entry.permit.release()
            if _is_cancellation(exc):
                set_cancelled(exc)
            elif isinstance(exc, Exception):
                status, reason = classify_tdoc_exception(exc)
                record(entry, status, None, reason)
            elif fatal is None:
                fatal = exc

        def submit_preparation(permit: PipelinePermit) -> None:
            nonlocal next_index
            item = source[next_index]
            entry = _Pending(next_index, item.key, permit, "prepare")
            next_index += 1
            try:
                future = self._runtime.submit_prepare(permit, prepare, item.value)
                wrapped = asyncio.wrap_future(future)
            except BaseException as exc:  # noqa: BLE001 - cancellation must be drained
                record_exception(entry, exc)
                return
            pending[wrapped] = entry

        def process_preparation(entry: _Pending, prepared: P | R) -> None:
            nonlocal fatal
            check_cancelled()
            if cancellation is not None or fatal is not None:
                entry.permit.release()
                return
            if persist is None:
                entry.permit.release()
                record(entry, "success", prepared, None)
                return
            try:
                complete = is_complete(prepared) if is_complete is not None else False
            except BaseException as exc:  # noqa: BLE001 - cancellation must be drained
                record_exception(entry, exc)
                return
            if complete:
                entry.permit.release()
                record(entry, "success", prepared, None)
                return
            try:
                future = self._runtime.submit_persist(
                    entry.permit,
                    cast(Callable[[Any], Any], persist),
                    prepared,
                )
                wrapped = asyncio.wrap_future(future)
            except BaseException as exc:  # noqa: BLE001 - cancellation must be drained
                record_exception(entry, exc)
                return
            entry.phase = "persist"
            pending[wrapped] = entry

        async def wait_for_done(
            waitables: set[asyncio.Future[Any]],
        ) -> set[asyncio.Future[Any]]:
            while True:
                try:
                    done, _ = await asyncio.wait(
                        waitables,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except asyncio.CancelledError as exc:
                    set_cancelled(exc)
                    stop_reservation_wait()
                    continue
                return done

        while reserve_task is not None or pending or next_index < total:
            check_cancelled()
            if cancellation is not None or fatal is not None:
                stop_reservation_wait()
            if (
                reserve_task is None
                and next_index < total
                and cancellation is None
                and fatal is None
            ):
                reserve_task = asyncio.create_task(self._runtime.reserve_async())

            waitables: set[asyncio.Future[Any]] = set(pending)
            if reserve_task is not None:
                waitables.add(reserve_task)
            if not waitables:
                break
            done = await wait_for_done(waitables)

            if reserve_task is not None and reserve_task in done:
                completed_reservation = reserve_task
                reserve_task = None
                try:
                    permit = completed_reservation.result()
                except BaseException as exc:  # noqa: BLE001 - cancellation must be drained
                    if _is_cancellation(exc):
                        set_cancelled(exc)
                    elif fatal is None:
                        fatal = exc
                else:
                    check_cancelled()
                    if cancellation is not None or fatal is not None or next_index >= total:
                        permit.release()
                    else:
                        submit_preparation(permit)

            for future in done:
                if future not in pending:
                    continue
                entry = pending.pop(future)
                if entry.phase == "prepare":
                    try:
                        prepared = future.result()
                    except BaseException as exc:  # noqa: BLE001 - cancellation must be drained
                        record_exception(entry, exc)
                    else:
                        process_preparation(entry, prepared)
                    continue
                try:
                    value = future.result()
                except BaseException as exc:  # noqa: BLE001 - cancellation must be drained
                    record_exception(entry, exc)
                else:
                    entry.permit.release()
                    record(entry, "success", value, None)

        if cancellation is not None:
            raise cancellation
        if fatal is not None:
            raise fatal
        return cast(list[PipelineOutcome[R | P]], outcomes)


__all__ = [
    "PipelineItem",
    "PipelineOutcome",
    "PipelineProgress",
    "TDocPipeline",
    "classify_tdoc_exception",
]
