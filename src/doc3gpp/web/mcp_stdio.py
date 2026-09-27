from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
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
            'with `pip install "doc3gpp[web]"`.'
        ) from exc
    return _StdioDependencies(
        build_state=build_state,
        build_mcp_server=build_mcp_server,
        worker_type=JobWorker,
        handle_type=JobWorkerHandle,
    )


async def run_stdio(settings: Settings) -> None:
    """Run the shared doc3gpp MCP server over stdio until client EOF."""
    deps = _load_dependencies()
    state = deps.build_state(settings)
    handle: JobWorkerHandle | None = None
    try:
        handle = deps.handle_type(
            max_concurrent_jobs=settings.server.max_concurrent_jobs,
        )
        state.jobs = handle
        worker = deps.worker_type(state)
        handle.task = asyncio.create_task(worker.run())
        server = deps.build_mcp_server(state)
        await server.run_stdio_async()
    finally:
        if handle is not None:
            await handle.shutdown()
        state.engine.dispose()
        state.testcase_engine.dispose()
        if state.specdata_engine is not None:
            state.specdata_engine.dispose()


__all__ = ["MCPStdioDependencyError", "run_stdio"]
