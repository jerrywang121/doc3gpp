"""Preparation/persistence boundary tests for queued TDoc parsing."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from test_tdoc_cr_service import FIXTURES_DIR, _build_service, _docx_available

from doc3gpp.models.tdoc import TDoc
from doc3gpp.services.pipeline_runtime import PipelineRuntime
from doc3gpp.services.tdoc_cr_service import (
    PreparedDirectTDoc,
    TDocNotYetOnFTPError,
    TDocTooLargeError,
)
from doc3gpp.services.tdoc_pipeline import (
    PipelineItem,
    TDocPipeline,
    classify_tdoc_exception,
)


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_direct_url_writer_persists_prepared_result(tmp_path: Path) -> None:
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    service, _scraper, _cache, cr_repo, cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=fixture.read_bytes()
    )
    tdoc_repo.get_by_id.return_value = TDoc(tdoc_id="R5s260009", type="CR")

    prepared = service.prepare_from_url(
        "https://www.3gpp.org/ftp/.../R5s260009.zip",
        force=True,
    )
    assert isinstance(prepared, PreparedDirectTDoc)

    result = service.persist_prepared_url(prepared)

    assert result.persisted is True
    cr_repo.upsert.assert_called_once_with(prepared.details)
    cr_repo.upsert_extract_meta.assert_called_once_with(prepared.extract_meta)
    if prepared.ttcn is not None:
        cr_ttcn_repo.upsert.assert_called_once_with(prepared.ttcn)


def test_prepare_tasks_overlap_but_persist_tasks_are_serialized() -> None:
    runtime = PipelineRuntime(prepare_workers=2, max_pending=4)
    runner = TDocPipeline(runtime)
    prepare_barrier = threading.Barrier(2)
    first_writer_started = threading.Event()
    second_writer_started = threading.Event()
    release_first_writer = threading.Event()
    writer_lock = threading.Lock()
    writer_calls = 0
    active_writers = 0
    max_active_writers = 0
    result_box: list[Any] = []
    error_box: list[BaseException] = []

    def prepare(value: int) -> int:
        prepare_barrier.wait(timeout=2)
        return value

    def persist(value: int) -> int:
        nonlocal writer_calls, active_writers, max_active_writers
        with writer_lock:
            writer_calls += 1
            active_writers += 1
            max_active_writers = max(max_active_writers, active_writers)
            call_number = writer_calls
        try:
            if call_number == 1:
                first_writer_started.set()
                assert release_first_writer.wait(timeout=2)
            else:
                second_writer_started.set()
            return value * 10
        finally:
            with writer_lock:
                active_writers -= 1

    def run() -> None:
        try:
            result_box.append(
                runner.run_sync(
                    [
                        PipelineItem(key="one", value=1),
                        PipelineItem(key="two", value=2),
                    ],
                    prepare,
                    persist=persist,
                )
            )
        except BaseException as exc:  # noqa: BLE001 - assertion aid
            error_box.append(exc)

    thread = threading.Thread(target=run)
    try:
        thread.start()
        assert first_writer_started.wait(timeout=2)
        assert not second_writer_started.is_set()
        release_first_writer.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert not error_box
        assert max_active_writers == 1
        outcomes = result_box[0]
        assert [outcome.key for outcome in outcomes] == ["one", "two"]
        assert [outcome.status for outcome in outcomes] == ["success", "success"]
    finally:
        release_first_writer.set()
        thread.join(timeout=2)
        runtime.shutdown()


def test_writer_failure_is_isolated_to_one_item() -> None:
    runtime = PipelineRuntime(prepare_workers=2, max_pending=4)
    runner = TDocPipeline(runtime)
    first_prepared = threading.Event()
    allow_second = threading.Event()
    writer_order: list[int] = []

    def prepare(value: int) -> int:
        if value == 1:
            first_prepared.set()
        elif value == 2:
            assert first_prepared.wait(timeout=2)
            assert allow_second.wait(timeout=2)
        return value

    def persist(value: int) -> int:
        writer_order.append(value)
        if value == 1:
            allow_second.set()
            raise RuntimeError("writer exploded")
        return value

    try:
        outcomes = runner.run_sync(
            [
                PipelineItem(key="failed", value=1),
                PipelineItem(key="later", value=2),
            ],
            prepare,
            persist=persist,
        )
        assert writer_order == [1, 2]
        assert [(outcome.key, outcome.status) for outcome in outcomes] == [
            ("failed", "failed"),
            ("later", "success"),
        ]
        assert outcomes[0].reason == "RuntimeError: writer exploded"
        assert outcomes[1].value == 2
    finally:
        allow_second.set()
        runtime.shutdown()


def test_outcomes_are_returned_in_source_order() -> None:
    runtime = PipelineRuntime(prepare_workers=2, max_pending=5)
    runner = TDocPipeline(runtime)
    first_release = threading.Event()
    later_prepares = 0
    later_lock = threading.Lock()
    later_done = threading.Event()
    not_yet = TDocNotYetOnFTPError("skip")
    too_large = TDocTooLargeError("big.zip", 11, 10)
    result_box: list[Any] = []
    error_box: list[BaseException] = []

    def prepare(value: str) -> str:
        nonlocal later_prepares
        if value == "first":
            assert first_release.wait(timeout=2)
        else:
            with later_lock:
                later_prepares += 1
                if later_prepares == 4:
                    later_done.set()
        if value == "skip":
            raise not_yet
        if value == "large":
            raise too_large
        if value == "fail":
            raise ValueError("bad input")
        return value

    def run() -> None:
        try:
            result_box.append(
                runner.run_sync(
                    [
                        PipelineItem(key="first", value="first"),
                        PipelineItem(key="skip", value="skip"),
                        PipelineItem(key="large", value="large"),
                        PipelineItem(key="fail", value="fail"),
                        PipelineItem(key="last", value="last"),
                    ],
                    prepare,
                )
            )
        except BaseException as exc:  # noqa: BLE001 - assertion aid
            error_box.append(exc)

    thread = threading.Thread(target=run)
    try:
        thread.start()
        assert later_done.wait(timeout=2)
        first_release.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert not error_box
        outcomes = result_box[0]
        assert [outcome.key for outcome in outcomes] == [
            "first",
            "skip",
            "large",
            "fail",
            "last",
        ]
        assert outcomes[0].value == "first"
        assert outcomes[1].status == "skipped"
        assert outcomes[1].reason == f"TDocNotYetOnFTPError: {not_yet}"
        assert outcomes[2].status == "skipped"
        assert outcomes[2].reason == f"TDocTooLargeError: {too_large}"
        assert outcomes[3].status == "failed"
        assert outcomes[3].reason == "ValueError: bad input"
        assert outcomes[4].value == "last"
    finally:
        first_release.set()
        thread.join(timeout=2)
        runtime.shutdown()


def test_async_runner_applies_backpressure_without_blocking() -> None:
    asyncio.run(_run_async_backpressure_test())


async def _run_async_backpressure_test() -> None:
    runtime = PipelineRuntime(prepare_workers=1, max_pending=1)
    runner = TDocPipeline(runtime)
    preparation_started = threading.Event()
    release_preparation = threading.Event()

    def prepare(value: int) -> int:
        if value == 1:
            preparation_started.set()
            assert release_preparation.wait(timeout=2)
        return value

    async def run() -> Any:
        return await runner.run_async(
            [
                PipelineItem(key="one", value=1),
                PipelineItem(key="two", value=2),
            ],
            prepare,
        )

    try:
        task = asyncio.create_task(run())
        await asyncio.wait_for(asyncio.to_thread(preparation_started.wait), timeout=2)
        unrelated_ran = asyncio.Event()

        async def unrelated() -> None:
            unrelated_ran.set()

        await asyncio.wait_for(unrelated(), timeout=2)
        assert unrelated_ran.is_set()
        assert not task.done()
        release_preparation.set()
        outcomes = await asyncio.wait_for(task, timeout=2)
        assert [outcome.key for outcome in outcomes] == ["one", "two"]
    finally:
        release_preparation.set()
        await runtime.shutdown_async()


def test_cancellation_stops_new_submissions_and_drains_writer() -> None:
    asyncio.run(_run_cancellation_test())


async def _run_cancellation_test() -> None:
    runtime = PipelineRuntime(prepare_workers=2, max_pending=2)
    runner = TDocPipeline(runtime)
    release_second_preparation = threading.Event()
    cancellation = threading.Event()
    started_values: list[int] = []
    persisted_values: list[int] = []
    started_lock = threading.Lock()
    second_preparation_started = threading.Event()
    writer_started = threading.Event()
    release_writer = threading.Event()

    def prepare(value: int) -> int:
        with started_lock:
            started_values.append(value)
        if value == 2:
            second_preparation_started.set()
            assert release_second_preparation.wait(timeout=2)
        return value

    def persist(value: int) -> int:
        persisted_values.append(value)
        writer_started.set()
        assert release_writer.wait(timeout=2)
        return value

    async def run() -> Any:
        return await runner.run_async(
            [PipelineItem(key=str(value), value=value) for value in (1, 2, 3, 4)],
            prepare,
            persist=persist,
            is_cancelled=cancellation.is_set,
        )

    try:
        task = asyncio.create_task(run())
        await asyncio.wait_for(asyncio.to_thread(second_preparation_started.wait), timeout=2)
        await asyncio.wait_for(asyncio.to_thread(writer_started.wait), timeout=2)
        cancellation.set()
        release_second_preparation.set()
        assert not task.done()
        release_writer.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert sorted(started_values) == [1, 2]
        assert persisted_values == [1]
    finally:
        cancellation.set()
        release_second_preparation.set()
        release_writer.set()
        await runtime.shutdown_async()


def test_tdoc_exception_classifier_uses_skip_and_failure_buckets() -> None:
    not_yet = TDocNotYetOnFTPError("R5-260001")
    too_large = TDocTooLargeError("R5-260001.zip", 20, 10)

    assert classify_tdoc_exception(not_yet) == (
        "skipped",
        f"TDocNotYetOnFTPError: {not_yet}",
    )
    assert classify_tdoc_exception(too_large) == (
        "skipped",
        f"TDocTooLargeError: {too_large}",
    )
    assert classify_tdoc_exception(LookupError("missing")) == (
        "failed",
        "LookupError: missing",
    )
