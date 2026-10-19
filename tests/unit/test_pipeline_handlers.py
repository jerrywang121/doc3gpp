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
