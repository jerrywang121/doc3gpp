"""Unit tests for LISTSERV job kinds, handlers and worker/stdio lifecycle."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from doc3gpp.models.jobs import JobKind, JobStatus
from doc3gpp.models.listserv import ListservBatchResult
from doc3gpp.services import factory
from doc3gpp.settings.schema import Settings
from doc3gpp.web import mcp_stdio
from doc3gpp.web.state import JobWorkerHandle
from doc3gpp.web.workers import handlers
from doc3gpp.web.workers.handlers import JobHandlers
from doc3gpp.web.workers.job_worker import JobWorker

LIST_ID = "3GPP_TSG_RAN_WG1"
U1 = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=X&P=1"
U2 = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=X&P=2"

SYNC_PARSE = [
    (JobKind.LISTSERV_SYNC, "build_listserv_sync_service", "sync"),
    (JobKind.LISTSERV_PARSE, "build_listserv_parse_service", "parse"),
]


def _job(kind, **params):
    base = {"list_id": LIST_ID, "dates": ["= 2026-10-01"], "force": False}
    base.update(params)
    return SimpleNamespace(id="j1", kind=kind, params=base)


def test_kinds_are_distinct_and_registered():
    assert JobKind.LISTSERV_SYNC.value == "listserv_sync"
    assert JobKind.LISTSERV_PARSE.value == "listserv_parse"
    assert JobKind.LISTSERV_INDEX.value == "listserv_index"
    for kind in (JobKind.LISTSERV_SYNC, JobKind.LISTSERV_PARSE, JobKind.LISTSERV_INDEX):
        assert kind in JobHandlers.KIND_TO_HANDLER


class FakeService:
    def __init__(self, result=None, *, hook=None):
        self.calls = []
        self.events = []
        self.result = result or ListservBatchResult(1, 0, {})
        self.hook = hook
        self.threads = []

    def _run(self, verb, list_id, **kwargs):
        self.threads.append(threading.current_thread())
        self.calls.append((verb, list_id, kwargs))
        self.events.append("call")
        if self.hook is not None:
            self.hook(kwargs)
        kwargs["on_progress"]("page done")
        self.events.append("call done")
        return self.result

    def sync(self, list_id, **kwargs):
        return self._run("sync", list_id, **kwargs)

    def parse(self, list_id, **kwargs):
        return self._run("parse", list_id, **kwargs)

    def close(self):
        self.events.append("close")


async def _invoke(kind, services, job=None):
    cancel = asyncio.Event()
    logs = []
    handler = JobHandlers.KIND_TO_HANDLER[kind]
    task = handler(
        job or _job(kind),
        services,
        Settings(),
        progress=lambda m, force=False: logs.append(m),
        cancel_event=cancel,
    )
    return task, cancel, logs


@pytest.mark.parametrize(("kind", "builder", "verb"), SYNC_PARSE)
def test_handler_runs_in_worker_thread_and_closes_after(monkeypatch, kind, builder, verb):
    fake = FakeService(ListservBatchResult(2, 1, {}))
    monkeypatch.setattr(factory, builder, lambda settings=None: fake)

    async def go():
        main = threading.current_thread()
        coro, _cancel, logs = await _invoke(kind, SimpleNamespace())
        summary = await coro
        assert fake.threads and fake.threads[0] is not main
        return summary, logs

    summary, logs = asyncio.run(go())
    (call,) = fake.calls
    assert call[0] == verb and call[1] == LIST_ID
    assert call[2]["dates"] == ["= 2026-10-01"] and call[2]["force"] is False
    assert callable(call[2]["cancelled"])
    assert fake.events == ["call", "call done", "close"]
    assert summary["processed"] == 2 and summary["skipped"] == 1
    assert summary["failed"] == 0 and summary["index_failed"] == 0
    assert "page done" in logs


@pytest.mark.parametrize(("kind", "builder", "verb"), SYNC_PARSE)
def test_handler_separates_source_and_index_failures(monkeypatch, kind, builder, verb):
    failures = {
        U1: "HTTP 500",
        U2: "FTS5 index update failed (RuntimeError)",
    }
    fake = FakeService(ListservBatchResult(3, 0, failures))
    monkeypatch.setattr(factory, builder, lambda settings=None: fake)

    async def go():
        coro, _c, _l = await _invoke(kind, SimpleNamespace())
        return await coro

    summary = asyncio.run(go())
    assert summary["processed"] == 3
    assert summary["failed"] == 1 and summary["index_failed"] == 1
    assert summary["failures"] == {U1: "HTTP 500"}
    assert summary["index_failures"] == {U2: "FTS5 index update failed (RuntimeError)"}
    assert "source data was committed" in summary["message"]


@pytest.mark.parametrize(("kind", "builder", "verb"), SYNC_PARSE)
def test_cancel_between_items_stops_later_work_and_closes_after(
    monkeypatch, kind, builder, verb
):
    state = {}

    def hook(kwargs):
        # Service loop: first item processed, then cancellation is observed.
        state["cancel"].set()
        state["seen"] = kwargs["cancelled"]()

    fake = FakeService(hook=hook)
    monkeypatch.setattr(factory, builder, lambda settings=None: fake)

    async def go():
        coro, cancel, _l = await _invoke(kind, SimpleNamespace())
        state["cancel"] = cancel
        with pytest.raises(asyncio.CancelledError):
            await coro

    asyncio.run(go())
    assert state["seen"] is True
    assert fake.events[-1] == "close"


@pytest.mark.parametrize(("kind", "builder", "verb"), SYNC_PARSE)
def test_close_happens_even_when_service_raises(monkeypatch, kind, builder, verb):
    fake = FakeService(hook=lambda kw: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(factory, builder, lambda settings=None: fake)

    async def go():
        coro, _c, _l = await _invoke(kind, SimpleNamespace())
        with pytest.raises(RuntimeError):
            await coro

    asyncio.run(go())
    assert fake.events == ["call", "close"]


def test_invalid_params_rejected_before_building_service(monkeypatch):
    def builder(settings=None):
        raise AssertionError("must not build")

    monkeypatch.setattr(factory, "build_listserv_sync_service", builder)

    async def go():
        coro, _c, _l = await _invoke(
            JobKind.LISTSERV_SYNC, SimpleNamespace(), _job(JobKind.LISTSERV_SYNC, dates=["x"])
        )
        with pytest.raises(ValueError):
            await coro

    asyncio.run(go())


def test_index_handler_passes_cancelled_and_maps_cancellation():
    from doc3gpp.services.listserv_index import IndexRebuildCancelled

    seen = {}

    class Index:
        def rebuild(self, request, *, quiet, on_progress, cancelled):
            seen["thread"] = threading.current_thread()
            seen["rebuild"] = request.rebuild
            raise IndexRebuildCancelled("stop")

    services = SimpleNamespace(listserv_index=Index())

    async def go():
        job = _job(JobKind.LISTSERV_INDEX, rebuild=True)
        coro, _c, _l = await _invoke(JobKind.LISTSERV_INDEX, services, job)
        with pytest.raises(asyncio.CancelledError):
            await coro
        return threading.current_thread()

    main = asyncio.run(go())
    assert seen["rebuild"] is True and seen["thread"] is not main


def test_index_handler_returns_status_summary():
    class Result:
        def to_dict(self):
            return {"fts5_processed": 2, "vector_processed": 0}

    class Index:
        def rebuild(self, request, **kwargs):
            return Result()

    async def go():
        coro, _c, _l = await _invoke(
            JobKind.LISTSERV_INDEX, SimpleNamespace(listserv_index=Index())
        )
        return await coro

    assert asyncio.run(go()) == {"fts5_processed": 2, "vector_processed": 0}



def test_index_handler_keeps_progress_callback_enabled():
    class Result:
        def to_dict(self):
            return {"fts5_processed": 2, "vector_processed": 0}

    class Index:
        def rebuild(self, request, **kwargs):
            assert kwargs["quiet"] is True
            assert callable(kwargs["on_progress"])
            kwargs["on_progress"]("listserv fts5: 1/1")
            return Result()

    async def go():
        coro, _cancel, logs = await _invoke(
            JobKind.LISTSERV_INDEX,
            SimpleNamespace(listserv_index=Index()),
            _job(JobKind.LISTSERV_INDEX, rebuild=True),
        )
        return await coro, logs

    summary, logs = asyncio.run(go())
    assert summary == {"fts5_processed": 2, "vector_processed": 0}
    assert "listserv fts5: 1/1" in logs


class _Repo:
    def __init__(self, job):
        self.job = job
        self.status = JobStatus.QUEUED
        self.failed = None
        self.succeeded = None

    def delete_older_than(self, _c):
        return 0

    def list(self, *, status, limit):
        return [self.job] if status == self.status == JobStatus.QUEUED else []

    def mark_running(self, _id, **_k):
        self.status = JobStatus.RUNNING
        return True, None

    def append_log(self, _id, **_k):
        pass

    def mark_cancelled(self, _id):
        self.status = JobStatus.CANCELLED

    def mark_succeeded(self, _id, **kw):
        self.status = JobStatus.SUCCEEDED
        self.succeeded = kw

    def mark_failed(self, _id, **kw):
        self.status = JobStatus.FAILED
        self.failed = kw


def _worker_state(repo, **services):
    settings = Settings()
    settings.server.poll_interval_seconds = 0.05
    return SimpleNamespace(
        settings=settings,
        services=SimpleNamespace(job_repo=repo, **services),
        jobs=JobWorkerHandle(),
    )


@pytest.mark.parametrize(
    ("failures", "expect"),
    [
        ({U1: "HTTP 500"}, "1 source failure"),
        ({U2: "vector index update failed (X)"}, "1 derived-index failure"),
    ],
)
def test_worker_marks_partial_failures_failed_with_summary(monkeypatch, failures, expect):
    fake = FakeService(ListservBatchResult(2, 0, failures))
    monkeypatch.setattr(factory, "build_listserv_sync_service", lambda settings=None: fake)
    repo = _Repo(_job(JobKind.LISTSERV_SYNC))
    state = _worker_state(repo)

    async def go():
        worker = JobWorker(state)
        task = asyncio.create_task(worker.run())
        for _ in range(100):
            if repo.status in (JobStatus.FAILED, JobStatus.SUCCEEDED):
                break
            await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert repo.status == JobStatus.FAILED
    assert expect in repo.failed["error"]
    assert repo.failed["summary"]["processed"] == 2
    if "index" in expect:
        assert "source data was committed" in repo.failed["error"]


def test_worker_marks_clean_run_succeeded(monkeypatch):
    fake = FakeService(ListservBatchResult(2, 0, {}))
    monkeypatch.setattr(factory, "build_listserv_parse_service", lambda settings=None: fake)
    repo = _Repo(_job(JobKind.LISTSERV_PARSE))
    state = _worker_state(repo)

    async def go():
        task = asyncio.create_task(JobWorker(state).run())
        for _ in range(100):
            if repo.status == JobStatus.SUCCEEDED:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert repo.status == JobStatus.SUCCEEDED
    assert repo.succeeded["summary"]["processed"] == 2


def test_stdio_shutdown_waits_for_listserv_thread_before_closing_resources(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    events: list[str] = []

    def hook(_kwargs):
        started.set()
        assert release.wait(15), "test did not release thread"
        events.append("thread finished")

    fake = FakeService(hook=hook)
    real_close = fake.close

    def close():
        events.append("source closed")
        real_close()

    fake.close = close
    monkeypatch.setattr(factory, "build_listserv_sync_service", lambda settings=None: fake)

    settings = Settings()
    settings.server.poll_interval_seconds = 0.05
    repo = _Repo(_job(JobKind.LISTSERV_SYNC))
    state = SimpleNamespace(
        settings=settings,
        services=SimpleNamespace(job_repo=repo, pipeline=None),
        jobs=None,
        engine=SimpleNamespace(dispose=lambda: events.append("engine")),
        testcase_engine=SimpleNamespace(dispose=lambda: events.append("testcase")),
        specdata_engine=SimpleNamespace(dispose=lambda: events.append("specdata")),
        close_docx_pool=lambda: events.append("docx"),
        close_listserv_services=lambda: events.append("listserv services"),
        close_embedder=lambda: events.append("embedder"),
    )

    class Worker(JobWorker):
        def __init__(self, st):
            super().__init__(st, handlers={JobKind.LISTSERV_SYNC: JobHandlers.KIND_TO_HANDLER[JobKind.LISTSERV_SYNC]})

    class Server:
        async def run_stdio_async(self):
            while not started.is_set():
                await asyncio.sleep(0.01)

    monkeypatch.setattr(
        mcp_stdio,
        "_load_dependencies",
        lambda: mcp_stdio._StdioDependencies(
            build_state=lambda _: state,
            build_mcp_server=lambda _: Server(),
            worker_type=Worker,
            handle_type=JobWorkerHandle,
        ),
    )

    async def exercise():
        runner = asyncio.create_task(mcp_stdio.run_stdio(settings))
        try:
            await asyncio.sleep(0.5)
            assert not runner.done()
            assert "engine" not in events and "source closed" not in events
        finally:
            release.set()
            await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(exercise())
    assert events.index("thread finished") < events.index("source closed")
    assert events.index("source closed") < events.index("listserv services")
    assert events.index("listserv services") < events.index("embedder")
    assert events.index("embedder") < events.index("engine")
    assert handlers  # module imported for the shared _to_thread path


# ---------------------------------------------------------------------------
# Real service loops with deterministic fake sources, driven through handlers
# ---------------------------------------------------------------------------


class _Source:
    """Deterministic fake LISTSERV source recording every fetched URL."""

    def __init__(self, pages, on_fetch=None):
        from doc3gpp.scraping.listserv_source import FetchedPage

        self._page_cls = FetchedPage
        self.pages = pages
        self.requests: list[str] = []
        self.on_fetch = on_fetch
        self.closed = False
        self.closed_while_fetching = False
        self._active = 0

    def fetch(self, url):
        self.requests.append(url)
        self._active += 1
        try:
            if self.on_fetch is not None:
                self.on_fetch(url)
            entry = self.pages[url]
            if isinstance(entry, self._page_cls):
                return entry
            return self._page_cls(text=entry, content_type="text/html", final_url=url)
        finally:
            self._active -= 1

    def close(self):
        self.closed_while_fetching = self._active > 0
        self.closed = True


def _listing(rows, next_url=None):
    body = "<tr><th>Subject</th><th>From</th><th>Date</th></tr>" + "".join(
        f'<tr><td><a href="{h}">{s}</a></td><td>Alice</td><td>{d}</td></tr>'
        for h, s, d in rows
    )
    nav = f'<div><a href="{next_url}" rel="next">Next Page</a></div>' if next_url else ""
    return f"<html><body>{nav}<table>{body}</table></body></html>"


def _detail(body_url):
    return (
        "<html><body><table>"
        "<tr><th>Subject:</th><td>S</td></tr>"
        "<tr><th>From:</th><td>Alice</td></tr>"
        "<tr><th>Date:</th><td>2026-10-01 12:00:00 +0000</td></tr>"
        f'<tr><th>Parts/Attachments:</th><td><a href="{body_url}">text/plain</a></td></tr>'
        "</table></body></html>"
    )


def test_sync_handler_cancel_passes_event_and_stops_later_pages(sqlite_env, monkeypatch):
    from doc3gpp.services.listserv_sync import ListservSyncService
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.listserv_sql import SQLAlchemyListservRepository

    create_schema("main")
    lid = "CANCEL_LIST"
    index = f"https://list.etsi.org/scripts/wa.exe?A0={lid}"
    p1 = f"https://list.etsi.org/scripts/wa.exe?A1=ind2603W1&L={lid}"
    p2 = f"https://list.etsi.org/scripts/wa.exe?A1=ind2604W2&L={lid}"
    p1_next = p1 + "&S=2"
    m1 = f"https://list.etsi.org/scripts/wa.exe?A2=ind2603W1&L={lid}&P=1"
    m1n = f"https://list.etsi.org/scripts/wa.exe?A2=ind2603W1&L={lid}&P=2"
    m2 = f"https://list.etsi.org/scripts/wa.exe?A2=ind2604W2&L={lid}&P=1"
    index_html = (
        "<html><body><ul>"
        f'<li><a href="{p1}">March 2026, Week 1</a></li>'
        f'<li><a href="{p2}">April 2026, Week 2</a></li>'
        "</ul></body></html>"
    )
    cancel_box = {}
    source = _Source(
        {
            index: index_html,
            p1: _listing([(m1, "Msg1", "2026-03-03 10:00:00 +0000")], next_url=p1_next),
            p1_next: _listing([(m1n, "Msg1n", "2026-03-04 10:00:00 +0000")]),
            p2: _listing([(m2, "Msg2", "2026-04-10 10:00:00 +0000")]),
        },
        on_fetch=lambda url: cancel_box["e"].set() if url == p1 else None,
    )
    from datetime import date

    service = ListservSyncService(
        repository=SQLAlchemyListservRepository(),
        source=source,
        today_fn=lambda: date(2026, 4, 25),
        max_workers=1,
    )
    monkeypatch.setattr(factory, "build_listserv_sync_service", lambda settings=None: service)

    async def go():
        coro, cancel, _l = await _invoke(
            JobKind.LISTSERV_SYNC,
            SimpleNamespace(),
            _job(JobKind.LISTSERV_SYNC, list_id=lid, dates=[">= 2026-03-01", "<= 2026-04-30"]),
        )
        cancel_box["e"] = cancel
        with pytest.raises(asyncio.CancelledError):
            await coro

    asyncio.run(go())
    # The first page was in flight; later pages (next page, second period) were never fetched.
    assert source.requests == [index, p1]
    assert p1_next not in source.requests and p2 not in source.requests
    repo = SQLAlchemyListservRepository()
    assert repo.get(m1) is not None
    assert repo.get(m1n) is None and repo.get(m2) is None
    # Source closed only after the blocking call finished.
    assert source.closed and not source.closed_while_fetching


def test_parse_handler_cancel_passes_event_and_stops_later_messages(sqlite_env, monkeypatch):
    from datetime import datetime, timezone

    from doc3gpp.models.listserv import ArchivePage, ListservMessage, parse_date_set
    from doc3gpp.scraping.listserv_source import FetchedPage
    from doc3gpp.services.listserv_parse import ListservParseService
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.listserv_sql import SQLAlchemyListservRepository

    create_schema("main")
    lid = "CANCEL_LIST"
    repo = SQLAlchemyListservRepository()
    page = ArchivePage(
        list_id=lid,
        url=f"https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L={lid}",
        period="2026-10-W1",
    )
    urls = [f"https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L={lid}&P={i}" for i in range(1, 5)]
    msgs = [
        ListservMessage(
            url=u, list_id=lid, subject=f"Topic {i}", sender="Alice <x>",
            date=datetime(2026, 10, 1, 0, i, tzinfo=timezone.utc),
            date_precision="datetime", week="2026-10-W1", subject_tdocs=(),
        )
        for i, u in enumerate(urls, start=1)
    ]
    repo.commit_page(page, msgs, parse_date_set(["= 2026-10-01"]), ())
    pages = {}
    for i, u in enumerate(urls, start=1):
        body = u.replace("A2=", "A3=") + "&T=text%2Fplain"
        pages[u] = _detail(body)
        pages[body] = FetchedPage(text=f"Body {i}", content_type="text/plain", final_url=body)
    first = urls[-1]  # newest first
    first_body = first.replace("A2=", "A3=") + "&T=text%2Fplain"
    cancel_box = {}
    source = _Source(
        pages, on_fetch=lambda url: cancel_box["e"].set() if url == first else None
    )
    service = ListservParseService(repository=repo, source=source, max_workers=1)
    monkeypatch.setattr(factory, "build_listserv_parse_service", lambda settings=None: service)

    async def go():
        coro, cancel, _l = await _invoke(
            JobKind.LISTSERV_PARSE, SimpleNamespace(),
            _job(JobKind.LISTSERV_PARSE, list_id=lid, dates=["= 2026-10-01"]),
        )
        cancel_box["e"] = cancel
        with pytest.raises(asyncio.CancelledError):
            await coro

    asyncio.run(go())
    assert source.requests == [first, first_body]
    assert repo.get(first).detail.content == "Body 4"
    for later in urls[:3]:
        assert repo.get(later).detail is None
    assert source.closed and not source.closed_while_fetching



@pytest.mark.parametrize(
    ("kind", "builder_name"),
    [
        (JobKind.LISTSERV_SYNC, "build_listserv_sync_service"),
        (JobKind.LISTSERV_PARSE, "build_listserv_parse_service"),
    ],
)
def test_listserv_worker_passes_shared_embedder_factory(
    monkeypatch, kind, builder_name
):
    from types import SimpleNamespace

    from doc3gpp.services import factory

    provider = lambda: object()
    service = FakeService()
    seen = {}

    def build_service(settings=None, *, embedder_factory=None):
        seen["provider"] = embedder_factory
        return service

    monkeypatch.setattr(factory, builder_name, build_service)
    job = _job(kind, list_id="EXAMPLE", dates=["= 2026-10-01"])
    services = SimpleNamespace(
        job_repo=_Repo(job),
        pipeline=None,
        listserv_embedder_factory=provider,
    )

    async def run():
        task, _cancel, _logs = await _invoke(kind, services, job)
        return await task

    asyncio.run(run())

    assert seen["provider"] is provider
    assert service.events[-1] == "close"
