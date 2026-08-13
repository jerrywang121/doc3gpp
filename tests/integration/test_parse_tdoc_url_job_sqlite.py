"""Integration test: a ``PARSE_TDOC_URL`` job is enqueued, the worker runs
the handler, and the result lands in the ``jobs`` row with the right
``result_summary`` + ``log_lines``.

Mirrors the offline-sqlite pattern used by the existing job tests. The
service layer is patched so no 3GPP network call is made.
"""
from __future__ import annotations

import asyncio


def _make_state_and_worker():
    from doc3gpp.settings.loader import get_settings
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.web.app import build_state
    from doc3gpp.web.workers.job_worker import JobWorker

    create_schema()
    state = build_state(get_settings())
    worker = JobWorker(state, repo=state.services.job_repo)
    return state, worker


def _run_once(worker, repo, job) -> None:
    async def _claim() -> None:
        sem = asyncio.Semaphore(1)
        await worker._claim_and_run(job, sem)  # type: ignore[attr-defined]

    asyncio.run(_claim())


def test_parse_tdoc_url_job_end_to_end(sqlite_env, monkeypatch) -> None:
    """A 3GPP-URL ``PARSE_TDOC_URL`` job runs, succeeds, summary correct."""
    from doc3gpp.models.jobs import JobKind, JobStatus
    from doc3gpp.models.tdoc_cr import DirectParseBatchResult, DirectParseResult

    state, worker = _make_state_and_worker()

    fake_result = DirectParseBatchResult(
        results=[
            DirectParseResult(
                source_kind="url-3gpp",
                markdown="",
                details=None,
                extract_meta=None,
                from_cache=False,
                persisted=True,
                tdoc_id="R5-260001",
                tdoc_id_in_tdocs=True,
                source_url="https://www.3gpp.org/ftp/R5s260001.zip",
            ),
        ],
        failures={},
        skipped={},
    )

    def fake_extract_from_url_batch(
        url, *, max_depth, force, full, max_tdoc_size_bytes
    ):
        assert url.startswith("https://www.3gpp.org/ftp/")
        assert max_depth == 2  # default
        assert force is False
        assert full is False
        return fake_result

    monkeypatch.setattr(
        state.services.tdoc_cr,
        "extract_from_url_batch",
        fake_extract_from_url_batch,
    )
    monkeypatch.setattr(
        state.services.tdoc_cr,
        "collect_3gpp_file_urls",
        lambda url, *, max_depth: (),
    )
    state.settings.sync.auto_sync = False

    job = state.services.job_repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/", "max_depth": 2},
    )

    _run_once(worker, state.services.job_repo, job)

    done = state.services.job_repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.SUCCEEDED
    assert done.result_summary == {
        "requested": 1,
        "successes": 1,
        "failures": 0,
        "skipped": 0,
        "files": [
            {
                "tdoc_id": "R5-260001",
                "ftp_url": "https://www.3gpp.org/ftp/R5s260001.zip",
                "status": "ok",
            },
        ],
    }
    assert any("done:" in line for line in done.log_lines)
