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
    """A 3GPP-URL ``PARSE_TDOC_URL`` job runs through the shared runner."""
    import hashlib

    from doc3gpp.models.jobs import JobKind, JobStatus
    from doc3gpp.models.tdoc_cr import (
        DirectParseResult,
        TDocCRDetails,
        TDocExtractMeta,
    )
    from doc3gpp.models.tdoc_document import TDocDocument
    from doc3gpp.services.pipeline_runtime import PipelineRuntime

    state, worker = _make_state_and_worker()
    pipeline = PipelineRuntime(prepare_workers=2)
    state.services.pipeline = pipeline

    source_file = "z/R5-260001.docx"
    markdown = "queued main document body"
    document_id = hashlib.sha256(b"queued-url-main").hexdigest()
    document = TDocDocument(
        document_id=document_id,
        source_file=source_file,
        markdown_file=f"documents/{document_id}/R5-260001.md",
        file_order=0,
        markdown_text=markdown,
        content_hash=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
    )
    fake_result = DirectParseResult(
        source_kind="url-3gpp",
        documents=(document,),
        main_source_file=source_file,
        details=TDocCRDetails(tdoc_id="R5-260001"),
        extract_meta=TDocExtractMeta(
            ftp_url="Docs/R5s260001.zip",
            tdoc_id="R5-260001",
            cache_file="R5s260001.zip",
            doc_filename=source_file,
            extraction_version=2,
            document_count=1,
        ),
        from_cache=False,
        persisted=True,
        tdoc_id="R5-260001",
        tdoc_id_in_tdocs=True,
        source_url="https://www.3gpp.org/ftp/R5s260001.zip",
    )
    assert fake_result.documents == (document,)
    assert fake_result.main_source_file == source_file

    def fake_prepare(
        url,
        *,
        force,
        max_tdoc_size_bytes,
    ):
        assert url.endswith("R5s260001.zip")
        assert force is False
        assert max_tdoc_size_bytes is not None
        return url

    def fake_persist(prepared):
        assert prepared.endswith("R5s260001.zip")
        return fake_result

    monkeypatch.setattr(
        state.services.tdoc_cr,
        "collect_3gpp_file_urls",
        lambda url, *, max_depth: [
            "https://www.3gpp.org/ftp/R5s260001.zip"
        ],
    )
    monkeypatch.setattr(state.services.tdoc_cr, "prepare_from_url", fake_prepare)
    monkeypatch.setattr(
        state.services.tdoc_cr,
        "persist_prepared_url",
        fake_persist,
    )
    state.settings.sync.auto_sync = False

    job = state.services.job_repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/", "max_depth": 2},
    )

    try:
        _run_once(worker, state.services.job_repo, job)
    finally:
        pipeline.shutdown()

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
                "main_source_file": source_file,
                "documents": [
                    {
                        "document_id": document_id,
                        "source_file": source_file,
                        "file_order": 0,
                    },
                ],
            },
        ],
    }
    assert any("done:" in line for line in done.log_lines)
    assert any("1 document" in line for line in done.log_lines)


def test_parse_tdoc_url_job_keeps_member_source_in_failure_details(
    sqlite_env, monkeypatch,
) -> None:
    from doc3gpp.models.jobs import JobKind, JobStatus
    from doc3gpp.services.pipeline_runtime import PipelineRuntime

    state, worker = _make_state_and_worker()
    pipeline = PipelineRuntime(prepare_workers=1)
    state.services.pipeline = pipeline
    state.settings.sync.auto_sync = False

    source_url = "https://www.3gpp.org/ftp/Docs/R5s260099.zip"
    source_file = "b/shared.docx"
    failure = (
        "Failed to convert supported DOCX member 'b/shared.docx': invalid package"
    )
    monkeypatch.setattr(
        state.services.tdoc_cr,
        "collect_3gpp_file_urls",
        lambda _url, *, max_depth: [source_url],
    )

    def fail_prepare(_url, **_kwargs):
        raise ValueError(failure)

    monkeypatch.setattr(state.services.tdoc_cr, "prepare_from_url", fail_prepare)
    job = state.services.job_repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/Docs/", "max_depth": 1},
    )

    try:
        _run_once(worker, state.services.job_repo, job)
    finally:
        pipeline.shutdown()

    done = state.services.job_repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.SUCCEEDED
    assert done.result_summary is not None
    assert done.result_summary["requested"] == 1
    assert done.result_summary["successes"] == 0
    assert done.result_summary["failures"] == 1
    assert done.result_summary["failure_details"] == {
        source_url: f"ValueError: {failure}",
    }
    assert source_file in done.result_summary["failure_details"][source_url]
