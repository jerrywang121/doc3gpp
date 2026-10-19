import asyncio
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from doc3gpp.models.jobs import Job, JobKind, JobStatus
from doc3gpp.settings.schema import Settings
from doc3gpp.web.workers.handlers import _parse_tdocs
from doc3gpp.web.workers.pipeline import PipelineRuntime


def _job() -> Job:
    return Job(
        id="job",
        kind=JobKind.PARSE_TDOCS,
        status=JobStatus.RUNNING,
        params={"filter": {}},
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
