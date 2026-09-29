from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import ClassVar

import pytest

from doc3gpp.settings.schema import Settings
from doc3gpp.web import mcp_stdio


class _FakeEngine:
    def __init__(self) -> None:
        self.dispose_calls = 0

    def dispose(self) -> None:
        self.dispose_calls += 1


@dataclass
class _FakeState:
    settings: Settings
    engine: _FakeEngine
    testcase_engine: _FakeEngine
    specdata_engine: _FakeEngine | None
    jobs: object | None = None


class _FakeHandle:
    instances: ClassVar[list[_FakeHandle]] = []

    def __init__(self, *, max_concurrent_jobs: int) -> None:
        self.max_concurrent_jobs = max_concurrent_jobs
        self.task: asyncio.Task | None = None
        self.shutdown_calls = 0
        self.__class__.instances.append(self)

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class _FakeWorker:
    def __init__(self, state: _FakeState) -> None:
        self.state = state

    async def run(self) -> None:
        await asyncio.Event().wait()


class _FakeServer:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    async def run_stdio_async(self) -> None:
        self.events.append("stdio")
        if self.fail:
            raise RuntimeError("stdio failed")


def _dependencies(
    state: _FakeState,
    events: list[str],
    *,
    fail_build: bool = False,
    fail_server: bool = False,
) -> mcp_stdio._StdioDependencies:
    def build_server(_state: _FakeState) -> _FakeServer:
        if fail_build:
            raise RuntimeError("server build failed")
        return _FakeServer(events, fail=fail_server)

    return mcp_stdio._StdioDependencies(
        build_state=lambda _settings: state,
        build_mcp_server=build_server,
        worker_type=_FakeWorker,
        handle_type=_FakeHandle,
    )


def _state(settings: Settings, *, with_specdata: bool = True) -> _FakeState:
    return _FakeState(
        settings=settings,
        engine=_FakeEngine(),
        testcase_engine=_FakeEngine(),
        specdata_engine=_FakeEngine() if with_specdata else None,
    )


def test_run_stdio_starts_server_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings()
    state = _state(settings)
    events: list[str] = []
    _FakeHandle.instances.clear()
    monkeypatch.setattr(
        mcp_stdio,
        "_load_dependencies",
        lambda: _dependencies(state, events),
    )

    asyncio.run(mcp_stdio.run_stdio(settings))

    assert events == ["stdio"]
    assert state.jobs is _FakeHandle.instances[0]
    assert _FakeHandle.instances[0].shutdown_calls == 1
    assert state.engine.dispose_calls == 1
    assert state.testcase_engine.dispose_calls == 1
    assert state.specdata_engine is not None
    assert state.specdata_engine.dispose_calls == 1


def test_run_stdio_cleans_up_when_server_execution_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    state = _state(settings)
    events: list[str] = []
    _FakeHandle.instances.clear()
    monkeypatch.setattr(
        mcp_stdio,
        "_load_dependencies",
        lambda: _dependencies(state, events, fail_server=True),
    )

    with pytest.raises(RuntimeError, match="stdio failed"):
        asyncio.run(mcp_stdio.run_stdio(settings))

    assert _FakeHandle.instances[0].shutdown_calls == 1
    assert state.engine.dispose_calls == 1
    assert state.testcase_engine.dispose_calls == 1
    assert state.specdata_engine is not None
    assert state.specdata_engine.dispose_calls == 1


def test_run_stdio_cleans_up_when_server_build_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    state = _state(settings)
    events: list[str] = []
    _FakeHandle.instances.clear()
    monkeypatch.setattr(
        mcp_stdio,
        "_load_dependencies",
        lambda: _dependencies(state, events, fail_build=True),
    )

    with pytest.raises(RuntimeError, match="server build failed"):
        asyncio.run(mcp_stdio.run_stdio(settings))

    assert _FakeHandle.instances[0].shutdown_calls == 1
    assert state.engine.dispose_calls == 1
    assert state.testcase_engine.dispose_calls == 1
    assert state.specdata_engine is not None
    assert state.specdata_engine.dispose_calls == 1


def test_run_stdio_cleans_up_when_handle_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    state = _state(settings)

    class _FailingHandle:
        def __init__(self, *, max_concurrent_jobs: int) -> None:
            del max_concurrent_jobs
            raise RuntimeError("handle construction failed")

    dependencies = _dependencies(state, [])
    dependencies = mcp_stdio._StdioDependencies(
        build_state=dependencies.build_state,
        build_mcp_server=dependencies.build_mcp_server,
        worker_type=dependencies.worker_type,
        handle_type=_FailingHandle,
    )
    monkeypatch.setattr(mcp_stdio, "_load_dependencies", lambda: dependencies)

    with pytest.raises(RuntimeError, match="handle construction failed"):
        asyncio.run(mcp_stdio.run_stdio(settings))

    assert state.engine.dispose_calls == 1
    assert state.testcase_engine.dispose_calls == 1
    assert state.specdata_engine is not None
    assert state.specdata_engine.dispose_calls == 1


def test_run_stdio_cleans_up_when_specdata_engine_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    state = _state(settings, with_specdata=False)
    events: list[str] = []
    _FakeHandle.instances.clear()
    monkeypatch.setattr(
        mcp_stdio,
        "_load_dependencies",
        lambda: _dependencies(state, events),
    )

    asyncio.run(mcp_stdio.run_stdio(settings))

    assert events == ["stdio"]
    assert _FakeHandle.instances[0].shutdown_calls == 1
    assert state.engine.dispose_calls == 1
    assert state.testcase_engine.dispose_calls == 1
    assert state.specdata_engine is None


def test_dependency_error_happens_before_state_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()

    def fail_dependencies() -> mcp_stdio._StdioDependencies:
        raise mcp_stdio.MCPStdioDependencyError("install the web extra")

    monkeypatch.setattr(mcp_stdio, "_load_dependencies", fail_dependencies)

    with pytest.raises(mcp_stdio.MCPStdioDependencyError, match="web extra"):
        asyncio.run(mcp_stdio.run_stdio(settings))
