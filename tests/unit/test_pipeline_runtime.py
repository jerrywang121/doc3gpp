from __future__ import annotations

import asyncio
import threading
import time

from doc3gpp.web.workers.pipeline import PipelineRuntime
from doc3gpp.web.state import JobWorkerHandle


def test_preparation_tasks_overlap() -> None:
    runtime = PipelineRuntime(prepare_workers=2)
    started = threading.Barrier(2)
    release = threading.Event()

    def prepare(value: int) -> int:
        started.wait(timeout=2)
        release.wait(timeout=2)
        return value

    try:
        first = runtime.submit_prepare(prepare, 1)
        second = runtime.submit_prepare(prepare, 2)
        release.set()
        assert {first.result(timeout=2), second.result(timeout=2)} == {1, 2}
    finally:
        runtime.shutdown()


def test_persistence_tasks_never_run_concurrently() -> None:
    runtime = PipelineRuntime(prepare_workers=2)
    active = 0
    maximum = 0
    lock = threading.Lock()

    def persist(value: int) -> int:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return value

    try:
        futures = [runtime.submit_persist(persist, value) for value in range(4)]
        assert [future.result(timeout=2) for future in futures] == [0, 1, 2, 3]
        assert maximum == 1
    finally:
        runtime.shutdown()


def test_writer_survives_a_failed_persistence_command() -> None:
    runtime = PipelineRuntime(prepare_workers=1)

    def persist(value: int) -> int:
        if value == 1:
            raise RuntimeError("write failed")
        return value

    try:
        failed = runtime.submit_persist(persist, 1)
        succeeding = runtime.submit_persist(persist, 2)
        try:
            failed.result(timeout=2)
        except RuntimeError as exc:
            assert str(exc) == "write failed"
        else:
            raise AssertionError("expected the first write to fail")
        assert succeeding.result(timeout=2) == 2
    finally:
        runtime.shutdown()


def test_pending_preparation_is_bounded() -> None:
    runtime = PipelineRuntime(prepare_workers=1, max_pending=1)
    started = threading.Event()
    release = threading.Event()

    def prepare(value: int) -> int:
        started.set()
        release.wait(timeout=2)
        return value

    try:
        first = runtime.submit_prepare(prepare, 1)
        assert started.wait(timeout=2)
        submitted = threading.Event()

        def submit_second() -> None:
            runtime.submit_prepare(prepare, 2)
            submitted.set()

        thread = threading.Thread(target=submit_second)
        thread.start()
        time.sleep(0.03)
        assert not submitted.is_set()
        release.set()
        thread.join(timeout=2)
        assert submitted.is_set()
        assert first.result(timeout=2) == 1
    finally:
        runtime.shutdown()


def test_shutdown_drains_submitted_persistence_commands() -> None:
    runtime = PipelineRuntime(prepare_workers=1)
    release = threading.Event()
    persisted: list[int] = []

    def persist(value: int) -> int:
        release.wait(timeout=2)
        persisted.append(value)
        return value

    future = runtime.submit_persist(persist, 7)
    shutdown_thread = threading.Thread(target=runtime.shutdown)
    shutdown_thread.start()
    time.sleep(0.02)
    assert shutdown_thread.is_alive()
    release.set()
    shutdown_thread.join(timeout=2)
    assert persisted == [7]
    assert future.result(timeout=2) == 7


def test_job_handle_shutdown_drains_pipeline_without_worker_task() -> None:
    runtime = PipelineRuntime(prepare_workers=1)
    persisted: list[int] = []
    future = runtime.submit_persist(lambda value: persisted.append(value) or value, 9)
    handle = JobWorkerHandle(pipeline=runtime)

    asyncio.run(handle.shutdown())

    assert future.result(timeout=2) == 9
    assert persisted == [9]
