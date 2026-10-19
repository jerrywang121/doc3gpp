from __future__ import annotations

import asyncio
import threading

from doc3gpp.services.pipeline_runtime import PipelineRuntime
from doc3gpp.web.state import JobWorkerHandle
from doc3gpp.web.workers.pipeline import PipelineRuntime as WebPipelineRuntime


def test_web_pipeline_module_reexports_service_runtime():
    assert WebPipelineRuntime is PipelineRuntime


def test_preparation_tasks_overlap_with_configured_worker_count():
    runtime = PipelineRuntime(prepare_workers=2, max_pending=2)
    started = threading.Barrier(2)
    permits = [runtime.reserve(), runtime.reserve()]

    def prepare(value: str) -> str:
        assert started.wait(timeout=2) is not None
        return value

    try:
        futures = [
            runtime.submit_prepare(permit, prepare, value)
            for permit, value in zip(permits, ("first", "second"))
        ]
        assert sorted(future.result(timeout=2) for future in futures) == [
            "first",
            "second",
        ]
    finally:
        for permit in permits:
            permit.release()
        runtime.shutdown()


def test_persistence_tasks_never_run_concurrently():
    runtime = PipelineRuntime(prepare_workers=2, max_pending=2)
    permits = [runtime.reserve(), runtime.reserve()]
    first_started = threading.Event()
    allow_first_to_finish = threading.Event()
    second_started = threading.Event()
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    def persist(value: str) -> str:
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        if value == "first":
            first_started.set()
            assert allow_first_to_finish.wait(timeout=2)
        else:
            second_started.set()
        with lock:
            active -= 1
        return value

    try:
        first = runtime.submit_persist(permits[0], persist, "first")
        assert first_started.wait(timeout=2)
        second = runtime.submit_persist(permits[1], persist, "second")
        assert not second_started.wait(timeout=0.05)
        allow_first_to_finish.set()
        assert first.result(timeout=2) == "first"
        assert second.result(timeout=2) == "second"
        assert maximum_active == 1
    finally:
        allow_first_to_finish.set()
        for permit in permits:
            permit.release()
        runtime.shutdown()


def test_writer_survives_a_failed_persistence_command():
    runtime = PipelineRuntime(prepare_workers=1, max_pending=2)
    permits = [runtime.reserve(), runtime.reserve()]

    def persist(value: int) -> int:
        if value == 1:
            raise RuntimeError("write failed")
        return value

    try:
        failed = runtime.submit_persist(permits[0], persist, 1)
        succeeding = runtime.submit_persist(permits[1], persist, 2)
        try:
            failed.result(timeout=2)
        except RuntimeError as exc:
            assert str(exc) == "write failed"
        else:
            raise AssertionError("expected the first write to fail")
        assert succeeding.result(timeout=2) == 2
    finally:
        for permit in permits:
            permit.release()
        runtime.shutdown()


def test_preparation_and_persistence_share_pending_capacity():
    runtime = PipelineRuntime(prepare_workers=1, max_pending=1)
    permit = runtime.reserve()
    acquired = threading.Event()

    def wait_for_capacity():
        waiting_permit = runtime.reserve()
        acquired.set()
        waiting_permit.release()

    waiter: threading.Thread | None = None
    try:
        prepared = runtime.submit_prepare(permit, lambda: "prepared").result(timeout=2)
        persisted = runtime.submit_persist(permit, lambda value: value, prepared)
        waiter = threading.Thread(target=wait_for_capacity)
        waiter.start()
        assert not acquired.wait(timeout=0.05)
        assert persisted.result(timeout=2) == "prepared"
        permit.release()
        assert acquired.wait(timeout=2)
        waiter.join(timeout=2)
        assert not waiter.is_alive()
    finally:
        permit.release()
        if waiter is not None:
            waiter.join(timeout=2)
        runtime.shutdown()


def test_pending_capacity_is_bounded_by_reserved_permits():
    runtime = PipelineRuntime(prepare_workers=1, max_pending=1)
    held = runtime.reserve()
    acquired = threading.Event()

    def wait_for_capacity():
        waiting_permit = runtime.reserve()
        acquired.set()
        waiting_permit.release()

    waiter = threading.Thread(target=wait_for_capacity)
    try:
        waiter.start()
        assert not acquired.wait(timeout=0.05)
        held.release()
        assert acquired.wait(timeout=2)
        waiter.join(timeout=2)
        assert not waiter.is_alive()
    finally:
        held.release()
        waiter.join(timeout=2)
        runtime.shutdown()


def test_permit_release_is_idempotent():
    runtime = PipelineRuntime(prepare_workers=1, max_pending=1)
    permit = runtime.reserve()
    permit.release()
    permit.release()
    held = runtime.reserve()
    acquired = threading.Event()

    def wait_for_capacity():
        waiting_permit = runtime.reserve()
        acquired.set()
        waiting_permit.release()

    waiter = threading.Thread(target=wait_for_capacity)
    try:
        waiter.start()
        assert not acquired.wait(timeout=0.05)
        held.release()
        assert acquired.wait(timeout=2)
        waiter.join(timeout=2)
        assert not waiter.is_alive()
    finally:
        held.release()
        waiter.join(timeout=2)
        runtime.shutdown()


async def _run_async_capacity_wait_test():
    runtime = PipelineRuntime(prepare_workers=1, max_pending=1)
    held = runtime.reserve()
    unrelated_ran = asyncio.Event()

    async def unrelated():
        unrelated_ran.set()

    try:
        waiter = asyncio.create_task(runtime.reserve_async())
        unrelated_task = asyncio.create_task(unrelated())
        await asyncio.wait_for(unrelated_task, timeout=2)
        assert unrelated_ran.is_set()
        assert not waiter.done()
        held.release()
        waiting_permit = await asyncio.wait_for(waiter, timeout=2)
        waiting_permit.release()
    finally:
        held.release()
        await runtime.shutdown_async()


def test_default_pending_capacity_is_twice_prepare_workers():
    runtime = PipelineRuntime(prepare_workers=2)
    assert runtime._capacity._limit == 4
    permits = [runtime.reserve() for _ in range(4)]
    acquired = threading.Event()

    def wait_for_capacity():
        waiting_permit = runtime.reserve()
        acquired.set()
        waiting_permit.release()

    waiter = threading.Thread(target=wait_for_capacity)
    try:
        waiter.start()
        assert not acquired.wait(timeout=0.05)
        permits[0].release()
        assert acquired.wait(timeout=2)
        waiter.join(timeout=2)
        assert not waiter.is_alive()
    finally:
        for permit in permits:
            permit.release()
        waiter.join(timeout=2)
        runtime.shutdown()


async def _run_cancelled_async_capacity_wait_test():
    runtime = PipelineRuntime(prepare_workers=1, max_pending=1)
    held = runtime.reserve()
    waiter = asyncio.create_task(runtime.reserve_async())

    try:
        await asyncio.sleep(0.05)
        assert not waiter.done()
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("expected blocked reservation to be cancelled")

        held.release()
        replacement = await asyncio.wait_for(runtime.reserve_async(), timeout=2)
        replacement.release()
    finally:
        held.release()
        await runtime.shutdown_async()


def test_cancelled_async_capacity_wait_does_not_consume_capacity():
    asyncio.run(_run_cancelled_async_capacity_wait_test())


def test_async_capacity_wait_does_not_block_event_loop():
    asyncio.run(_run_async_capacity_wait_test())


def test_shutdown_drains_submitted_persistence_commands():
    runtime = PipelineRuntime(prepare_workers=1, max_pending=1)
    permit = runtime.reserve()
    release = threading.Event()
    persisted: list[int] = []
    shutdown_finished = threading.Event()

    def persist(value: int) -> int:
        assert release.wait(timeout=2)
        persisted.append(value)
        return value

    def shutdown():
        runtime.shutdown()
        shutdown_finished.set()

    shutdown_thread: threading.Thread | None = None
    try:
        future = runtime.submit_persist(permit, persist, 7)
        shutdown_thread = threading.Thread(target=shutdown)
        shutdown_thread.start()
        assert not shutdown_finished.wait(timeout=0.05)
        release.set()
        shutdown_thread.join(timeout=2)
        assert not shutdown_thread.is_alive()
        assert persisted == [7]
        assert future.result(timeout=2) == 7
    finally:
        release.set()
        permit.release()
        if shutdown_thread is not None:
            shutdown_thread.join(timeout=2)
        if not shutdown_finished.is_set():
            runtime.shutdown()


def test_job_handle_shutdown_drains_pipeline_without_worker_task():
    runtime = PipelineRuntime(prepare_workers=1, max_pending=1)
    permit = runtime.reserve()
    persisted: list[int] = []
    future = runtime.submit_persist(
        permit,
        lambda value: persisted.append(value) or value,
        9,
    )
    handle = JobWorkerHandle(pipeline=runtime)

    asyncio.run(handle.shutdown())

    permit.release()
    assert future.result(timeout=2) == 9
    assert persisted == [9]
