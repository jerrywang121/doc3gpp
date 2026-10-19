import asyncio
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from doc3gpp.models.jobs import Job, JobKind, JobStatus
from doc3gpp.models.tdoc_cr import DirectParseResult
from doc3gpp.services.tdoc_cr_service import TDocNotYetOnFTPError, TDocTooLargeError
from doc3gpp.settings.schema import Settings
from doc3gpp.web.workers.handlers import _parse_tdoc_url, _parse_tdocs
from doc3gpp.web.workers.pipeline import PipelineRuntime


def _job(*, max_batch: int | None = None) -> Job:
    return Job(
        id="job",
        kind=JobKind.PARSE_TDOCS,
        status=JobStatus.RUNNING,
        params=(
            {"filter": {}}
            if max_batch is None
            else {"filter": {}, "max_batch": max_batch}
        ),
        log_lines=(),
        result_summary=None,
        error=None,
        created_at=datetime.now(timezone.utc),
        started_at=None,
        finished_at=None,
    )


def _url_job(url: str) -> Job:
    return Job(
        id="url-job",
        kind=JobKind.PARSE_TDOC_URL,
        status=JobStatus.RUNNING,
        params={"url": url},
        log_lines=(),
        result_summary=None,
        error=None,
        created_at=datetime.now(timezone.utc),
        started_at=None,
        finished_at=None,
    )


def test_parse_tdocs_prepares_in_parallel_and_persists_on_shared_lane():
    async def run():
        return await _parse_tdocs(
            _job(), services, Settings(), progress=lambda *_args, **_kwargs: None,
            cancel_event=asyncio.Event(),
        )

    pipeline = PipelineRuntime(prepare_workers=2)
    prep_started = threading.Barrier(2)
    active_writes = 0
    max_active_writes = 0
    lock = threading.Lock()

    def prepare(tdoc_id, **_kwargs):
        prep_started.wait(timeout=2)
        return tdoc_id

    def persist(prepared):
        nonlocal active_writes, max_active_writes
        with lock:
            active_writes += 1
            max_active_writes = max(max_active_writes, active_writes)
        time.sleep(0.02)
        with lock:
            active_writes -= 1
        return prepared

    services = SimpleNamespace(
        pipeline=pipeline,
        tdoc_repo=SimpleNamespace(
            list=lambda **_kwargs: [SimpleNamespace(tdoc_id="A"), SimpleNamespace(tdoc_id="B")]
        ),
        tdoc_cr=SimpleNamespace(
            prepare_extract=prepare,
            persist_prepared_extract=persist,
        ),
    )
    try:
        result = asyncio.run(run())
    finally:
        pipeline.shutdown()

    assert result["successes"] == 2
    assert max_active_writes == 1


def test_parse_tdocs_parallel_preserves_failure_details():
    async def run():
        return await _parse_tdocs(
            _job(max_batch=2), services, Settings(), progress=lambda *_args, **_kwargs: None,
            cancel_event=asyncio.Event(),
        )

    pipeline = PipelineRuntime(prepare_workers=4)

    def prepare(tdoc_id, **_kwargs):
        if tdoc_id == "FAIL":
            raise ValueError("bad parse")
        if tdoc_id == "FTP":
            raise TDocNotYetOnFTPError(tdoc_id)
        if tdoc_id == "BIG":
            raise TDocTooLargeError(source=tdoc_id, size=10, limit=5)
        return tdoc_id

    services = SimpleNamespace(
        pipeline=pipeline,
        tdoc_repo=SimpleNamespace(
            list=lambda **_kwargs: [
                SimpleNamespace(tdoc_id="OK"),
                SimpleNamespace(tdoc_id="FAIL"),
                SimpleNamespace(tdoc_id="FTP"),
                SimpleNamespace(tdoc_id="BIG"),
            ]
        ),
        tdoc_cr=SimpleNamespace(
            prepare_extract=prepare,
            persist_prepared_extract=lambda prepared: prepared,
        ),
    )
    try:
        result = asyncio.run(run())
    finally:
        pipeline.shutdown()

    assert result["requested"] == 4
    assert result["successes"] == 1
    assert result["failures"] == 1
    assert result["skipped"] == 2
    assert result["failure_details"] == {"FAIL": "ValueError: bad parse"}
    assert result["skipped_details"] == {
        "FTP": f"TDocNotYetOnFTPError: {TDocNotYetOnFTPError('FTP')}",
        "BIG": f"TDocTooLargeError: {TDocTooLargeError(source='BIG', size=10, limit=5)}",
    }


def test_parse_tdoc_url_folder_prepares_in_parallel_and_preserves_discovery_order():
    url = "https://www.3gpp.org/ftp/TSG_RAN/WG5/"
    files = [
        "https://www.3gpp.org/ftp/R5-260001.zip",
        "https://www.3gpp.org/ftp/R5-260002.zip",
    ]
    prep_started = threading.Barrier(2)
    active_writes = 0
    max_active_writes = 0
    persisted: list[str] = []
    lock = threading.Lock()

    def prepare(file_url, **_kwargs):
        prep_started.wait(timeout=2)
        time.sleep(0.03 if file_url == files[0] else 0.005)
        return file_url

    def persist(file_url):
        nonlocal active_writes, max_active_writes
        with lock:
            active_writes += 1
            max_active_writes = max(max_active_writes, active_writes)
            persisted.append(file_url)
        time.sleep(0.01)
        with lock:
            active_writes -= 1
        return DirectParseResult(
            source_kind="url-3gpp",
            markdown="",
            details=None,
            extract_meta=None,
            from_cache=False,
            persisted=True,
            tdoc_id=file_url.rsplit("/", 1)[-1].removesuffix(".zip"),
            tdoc_id_in_tdocs=True,
            source_url=file_url,
        )

    pipeline = PipelineRuntime(prepare_workers=2)
    services = SimpleNamespace(
        pipeline=pipeline,
        tdoc_cr=SimpleNamespace(
            collect_3gpp_file_urls=lambda _url, *, max_depth: list(files),
            prepare_from_url=prepare,
            persist_prepared_url=persist,
        ),
    )
    settings = Settings()
    settings.sync.auto_sync = False

    async def run():
        return await _parse_tdoc_url(
            _url_job(url),
            services,
            settings,
            progress=lambda *_args, **_kwargs: None,
            cancel_event=asyncio.Event(),
        )

    try:
        result = asyncio.run(run())
    finally:
        pipeline.shutdown()

    assert max_active_writes == 1
    assert sorted(persisted) == sorted(files)
    assert result["requested"] == 2
    assert result["successes"] == 2
    assert result["failures"] == 0
    assert result["skipped"] == 0
    assert [item["ftp_url"] for item in result["files"]] == files


def test_parse_tdoc_url_single_file_uses_one_item_runner_contract():
    file_url = "https://www.3gpp.org/ftp/RP-2601234.zip"
    persisted: list[str] = []

    def prepare(value, **_kwargs):
        return value

    def persist(value):
        persisted.append(value)
        return DirectParseResult(
            source_kind="url-3gpp",
            markdown="",
            details=None,
            extract_meta=None,
            from_cache=False,
            persisted=True,
            tdoc_id="RP-2601234",
            tdoc_id_in_tdocs=True,
            source_url=value,
        )

    pipeline = PipelineRuntime(prepare_workers=1)
    services = SimpleNamespace(
        pipeline=pipeline,
        tdoc_cr=SimpleNamespace(
            prepare_from_url=prepare,
            persist_prepared_url=persist,
        ),
    )
    settings = Settings()
    settings.sync.auto_sync = False

    async def run():
        return await _parse_tdoc_url(
            _url_job(file_url),
            services,
            settings,
            progress=lambda *_args, **_kwargs: None,
            cancel_event=asyncio.Event(),
        )

    try:
        result = asyncio.run(run())
    finally:
        pipeline.shutdown()

    assert persisted == [file_url]
    assert result["requested"] == 1
    assert result["successes"] == 1
    assert result["files"] == [
        {
            "tdoc_id": "RP-2601234",
            "ftp_url": file_url,
            "status": "ok",
        }
    ]


def test_shared_runtime_serializes_persistence_across_parse_jobs():
    pipeline = PipelineRuntime(prepare_workers=2)
    active_writes = 0
    max_active_writes = 0
    lock = threading.Lock()

    def make_services(tdoc_id: str):
        def persist(prepared):
            nonlocal active_writes, max_active_writes
            with lock:
                active_writes += 1
                max_active_writes = max(max_active_writes, active_writes)
            time.sleep(0.02)
            with lock:
                active_writes -= 1
            return prepared

        return SimpleNamespace(
            pipeline=pipeline,
            tdoc_repo=SimpleNamespace(
                list=lambda **_kwargs: [SimpleNamespace(tdoc_id=tdoc_id)]
            ),
            tdoc_cr=SimpleNamespace(
                prepare_extract=lambda value, **_kwargs: value,
                persist_prepared_extract=persist,
            ),
        )

    async def run():
        await asyncio.gather(
            _parse_tdocs(
                _job(),
                make_services("JOB-A"),
                Settings(),
                progress=lambda *_args, **_kwargs: None,
                cancel_event=asyncio.Event(),
            ),
            _parse_tdocs(
                _job(),
                make_services("JOB-B"),
                Settings(),
                progress=lambda *_args, **_kwargs: None,
                cancel_event=asyncio.Event(),
            ),
        )

    try:
        asyncio.run(run())
    finally:
        pipeline.shutdown()

    assert max_active_writes == 1


def _gated_runtime_services(pipeline, tdoc_ids, prepare, persist):
    return SimpleNamespace(
        pipeline=pipeline,
        tdoc_repo=SimpleNamespace(
            list=lambda **_kwargs: [SimpleNamespace(tdoc_id=i) for i in tdoc_ids]
        ),
        tdoc_cr=SimpleNamespace(
            prepare_extract=prepare,
            persist_prepared_extract=persist,
        ),
    )


def test_two_jobs_on_one_runtime_never_overlap_writer_callbacks():
    pipeline = PipelineRuntime(prepare_workers=2)
    first_in_writer = threading.Event()
    release_first = threading.Event()
    second_prepared = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0
    order: list[str] = []

    def persist(prepared):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            order.append(f"start:{prepared}")
        try:
            if prepared == "A":
                first_in_writer.set()
                # Job B is fully prepared and queued behind the blocked writer.
                assert second_prepared.wait(timeout=5)
                assert release_first.wait(timeout=5)
            return prepared
        finally:
            with lock:
                order.append(f"end:{prepared}")
                active -= 1

    def prepare(tdoc_id, **_kwargs):
        if tdoc_id == "B":
            assert first_in_writer.wait(timeout=5)
            second_prepared.set()
        return tdoc_id

    async def run():
        async def job(tdoc_id):
            return await _parse_tdocs(
                _job(),
                _gated_runtime_services(pipeline, [tdoc_id], prepare, persist),
                Settings(),
                progress=lambda *_a, **_k: None,
                cancel_event=asyncio.Event(),
            )

        task_a = asyncio.create_task(job("A"))
        task_b = asyncio.create_task(job("B"))
        while not second_prepared.is_set():
            await asyncio.sleep(0)
        release_first.set()
        return await asyncio.gather(task_a, task_b)

    try:
        results = asyncio.run(run())
    finally:
        release_first.set()
        pipeline.shutdown()

    assert [r["successes"] for r in results] == [1, 1]
    assert max_active == 1
    assert order == ["start:A", "end:A", "start:B", "end:B"]


def test_web_cancellation_and_shutdown_drain_submitted_writer_work():
    pipeline = PipelineRuntime(prepare_workers=1, max_pending=1)
    writer_started = threading.Event()
    release_writer = threading.Event()
    persisted: list[str] = []
    prepared_ids: list[str] = []
    cancel_event = asyncio.Event()

    def prepare(tdoc_id, **_kwargs):
        prepared_ids.append(tdoc_id)
        return tdoc_id

    def persist(prepared):
        writer_started.set()
        assert release_writer.wait(timeout=5)
        persisted.append(prepared)
        return prepared

    async def run():
        task = asyncio.create_task(
            _parse_tdocs(
                _job(),
                _gated_runtime_services(
                    pipeline, ["A", "B", "C"], prepare, persist
                ),
                Settings(),
                progress=lambda *_a, **_k: None,
                cancel_event=cancel_event,
            )
        )
        while not writer_started.is_set():
            await asyncio.sleep(0)
        cancel_event.set()
        release_writer.set()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        return "finished"

    try:
        outcome = asyncio.run(run())
    finally:
        release_writer.set()
        pipeline.shutdown()

    assert outcome == "cancelled"
    # The already-submitted write drained; nothing after cancellation started.
    assert persisted == ["A"]
    assert prepared_ids == ["A"]


def _done(tdoc_id):
    from doc3gpp.services.tdoc_cr_service import ExtractResult

    return ExtractResult(
        details=SimpleNamespace(tdoc_id=tdoc_id), extract_meta=None, from_cache=False
    )


def test_cli_service_and_web_classify_skips_and_failures_identically():
    from doc3gpp.services.tdoc_cr_service import TDocCrService

    ids = ["OK", "BAD", "FTP", "BIG"]

    def prepare(tdoc_id, **_kwargs):
        if tdoc_id == "BAD":
            raise ValueError("bad parse")
        if tdoc_id == "FTP":
            raise TDocNotYetOnFTPError(tdoc_id)
        if tdoc_id == "BIG":
            raise TDocTooLargeError(source=tdoc_id, size=10, limit=5)
        return tdoc_id

    service = object.__new__(TDocCrService)
    service.prepare_extract = prepare
    service.persist_prepared_extract = _done

    batch = service.extract_many(ids)
    assert batch.failures == {"BAD": "ValueError: bad parse"}
    assert set(batch.skipped) == {"FTP", "BIG"}

    pipeline = PipelineRuntime(prepare_workers=2)
    services = _gated_runtime_services(
        pipeline, ids, prepare, _done
    )

    async def run():
        return await _parse_tdocs(
            _job(),
            services,
            Settings(),
            progress=lambda *_a, **_k: None,
            cancel_event=asyncio.Event(),
        )

    try:
        web = asyncio.run(run())
    finally:
        pipeline.shutdown()

    assert web["failure_details"] == batch.failures
    assert web["skipped_details"] == batch.skipped
    assert web["skipped"] == len(batch.skipped)
    assert web["failures"] == len(batch.failures)


def _store_backed_service(*, hold_first_prepare: bool = False):
    from doc3gpp.services.tdoc_cr_service import ExtractResult, TDocCrService

    stored: set[str] = set()
    lock = threading.Lock()
    calls: list[str] = []
    persists: list[str] = []
    second_started = threading.Event()
    counter = {"n": 0}

    def prepare(tdoc_id, *, force=False, **_kwargs):
        with lock:
            counter["n"] += 1
            number = counter["n"]
            calls.append(tdoc_id)
            cached = tdoc_id in stored and not force
        if number == 2:
            second_started.set()
        elif number == 1 and hold_first_prepare:
            # Old code runs the repeat concurrently, so it starts promptly and
            # sees the empty store; the timeout is only the fixed-code path.
            second_started.wait(timeout=0.2)
        if cached:
            return ExtractResult(
                details=SimpleNamespace(tdoc_id=tdoc_id),
                extract_meta=None,
                from_cache=True,
            )
        return tdoc_id

    def persist(prepared):
        with lock:
            persists.append(prepared)
            stored.add(prepared)
        return ExtractResult(
            details=SimpleNamespace(tdoc_id=prepared),
            extract_meta=None,
            from_cache=False,
        )

    service = object.__new__(TDocCrService)
    service.prepare_extract = prepare
    service.persist_prepared_extract = persist
    return service, calls, persists


def test_extract_many_duplicate_repeat_sees_first_persist_with_parallel_runner():
    from doc3gpp.services.tdoc_pipeline import TDocPipeline

    service, calls, persists = _store_backed_service(hold_first_prepare=True)
    runtime = PipelineRuntime(prepare_workers=2)
    messages: list[str] = []
    try:
        batch = service.extract_many(
            ["A", "B", "A"],
            on_progress=messages.append,
            runner=TDocPipeline(runtime),
        )
    finally:
        runtime.shutdown()

    assert persists == ["A", "B"] or persists == ["B", "A"]
    assert batch.successes["A"].from_cache is True
    assert batch.successes["B"].from_cache is False
    assert list(batch.successes) == ["A", "B"]
    counts = [int(m.split()[1].split("/")[0]) for m in messages]
    assert counts == [1, 2, 3]
    assert messages[-1] == "parsed 3/3 TDocs"


def test_extract_many_duplicate_force_reparses_repeat_and_keeps_source_order():
    from doc3gpp.services.tdoc_pipeline import TDocPipeline

    service, calls, persists = _store_backed_service()
    runtime = PipelineRuntime(prepare_workers=2)
    try:
        batch = service.extract_many(
            ["B", "A", "B"],
            force=True,
            runner=TDocPipeline(runtime),
        )
    finally:
        runtime.shutdown()

    assert persists.count("B") == 2  # force: the repeat is re-parsed too
    assert batch.successes["B"].from_cache is False
    assert list(batch.successes) == ["B", "A"]


def test_shutdown_waits_for_blocked_submitted_writer_and_write_completes():
    pipeline = PipelineRuntime(prepare_workers=1)
    writer_started = threading.Event()
    release_writer = threading.Event()
    shutdown_returned = threading.Event()
    log: list[str] = []

    def persist(prepared):
        writer_started.set()
        assert release_writer.wait(timeout=5)
        log.append("write-done")
        return prepared

    permit = pipeline.reserve()
    future = pipeline.submit_persist(permit, persist, "A")
    assert writer_started.wait(timeout=5)

    def stop():
        pipeline.shutdown()
        log.append("shutdown-returned")
        shutdown_returned.set()

    stopper = threading.Thread(target=stop)
    stopper.start()
    try:
        # The writer is still blocked, so shutdown cannot have returned.
        assert not shutdown_returned.is_set()
        assert not future.done()
        release_writer.set()
        stopper.join(timeout=5)
        assert shutdown_returned.is_set()
    finally:
        release_writer.set()
        stopper.join(timeout=5)

    assert future.result(timeout=1) == "A"
    assert log == ["write-done", "shutdown-returned"]
