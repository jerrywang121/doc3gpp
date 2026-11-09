from __future__ import annotations

import asyncio
import importlib
import os
import sys
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from doc3gpp.settings.schema import Settings
    from doc3gpp.web.state import JobWorkerHandle, WebState
    from doc3gpp.web.workers.job_worker import JobWorker


class MCPStdioDependencyError(RuntimeError):
    """Raised when the optional web/MCP dependencies are unavailable."""


@dataclass(frozen=True, slots=True)
class _StdioDependencies:
    build_state: Callable[[Settings], WebState]
    build_mcp_server: Callable[[WebState], Any]
    worker_type: type[JobWorker]
    handle_type: type[JobWorkerHandle]


def _load_dependencies() -> _StdioDependencies:
    """Load the optional web stack only when the stdio command is invoked."""
    try:
        importlib.import_module("mcp.server.mcpserver")
        from doc3gpp.web.app import build_state
        from doc3gpp.web.mcp_server import build_mcp_server
        from doc3gpp.web.state import JobWorkerHandle
        from doc3gpp.web.workers.job_worker import JobWorker
    except ImportError as exc:
        raise MCPStdioDependencyError(
            "MCP stdio requires the optional web dependencies; install them "
            'with `pip install "doc3gpp[web]"`. If MCP is already installed, '
            "upgrade it with `pip install --upgrade mcp`."
        ) from exc
    return _StdioDependencies(
        build_state=build_state,
        build_mcp_server=build_mcp_server,
        worker_type=JobWorker,
        handle_type=JobWorkerHandle,
    )


class _ProtocolStdout:
    """Send Python diagnostics to stderr while exposing the wire to the SDK."""

    def __init__(self, wire: Any) -> None:
        self.buffer = wire

    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def flush(self) -> None:
        sys.stderr.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(sys.stderr, name)


def _snapshot_windows_stdout() -> tuple[int | None, bool]:
    """Record the native stdout slot and whether it is owned by CRT fd 1."""
    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_std_handle = kernel32.GetStdHandle
    get_std_handle.argtypes = [ctypes.c_uint32]
    get_std_handle.restype = ctypes.c_void_p
    native = get_std_handle(-11)
    if native == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return native, native == msvcrt.get_osfhandle(1)


def _set_windows_stdout(handle: int | None) -> None:
    """Set the Win32 stdout slot without depending on optional SDK packages."""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_std_handle = kernel32.SetStdHandle
    set_std_handle.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    set_std_handle.restype = ctypes.c_int
    if not set_std_handle(-11, handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _rebind_windows_stdout() -> None:
    """Point the Win32 stdout slot at the current CRT fd 1 handle."""
    import msvcrt

    _set_windows_stdout(msvcrt.get_osfhandle(1))


@contextmanager
def _protect_stdout() -> Iterator[None]:
    """Reserve stdout for MCP, including during startup and teardown.

    The SDK reads sys.stdout.buffer as its wire. Give it a duplicate of the
    original fd 1, but divert fd 1 and ordinary Python writes to stderr for
    the entire lifecycle, including buffered writes and child processes.
    """
    # The CLI owns the outer scope through asyncio.run() teardown.
    if isinstance(sys.stdout, _ProtocolStdout):
        yield
        return

    original = sys.stdout
    saved_fd = os.dup(1)
    wire = None
    try:
        windows_stdout = _snapshot_windows_stdout() if sys.platform == "win32" else None
        buffer = getattr(original, "buffer", None)
        try:
            backed_by_fd1 = buffer is not None and buffer.fileno() == 1
        except (AttributeError, OSError, ValueError):
            backed_by_fd1 = False
        if backed_by_fd1:
            wire = os.fdopen(os.dup(saved_fd), "wb", buffering=0)
            buffer = wire
        os.dup2(2, 1)
        try:
            if sys.platform == "win32":
                _rebind_windows_stdout()
            # Flush any output buffered before entering the runtime to stderr.
            original.flush()
            sys.stdout = _ProtocolStdout(buffer)
            yield
        finally:
            active_error = sys.exc_info()[1]
            try:
                try:
                    sys.stdout.flush()
                finally:
                    # Code may retain the original stream and buffer writes on it.
                    original.flush()
            except Exception:
                if active_error is None:
                    raise
            finally:
                prior_error = sys.exc_info()[1]
                sys.stdout = original
                try:
                    try:
                        os.dup2(saved_fd, 1)
                    except Exception:
                        # A distinct native handle survives even if CRT restoration fails.
                        if windows_stdout is not None and not windows_stdout[1]:
                            with suppress(Exception):
                                _set_windows_stdout(windows_stdout[0])
                        raise
                    if windows_stdout is not None:
                        native_handle, shared_with_crt = windows_stdout
                        if shared_with_crt:
                            # dup2 closed the old CRT handle; fd 1 now owns its duplicate.
                            _rebind_windows_stdout()
                        else:
                            _set_windows_stdout(native_handle)
                except Exception as exc:
                    if prior_error is None:
                        raise
                    with suppress(Exception):
                        print(f"MCP stdio stdout restoration failed: {exc!r}", file=sys.stderr)
    finally:
        if wire is not None:
            wire.close()
        os.close(saved_fd)


class _StdioWorkerTask(asyncio.Task):
    """Track cancellation requests on Python versions without Task.cancelling()."""

    def __init__(self, coroutine: Any) -> None:
        super().__init__(coroutine)
        self.stop_requested = False

    def cancel(self, msg: Any = None) -> bool:
        requested = super().cancel(msg)
        if requested:
            self.stop_requested = True
        return requested


def _run_stdio_sync(settings: Settings) -> None:
    """Keep the protocol wire protected until asyncio has shut down."""
    with _protect_stdout():
        asyncio.run(run_stdio(settings))


async def run_stdio(settings: Settings) -> None:
    """Run the shared doc3gpp MCP server over stdio until client EOF."""
    with _protect_stdout():
        from doc3gpp.services.pipeline_runtime import PipelineRuntime

        deps = _load_dependencies()
        from doc3gpp.web.workers.handlers import _worker_executor

        state = deps.build_state(settings)
        handle: JobWorkerHandle | None = None
        worker_executor: ThreadPoolExecutor | None = None
        try:
            handle = deps.handle_type(
                max_concurrent_jobs=settings.server.max_concurrent_jobs,
            )
            state.jobs = handle
            server = deps.build_mcp_server(state)
            run_stdio_async = getattr(server, "run_stdio_async", None)
            if not callable(run_stdio_async):
                raise MCPStdioDependencyError(
                    "The installed MCP SDK does not support stdio; "
                    "upgrade it with `pip install --upgrade mcp`."
                )
            handle.pipeline = PipelineRuntime(prepare_workers=settings.pipeline.prepare_workers)
            state.services.pipeline = handle.pipeline
            worker = deps.worker_type(state)
            worker_executor = ThreadPoolExecutor(thread_name_prefix="doc3gpp-stdio-worker")
            token = _worker_executor.set(worker_executor)
            try:
                handle.task = _StdioWorkerTask(worker.run())
            finally:
                _worker_executor.reset(token)
            await run_stdio_async()
        finally:
            active_error = sys.exc_info()[1]
            cleanup_error: BaseException | None = None
            cancellation: asyncio.CancelledError | None = None

            def record_error(exc: BaseException) -> None:
                nonlocal cleanup_error, cancellation
                if isinstance(exc, asyncio.CancelledError):
                    cancellation = exc
                if cleanup_error is None:
                    cleanup_error = exc
                if active_error is not None or cleanup_error is not exc:
                    with suppress(Exception):
                        print(f"MCP stdio cleanup failed: {exc!r}", file=sys.stderr)

            async def finish_cleanup(awaitable: Any) -> None:
                # Cancellation of the caller must not abandon resource cleanup.
                pending = asyncio.ensure_future(awaitable)
                while True:
                    try:
                        await asyncio.shield(pending)
                        return
                    except asyncio.CancelledError as exc:
                        if pending.cancelled():
                            raise
                        record_error(exc)

            if handle is not None:
                # Stop polling once, then let the worker drain its child handlers.
                # A second cancellation would propagate into gather and detach
                # threads awaited by those handlers.
                for event in getattr(handle, "cancel_events", {}).values():
                    event.set()
                task = getattr(handle, "task", None)
                if task is not None and not task.done():
                    if not getattr(task, "stop_requested", False):
                        task.cancel()
                    try:
                        await finish_cleanup(asyncio.gather(task, return_exceptions=True))
                    except BaseException as exc:  # noqa: BLE001
                        record_error(exc)

                # The shared HTTP handle closes the pipeline after a bounded
                # wait. Detach it here: stdio owns its unbounded shutdown.
                pipeline = getattr(handle, "pipeline", None)
                if pipeline is not None:
                    handle.pipeline = None
                try:
                    await finish_cleanup(handle.shutdown())
                except BaseException as exc:  # noqa: BLE001
                    record_error(exc)
                finally:
                    if pipeline is not None:
                        handle.pipeline = pipeline

            # A cancelled handler can leave a blocking call running. Never
            # dispose resources unless both the worker and pipeline drained.
            # A failed submit can even have queued work before raising, so do
            # not synchronously join its executor on the event-loop thread.
            task = getattr(handle, "task", None) if handle is not None else None
            drained = task is None or task.done()
            pipeline = getattr(handle, "pipeline", None) if handle is not None else None
            if drained and (worker_executor is not None or pipeline is not None):
                from functools import partial

                cleanup_executor = None
                try:
                    cleanup_executor = ThreadPoolExecutor(max_workers=1)
                    if worker_executor is not None:
                        await finish_cleanup(asyncio.get_running_loop().run_in_executor(
                            cleanup_executor, worker_executor.shutdown,
                        ))
                    if pipeline is not None:
                        await finish_cleanup(asyncio.get_running_loop().run_in_executor(
                            cleanup_executor,
                            partial(pipeline.shutdown, wait=True, cancel_pending_preparation=True),
                        ))
                except BaseException as exc:  # noqa: BLE001
                    drained = False
                    record_error(exc)
                finally:
                    if cleanup_executor is not None:
                        try:
                            cleanup_executor.shutdown(wait=False)
                        except BaseException as exc:  # noqa: BLE001
                            drained = False
                            record_error(exc)
            if drained:
                for cleanup in (
                    state.close_docx_pool,
                    getattr(state, "close_embedder", None),
                    state.engine.dispose,
                    state.testcase_engine.dispose,
                    state.specdata_engine.dispose if state.specdata_engine is not None else None,
                ):
                    if cleanup is not None:
                        try:
                            cleanup()
                        except BaseException as exc:  # noqa: BLE001 - cleanup must continue on cancellation
                            record_error(exc)
            if cancellation is not None and not isinstance(active_error, asyncio.CancelledError):
                raise cancellation
            if active_error is None and cleanup_error is not None:
                raise cleanup_error


__all__ = ["MCPStdioDependencyError", "run_stdio"]
