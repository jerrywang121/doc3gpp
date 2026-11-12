from __future__ import annotations

import asyncio
import ctypes
import json
import os
import queue
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest

from doc3gpp.models.jobs import JobStatus
from doc3gpp.settings.schema import Settings
from doc3gpp.web import mcp_stdio
from doc3gpp.web.state import JobWorkerHandle
from doc3gpp.web.workers.job_worker import JobWorker


def _state(settings: Settings, events: list[str]) -> SimpleNamespace:
    def close(name: str, *, fail: bool = False) -> None:
        events.append(name)
        if fail:
            raise RuntimeError(f"{name} failed")

    return SimpleNamespace(
        settings=settings,
        services=SimpleNamespace(pipeline=None),
        jobs=None,
        engine=SimpleNamespace(dispose=lambda: close("engine")),
        testcase_engine=SimpleNamespace(dispose=lambda: close("testcase")),
        specdata_engine=SimpleNamespace(dispose=lambda: close("specdata")),
        close_docx_pool=lambda: close("docx"),
        close_embedder=lambda: close("embedder"),
    )


@pytest.mark.parametrize("mode", ["success", "startup-error", "cancelled"])
def test_embedded_stdio_preserves_host_executor_and_does_not_wait_for_host_work(
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    from threading import Event

    settings = Settings()
    events: list[str] = []
    state = _state(settings, events)
    host_started = Event()
    release_host = Event()
    server_started = asyncio.Event()

    def host_work():
        host_started.set()
        assert release_host.wait(10), "host thread was not released"
        return "host finished"

    class Worker:
        def __init__(self, _state):
            pass

        async def run(self):
            await asyncio.Event().wait()

    class Server:
        async def run_stdio_async(self):
            server_started.set()
            if mode == "cancelled":
                await asyncio.Event().wait()

    def build_server(_state):
        if mode == "startup-error":
            raise ValueError("startup failed")
        return Server()

    monkeypatch.setattr(mcp_stdio, "_load_dependencies", lambda: mcp_stdio._StdioDependencies(
        build_state=lambda _: state, build_mcp_server=build_server,
        worker_type=Worker, handle_type=JobWorkerHandle,
    ))

    async def exercise():
        host = asyncio.create_task(asyncio.to_thread(host_work))
        while not host_started.is_set():
            await asyncio.sleep(0.001)
        runner = asyncio.create_task(mcp_stdio.run_stdio(settings))
        try:
            if mode == "cancelled":
                await asyncio.wait_for(server_started.wait(), 2)
                runner.cancel()
            done, _ = await asyncio.wait({runner}, timeout=1)
            assert runner in done, "stdio waited for unrelated host work"
            result = (await asyncio.gather(runner, return_exceptions=True))[0]
            if mode == "startup-error":
                assert isinstance(result, ValueError) and str(result) == "startup failed"
            elif mode == "cancelled":
                assert isinstance(result, asyncio.CancelledError)
            else:
                assert result is None
            assert not host.done()
            assert await asyncio.to_thread(lambda: 42) == 42
            assert "engine" in events
        finally:
            release_host.set()
            await asyncio.gather(runner, return_exceptions=True)
            assert await host == "host finished"

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["success", "startup-error", "cancelled"])
def test_embedded_stdio_leaves_default_executor_usable(
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    settings = Settings()
    state = _state(settings, [])
    server_started = asyncio.Event()

    class Worker:
        def __init__(self, _state):
            pass

        async def run(self):
            await asyncio.Event().wait()

    class Server:
        async def run_stdio_async(self):
            server_started.set()
            if mode == "cancelled":
                await asyncio.Event().wait()

    def build_server(_state):
        if mode == "startup-error":
            raise ValueError("startup failed")
        return Server()

    monkeypatch.setattr(mcp_stdio, "_load_dependencies", lambda: mcp_stdio._StdioDependencies(
        build_state=lambda _: state, build_mcp_server=build_server,
        worker_type=Worker, handle_type=JobWorkerHandle,
    ))

    async def exercise():
        assert await asyncio.to_thread(lambda: "before") == "before"
        runner = asyncio.create_task(mcp_stdio.run_stdio(settings))
        if mode == "cancelled":
            await asyncio.wait_for(server_started.wait(), 2)
            runner.cancel()
        result = (await asyncio.gather(runner, return_exceptions=True))[0]
        if mode == "startup-error":
            assert isinstance(result, ValueError)
        elif mode == "cancelled":
            assert isinstance(result, asyncio.CancelledError)
        else:
            assert result is None
        assert await asyncio.to_thread(lambda: "after") == "after"

    asyncio.run(exercise())


def test_detached_handler_thread_drains_without_waiting_for_host_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import ContextVar
    from threading import Event

    from doc3gpp.web.workers.handlers import _index_tdocs

    settings = Settings()
    events: list[str] = []
    state = _state(settings, events)
    host_started, host_release = Event(), Event()
    owned_started, owned_release = Event(), Event()
    request_context = ContextVar("request_context", default="missing")
    detached = asyncio.Event()
    job = SimpleNamespace(params={})

    def host_work():
        host_started.set()
        assert host_release.wait(10)

    def rebuild(*_args, **_kwargs):
        assert request_context.get() == "stdio request"
        owned_started.set()
        assert owned_release.wait(10)
        events.append("owned thread finished")
        return {"status": "complete"}

    state.services.tdoc_index = SimpleNamespace(rebuild=rebuild)

    class Worker:
        def __init__(self, _state):
            pass

        async def run(self):
            handler = asyncio.create_task(_index_tdocs(
                job, state.services, settings, progress=lambda *_a, **_kw: None,
                cancel_event=asyncio.Event(),
            ))
            while not owned_started.is_set():
                await asyncio.sleep(0.001)
            handler.cancel()
            await asyncio.gather(handler, return_exceptions=True)
            detached.set()
            await asyncio.Event().wait()

    class Server:
        async def run_stdio_async(self):
            await detached.wait()

    monkeypatch.setattr(mcp_stdio, "_load_dependencies", lambda: mcp_stdio._StdioDependencies(
        build_state=lambda _: state, build_mcp_server=lambda _: Server(),
        worker_type=Worker, handle_type=JobWorkerHandle,
    ))

    async def exercise():
        # Saturate the host executor: stdio must have an independent lane.
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        host = asyncio.create_task(asyncio.to_thread(host_work))
        while not host_started.is_set():
            await asyncio.sleep(0.001)
        request_context.set("stdio request")
        runner = asyncio.create_task(mcp_stdio.run_stdio(settings))
        try:
            await asyncio.wait_for(detached.wait(), 2)
            done, _ = await asyncio.wait({runner}, timeout=0.1)
            assert not done
            assert "engine" not in events and "embedder" not in events
            owned_release.set()
            done, _ = await asyncio.wait({runner}, timeout=1)
            assert runner in done, "stdio waited for unrelated host work"
            await runner
            assert not host.done()
            assert events.index("owned thread finished") < events.index("embedder")
            assert events.index("owned thread finished") < events.index("engine")
        finally:
            owned_release.set()
            host_release.set()
            await asyncio.gather(runner, host, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("failed_drain", ["worker", "pipeline"])
@pytest.mark.parametrize("fail_timing", ["before-submit", "after-submit"])
def test_failed_drain_submission_preserves_resources_while_owned_thread_runs(
    monkeypatch: pytest.MonkeyPatch, failed_drain: str, fail_timing: str,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from functools import partial
    from threading import Event

    from doc3gpp.web.workers.handlers import _index_tdocs

    settings = Settings()
    events: list[str] = []
    state = _state(settings, events)
    started, release, finished = Event(), Event(), Event()
    ready = asyncio.Event()

    def blocking_work(*_args, **_kwargs):
        started.set()
        assert release.wait(10), "test did not release owned thread"
        events.append("owned thread finished")
        finished.set()
        return {"status": "complete"}

    state.services.tdoc_index = SimpleNamespace(rebuild=blocking_work)

    class Worker:
        def __init__(self, _state):
            pass

        async def run(self):
            if failed_drain == "worker":
                handler = asyncio.create_task(_index_tdocs(
                    SimpleNamespace(params={}), state.services, settings,
                    progress=lambda *_a, **_kw: None, cancel_event=asyncio.Event(),
                ))
                while not started.is_set():
                    await asyncio.sleep(0.001)
                handler.cancel()
                await asyncio.gather(handler, return_exceptions=True)
            else:
                state.jobs.pipeline._prepare.submit(blocking_work)
                while not started.is_set():
                    await asyncio.sleep(0.001)
            ready.set()
            await asyncio.Event().wait()

    class Server:
        async def run_stdio_async(self):
            await ready.wait()

    class FailingCleanupExecutor(ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):
            kind = "pipeline" if isinstance(fn, partial) else "worker"
            if kind == failed_drain:
                if fail_timing == "after-submit":
                    super().submit(fn, *args, **kwargs)
                raise RuntimeError(f"cannot submit {kind} drain")
            return super().submit(fn, *args, **kwargs)

    def executor_factory(*args, **kwargs):
        if kwargs.get("thread_name_prefix") == "doc3gpp-stdio-worker":
            return ThreadPoolExecutor(*args, **kwargs)
        return FailingCleanupExecutor(*args, **kwargs)

    monkeypatch.setattr(mcp_stdio, "ThreadPoolExecutor", executor_factory)
    monkeypatch.setattr(mcp_stdio, "_load_dependencies", lambda: mcp_stdio._StdioDependencies(
        build_state=lambda _: state, build_mcp_server=lambda _: Server(),
        worker_type=Worker, handle_type=JobWorkerHandle,
    ))

    async def exercise():
        runner = asyncio.create_task(mcp_stdio.run_stdio(settings))
        try:
            await asyncio.wait_for(ready.wait(), 2)
            done, _ = await asyncio.wait({runner}, timeout=0.25)
            assert not any(name in events for name in (
                "docx", "embedder", "engine", "testcase", "specdata",
            )), events
            if failed_drain == "worker":
                assert state.jobs.pipeline._prepare.submit(lambda: 42).result(timeout=1) == 42
            if done:
                result = (await asyncio.gather(runner, return_exceptions=True))[0]
                assert isinstance(result, RuntimeError)
                assert str(result) == f"cannot submit {failed_drain} drain"
        finally:
            release.set()
            result = (await asyncio.gather(runner, return_exceptions=True))[0]
            async def wait_for_thread():
                while not finished.is_set():
                    await asyncio.sleep(0.001)

            await asyncio.wait_for(wait_for_thread(), 2)
            state.jobs.pipeline.shutdown()
        assert isinstance(result, RuntimeError)
        assert str(result) == f"cannot submit {failed_drain} drain"

    asyncio.run(exercise())


def test_worker_runs_with_shared_configured_pipeline_and_shuts_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    settings.pipeline.prepare_workers = 3
    events: list[str] = []
    state = _state(settings, events)
    started = asyncio.Event()
    observed = {}

    class Worker:
        def __init__(self, state: SimpleNamespace) -> None:
            self.state = state

        async def run(self) -> None:
            observed["pipeline"] = self.state.services.pipeline
            events.append("worker started")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("worker stopped")

    class Server:
        async def run_stdio_async(self) -> None:
            await asyncio.wait_for(started.wait(), 2)
            assert state.services.pipeline is state.jobs.pipeline

    deps = mcp_stdio._StdioDependencies(
        build_state=lambda _: state,
        build_mcp_server=lambda _: Server(),
        worker_type=Worker,
        handle_type=JobWorkerHandle,
    )
    monkeypatch.setattr(mcp_stdio, "_load_dependencies", lambda: deps)
    asyncio.run(mcp_stdio.run_stdio(settings))

    pipeline = observed["pipeline"]
    assert pipeline is not None
    assert pipeline._prepare._max_workers == 3
    assert events == ["worker started", "worker stopped", "docx", "embedder", "engine", "testcase", "specdata"]
    with pytest.raises(RuntimeError, match="shutdown"):
        pipeline._prepare.submit(lambda: None)


def test_real_job_worker_polls_and_is_cancelled_at_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings()
    state = _state(settings, [])
    polled = asyncio.Event()

    class Repo:
        def delete_older_than(self, _cutoff):
            return 0

        def list(self, *, status, limit):
            del limit
            if status == JobStatus.QUEUED:
                polled.set()
            return []

    state.services.job_repo = Repo()

    class Server:
        async def run_stdio_async(self) -> None:
            await asyncio.wait_for(polled.wait(), 2)
            assert not state.jobs.task.done()
            assert state.services.pipeline is state.jobs.pipeline

    monkeypatch.setattr(mcp_stdio, "_load_dependencies", lambda: mcp_stdio._StdioDependencies(
        build_state=lambda _: state, build_mcp_server=lambda _: Server(),
        worker_type=JobWorker, handle_type=JobWorkerHandle,
    ))
    asyncio.run(mcp_stdio.run_stdio(settings))
    assert state.jobs.task.cancelled()


def test_stdout_is_restored_even_if_diagnostic_flush_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenStderr:
        def flush(self) -> None:
            raise OSError("stderr flush failed")

    original_stdout = sys.stdout
    saved_fd = os.dup(1)
    monkeypatch.setattr(sys, "stderr", BrokenStderr())
    try:
        with pytest.raises(OSError, match="stderr flush failed"), mcp_stdio._protect_stdout():
            pass
        assert sys.stdout is original_stdout
        assert os.fstat(1) == os.fstat(saved_fd)
    finally:
        sys.stdout = original_stdout
        os.dup2(saved_fd, 1)
        os.close(saved_fd)


def test_stdout_flush_failure_does_not_replace_active_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenStderr:
        def flush(self) -> None:
            raise OSError("stderr flush failed")

    original_stdout = sys.stdout
    saved_fd = os.dup(1)
    monkeypatch.setattr(sys, "stderr", BrokenStderr())
    try:
        with pytest.raises(ValueError, match="server failed"), mcp_stdio._protect_stdout():
            raise ValueError("server failed")
    finally:
        sys.stdout = original_stdout
        os.dup2(saved_fd, 1)
        os.close(saved_fd)


def test_cleanup_continues_if_reporting_a_cleanup_error_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    events: list[str] = []
    state = _state(settings, events)

    class BrokenStderr:
        def write(self, _text: str) -> int:
            raise OSError("stderr write failed")

        def flush(self) -> None:
            pass

    def failing_docx() -> None:
        events.append("docx")
        raise RuntimeError("docx failed")

    state.close_docx_pool = failing_docx

    class Worker:
        def __init__(self, _state: SimpleNamespace) -> None:
            pass

        async def run(self) -> None:
            await asyncio.Event().wait()

    class Server:
        async def run_stdio_async(self) -> None:
            raise ValueError("server failed")

    monkeypatch.setattr(sys, "stderr", BrokenStderr())
    monkeypatch.setattr(mcp_stdio, "_load_dependencies", lambda: mcp_stdio._StdioDependencies(
        build_state=lambda _: state, build_mcp_server=lambda _: Server(),
        worker_type=Worker, handle_type=JobWorkerHandle,
    ))
    with pytest.raises(ValueError, match="server failed"):
        asyncio.run(mcp_stdio.run_stdio(settings))
    assert events == ["docx", "embedder", "engine", "testcase", "specdata"]


@pytest.mark.parametrize("outcome", ["failure", "cancel", "normal"])
def test_all_cleanup_runs_after_cleanup_failure_and_preserves_outcome(
    monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    settings = Settings()
    events: list[str] = []
    state = _state(settings, events)

    def failing_docx() -> None:
        events.append("docx")
        raise RuntimeError("docx failed")

    state.close_docx_pool = failing_docx

    class Worker:
        def __init__(self, _state: SimpleNamespace) -> None:
            pass

        async def run(self) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                events.append("worker stopped")

    class Server:
        async def run_stdio_async(self) -> None:
            await asyncio.sleep(0)
            if outcome == "failure":
                raise ValueError("server failed")
            if outcome == "cancel":
                raise asyncio.CancelledError()

    monkeypatch.setattr(mcp_stdio, "_load_dependencies", lambda: mcp_stdio._StdioDependencies(
        build_state=lambda _: state, build_mcp_server=lambda _: Server(),
        worker_type=Worker, handle_type=JobWorkerHandle,
    ))
    expected = {"failure": ValueError, "cancel": asyncio.CancelledError, "normal": RuntimeError}
    with pytest.raises(expected[outcome]):
        asyncio.run(mcp_stdio.run_stdio(settings))
    assert events == ["worker stopped", "docx", "embedder", "engine", "testcase", "specdata"]


@pytest.mark.parametrize("mode", ["slow-handler", "detached-thread", "already-stopping"])
def test_shutdown_keeps_resources_alive_for_inflight_job_thread(
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    from threading import Event

    from doc3gpp.models.jobs import JobKind
    from doc3gpp.web.workers.handlers import _index_tdocs

    settings = Settings()
    settings.server.poll_interval_seconds = 0.01
    events: list[str] = []
    state = _state(settings, events)
    thread_started = Event()
    release_thread = Event()
    job = SimpleNamespace(id="slow-index", kind=JobKind.INDEX_TDOCS, params={})

    class Repo:
        status = JobStatus.QUEUED

        def delete_older_than(self, _cutoff):
            return 0

        def list(self, *, status, limit):
            return [job] if status == self.status == JobStatus.QUEUED else []

        def mark_running(self, _id, **_kwargs):
            self.status = JobStatus.RUNNING
            return True, None

        def append_log(self, _id, **_kwargs):
            pass

        def mark_cancelled(self, _id):
            self.status = JobStatus.CANCELLED
            events.append("job cancelled")

        def mark_succeeded(self, _id, **_kwargs):
            self.status = JobStatus.SUCCEEDED

        def mark_failed(self, _id, **_kwargs):
            self.status = JobStatus.FAILED

    def rebuild(*_args, **_kwargs):
        thread_started.set()
        assert release_thread.wait(15), "test did not release index thread"
        events.append("thread finished")
        return {"status": "complete"}

    state.services.job_repo = Repo()
    state.services.tdoc_index = SimpleNamespace(rebuild=rebuild)
    state.close_listserv_services = lambda: events.append("listserv")

    class Worker(JobWorker):
        def __init__(self, state):
            super().__init__(state, handlers={JobKind.INDEX_TDOCS: _index_tdocs})

    class Server:
        async def run_stdio_async(self):
            while not thread_started.is_set():
                await asyncio.sleep(0.01)
            if mode == "detached-thread":
                handler = next(t for t in asyncio.all_tasks() if t.get_name() == job.id)
                handler.cancel()
                await asyncio.gather(handler, return_exceptions=True)
            elif mode == "already-stopping":
                state.jobs.task.cancel()
                while not state.jobs.cancel_events[job.id].is_set():
                    await asyncio.sleep(0)

    monkeypatch.setattr(mcp_stdio, "_load_dependencies", lambda: mcp_stdio._StdioDependencies(
        build_state=lambda _: state, build_mcp_server=lambda _: Server(),
        worker_type=Worker, handle_type=JobWorkerHandle,
    ))

    async def exercise():
        runner = asyncio.create_task(mcp_stdio.run_stdio(settings))
        try:
            # Exceed the shared handle's real five-second timeout.
            await asyncio.sleep(5.3)
            runner.cancel()
            runner.cancel()
            await asyncio.sleep(0.1)
            assert not runner.done()
            assert not any(name in events for name in ("docx", "listserv", "embedder", "engine"))
            if mode != "detached-thread":
                assert state.services.job_repo.status == JobStatus.RUNNING
            # The pipeline must remain usable while the handler is still running.
            assert state.jobs.pipeline._prepare.submit(lambda: 42).result(timeout=1) == 42
        finally:
            release_thread.set()
            result = (await asyncio.gather(runner, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError)
        assert events.index("thread finished") < events.index("listserv")
        assert events.index("listserv") < events.index("embedder")
        assert events.index("embedder") < events.index("engine")
        assert state.services.job_repo.status == JobStatus.CANCELLED

    asyncio.run(exercise())


def test_missing_mcp_v1_module_gives_upgrade_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    original = mcp_stdio.importlib.import_module

    def import_module(name: str):
        if name == "mcp.server.mcpserver":
            raise ModuleNotFoundError("No module named 'mcp.server.mcpserver'", name=name)
        return original(name)

    monkeypatch.setattr(mcp_stdio.importlib, "import_module", import_module)
    with pytest.raises(mcp_stdio.MCPStdioDependencyError, match="pip install --upgrade mcp"):
        mcp_stdio._load_dependencies()


def test_shutdown_failure_does_not_skip_other_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings()
    events: list[str] = []
    state = _state(settings, events)

    class Handle(JobWorkerHandle):
        async def shutdown(self) -> None:
            await super().shutdown()
            events.append("shutdown")
            raise RuntimeError("shutdown failed")

    class Worker:
        def __init__(self, _state: SimpleNamespace) -> None:
            pass

        async def run(self) -> None:
            await asyncio.Event().wait()

    class Server:
        async def run_stdio_async(self) -> None:
            await asyncio.sleep(0)

    monkeypatch.setattr(mcp_stdio, "_load_dependencies", lambda: mcp_stdio._StdioDependencies(
        build_state=lambda _: state, build_mcp_server=lambda _: Server(),
        worker_type=Worker, handle_type=Handle,
    ))
    with pytest.raises(RuntimeError, match="shutdown failed"):
        asyncio.run(mcp_stdio.run_stdio(settings))
    assert events == ["shutdown", "docx", "embedder", "engine", "testcase", "specdata"]


def test_shutdown_failure_still_stops_worker_and_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings()
    events: list[str] = []
    state = _state(settings, events)
    started = asyncio.Event()

    class Handle(JobWorkerHandle):
        async def shutdown(self) -> None:
            raise RuntimeError("shutdown failed before stopping worker")

    class Worker:
        def __init__(self, _state: SimpleNamespace) -> None:
            pass

        async def run(self) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("worker stopped")

    class Server:
        async def run_stdio_async(self) -> None:
            await asyncio.wait_for(started.wait(), 2)

    monkeypatch.setattr(mcp_stdio, "_load_dependencies", lambda: mcp_stdio._StdioDependencies(
        build_state=lambda _: state, build_mcp_server=lambda _: Server(),
        worker_type=Worker, handle_type=Handle,
    ))
    with pytest.raises(RuntimeError, match="shutdown failed before stopping worker"):
        asyncio.run(mcp_stdio.run_stdio(settings))
    assert events == ["worker stopped", "docx", "embedder", "engine", "testcase", "specdata"]
    with pytest.raises(RuntimeError, match="shutdown"):
        state.jobs.pipeline._prepare.submit(lambda: None)


@pytest.mark.parametrize("server_fails", [False, True])
def test_cancellation_during_failed_shutdown_waits_for_worker_before_disposal(
    monkeypatch: pytest.MonkeyPatch, server_fails: bool,
) -> None:
    settings = Settings()
    events: list[str] = []
    state = _state(settings, events)
    started = asyncio.Event()
    stopping = asyncio.Event()
    allow_stop = asyncio.Event()

    class Handle(JobWorkerHandle):
        async def shutdown(self) -> None:
            raise RuntimeError("shutdown failed")

    class Worker:
        def __init__(self, _state: SimpleNamespace) -> None:
            pass

        async def run(self) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                stopping.set()
                await allow_stop.wait()
                events.append("worker stopped")
                raise

    class Server:
        async def run_stdio_async(self) -> None:
            await started.wait()
            if server_fails:
                raise ValueError("server failed")

    monkeypatch.setattr(mcp_stdio, "_load_dependencies", lambda: mcp_stdio._StdioDependencies(
        build_state=lambda _: state, build_mcp_server=lambda _: Server(),
        worker_type=Worker, handle_type=Handle,
    ))

    async def exercise() -> None:
        task = asyncio.create_task(mcp_stdio.run_stdio(settings))
        try:
            await asyncio.wait_for(stopping.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 0.1)
            assert "engine" not in events
        finally:
            allow_stop.set()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError)
        assert events == ["worker stopped", "docx", "embedder", "engine", "testcase", "specdata"]
        assert state.jobs.task.done()
        with pytest.raises(RuntimeError, match="shutdown"):
            state.jobs.pipeline._prepare.submit(lambda: None)

    asyncio.run(exercise())


def test_worker_shutdown_supports_python_310_task_without_cancelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    events: list[str] = []
    state = _state(settings, events)
    started = asyncio.Event()

    class LegacyTask:
        def __init__(self, task: asyncio.Task) -> None:
            self.task = task

        def done(self) -> bool:
            return self.task.done()

        def cancel(self) -> bool:
            return self.task.cancel()

        def __await__(self):
            return self.task.__await__()

    class Handle(JobWorkerHandle):
        async def shutdown(self) -> None:
            self.task = LegacyTask(self.task)
            raise RuntimeError("shutdown failed")

    class Worker:
        def __init__(self, _state: SimpleNamespace) -> None:
            pass

        async def run(self) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("worker stopped")

    class Server:
        async def run_stdio_async(self) -> None:
            await started.wait()

    monkeypatch.setattr(mcp_stdio, "_load_dependencies", lambda: mcp_stdio._StdioDependencies(
        build_state=lambda _: state, build_mcp_server=lambda _: Server(),
        worker_type=Worker, handle_type=Handle,
    ))
    with pytest.raises(RuntimeError, match="shutdown failed"):
        asyncio.run(mcp_stdio.run_stdio(settings))
    assert events == ["worker stopped", "docx", "embedder", "engine", "testcase", "specdata"]


@pytest.mark.parametrize("mode", ["normal", "cancelled", "failed", "failed-cancelled"])
def test_uncooperative_worker_keeps_resources_until_it_stops(
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    settings = Settings()
    events: list[str] = []
    state = _state(settings, events)
    started = asyncio.Event()
    stopping = asyncio.Event()
    release = asyncio.Event()

    class Handle(JobWorkerHandle):
        async def shutdown(self) -> None:
            if mode.startswith("failed"):
                raise RuntimeError("shutdown failed")
            await super().shutdown()

    class Worker:
        def __init__(self, _state: SimpleNamespace) -> None:
            pass

        async def run(self) -> None:
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    stopping.set()
            events.append("worker stopped")

    class Server:
        async def run_stdio_async(self) -> None:
            await started.wait()

    monkeypatch.setattr(mcp_stdio, "_load_dependencies", lambda: mcp_stdio._StdioDependencies(
        build_state=lambda _: state, build_mcp_server=lambda _: Server(),
        worker_type=Worker, handle_type=Handle,
    ))

    async def exercise() -> None:
        runner = asyncio.create_task(mcp_stdio.run_stdio(settings))
        try:
            await asyncio.wait_for(stopping.wait(), 2)
            if "cancelled" in mode:
                runner.cancel()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(runner), 0.1)
            assert events == []
            assert state.jobs.pipeline._prepare.submit(lambda: 42).result(timeout=1) == 42
        finally:
            release.set()
            result = (await asyncio.gather(runner, return_exceptions=True))[0]
        if "cancelled" in mode:
            assert isinstance(result, asyncio.CancelledError)
        elif mode == "failed":
            assert isinstance(result, RuntimeError)
        else:
            assert result is None
        assert events == ["worker stopped", "docx", "embedder", "engine", "testcase", "specdata"]
        with pytest.raises(RuntimeError, match="shutdown"):
            state.jobs.pipeline._prepare.submit(lambda: None)

    asyncio.run(exercise())


def test_buffered_startup_diagnostic_does_not_reach_protocol() -> None:
    code = textwrap.dedent('''
        import os
        import sys
        from doc3gpp.web.mcp_stdio import _protect_stdout
        print("buffered startup diagnostic")
        with _protect_stdout():
            sys.stdout.buffer.write(b'{"wire":true}\\n')
    ''')
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, timeout=10,
        env={**os.environ, "PYTHONUNBUFFERED": "0",
             "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")},
        check=True,
    )
    assert result.stdout == b'{"wire":true}\n'
    assert b"buffered startup diagnostic" in result.stderr


def test_buffered_cli_settings_diagnostic_does_not_reach_protocol() -> None:
    code = textwrap.dedent('''
        import asyncio
        import sys
        from types import SimpleNamespace
        from doc3gpp import cli
        from doc3gpp.settings.schema import Settings
        from doc3gpp.web import mcp_stdio
        from doc3gpp.web.state import JobWorkerHandle

        def get_settings():
            print("settings startup diagnostic")
            return Settings()
        cli.get_settings = get_settings
        class Worker:
            def __init__(self, state): pass
            async def run(self): await asyncio.Event().wait()
        class Server:
            async def run_stdio_async(self):
                sys.stdout.buffer.write(b'{"wire":true}\\n')
        def build_state(settings):
            engine = SimpleNamespace(dispose=lambda: None)
            return SimpleNamespace(settings=settings, services=SimpleNamespace(pipeline=None),
                jobs=None, engine=engine, testcase_engine=engine, specdata_engine=None,
                close_docx_pool=None, close_embedder=None)
        mcp_stdio._load_dependencies = lambda: mcp_stdio._StdioDependencies(
            build_state=build_state, build_mcp_server=lambda state: Server(),
            worker_type=Worker, handle_type=JobWorkerHandle)
        cli.mcp_command()
    ''')
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, timeout=10,
        env={**os.environ, "PYTHONUNBUFFERED": "0",
             "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")},
        check=True,
    )
    assert result.stdout == b'{"wire":true}\n'
    assert b"settings startup diagnostic" in result.stderr


@pytest.mark.parametrize("outcome", ["eof", "cancel", "failure"])
def test_cli_stdio_wire_excludes_late_loop_teardown_diagnostics(outcome: str) -> None:
    pytest.importorskip("mcp.server.mcpserver")
    code = textwrap.dedent('''
        import asyncio
        import os
        import sys
        from types import SimpleNamespace
        from mcp.server.mcpserver import MCPServer
        from doc3gpp import cli
        from doc3gpp.settings.schema import Settings
        from doc3gpp.web import mcp_stdio
        from doc3gpp.web.state import JobWorkerHandle

        original_stdout = sys.stdout
        started = asyncio.Event()
        background_tasks = set()
        cli.get_settings = lambda: Settings()
        class Worker:
            def __init__(self, state): pass
            async def run(self):
                started.set()
                await asyncio.Event().wait()
        class Server:
            async def run_stdio_async(self):
                async def late_task():
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        print("late loop diagnostic")
                        print("late retained stdout diagnostic", file=original_stdout)
                        os.write(1, b"late fd diagnostic\\n")
                task = asyncio.create_task(late_task())
                background_tasks.add(task)
                task.add_done_callback(background_tasks.discard)
                del task
                await started.wait()
                await asyncio.sleep(0)
                await MCPServer("stdio-teardown-test").run_stdio_async()
                import gc
                gc.collect()
                if sys.argv[1] == "cancel":
                    raise asyncio.CancelledError()
                if sys.argv[1] == "failure":
                    raise RuntimeError("server failed")
        def build_state(settings):
            engine = SimpleNamespace(dispose=lambda: None)
            return SimpleNamespace(settings=settings, services=SimpleNamespace(pipeline=None),
                jobs=None, engine=engine, testcase_engine=engine, specdata_engine=None,
                close_docx_pool=None, close_embedder=None)
        mcp_stdio._load_dependencies = lambda: mcp_stdio._StdioDependencies(
            build_state=build_state, build_mcp_server=lambda state: Server(),
            worker_type=Worker, handle_type=JobWorkerHandle)
        try:
            cli.mcp_command()
        except (asyncio.CancelledError, RuntimeError) as exc:
            print("propagated " + type(exc).__name__, file=sys.stderr)
    ''')
    deadline = time.monotonic() + 30
    process = subprocess.Popen(
        [sys.executable, "-c", code, outcome], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env={
            **os.environ, "PYTHONUNBUFFERED": "0",
            "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
        },
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        }}
        process.stdin.write((json.dumps(request) + "\n").encode())
        process.stdin.flush()
        lines: queue.Queue[bytes] = queue.Queue()
        Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
        try:
            first_line = lines.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            pytest.fail(
                "server did not produce an initialize response "
                f"(child exit status: {process.poll()})"
            )
        assert first_line, "server closed stdout without an initialize response"
        for message in (
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ):
            process.stdin.write((json.dumps(message) + "\n").encode())
        process.stdin.flush()
        Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
        try:
            second_line = lines.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            pytest.fail(
                "server did not produce a tools/list response "
                f"(child exit status: {process.poll()})"
            )
        assert second_line, "server closed stdout without a tools/list response"
        timeout_left = deadline - time.monotonic()
        assert timeout_left > 0, "stdio subprocess exceeded its 30-second deadline"
        remaining, stderr = process.communicate(timeout=timeout_left)
        assert process.returncode == 0, stderr.decode()
        messages = [json.loads(line) for line in (first_line + second_line + remaining).splitlines()]
        assert any(message.get("id") == 1 and "result" in message for message in messages)
        assert any(message.get("id") == 2 and "result" in message for message in messages)
        for diagnostic in (b"late loop diagnostic", b"late retained stdout diagnostic",
                           b"late fd diagnostic"):
            assert diagnostic in stderr
        if outcome == "cancel":
            assert b"propagated CancelledError" in stderr
        elif outcome == "failure":
            assert b"propagated RuntimeError" in stderr
        else:
            assert b"propagated " not in stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_windows_stdout_handle_rebound_for_diversion_and_restoration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_stdout = os.fstat(1)
    stderr = os.fstat(2)
    rebound: list[os.stat_result] = []
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        patch.setattr(mcp_stdio, "_snapshot_windows_stdout", lambda: (1234, True))
        patch.setattr(mcp_stdio, "_rebind_windows_stdout", lambda: rebound.append(os.fstat(1)), raising=False)
        with mcp_stdio._protect_stdout():
            assert os.fstat(1) == stderr
        assert os.fstat(1) == old_stdout
    assert rebound == [stderr, old_stdout]


def test_nested_windows_stdout_protection_keeps_wire_and_restores_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_stdout = os.fstat(1)
    stderr = os.fstat(2)
    rebound: list[os.stat_result] = []
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        patch.setattr(mcp_stdio, "_snapshot_windows_stdout", lambda: (1234, True))
        patch.setattr(mcp_stdio, "_rebind_windows_stdout", lambda: rebound.append(os.fstat(1)))
        with mcp_stdio._protect_stdout():
            wire = sys.stdout.buffer
            with mcp_stdio._protect_stdout():
                assert sys.stdout.buffer is wire
                assert os.fstat(1) == stderr
            assert os.fstat(1) == stderr
        assert os.fstat(1) == old_stdout
    assert rebound == [stderr, old_stdout]


def test_windows_rebind_failure_restores_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    old_fd = os.fstat(1)
    old_stdout = sys.stdout
    calls = []

    def rebind() -> None:
        calls.append(os.fstat(1))
        if len(calls) == 1:
            raise OSError("SetStdHandle failed")

    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        patch.setattr(mcp_stdio, "_snapshot_windows_stdout", lambda: (1234, True))
        patch.setattr(mcp_stdio, "_rebind_windows_stdout", rebind, raising=False)
        with pytest.raises(OSError, match="SetStdHandle failed"), mcp_stdio._protect_stdout():
            pass
        assert sys.stdout is old_stdout
        assert os.fstat(1) == old_fd
    assert calls == [os.fstat(2), old_fd]


def test_windows_rebind_updates_native_handle_from_current_crt_fd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    class SetStdHandle:
        def __call__(self, slot, handle):
            calls.append((slot, handle))
            return 1

    kernel = SimpleNamespace(GetStdHandle=lambda _slot: 1234, SetStdHandle=SetStdHandle())
    fake_msvcrt = SimpleNamespace(get_osfhandle=lambda fd: 1234 if fd == 1 else -1)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        patch.setitem(sys.modules, "msvcrt", fake_msvcrt)
        patch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
        with mcp_stdio._protect_stdout():
            pass
    assert calls == [(-11, 1234), (-11, 1234)]


@pytest.mark.parametrize("native_initial", [100, 999], ids=["same-as-crt", "distinct"])
@pytest.mark.parametrize("fail_diversion", [False, True], ids=["normal", "diversion-error"])
def test_windows_restores_original_native_stdout_handle(
    monkeypatch: pytest.MonkeyPatch, native_initial: int, fail_diversion: bool,
) -> None:
    current_native = [native_initial]
    native_calls = []
    crt_calls = []
    old_stdout = os.fstat(1)

    class GetStdHandle:
        def __call__(self, slot):
            assert slot == -11
            return current_native[0]

    class SetStdHandle:
        def __call__(self, slot, handle):
            native_calls.append((slot, handle))
            if fail_diversion and len(native_calls) == 1:
                return 0
            current_native[0] = handle
            return 1

    def crt_handle(fd):
        assert fd == 1
        crt_calls.append(fd)
        return [100, 200, 300][len(crt_calls) - 1]

    kernel = SimpleNamespace(GetStdHandle=GetStdHandle(), SetStdHandle=SetStdHandle())
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        patch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=crt_handle))
        patch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
        patch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
        patch.setattr(ctypes, "WinError", lambda code: OSError(f"SetStdHandle: {code}"), raising=False)
        if fail_diversion:
            with pytest.raises(OSError, match="SetStdHandle: 5"), mcp_stdio._protect_stdout():
                pass
        else:
            with mcp_stdio._protect_stdout():
                assert current_native[0] == 200
        assert os.fstat(1) == old_stdout
    # dup2 closes the original CRT handle. When the native slot shared it,
    # restoration must use the restored CRT handle, not the now-invalid 100.
    expected = 300 if native_initial == 100 else native_initial
    assert current_native[0] == expected
    assert native_calls == [(-11, 200), (-11, expected)]


def test_windows_native_stdout_snapshot_failure_leaves_stdout_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_stdout = os.fstat(1)
    calls = []

    class GetStdHandle:
        def __call__(self, _slot):
            return ctypes.c_void_p(-1).value

    class SetStdHandle:
        def __call__(self, *args):
            calls.append(args)
            return 1

    kernel = SimpleNamespace(GetStdHandle=GetStdHandle(), SetStdHandle=SetStdHandle())
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        patch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda _: 1234))
        patch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
        patch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
        patch.setattr(ctypes, "WinError", lambda code: OSError(f"GetStdHandle: {code}"), raising=False)
        with pytest.raises(OSError, match="GetStdHandle: 5"), mcp_stdio._protect_stdout():
            pass
        assert os.fstat(1) == old_stdout
    assert calls == []


def test_windows_restores_distinct_native_handle_when_crt_restoration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_stdout_fd = os.dup(1)
    original_dup2 = os.dup2
    native = [999]
    calls = []

    class SetStdHandle:
        def __call__(self, slot, handle):
            calls.append((slot, handle))
            native[0] = handle
            return 1

    def dup2(source: int, target: int) -> int:
        if source != 2 and target == 1:
            raise OSError("CRT restoration failed")
        return original_dup2(source, target)

    kernel = SimpleNamespace(GetStdHandle=lambda _slot: 999, SetStdHandle=SetStdHandle())
    try:
        with monkeypatch.context() as patch:
            patch.setattr(sys, "platform", "win32")
            patch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda _: 100))
            patch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
            patch.setattr(os, "dup2", dup2)
            with pytest.raises(OSError, match="CRT restoration failed"), mcp_stdio._protect_stdout():
                assert native[0] == 100
        assert native[0] == 999
        assert calls == [(-11, 100), (-11, 999)]
    finally:
        original_dup2(old_stdout_fd, 1)
        os.close(old_stdout_fd)


def test_windows_native_rebind_error_fails_closed_and_restores_fd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_stdout = os.fstat(1)
    calls = []

    class SetStdHandle:
        def __call__(self, slot, handle):
            calls.append((slot, handle))
            return len(calls) != 1

    kernel = SimpleNamespace(GetStdHandle=lambda _slot: 1234, SetStdHandle=SetStdHandle())
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        patch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda _: 1234))
        patch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
        patch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
        patch.setattr(ctypes, "WinError", lambda code: OSError(f"SetStdHandle: {code}"), raising=False)
        with pytest.raises(OSError, match="SetStdHandle: 5"), mcp_stdio._protect_stdout():
            pass
        assert os.fstat(1) == old_stdout
    assert calls == [(-11, 1234), (-11, 1234)]


def test_windows_restoration_failure_does_not_replace_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def rebind() -> None:
        calls.append(os.fstat(1))
        if len(calls) == 2:
            raise OSError("restore failed")

    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        patch.setattr(mcp_stdio, "_snapshot_windows_stdout", lambda: (1234, True))
        patch.setattr(mcp_stdio, "_rebind_windows_stdout", rebind, raising=False)
        with pytest.raises(ValueError, match="server failed"), mcp_stdio._protect_stdout():
            raise ValueError("server failed")
    assert len(calls) == 2


def test_stdio_preserves_protocol_and_routes_diagnostics_to_stderr() -> None:
    pytest.importorskip("mcp.server.mcpserver")
    code = textwrap.dedent('''
        import asyncio
        import os
        import subprocess
        from types import SimpleNamespace
        from mcp.server.mcpserver import MCPServer
        from doc3gpp.settings.schema import Settings
        from doc3gpp.web import mcp_stdio
        from doc3gpp.web.state import JobWorkerHandle
        import sys

        old_stdout = sys.stdout
        import builtins
        original_import = builtins.__import__
        def diagnostic_import(name, *args, **kwargs):
            if name == "doc3gpp.services.pipeline_runtime":
                print("pipeline import diagnostic")
            return original_import(name, *args, **kwargs)
        builtins.__import__ = diagnostic_import
        settings = Settings()
        trigger = asyncio.Event()
        printed = asyncio.Event()
        class Worker:
            def __init__(self, state): pass
            async def run(self):
                await trigger.wait()
                print("worker buffered diagnostic")
                print("saved stdout diagnostic", file=old_stdout)
                os.write(1, b"worker fd diagnostic\\n")
                subprocess.run([sys.executable, "-c", "print('child diagnostic')"], check=True)
                printed.set()
                await asyncio.Event().wait()
        class Server:
            async def run_stdio_async(self):
                server = MCPServer("stdio-test")
                @server.tool()
                async def trigger_worker() -> str:
                    trigger.set()
                    await printed.wait()
                    return "done"
                await server.run_stdio_async()
        def build_state(settings):
            print("startup diagnostic")
            os.write(1, b"startup fd diagnostic\\n")
            engine = SimpleNamespace(dispose=lambda: None)
            return SimpleNamespace(settings=settings, services=SimpleNamespace(pipeline=None),
                jobs=None, engine=engine, testcase_engine=engine, specdata_engine=None,
                close_docx_pool=lambda: print("teardown diagnostic"), close_embedder=None)
        mcp_stdio._load_dependencies = lambda: mcp_stdio._StdioDependencies(
            build_state=build_state, build_mcp_server=lambda state: Server(),
            worker_type=Worker, handle_type=JobWorkerHandle)
        asyncio.run(mcp_stdio.run_stdio(settings))
    ''')
    process = subprocess.Popen(
        [sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env={
            **os.environ, "PYTHONUNBUFFERED": "0",
            "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
        },
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        }}
        process.stdin.write((json.dumps(request) + "\n").encode())
        process.stdin.flush()
        def read_line() -> bytes:
            lines: queue.Queue[bytes] = queue.Queue()
            Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
            try:
                line = lines.get(timeout=10)
            except queue.Empty:
                pytest.fail("server did not produce a protocol response")
            assert line, "server closed stdout without a protocol response"
            return line

        first_line = read_line()
        for message in (
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "trigger_worker", "arguments": {}}},
        ):
            process.stdin.write((json.dumps(message) + "\n").encode())
        process.stdin.flush()
        second_line = read_line()
        remaining, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr.decode()
        messages = [json.loads(line) for line in (first_line + second_line + remaining).splitlines()]
        assert any(message.get("id") == 1 and "result" in message for message in messages)
        assert any(message.get("id") == 2 and "result" in message for message in messages)
        for diagnostic in (b"pipeline import diagnostic", b"startup diagnostic",
                           b"startup fd diagnostic",
                           b"worker buffered diagnostic", b"worker fd diagnostic",
                           b"saved stdout diagnostic", b"child diagnostic",
                           b"teardown diagnostic"):
            assert diagnostic in stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
