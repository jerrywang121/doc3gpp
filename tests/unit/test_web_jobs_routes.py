"""Tests for the job HTTP routes (:mod:`doc3gpp.web.routes.jobs`).

These are unit tests: they exercise the POST / poll / SSE / cancel
surface against an in-memory sqlite ``jobs`` table wired through a
``get_job_repo`` dependency override, and a fake ``JobWorkerHandle``
so the real background worker never races the assertions. The real
worker, if started by the lifespan, uses ``state.services.job_repo``
on the default engine and never sees the jobs created in the override
repository — so tests are deterministic.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from doc3gpp.models.jobs import JobKind, JobStatus
from doc3gpp.storage.db.base import Base
from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository
from doc3gpp.web.app import build_app
from doc3gpp.web.deps import get_job_repo, get_job_worker
from doc3gpp.web.state import JobWorkerHandle


def _make_repo() -> SQLAlchemyJobRepository:
    """Build an in-memory sqlite-backed ``JobRepository``."""
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


class _FakeJobWorkerHandle(JobWorkerHandle):
    """A handle that records cancellation requests instead of setting events."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.event_queues: dict[str, asyncio.Queue[dict]] = {}

    def register_queue(self, job_id: str, queue: asyncio.Queue[dict]) -> None:
        self.event_queues[job_id] = queue

    def unregister_queue(self, job_id: str) -> None:
        self.event_queues.pop(job_id, None)

    def cancel(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return True


@pytest.fixture()
def client(
    sqlite_env: Any,
) -> tuple[TestClient, SQLAlchemyJobRepository, _FakeJobWorkerHandle]:
    repo = _make_repo()
    handle = _FakeJobWorkerHandle()
    app: FastAPI = build_app()
    app.dependency_overrides[get_job_repo] = lambda: repo
    app.dependency_overrides[get_job_worker] = lambda: handle
    with TestClient(app) as c:
        yield c, repo, handle


# ---------------------------------------------------------------------------
# POST enqueue endpoints
# ---------------------------------------------------------------------------


def test_post_sync_meetings_creates_job(client: Any) -> None:
    c, repo, _ = client
    r = c.post("/jobs/sync/meetings", json={"tsg": "SA2"})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert body["job_id"]
    assert body["links"]["self"] == f"/jobs/{body['job_id']}"
    assert body["links"]["events"] == f"/jobs/{body['job_id']}/events"
    job = repo.get(body["job_id"])
    assert job is not None
    assert job.kind is JobKind.SYNC_MEETINGS
    assert job.params == {"tsg": "SA2"}


def test_post_sync_meetings_requires_tsg(client: Any) -> None:
    c, _, _ = client
    r = c.post("/jobs/sync/meetings", json={})
    assert r.status_code == 422


def test_post_sync_tdocs_by_meeting_id(client: Any) -> None:
    c, repo, _ = client
    r = c.post("/jobs/sync/tdocs", json={"meeting_id": 12})
    assert r.status_code == 202
    job = repo.get(r.json()["job_id"])
    assert job is not None
    assert job.params == {"force": False, "meeting_id": 12}


def test_post_sync_tdocs_by_meeting_name(client: Any) -> None:
    c, repo, _ = client
    r = c.post("/jobs/sync/tdocs", json={"meeting": "SA2#156"})
    assert r.status_code == 202
    job = repo.get(r.json()["job_id"])
    assert job is not None
    assert job.params == {"force": False, "meeting_name": "SA2#156"}


def test_post_sync_tdocs_requires_selector(client: Any) -> None:
    c, _, _ = client
    r = c.post("/jobs/sync/tdocs", json={})
    assert r.status_code == 400


def test_post_sync_tdocs_all(client: Any) -> None:
    c, repo, _ = client
    r = c.post("/jobs/sync/tdocs/all", json={"force": True})
    assert r.status_code == 202
    job = repo.get(r.json()["job_id"])
    assert job is not None
    assert job.kind is JobKind.SYNC_TDOCS_ALL
    assert job.params == {"force": True}


def test_post_sync_specs_creates_job(client: Any) -> None:
    c, repo, _ = client
    r = c.post("/jobs/sync/specs", json={"tsg": "R5", "force": True})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert body["job_id"]
    assert body["links"]["self"] == f"/jobs/{body['job_id']}"
    assert body["links"]["events"] == f"/jobs/{body['job_id']}/events"
    job = repo.get(body["job_id"])
    assert job is not None
    assert job.kind is JobKind.SYNC_SPECS
    assert job.params == {"tsg": "R5", "force": True, "per_version_details": False}


def test_post_sync_specs_by_spec_id(client: Any) -> None:
    c, repo, _ = client
    r = c.post("/jobs/sync/specs", json={"spec_id": "36.579-5", "force": False})
    assert r.status_code == 202
    job = repo.get(r.json()["job_id"])
    assert job is not None
    assert job.kind is JobKind.SYNC_SPECS
    assert job.params == {"spec_id": "36.579-5", "force": False, "per_version_details": False}


def test_post_sync_specs_forwards_per_version_details(client: Any) -> None:
    """``per_version_details`` in the JSON body is written into ``job.params``."""
    c, repo, _ = client
    r = c.post(
        "/jobs/sync/specs",
        json={"tsg": "R5", "force": False, "per_version_details": True},
    )
    assert r.status_code == 202
    job = repo.get(r.json()["job_id"])
    assert job is not None
    assert job.params == {"tsg": "R5", "force": False, "per_version_details": True}


def test_post_sync_specs_requires_one_selector(client: Any) -> None:
    c, _, _ = client
    r = c.post("/jobs/sync/specs", json={"tsg": "R5", "spec_id": "36.579-5"})
    assert r.status_code == 400
    r2 = c.post("/jobs/sync/specs", json={})
    assert r2.status_code == 400


def test_post_parse_tdocs(client: Any) -> None:
    c, repo, _ = client
    r = c.post(
        "/jobs/parse/tdocs",
        json={"filter": {"meeting_id": 5}, "force": False, "full": True},
    )
    assert r.status_code == 202
    job = repo.get(r.json()["job_id"])
    assert job is not None
    assert job.kind is JobKind.PARSE_TDOCS
    assert job.params == {
        "filter": {"meeting_id": 5},
        "force": False,
        "full": True,
    }


def test_post_parse_tdocs_single_tdoc_payload(client: Any) -> None:
    """The tdoc detail page's payload enqueues a single-tdoc parse job."""
    c, repo, _ = client
    r = c.post(
        "/jobs/parse/tdocs",
        json={
            "filter": {"tdoc_id": "R5-260001"},
            "force": True,
            "full": True,
        },
    )
    assert r.status_code == 202
    job = repo.get(r.json()["job_id"])
    assert job is not None
    assert job.kind is JobKind.PARSE_TDOCS
    assert job.params == {
        "filter": {"tdoc_id": "R5-260001"},
        "force": True,
        "full": True,
    }


def test_post_search_rebuild(client: Any) -> None:
    c, repo, _ = client
    r = c.post("/jobs/search/rebuild", json={"stale_only": True, "resume": False})
    assert r.status_code == 202
    job = repo.get(r.json()["job_id"])
    assert job is not None
    assert job.kind is JobKind.REBUILD_SEARCH
    assert job.params == {"stale_only": True, "resume": False}


def test_post_cache_purge_requires_yes(client: Any) -> None:
    c, _, _ = client
    r = c.post("/jobs/cache/purge", json={"scope": "markdown"})
    assert r.status_code == 400


def test_post_cache_purge(client: Any) -> None:
    c, repo, _ = client
    r = c.post("/jobs/cache/purge", json={"scope": "zips", "yes": True})
    assert r.status_code == 202
    job = repo.get(r.json()["job_id"])
    assert job is not None
    assert job.kind is JobKind.CACHE_PURGE
    assert job.params == {"scope": "zips"}


def test_post_cache_purge_rejects_bad_scope(client: Any) -> None:
    c, _, _ = client
    r = c.post("/jobs/cache/purge", json={"scope": "nope", "yes": True})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Flat alias from meeting_show.html
# ---------------------------------------------------------------------------


def test_post_sync_tdocs_flat_form(client: Any) -> None:
    c, repo, _ = client
    r = c.post("/jobs/sync_tdocs", data={"meeting_id": "42"})
    assert r.status_code == 202
    job = repo.get(r.json()["job_id"])
    assert job is not None
    assert job.kind is JobKind.SYNC_TDOCS
    assert job.params == {"meeting_id": 42}


def test_post_sync_tdocs_flat_json(client: Any) -> None:
    c, repo, _ = client
    r = c.post("/jobs/sync_tdocs", json={"meeting_id": 7})
    assert r.status_code == 202
    job = repo.get(r.json()["job_id"])
    assert job is not None
    assert job.params == {"meeting_id": 7}


def test_post_sync_tdocs_flat_requires_selector(client: Any) -> None:
    c, _, _ = client
    r = c.post("/jobs/sync_tdocs", data={})
    assert r.status_code == 400


def test_post_sync_tdocs_flat_rejects_non_numeric_meeting_id(client: Any) -> None:
    c, _, _ = client
    r = c.post("/jobs/sync_tdocs", data={"meeting_id": "abc"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_filter"


# ---------------------------------------------------------------------------
# GET list + detail
# ---------------------------------------------------------------------------


def test_get_jobs_lists_recent(client: Any) -> None:
    c, repo, _ = client
    j1 = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    j2 = repo.create(JobKind.CACHE_PURGE, {"scope": "all"})
    r = c.get("/jobs?format=json")
    assert r.status_code == 200
    body = r.json()
    ids = [job["job_id"] for job in body["jobs"]]
    assert j2.id in ids and j1.id in ids
    assert body["total"] == 2


def test_get_jobs_filters_by_status(client: Any) -> None:
    c, repo, _ = client
    queued = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    repo.mark_succeeded(queued.id, summary={"ok": True})
    repo.create(JobKind.SYNC_MEETINGS, {"tsg": "RAN2"})
    r = c.get("/jobs?format=json&status=succeeded")
    assert r.status_code == 200
    body = r.json()
    assert [job["job_id"] for job in body["jobs"]] == [queued.id]


def test_get_jobs_pagination(client: Any) -> None:
    c, repo, _ = client
    created = [repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"}) for _ in range(5)]
    r = c.get("/jobs?format=json&limit=2&offset=1")
    body = r.json()
    assert body["total"] == 2
    assert body["offset"] == 1
    # Descending created_at order → newest first.
    assert body["jobs"][0]["job_id"] == created[3].id
    assert body["jobs"][1]["job_id"] == created[2].id


def test_get_job_returns_detail(client: Any) -> None:
    c, repo, _ = client
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    repo.mark_running(job.id, message="starting")
    repo.append_log(job.id, line="[ts] fetching")
    r = c.get(f"/jobs/{job.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == job.id
    assert body["kind"] == "sync_meetings"
    assert body["status"] == "running"
    assert body["params"] == {"tsg": "SA2"}
    assert body["error"] is None
    assert body["summary"] is None
    assert body["result"] is None
    assert "starting" in body["log_tail"]
    assert "[ts] fetching" in body["log_tail"]
    assert body["started_at"] is not None
    assert body["completed_at"] is None
    assert body["links"]["self"] == f"/jobs/{job.id}"


def test_get_job_returns_404_for_unknown(client: Any) -> None:
    c, _, _ = client
    r = c.get("/jobs/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"] == "job_not_found"


def test_get_job_html_includes_params_section(client: Any) -> None:
    """The ?format=html detail page renders a 'Params' section with the params dict.

    Locks the happy path: the new <section class="card">Params</section> is present,
    wraps the params inside a <pre><code> block, and includes the supplied keys
    and values verbatim.
    """
    c, repo, _ = client
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    r = c.get(f"/jobs/{job.id}?format=html")
    assert r.status_code == 200
    body = r.text
    assert "<h2>Params</h2>" in body
    assert "<pre><code>" in body
    assert "</code></pre>" in body
    pre_start = body.index("<pre><code>")
    pre_end = body.index("</code></pre>")
    pre_block = body[pre_start:pre_end]
    assert '"tsg"' in pre_block
    assert '"SA2"' in pre_block


def test_get_job_html_params_for_nested_filter(client: Any) -> None:
    """A nested params dict (PARSE_TDOCS' filter) renders as nested JSON.

    Locks the case the happy-path test cannot reach: a Mapping whose value is
    itself a Mapping. The outer 'filter' key and its inner 'tdoc_id' key +
    value both surface inside the <pre> block.
    """
    c, repo, _ = client
    job = repo.create(
        JobKind.PARSE_TDOCS,
        {
            "filter": {"tdoc_id": "R5-123456"},
            "force": True,
            "full": False,
        },
    )
    r = c.get(f"/jobs/{job.id}?format=html")
    assert r.status_code == 200
    body = r.text
    pre_start = body.index("<pre><code>")
    pre_end = body.index("</code></pre>")
    pre_block = body[pre_start:pre_end]
    assert '"filter"' in pre_block
    assert '"tdoc_id"' in pre_block
    assert "R5-123456" in pre_block
    assert '"force"' in pre_block
    assert "true" in pre_block  # JSON boolean
    assert '"full"' in pre_block
    assert "false" in pre_block


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancel_returns_200_for_running(client: Any) -> None:
    c, repo, handle = client
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    repo.mark_running(job.id, message="starting")
    r = c.post(f"/jobs/{job.id}/cancel")
    assert r.status_code == 200
    assert handle.cancelled == [job.id]
    assert r.json()["job_id"] == job.id


def test_cancel_returns_200_when_terminal_idempotent_succeeded(client: Any) -> None:
    """Cancel on a SUCCEEDED job returns 200 + envelope (idempotent)."""
    c, repo, handle = client
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    repo.mark_succeeded(job.id, summary={"ok": True})
    r = c.post(f"/jobs/{job.id}/cancel")
    assert r.status_code == 200
    assert handle.cancelled == []  # no cancel event on a terminal job
    body = r.json()
    assert body["job_id"] == job.id
    assert body["status"] == "succeeded"
    assert body["summary"] == {"ok": True}


def test_cancel_returns_200_when_terminal_idempotent_failed(client: Any) -> None:
    """Cancel on a FAILED job returns 200 + envelope + error field."""
    c, repo, _ = client
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    repo.mark_failed(job.id, error="boom")
    r = c.post(f"/jobs/{job.id}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == job.id
    assert body["status"] == "failed"
    assert body["error"] == "boom"


def test_cancel_returns_200_when_terminal_idempotent_cancelled(client: Any) -> None:
    """Cancel on an already-CANCELLED job returns 200 + envelope."""
    c, repo, _ = client
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    repo.mark_cancelled(job.id)
    r = c.post(f"/jobs/{job.id}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == job.id
    assert body["status"] == "cancelled"


def test_cancel_returns_404_for_unknown(client: Any) -> None:
    c, _, _ = client
    r = c.post("/jobs/does-not-exist/cancel")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# SSE events stream
# ---------------------------------------------------------------------------


def test_events_stream_emits_terminal_replay(client: Any) -> None:
    c, repo, _ = client
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    repo.mark_running(job.id, message="starting")
    repo.append_log(job.id, line="[ts] fetched meeting SA2#156")
    repo.mark_succeeded(job.id, summary={"meetings": 14})

    with c.stream("GET", f"/jobs/{job.id}/events") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        text = "".join(r.iter_text())

    assert "event: status" in text
    assert "event: log" in text
    assert "running" in text
    assert "fetched meeting SA2#156" in text
    assert "succeeded" in text
    assert '"summary":{"meetings":14}' in text.replace(" ", "")


def test_events_stream_reuses_existing_queue_for_running_job(
    client: Any,
) -> None:
    """Connecting to a mid-flight (RUNNING) job must drain the SAME queue
    the worker already registered, not a fresh one (regression: clobbering
    the registered queue would lose the worker's terminal event and hang)."""
    c, repo, handle = client
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    repo.mark_running(job.id, message="starting")
    pre_registered: asyncio.Queue[dict] = asyncio.Queue()
    pre_registered.put_nowait(
        {"event": "status", "data": {"status": "succeeded", "summary": {"meetings": 3}}}
    )
    handle.register_queue(job.id, pre_registered)

    with c.stream("GET", f"/jobs/{job.id}/events") as r:
        assert r.status_code == 200
        text = "".join(r.iter_text())

    assert "succeeded" in text
    assert '"summary":{"meetings":3}' in text.replace(" ", "")


def test_events_stream_404_for_unknown(client: Any) -> None:
    c, _, _ = client
    r = c.get("/jobs/does-not-exist/events")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Nav badge (``get_pending_jobs``) — QUEUED + RUNNING both count
# ---------------------------------------------------------------------------


def test_nav_badge_counts_queued_jobs(client: Any) -> None:
    """The nav badge shows the count of QUEUED jobs.

    Regression for the user-visible bug where the badge disappeared the
    moment the worker picked a job up (status → RUNNING). The badge
    must keep counting in-flight jobs through the QUEUED → RUNNING
    transition so users always see whether a parse / sync is in
    progress. This test pins the QUEUED half of the contract; the
    RUNNING half is pinned in :func:`test_nav_badge_counts_running_jobs`.
    """
    c, repo, _ = client
    for _ in range(3):
        repo.create(JobKind.PARSE_TDOCS, {"filter": {"tdoc_id": "R5-260001"}})

    html = c.get("/").text
    assert 'class="nav-badge">3</span>' in html


def test_nav_badge_counts_running_jobs(client: Any) -> None:
    """The nav badge counts RUNNING jobs (not only QUEUED).

    Locks in the fix for the user-reported bug: clicking "Parse" made
    the badge vanish as soon as the worker transitioned the job from
    QUEUED → RUNNING, leaving no header indicator that the parse was
    still in flight. The badge must keep counting in-flight jobs
    through that transition.
    """
    c, repo, _ = client
    # Two QUEUED, one RUNNING.
    queued_1 = repo.create(JobKind.PARSE_TDOCS, {"filter": {"tdoc_id": "R5-1"}})
    queued_2 = repo.create(JobKind.PARSE_TDOCS, {"filter": {"tdoc_id": "R5-2"}})
    running = repo.create(JobKind.PARSE_TDOCS, {"filter": {"tdoc_id": "R5-3"}})
    repo.mark_running(running.id, message="starting")
    # One SUCCEEDED — must NOT count.
    done = repo.create(JobKind.PARSE_TDOCS, {"filter": {"tdoc_id": "R5-4"}})
    repo.mark_running(done.id, message="starting")
    repo.mark_succeeded(done.id, summary={"requested": 1, "successes": 1})

    html = c.get("/").text
    assert 'class="nav-badge">3</span>' in html
    # Sanity: make sure we actually created the jobs we expected.
    assert len(repo.list(status=JobStatus.QUEUED, limit=100)) == 2
    assert len(repo.list(status=JobStatus.RUNNING, limit=100)) == 1
    assert len(repo.list(status=JobStatus.SUCCEEDED, limit=100)) == 1
    # All four ids must exist.
    assert all(
        repo.get(jid) is not None
        for jid in (queued_1.id, queued_2.id, running.id, done.id)
    )


def test_nav_badge_hidden_when_no_in_flight_jobs(client: Any) -> None:
    """No QUEUED / RUNNING jobs → the badge is absent (not "0")."""
    c, repo, _ = client
    # A terminal job must NOT inflate the badge.
    done = repo.create(JobKind.PARSE_TDOCS, {})
    repo.mark_running(done.id, message="starting")
    repo.mark_succeeded(done.id, summary={})

    html = c.get("/").text
    assert 'class="nav-badge"' not in html


def test_nav_badge_on_jobs_list_page_counts_running(client: Any) -> None:
    """The badge on /jobs also picks up RUNNING jobs.

    The list route computes ``pending_jobs`` independently of the
    ``get_pending_jobs`` dependency (because the page renders a table
    + filter UI that needs the same count). It must apply the same
    QUEUED + RUNNING semantics.
    """
    c, repo, _ = client
    repo.create(JobKind.PARSE_TDOCS, {})
    running = repo.create(JobKind.PARSE_TDOCS, {})
    repo.mark_running(running.id, message="starting")

    html = c.get("/jobs").text
    assert 'class="nav-badge">2</span>' in html
