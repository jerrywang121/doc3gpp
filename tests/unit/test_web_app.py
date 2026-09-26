"""Tests for the FastAPI app factory + lifespan wiring."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from doc3gpp.settings.schema import Settings
from doc3gpp.web.app import ServiceContainer, WebState, build_app


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
        assert state.services.job_repo is not None


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
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.web.app import build_state
    from doc3gpp.settings.schema import Settings

    settings = Settings()
    create_schema()
    fake_embedder = MagicMock()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(factory, "build_embedder", lambda s: fake_embedder)
    try:
        state = build_state(settings)
    finally:
        monkeypatch.undo()
    assert state.services.search._reranker._embedder is fake_embedder
    assert state.services.semantic_search._embedder is fake_embedder
    assert state.services.tdoc_cr._semantic_service._embedder is fake_embedder


def test_build_state_wires_testcase_engine(sqlite_env) -> None:
    """``build_state`` carries the shared testcase engine on ``WebState``."""
    from doc3gpp.storage.db.session import get_testcase_engine
    from doc3gpp.web.app import build_state
    from doc3gpp.settings.schema import Settings

    state = build_state(Settings())
    assert state.testcase_engine is get_testcase_engine()
    assert state.testcase_engine is not state.engine
