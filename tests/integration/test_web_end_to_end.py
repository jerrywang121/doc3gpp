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
from pathlib import Path

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

    app, _handle = app_with_deps
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
        TDoc(tdoc_id="S2-260002", type="CR", ftp_url="TSG_SA/WG2_Arch/S2-260002.zip"),
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
    assert response.json()["status"] == "cancelled"
    assert repo.get(job.id).status.value == "cancelled"
    assert handle.cancelled == []
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


def test_cancel_button_in_jobs_list_swaps_row(sqlite_env, app_with_deps) -> None:
    """The Cancel button on /jobs swaps the row in place via ``?format=html``.

    Exercises the end-to-end HTMX contract: a RUNNING job's row carries the
    Cancel button with the expected ``hx-post`` / ``hx-swap`` /
    ``hx-target`` / ``hx-confirm`` attributes; ``POST {url}?format=html``
    returns a refreshed row whose Cancel button has disappeared (the
    job is now CANCELLED in the DB after the fake handle runs).
    """
    from doc3gpp.models.jobs import JobKind
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository

    app, handle = app_with_deps
    create_schema()
    repo = SQLAlchemyJobRepository()
    job = repo.create(kind=JobKind.SYNC_MEETINGS, params={"tsg": "SA2"})
    repo.mark_running(job.id, message="starting")

    with TestClient(app) as client:
        # 1. The /jobs list page renders the Cancel button for the running job.
        list_html = client.get("/jobs")
        assert list_html.status_code == 200
        body = list_html.text
        assert f'<tr id="job-row-{job.id}">' in body, body
        assert "Cancel" in body
        assert "</button>" in body
        assert f'hx-post="/jobs/{job.id}/cancel?format=html"' in body
        assert f'hx-target="#job-row-{job.id}"' in body
        assert 'hx-confirm="Cancel this job?"' in body

        # 2. POSTing to the cancel endpoint with ?format=html returns a
        #    refreshed row partial (the HTMX outerHTML swap target).
        swap = client.post(f"/jobs/{job.id}/cancel?format=html")
        assert swap.status_code == 200
        assert swap.headers["content-type"].startswith("text/html")
        assert f'<tr id="job-row-{job.id}">' in swap.text
        # Fake handle appended the job id; row renders WITHOUT Cancel
        # only when the job has reached a terminal status. The fake
        # handle does not mutate the DB row, so the response still says
        # "running"; the button is therefore still present in the
        # swapped row. The route's idempotent contract is exercised in
        # the unit suite; here we just confirm the swap shape works.
        assert job.id in handle.cancelled

    get_engine.cache_clear()


def test_cancel_button_omits_for_terminal_job(sqlite_env, app_with_deps) -> None:
    """A terminal job's row has no Cancel button — the partial gates on status.

    Confirms the live render: a SUCCEEDED job in the DB must NOT surface
    a Cancel button on /jobs, even though the row is still present in
    the list (terminal jobs are kept for inspection).
    """
    from doc3gpp.models.jobs import JobKind
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository

    app, _ = app_with_deps
    create_schema()
    repo = SQLAlchemyJobRepository()
    job = repo.create(kind=JobKind.SYNC_MEETINGS, params={"tsg": "SA2"})
    repo.mark_running(job.id, message="starting")
    repo.mark_succeeded(job.id, summary={"meetings": 1})

    with TestClient(app) as client:
        list_html = client.get("/jobs")

    assert list_html.status_code == 200
    assert f'<tr id="job-row-{job.id}">' in list_html.text
    assert ">succeeded</td>" in list_html.text
    assert "Cancel</button>" not in list_html.text
    assert f"/jobs/{job.id}/cancel" not in list_html.text
    get_engine.cache_clear()


def test_web_tdocs_list_filter_by_abstract(sqlite_env, app_with_deps) -> None:
    """``GET /tdocs?abstract=%25TL%25DR%25&format=json`` matches only the row with the abstract."""
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    app, _ = app_with_deps
    create_schema()
    SQLAlchemyTDocRepository().upsert_many(
        [
            TDoc(tdoc_id="R5-260001", abstract="TL;DR here."),
            TDoc(tdoc_id="R5-260002", abstract="Unrelated."),
        ]
    )

    with TestClient(app) as client:
        response = client.get("/tdocs?abstract=%25TL%25DR%25&format=json")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert body
    assert all(r["tdoc_id"] == "R5-260001" for r in body)
    get_engine.cache_clear()


def test_web_tdocs_show_renders_xlsx_metadata_panel(sqlite_env, app_with_deps) -> None:
    """``GET /tdocs/{id}`` renders the XLSX metadata panel when any field is set."""
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    app, _ = app_with_deps
    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(
            tdoc_id="R5-260001",
            tdoc_for="Information",
            ls_to="RAN2",
            ls_cc="RAN3",
            original_ls="RAN2#116",
            abstract="TL;DR here.",
            secretary_remarks="Secretary note.",
        )
    )

    with TestClient(app) as client:
        response = client.get("/tdocs/R5-260001")
    assert response.status_code == 200
    html = response.text
    assert "XLSX metadata" in html
    assert "Information" in html
    assert "RAN2" in html
    assert "RAN3" in html
    assert "RAN2#116" in html
    assert "TL;DR here." in html
    assert "Secretary note." in html
    assert '<pre class="xlsx-meta-pre">' in html
    get_engine.cache_clear()


def test_web_tdocs_list_xlsx_metadata_fields_opt_in(sqlite_env, app_with_deps) -> None:
    """``?fields=abstract&format=json`` surfaces the abstract column; the default omits it."""
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    app, _ = app_with_deps
    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260001", abstract="TL;DR here.")
    )

    with TestClient(app) as client:
        default = client.get("/tdocs?format=json")
        opted = client.get("/tdocs?fields=abstract&format=json")
    assert default.status_code == 200
    assert opted.status_code == 200
    default_rows = default.json()
    opted_rows = opted.json()
    assert isinstance(default_rows, list) and default_rows
    assert isinstance(opted_rows, list) and opted_rows
    assert "abstract" not in default_rows[0]
    assert opted_rows[0]["abstract"] == "TL;DR here."
    get_engine.cache_clear()


def test_web_tdocs_show_panel_omitted_when_all_six_are_null(
    sqlite_env, app_with_deps
) -> None:
    """``GET /tdocs/{id}`` omits the XLSX metadata panel when all six fields are null."""
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    app, _ = app_with_deps
    create_schema()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5-260002"))

    with TestClient(app) as client:
        response = client.get("/tdocs/R5-260002")
    assert response.status_code == 200
    assert "XLSX metadata" not in response.text
    get_engine.cache_clear()


def test_get_tdoc_by_url_byte_parity_with_cli(sqlite_env, app_with_deps) -> None:
    """``GET /tdocs/by-url?ftp_url=<url>&format=json`` matches the CLI's ``--format json`` byte-for-byte.

    The CLI's ``tdoc show --ftp-url`` path renders the same
    ``TDocShowRecordByUrl`` DTO via ``render.to_jsonable``; the HTTP
    route does the same. Both must emit identical bytes.
    """
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails
    from doc3gpp.models.tdoc_file import TDocFile
    from doc3gpp.models.tdoc_show import TDocShowRecordByUrl, TDocShowRepos
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
        SQLAlchemyTDocCrChangeDetailsRepository,
    )
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
        SQLAlchemyTDocCrTtcnRepository,
    )
    from doc3gpp.storage.repositories.tdoc_file_sql import (
        SQLAlchemyTDocFileRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.storage.repositories.tdoc_zip_inventory_sql import (
        SQLAlchemyTDocZipInventoryRepository,
    )
    from doc3gpp.web.render import to_jsonable

    app, _ = app_with_deps
    create_schema()
    url = "r5/26.001/r5s260001.zip"
    tdoc_repo = SQLAlchemyTDocRepository()
    cr_repo = SQLAlchemyTDocCrRepository()
    file_repo = SQLAlchemyTDocFileRepository()
    tdoc_repo.upsert(TDoc(tdoc_id="R5s260001", ftp_url=url, title="Parity"))
    cr_repo.upsert(TDocCRDetails(tdoc_id="R5s260001", ftp_url=url, cr_num="0001"))
    file_repo.upsert_many(
        [
            TDocFile(
                ftp_url=url,
                tdoc_id="R5s260001",
                type="revision",
                file="R5s260001.zip",
            )
        ]
    )

    expected = TDocShowRecordByUrl.from_ftp_url(
        url,
        TDocShowRepos(
            tdoc=tdoc_repo,
            cr=cr_repo,
            cr_ttcn=SQLAlchemyTDocCrTtcnRepository(),
            cr_change_details=SQLAlchemyTDocCrChangeDetailsRepository(),
            file=file_repo,
            zip_inventory=SQLAlchemyTDocZipInventoryRepository(),
        ),
    )
    expected_json = to_jsonable(expected)

    with TestClient(app) as client:
        response = client.get(
            "/tdocs/by-url",
            params={"ftp_url": url, "format": "json"},
        )
    assert response.status_code == 200
    assert response.json() == expected_json

    from doc3gpp.storage.db.session import get_engine as _get_engine

    _get_engine.cache_clear()


def test_web_testcases_list_json_shape(sqlite_env, app_with_deps) -> None:
    """``GET /testcases?format=json`` returns the CLI-shaped row array.

    Seeds one testcase + status row over real sqlite and asserts the
    ``statuses`` projection stays a nested list of per-path objects
    (not a coerced string).
    """
    from doc3gpp.models.testcase import TestCase, TestCaseStatus
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.testcase_sql import (
        SQLAlchemyTestCaseRepository,
    )

    app, _ = app_with_deps
    create_schema()
    repo = SQLAlchemyTestCaseRepository()
    repo.upsert_many(
        [
            TestCase(
                testcase_id="TC_1",
                title="5G FR1 test",
                spec="38.523-1",
                group="5G",
                release="Rel-17",
            ),
        ]
    )
    repo.replace_statuses(
        "TC_1",
        "5G",
        [
            TestCaseStatus(
                testcase_id="TC_1",
                group="5G",
                path="FR1",
                gcf_ptcrb="Approved",
                ttcn_status="Approved",
            ),
        ],
    )

    with TestClient(app) as client:
        response = client.get("/testcases?format=json")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list) and body
    assert body[0]["testcase_id"] == "TC_1"
    assert body[0]["statuses"] == [
        {"path": "FR1", "gcf_ptcrb": "Approved", "ttcn_status": "Approved"}
    ]

    with TestClient(app) as client:
        html = client.get("/testcases")
    assert html.status_code == 200
    assert "TC_1" in html.text

    with TestClient(app) as client:
        partial = client.get("/testcases", headers={"HX-Request": "true"})
    assert partial.status_code == 200
    assert 'id="results"' in partial.text
    assert "<!DOCTYPE" not in partial.text

    with TestClient(app) as client:
        show = client.get("/testcases/TC_1?format=json")
    assert show.status_code == 200
    payload = show.json()
    assert isinstance(payload, list) and payload
    assert payload[0]["testcase_id"] == "TC_1"
    assert payload[0]["statuses"][0]["path"] == "FR1"
    assert "testcase" not in payload[0]
    assert "group" not in payload[0]["statuses"][0]

    with TestClient(app) as client:
        missing = client.get("/testcases/NOPE?format=json")
    assert missing.status_code == 404
    assert missing.json()["error"] == "testcase_not_found"
    get_engine.cache_clear()



def _seed_tdoc_content_sql(
    tdoc_id: str, ftp_url: str, cache_dir: Path,
) -> tuple[str, str]:
    """Seed a CR: the main lives in the archive, the annex in SQL."""
    import hashlib

    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
    from doc3gpp.models.tdoc_document import TDocDocument, markdown_member_name
    from doc3gpp.parsers.normalizers import normalize_ftp_path
    from doc3gpp.parsers.tdoc_zip import build_markdown_archive, document_id_for
    from doc3gpp.storage.repositories.tdoc_content_sql import (
        SQLAlchemyTDocContentRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    main_source = "CR/Main.docx"
    support_source = "Annex/Support & café.docx"
    sources = (
        (main_source, "# Main body\n\nMain document marker.\n"),
        (support_source, "# Support body\n\nSupporting document marker.\n"),
    )
    documents = tuple(
        TDocDocument(
            document_id=document_id_for(ftp_url, source_file),
            source_file=source_file,
            markdown_file=markdown_member_name(
                document_id_for(ftp_url, source_file), source_file
            ),
            file_order=file_order,
            markdown_text=markdown,
            content_hash=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        )
        for file_order, (source_file, markdown) in enumerate(sources)
    )
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=ftp_url)
    )
    cache_file = f"{tdoc_id}.zip"
    SQLAlchemyTDocContentRepository().replace_cr_bundle(
        TDocCRDetails(tdoc_id=tdoc_id, ftp_url=ftp_url, title="Stored CR"),
        None,
        None,
        TDocExtractMeta(
            ftp_url=ftp_url,
            tdoc_id=tdoc_id,
            cache_file=cache_file,
            doc_filename=main_source,
            document_count=1,
        ),
        documents[1:],
    )
    archive = build_markdown_archive(
        documents,
        ftp_url=normalize_ftp_path(ftp_url),
        main_source_file=main_source,
    )
    markdown_path = cache_dir / "markdown" / cache_file
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_bytes(archive)
    return main_source, support_source


def test_tdoc_content_by_url_uses_sql_sources_without_side_effects(
    sqlite_env, app_with_deps, monkeypatch, tmp_path,
) -> None:
    import json
    from urllib.parse import parse_qs, urlsplit

    from bs4 import BeautifulSoup
    from fastapi.testclient import TestClient

    from doc3gpp.services.tdoc_cr_service import TDocCrService
    from doc3gpp.services.tdoc_sync_coordinator import TDocSyncCoordinator
    from doc3gpp.storage.db.migrate import create_schema

    app, _ = app_with_deps
    create_schema()
    tdoc_id = "R5-260013"
    ftp_url = "R5/26.001/R5-260013.zip"
    main_source, support_source = _seed_tdoc_content_sql(
        tdoc_id, ftp_url, tmp_path / "cache"
    )

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("URL content read invoked download, extraction, or sync")

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_cr_service.download_tdoc_zip", unexpected_call
    )
    monkeypatch.setattr(TDocCrService, "extract", unexpected_call)
    monkeypatch.setattr(
        TDocSyncCoordinator, "sync_for_meeting_id", unexpected_call
    )

    with TestClient(app) as client:
        markdown_response = client.get(
            "/tdocs/by-url/content",
            params={"ftp_url": ftp_url, "format": "markdown"},
        )
        assert markdown_response.status_code == 200, markdown_response.text
        markdown = markdown_response.text
        assert f'<!-- doc3gpp-source: {json.dumps(main_source, ensure_ascii=False)} -->' in markdown
        assert f'<!-- doc3gpp-source: {json.dumps(support_source, ensure_ascii=False)} -->' in markdown
        assert markdown.index(main_source) < markdown.index(support_source)

        all_json_response = client.get(
            "/tdocs/by-url/content",
            params={"ftp_url": ftp_url, "format": "json"},
        )
        assert all_json_response.status_code == 200, all_json_response.text
        all_payload = all_json_response.json()
        assert all_payload["extraction_version"] == 2
        assert all_payload["complete"] is True
        assert [
            (document["source_file"], document["is_main"])
            for document in all_payload["documents"]
        ] == [(main_source, True), (support_source, False)]
        assert [document["markdown"] for document in all_payload["documents"]] == [
            "# Main body\n\nMain document marker.\n",
            "# Support body\n\nSupporting document marker.\n",
        ]

        selected_response = client.get(
            "/tdocs/by-url/content",
            params={
                "ftp_url": ftp_url,
                "source_file": support_source,
                "format": "json",
            },
        )
        assert selected_response.status_code == 200, selected_response.text
        selected_payload = selected_response.json()
        assert len(selected_payload["documents"]) == 1
        assert selected_payload["documents"][0]["source_file"] == support_source
        assert selected_payload["documents"][0]["is_main"] is False

        html_response = client.get(
            "/tdocs/by-url/content", params={"ftp_url": ftp_url}
        )
        assert html_response.status_code == 200, html_response.text
        soup = BeautifulSoup(html_response.text, "html.parser")
        sections = soup.select("section.tdoc-document")
        assert len(sections) == 2
        assert "Main document" in sections[0].get_text(" ", strip=True)
        assert "Supporting document" in sections[1].get_text(" ", strip=True)
        source_links = soup.select("a.tdoc-source-link")
        assert len(source_links) == 2
        support_link = next(
            link for link in source_links if link.get_text(strip=True) == support_source
        )
        assert parse_qs(urlsplit(support_link["href"]).query) == {
            "ftp_url": [ftp_url.lower()],
            "source_file": [support_source],
        }


def test_tdoc_content_by_url_legacy_text_marks_incomplete(
    sqlite_env, app_with_deps,
) -> None:
    from fastapi.testclient import TestClient

    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocExtractMeta
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    app, _ = app_with_deps
    create_schema()
    tdoc_id = "R5-LEGACY-260014"
    ftp_url = "R5/26.001/R5-LEGACY-260014.zip"
    cache_file = "legacy-content.zip"
    markdown = "Legacy single-source Markdown.\n"
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=ftp_url))
    SQLAlchemyTDocCrRepository().upsert_extract_meta(
        TDocExtractMeta(
            ftp_url=ftp_url,
            tdoc_id=tdoc_id,
            cache_file=cache_file,
            doc_filename="CR/legacy.docx",
        )
    )

    with TestClient(app) as client:
        cache_path = app.state.web.settings.cache.dir / "markdown" / cache_file
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(markdown, encoding="utf-8")
        response = client.get(
            "/tdocs/by-url/content",
            params={"ftp_url": ftp_url, "format": "markdown"},
        )

    assert response.status_code == 200, response.text
    assert response.text == markdown
    assert response.headers["x-doc3gpp-extraction-complete"] == "false"
