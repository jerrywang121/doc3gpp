"""Tests for the FastAPI app factory + lifespan wiring."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from doc3gpp.services.pipeline_runtime import PipelineRuntime
from doc3gpp.settings.schema import Settings
from doc3gpp.web.app import ServiceContainer, WebState, build_app
from doc3gpp.web.state import JobWorkerHandle


def test_build_app_returns_fastapi_instance() -> None:
    """``build_app(Settings())`` returns a configured :class:`FastAPI`."""
    app = build_app(Settings())
    assert isinstance(app, FastAPI)


def test_build_app_default_server_enabled_on_loopback(sqlite_env) -> None:
    """Default :class:`Settings` enables the server on the loopback port."""
    app = build_app(Settings())
    with TestClient(app):
        state: WebState = app.state.web
        assert isinstance(state, WebState)
        assert state.settings.server.enabled is True
        assert state.settings.server.host == "127.0.0.1"
        assert state.settings.server.port == 13999


def test_build_state_wires_service_container(sqlite_env) -> None:
    """Lifespan-built :class:`WebState` carries a :class:`ServiceContainer`."""
    app = build_app(Settings())
    with TestClient(app):
        state: WebState = app.state.web
        assert isinstance(state.services, ServiceContainer)
        assert state.services.meeting is not None
        assert state.services.tdoc is not None
        assert state.services.tdoc_cr is not None
        assert state.services.wi is not None
        assert state.services.tdoc_file_repo is not None
        assert state.services.tdoc_search is not None
        assert state.services.spec_doc_search_facade is not None
        assert state.services.tdoc_index is not None
        assert state.services.spec_doc_index is not None
        assert state.services.job_repo is not None


def test_web_runtime_uses_pipeline_workers_independently_from_active_job_limit(sqlite_env) -> None:
    settings = Settings(
        pipeline={"prepare_workers": 3},
        server={"max_concurrent_jobs": 1},
    )
    app = build_app(settings)

    with TestClient(app):
        state: WebState = app.state.web
        runtime = state.jobs.pipeline
        assert isinstance(runtime, PipelineRuntime)
        assert runtime is state.services.pipeline
        assert runtime._prepare._max_workers == 3
        assert state.jobs.max_concurrent_jobs == 1


def test_job_worker_handle_awaits_pipeline_async_shutdown() -> None:
    class _ShutdownProbe:
        def __init__(self) -> None:
            self.called = False

        async def shutdown_async(self, *, cancel_pending_preparation: bool = False) -> None:
            assert cancel_pending_preparation is True
            self.called = True

        def shutdown(self, **_kwargs) -> None:
            raise AssertionError("blocking pipeline shutdown must not be used")

    pipeline = _ShutdownProbe()
    asyncio.run(JobWorkerHandle(pipeline=pipeline).shutdown())  # type: ignore[arg-type]
    assert pipeline.called is True


def test_healthz_returns_ok(sqlite_env) -> None:
    """``GET /healthz`` returns 200 with ``{"ok": True}``."""
    app = build_app(Settings())
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_build_state_shares_one_embedder(sqlite_env) -> None:
    from unittest.mock import MagicMock

    from doc3gpp.services import factory
    from doc3gpp.settings.schema import Settings
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.web.app import build_state

    settings = Settings()
    create_schema()
    fake_embedder = MagicMock()
    build_calls = []

    def build_embedder(_settings):
        build_calls.append(True)
        return fake_embedder

    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(factory, "build_embedder", build_embedder)
    try:
        state = build_state(settings)
        assert build_calls == []
        tdoc_embedder = state.services.semantic_search._embedder
        spec_doc_embedder = state.services.spec_doc_semantic._embedder
    finally:
        monkeypatch.undo()
    assert build_calls == [True]
    assert tdoc_embedder is fake_embedder
    assert spec_doc_embedder is fake_embedder
    assert state.services.spec_doc._embedder is None


def test_lifespan_closes_shared_embedder_on_shutdown(sqlite_env, monkeypatch) -> None:
    from unittest.mock import MagicMock

    from doc3gpp.services import factory
    from doc3gpp.storage.db.migrate import create_schema

    create_schema()
    fake_embedder = MagicMock()
    monkeypatch.setattr(factory, "build_embedder", lambda _settings: fake_embedder)
    app = build_app(Settings())
    with TestClient(app):
        assert app.state.web.services.semantic_search._embedder is fake_embedder
    fake_embedder.close.assert_called_once_with()


def test_build_state_shares_one_docx_pool_with_spec_doc_and_tdoc_cr(sqlite_env, monkeypatch) -> None:
    from doc3gpp.services.docx_convert_pool import DocxConvertPool
    from doc3gpp.web import app as web_app

    built: list[DocxConvertPool] = []

    class RecordingPool(DocxConvertPool):
        def __init__(self, workers: int) -> None:
            super().__init__(workers)
            self.workers = workers
            built.append(self)

    monkeypatch.setattr(web_app, "DocxConvertPool", RecordingPool)
    settings = Settings(pipeline={"docx_workers": 3})

    state = web_app.build_state(settings)

    assert len(built) == 1
    assert built[0].workers == 3
    assert state.close_docx_pool == built[0].close
    assert state.services.spec_doc._docx_pool is built[0]
    assert state.services.tdoc_cr._docx_pool is built[0]


def test_lifespan_closes_docx_pool_after_runtime_drains(sqlite_env, monkeypatch) -> None:
    from doc3gpp.web import app as web_app

    close_calls: list[bool] = []
    real_build_state = web_app.build_state

    def build_state_with_recording_close(settings: Settings) -> WebState:
        state = real_build_state(settings)

        def close_docx_pool() -> None:
            close_calls.append(state.jobs.task.done())

        state.close_docx_pool = close_docx_pool
        return state

    monkeypatch.setattr(web_app, "build_state", build_state_with_recording_close)
    app = build_app(Settings())
    with TestClient(app):
        pass

    assert close_calls == [True]


def test_build_state_wires_testcase_engine(sqlite_env) -> None:
    """``build_state`` carries the shared testcase engine on ``WebState``."""
    from doc3gpp.settings.schema import Settings
    from doc3gpp.storage.db.session import get_testcase_engine
    from doc3gpp.web.app import build_state

    state = build_state(Settings())
    assert state.testcase_engine is get_testcase_engine()
    assert state.testcase_engine is not state.engine
