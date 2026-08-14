"""Tests for ``POST /jobs/parse/tdoc-url`` (closes the MCP-vs-HTTP gap)."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from doc3gpp.models.jobs import JobKind
from doc3gpp.storage.db.base import Base
from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository
from doc3gpp.web.app import build_app
from doc3gpp.web.deps import get_job_repo, get_job_worker
from doc3gpp.web.state import JobWorkerHandle


class _NoopWorkerHandle(JobWorkerHandle):
    def register_queue(self, job_id, queue=None):
        pass

    def unregister_queue(self, job_id):
        pass

    def cancel(self, job_id):
        return False


def _make_repo() -> SQLAlchemyJobRepository:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    repo = SQLAlchemyJobRepository()
    repo._session_factory = Session  # type: ignore[attr-defined]
    return repo


@pytest.fixture()
def client(sqlite_env: Any):
    repo = _make_repo()
    handle = _NoopWorkerHandle()
    app: FastAPI = build_app()
    app.dependency_overrides[get_job_repo] = lambda: repo
    app.dependency_overrides[get_job_worker] = lambda: handle
    with TestClient(app) as c:
        yield c, repo


def test_post_parse_tdoc_url_happy_path(client: Any) -> None:
    c, repo = client
    r = c.post(
        "/jobs/parse/tdoc-url",
        json={
            "url": "https://www.3gpp.org/ftp/TSG_RAN/WG5_Radio/TSGR5_99/Docs/",
            "max_depth": 2,
            "force": False,
            "full": False,
        },
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert body["job_id"]
    job = repo.get(body["job_id"])
    assert job is not None
    assert job.kind is JobKind.PARSE_TDOC_URL
    assert job.params["url"].startswith("https://www.3gpp.org/ftp/")
    assert job.params["recursive"] is False
    assert job.params["max_depth"] == 2
    assert "max_depth" in job.params  # omitted when recursive=True


def test_post_parse_tdoc_url_rejects_non_3gpp_url(client: Any) -> None:
    c, _ = client
    r = c.post(
        "/jobs/parse/tdoc-url",
        json={"url": "https://example.com/evil/"},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_filter"


def test_post_parse_tdoc_url_xor_recursive_max_depth(client: Any) -> None:
    c, _ = client
    r = c.post(
        "/jobs/parse/tdoc-url",
        json={
            "url": "https://www.3gpp.org/ftp/TSG_RAN/WG5_Radio/TSGR5_99/Docs/",
            "recursive": True,
            "max_depth": 3,
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_filter"


def test_post_parse_tdoc_url_recursive_omits_max_depth(client: Any) -> None:
    c, repo = client
    r = c.post(
        "/jobs/parse/tdoc-url",
        json={
            "url": "https://www.3gpp.org/ftp/TSG_RAN/WG5_Radio/TSGR5_99/Docs/",
            "recursive": True,
        },
    )
    assert r.status_code == 202
    job = repo.get(r.json()["job_id"])
    assert job.params["recursive"] is True
    assert "max_depth" not in job.params
