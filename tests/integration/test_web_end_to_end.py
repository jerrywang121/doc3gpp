"""Web layer end-to-end integration tests (offline).

These exercise the real FastAPI app built via ``build_app`` against a
real SQLite engine (``sqlite_env``), over the HTTP surface only. The two
OS-facing seams injected are:

* the job worker's ``run()`` loop (paused so a queued job is never
  claimed by a real handler — which would hit the live 3GPP network),
* the worker handle the ``/jobs`` routes depend on (a fake so the test
  can complete a job deterministically and observe cancellations).

Everything else — route wiring, services, repositories, error mapping,
SSE framing, JSON/HTML rendering — is the real implementation.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from doc3gpp.settings.schema import CacheSettings, MCPSettings, ServerSettings, Settings
from doc3gpp.web.app import build_app
from doc3gpp.web.state import JobWorkerHandle


class _FakeJobWorkerHandle(JobWorkerHandle):
    """Records cancellations and lets the test complete a job."""

    def __init__(self) -> None:
        super().__init__()
        self.cancelled: list[str] = []

    def register_queue(self, job_id: str, queue=None) -> None:
        # No-op: the SSE replay path needs no live queue for a terminal job.
        pass

    def cancel(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return True


@pytest.fixture()
def app_with_deps(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A built app with the real job worker paused and a fake handle.

    ``build_app`` composes the real engine + services + job repository
    through ``build_state``; the real worker's ``run()`` loop is replaced
    by a no-op so queued jobs are never claimed by a network-touching
    handler, and the worker handle is swapped for a fake so the test can
    drive jobs deterministically.
    """
    from doc3gpp.web.workers.job_worker import JobWorker

    async def _noop_run(self) -> None:  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(JobWorker, "run", _noop_run)

    settings = Settings(
        server=ServerSettings(enabled=True),
        mcp=MCPSettings(enabled=True),
        cache=CacheSettings(dir=tmp_path / "cache"),
    )
    app = build_app(settings)
    handle = _FakeJobWorkerHandle()

    from doc3gpp.web.deps import get_job_worker

    app.dependency_overrides[get_job_worker] = lambda: handle
    return app, handle


def test_full_lifecycle(sqlite_env, app_with_deps) -> None:
    """healthz → seed → meetings JSON/HTML → enqueue → poll → SSE → cancel."""
    from doc3gpp.models.meeting import Meeting
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    app, handle = app_with_deps
    create_schema()

    with TestClient(app) as client:
        # healthz
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"ok": True}

        # seed
        SQLAlchemyMeetingRepository().upsert_many(
            [
                Meeting(
                    meeting_id=156,
                    name="SA2#156",
                    title="SA2 meeting 156",
                    location="online",
                    start_date=None,
                    end_date=None,
                    ftp_url="TSG_SA/WG2_Arch/",
                ),
            ]
        )
        SQLAlchemyTDocRepository().upsert_many(
            [
                TDoc(
                    tdoc_id="S2-260001",
                    title="A test tdoc",
                    meeting_id=156,
                    ftp_url="TSG_SA/WG2_Arch/S2-260001.zip",
                ),
            ]
        )

        # meetings JSON
        meetings_json = client.get("/meetings?format=json")
        assert meetings_json.status_code == 200
        rows = json.loads(meetings_json.content)
        assert rows and rows[0]["meeting_id"] == "156"

        # meetings detail JSON
        detail = client.get("/meetings/156?format=json")
        assert detail.status_code == 200
        assert detail.json()["meeting"]["name"] == "SA2#156"

        # meetings detail HTML
        html = client.get("/meetings/156")
        assert html.status_code == 200
        assert "SA2#156" in html.text

        # enqueue a job (sync_meetings)
        enqueue = client.post("/jobs/sync/meetings", json={"tsg": "SA2"})
        assert enqueue.status_code == 202
        envelope = enqueue.json()
        assert envelope["status"] == "queued"
        job_id = envelope["job_id"]
        assert envelope["links"]["self"] == f"/jobs/{job_id}"

        # drive the job to a terminal state in the DB (worker is paused)
        from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository

        repo = SQLAlchemyJobRepository()
        repo.mark_running(job_id, message="starting")
        repo.append_log(job_id, line="fetched meeting SA2#156")
        repo.mark_succeeded(job_id, summary={"meetings": 1})

        # poll until terminal
        status = None
        for _ in range(20):
            job = client.get(f"/jobs/{job_id}").json()
            status = job["status"]
            if status in ("succeeded", "failed", "cancelled"):
                break
        assert status == "succeeded"
        assert job["kind"] == "sync_meetings"
        assert job["params"] == {"tsg": "SA2"}
        assert job["summary"] == {"meetings": 1}

        # SSE stream replays running + log + terminal status
        with client.stream("GET", f"/jobs/{job_id}/events") as r:
            assert r.status_code == 200
            text = "".join(r.iter_text())
        assert "event: status" in text
        assert "running" in text
        assert "succeeded" in text
        assert "fetched meeting SA2#156" in text

        # cancel on a terminal job is a 200 (idempotent) returning the envelope
        envelope = client.post(f"/jobs/{job_id}/cancel")
        assert envelope.status_code == 200
        assert envelope.json()["status"] == "succeeded"

    get_engine.cache_clear()


def test_cache_miss_returns_hint(sqlite_env, app_with_deps, tmp_path) -> None:
    """``GET /tdocs/{id}/content?format=markdown`` on an unparsed tdoc → 404 + hint."""
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    app, _ = app_with_deps
    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="S2-260002", ftp_url="TSG_SA/WG2_Arch/S2-260002.zip"),
    )

    with TestClient(app) as client:
        response = client.get("/tdocs/S2-260002/content?format=markdown")
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "cache_miss"
    assert body["hint"] == "run: doc3gpp tdoc parse --tdoc S2-260002"
    get_engine.cache_clear()


def test_job_cancel(sqlite_env, app_with_deps) -> None:
    """``POST /jobs/{id}/cancel`` on a queued job hands off to the worker handle."""
    from doc3gpp.models.jobs import JobKind
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository

    app, handle = app_with_deps
    create_schema()
    repo = SQLAlchemyJobRepository()
    job = repo.create(kind=JobKind.SYNC_TDOCS, params={"meeting_id": 156})

    with TestClient(app) as client:
        response = client.post(f"/jobs/{job.id}/cancel")
    assert response.status_code == 200
    assert job.id in handle.cancelled
    get_engine.cache_clear()


def test_meetings_list_name_filter_rich_grammar(sqlite_env, app_with_deps) -> None:
    """``GET /meetings?name=!%25SA2%25`` is 200 and excludes ``SA2#156``.

    The ``!`` prefix is a ``NOT LIKE`` in the rich filter grammar; the
    route's ``parse_text_query`` is a pass-through, so the prefix must
    reach the SQL layer intact. A regression that stripped the
    ``!`` (e.g. a premature ``lstrip("!")``) would turn this into a
    plain ``LIKE '%SA2%'`` and silently include the SA2 row. The
    pass-through 200 status checks in the unit suite can't catch that;
    only an end-to-end exercise over real sqlite can. Seed a second
    meeting so a wrong "all rows returned" implementation still fails
    the negative assertion.
    """
    from doc3gpp.models.meeting import Meeting
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository

    app, _ = app_with_deps
    create_schema()
    SQLAlchemyMeetingRepository().upsert_many(
        [
            Meeting(
                meeting_id=156,
                name="SA2#156",
                title="SA2 meeting 156",
                location="online",
                start_date=None,
                end_date=None,
                ftp_url="TSG_SA/WG2_Arch/",
            ),
            Meeting(
                meeting_id=99,
                name="RAN5#99",
                title="RAN5 meeting 99",
                location="athens",
                start_date=None,
                end_date=None,
                ftp_url="TSGR5_99/",
            ),
        ]
    )

    with TestClient(app) as client:
        neg = client.get("/meetings?name=!%25SA2%25&format=json")
        assert neg.status_code == 200
        neg_rows = json.loads(neg.content)
        neg_names = [row["name"] for row in neg_rows]
        assert "SA2#156" not in neg_names
        assert "RAN5#99" in neg_names

        pos = client.get("/meetings?name=%25SA2%25&format=json")
        assert pos.status_code == 200
        pos_rows = json.loads(pos.content)
        pos_names = [row["name"] for row in pos_rows]
        assert "SA2#156" in pos_names
        assert "RAN5#99" not in pos_names

    get_engine.cache_clear()
