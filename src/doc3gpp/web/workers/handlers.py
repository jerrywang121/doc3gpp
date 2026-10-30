"""Per-:class:`JobKind` handlers for the background job worker.

This module is the **only** place that maps a :class:`JobKind` to the
service method that performs the work. Each handler is a thin
orchestrator: it reads the job's ``params``, calls into the existing
``services/factory.build_*`` methods, reports progress through the
``progress`` callback, honours cooperative cancellation via
``cancel_event``, and returns a JSON-shaped ``summary`` mapping.

No business logic is duplicated here — handlers never issue network
calls, parse documents, or run SQL directly. New job kinds land in
:data:`JobHandlers.KIND_TO_HANDLER`.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import ClassVar

from doc3gpp.cli_auto_sync import (
    collect_tdoc_candidates_for_url,
    trigger_auto_sync,
)
from doc3gpp.models.index import IndexRequest
from doc3gpp.models.jobs import Job, JobKind, JSONValue
from doc3gpp.models.spec_doc import (
    SpecDocBatchResult,
    SpecDocSelectionError,
    SpecDocSource,
    SpecDocTooLargeError,
)
from doc3gpp.models.tdoc_cr import DirectParseBatchResult, DirectParseResult
from doc3gpp.parsers.direct_extractor import (
    is_3gpp_file_url,
    validate_tdoc_ftp_url,
)
from doc3gpp.scraping.spec_doc_source import validate_release_version_alignment
from doc3gpp.services.tdoc_cr_service import ExtractResult
from doc3gpp.services.tdoc_family_service import FamilyExtractResult
from doc3gpp.services.tdoc_parse_selection import parse_selection_kwargs
from doc3gpp.services.tdoc_pipeline import PipelineItem, TDocPipeline
from doc3gpp.settings.schema import Settings
from doc3gpp.web.state import ServiceContainer

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, bool], None]
"""Signature of the ``progress`` callback passed to every handler.

The second ``force`` argument bypasses the worker's throttling interval so
the caller can guarantee a terminal-summary emission lands regardless of
when the previous live line was written. All intermediate per-item
progress calls use the default ``force=False`` and are throttled.

Formats and persists one log line (``[<ISO timestamp>] <message>``)
and fans it out to the job's SSE queue.
"""

Handler = Callable[
    [
        Job,
        ServiceContainer,
        Settings,
    ],
    Awaitable[Mapping[str, JSONValue]],
]
"""Handler signature — the ``progress`` / ``cancel_event`` kwargs are
appended by the worker when it resolves the handler (see
:meth:`doc3gpp.web.workers.job_worker.JobWorker.run`)."""


def _build_meeting_url(tsg: str) -> str:
    """Compose the 3GPP DynaReport meeting-calendar URL for ``tsg``."""
    return f"https://www.3gpp.org/dynareport?code=Meetings-{tsg.upper()}.htm"


async def _await_future(future):
    """Await a concurrent future without blocking the event loop."""
    return await asyncio.wrap_future(future)


async def _sync_meetings(
    job: Job,
    services: ServiceContainer,
    settings: Settings,
    *,
    progress: ProgressFn,
    cancel_event: asyncio.Event,
) -> Mapping[str, JSONValue]:
    tsg = job.params.get("tsg")
    if not tsg or not isinstance(tsg, str):
        raise ValueError("sync_meetings job requires a 'tsg' string parameter")
    if not services.tsg.is_known_short_name(tsg):
        raise ValueError(
            f"Unknown TSG short name '{tsg}'. "
            f"Run 'doc3gpp tsg list' for the full reference."
        )
    progress(f"syncing meetings for TSG {tsg}")
    url = _build_meeting_url(tsg)
    force = bool(job.params.get("force", False))
    if cancel_event.is_set():
        raise asyncio.CancelledError()
    outcome = await asyncio.to_thread(
        services.meeting.sync,
        url,
        tsg=tsg,
        force=force,
        on_progress=progress,
    )
    if cancel_event.is_set():
        raise asyncio.CancelledError()
    progress(outcome.reason, force=True)
    return {
        "status": outcome.status,
        "reason": outcome.reason,
        "synced_count": outcome.synced_count,
    }


async def _sync_wis(
    job: Job,
    services: ServiceContainer,
    settings: Settings,
    *,
    progress: ProgressFn,
    cancel_event: asyncio.Event,
) -> Mapping[str, JSONValue]:
    tsg = job.params.get("tsg")
    if not tsg or not isinstance(tsg, str):
        raise ValueError("sync_wis job requires a 'tsg' string parameter")
    if not services.tsg.is_known_short_name(tsg):
        raise ValueError(
            f"Unknown TSG short name '{tsg}'. "
            f"Run 'doc3gpp tsg list' for the full reference."
        )
    canonical_tsg = tsg.upper()
    progress(f"syncing WIs for TSG {canonical_tsg}")
    if cancel_event.is_set():
        raise asyncio.CancelledError()
    synced_count = await asyncio.to_thread(services.wi.sync, canonical_tsg)
    if cancel_event.is_set():
        raise asyncio.CancelledError()
    reason = f"WI sync complete: {synced_count} rows stored for {canonical_tsg}"
    progress(reason, force=True)
    return {
        "status": "synced",
        "reason": reason,
        "synced_count": synced_count,
    }


async def _sync_tdocs(
    job: Job,
    services: ServiceContainer,
    settings: Settings,
    *,
    progress: ProgressFn,
    cancel_event: asyncio.Event,
) -> Mapping[str, JSONValue]:
    force = bool(job.params.get("force", False))
    meeting_id = job.params.get("meeting_id")
    meeting_name = job.params.get("meeting_name")
    coordinator = services.tdoc_sync
    if cancel_event.is_set():
        raise asyncio.CancelledError()
    if meeting_id is not None:
        progress(f"syncing TDocs for meeting id {meeting_id}")
        outcome = await asyncio.to_thread(
            coordinator.sync_for_meeting_id,
            int(meeting_id),
            force=force,
            on_progress=progress,
        )
    elif meeting_name is not None:
        progress(f"syncing TDocs for meeting {meeting_name}")
        outcome = await asyncio.to_thread(
            coordinator.sync_for_meeting_name,
            str(meeting_name),
            force=force,
            on_progress=progress,
        )
    else:
        raise ValueError(
            "sync_tdocs job requires a 'meeting_id' or 'meeting_name' parameter"
        )
    if cancel_event.is_set():
        raise asyncio.CancelledError()
    progress(outcome.reason, force=True)
    return {
        "status": outcome.status,
        "reason": outcome.reason,
        "synced_count": outcome.synced_count,
        "file_count": outcome.file_count,
    }


async def _sync_tdocs_all(
    job: Job,
    services: ServiceContainer,
    settings: Settings,
    *,
    progress: ProgressFn,
    cancel_event: asyncio.Event,
) -> Mapping[str, JSONValue]:
    force = bool(job.params.get("force", False))
    progress("syncing TDocs for all tracked meetings")
    if cancel_event.is_set():
        raise asyncio.CancelledError()
    outcome = await asyncio.to_thread(
        services.tdoc_sync.sync_all_tracked_meetings,
        force=force,
        on_progress=progress,
    )
    if cancel_event.is_set():
        raise asyncio.CancelledError()
    progress(
        f"bulk sync complete: {outcome.synced_count} synced, "
        f"{outcome.skipped_count} skipped, {outcome.failed_count} failed",
        force=True,
    )
    return {
        "synced": outcome.synced_count,
        "skipped": outcome.skipped_count,
        "failed": outcome.failed_count,
        "total": outcome.total,
    }


async def _sync_specs(
    job: Job,
    services: ServiceContainer,
    settings: Settings,
    *,
    progress: ProgressFn,
    cancel_event: asyncio.Event,
) -> Mapping[str, JSONValue]:
    tsg = job.params.get("tsg")
    spec_id = job.params.get("spec_id")
    force = bool(job.params.get("force", False))
    per_version_details = bool(job.params.get("per_version_details", False))
    if (tsg is None) == (spec_id is None):
        raise ValueError("sync_specs job requires exactly one of 'tsg' or 'spec_id'")

    if spec_id is not None:
        if not isinstance(spec_id, str):
            raise ValueError("sync_specs job requires a 'spec_id' string parameter")
        progress(f"syncing spec {spec_id}")
    else:
        if not tsg or not isinstance(tsg, str):
            raise ValueError("sync_specs job requires a 'tsg' string parameter")
        if not services.tsg.is_known_short_name(tsg):
            raise ValueError(
                f"Unknown TSG short name '{tsg}'. "
                f"Run 'doc3gpp tsg list' for the full reference."
            )
        progress(f"syncing specs for TSG {tsg}")

    def on_progress(event: str, data: Mapping[str, object]) -> None:
        if event == "list_parsed":
            progress(f"parsed {data.get('total', 0)} specs for TSG {tsg}")
        elif event == "spec_done":
            progress(f"spec {data.get('spec_id', '')} done")

    if cancel_event.is_set():
        raise asyncio.CancelledError()
    if spec_id is not None:
        outcome = await asyncio.to_thread(
            services.spec.sync_spec,
            spec_id,
            force=force,
            per_version_details=per_version_details,
            on_progress=on_progress,
        )
    else:
        outcome = await asyncio.to_thread(
            services.spec.sync,
            tsg,
            force=force,
            per_version_details=per_version_details,
            on_progress=on_progress,
        )
    if cancel_event.is_set():
        raise asyncio.CancelledError()
    progress(outcome.reason, force=True)
    return {
        "status": outcome.status,
        "reason": outcome.reason,
        "synced_count": outcome.synced_count,
        "version_count": outcome.version_count,
    }


async def _sync_testcases(
    job: Job,
    services: ServiceContainer,
    settings: Settings,
    *,
    progress: ProgressFn,
    cancel_event: asyncio.Event,
) -> Mapping[str, JSONValue]:
    force = bool(job.params.get("force", False))
    progress("syncing testcases")

    def on_progress(event: str, data: Mapping[str, object]) -> None:
        if event == "listing":
            progress("fetched History listing")
        elif event == "downloaded":
            progress(f"downloaded {data.get('filename', '')}")
        elif event == "parsed":
            progress(f"parsed {data.get('testcases', 0)} testcases")

    if cancel_event.is_set():
        raise asyncio.CancelledError()
    outcome = await asyncio.to_thread(
        services.testcase.sync, force=force, on_progress=on_progress
    )
    if cancel_event.is_set():
        raise asyncio.CancelledError()
    progress(outcome.reason, force=True)
    return {
        "status": outcome.status,
        "reason": outcome.reason,
        "synced_count": outcome.synced_count,
    }


async def _parse_tdocs(
    job: Job,
    services: ServiceContainer,
    settings: Settings,
    *,
    progress: ProgressFn,
    cancel_event: asyncio.Event,
) -> Mapping[str, JSONValue]:
    filters = job.params.get("filter", {})
    if not isinstance(filters, dict):
        raise ValueError(  # noqa: TRY004 - preserve the handler's public error type
            "parse_tdocs job requires a 'filter' mapping parameter"
        )
    force = bool(job.params.get("force", False))
    max_batch = job.params.get("max_batch")
    max_batch = int(max_batch) if max_batch is not None else settings.tdoc_parse.max_batch

    _KNOWN_LIST_FILTERS = {
        "tdoc_id",
        "meeting",
        "meeting_id",
        "status",
        "cr_cat",
        "spec",
        "wi",
        "revision_of",
        "revised_to",
        "title",
        "ftp_url",
        "source",
        "tdoc_type",
        "uploaded_date",
        "release",
        "version",
        "cr_num",
        "cr_pack",
    }
    list_filters = {
        k: v
        for k, v in filters.items()
        if k in _KNOWN_LIST_FILTERS and k != "tdoc_type"
    }
    list_filters.update(parse_selection_kwargs(filters.get("tdoc_type")))
    if "meeting" in list_filters:
        list_filters["meeting_like"] = list_filters.pop("meeting")
    tdocs = services.tdoc_repo.list(
        limit=max_batch,
        offset=0,
        exclude_parsed=not force,
        **list_filters,
    )
    tdoc_ids = [t.tdoc_id for t in tdocs]
    if not tdoc_ids:
        progress("no TDocs matched the given filter")
        return {
            "requested": 0,
            "successes": 0,
            "failures": 0,
            "skipped": 0,
            "failure_details": {},
            "skipped_details": {},
        }

    progress(f"parsing {len(tdoc_ids)} TDocs in batches of {max_batch}")
    total_successes: dict[str, object] = {}
    total_failures: dict[str, str] = {}
    total_skipped: dict[str, str] = {}
    runner = TDocPipeline(services.pipeline) if services.pipeline is not None else None
    for start in range(0, len(tdoc_ids), max_batch):
        if cancel_event.is_set():
            raise asyncio.CancelledError()
        batch = tdoc_ids[start : start + max_batch]
        if runner is None:
            result = await asyncio.to_thread(
                services.tdoc_cr.extract_many,
                batch,
                force=force,
                is_cancelled=cancel_event.is_set,
            )
            total_successes.update(result.successes)
            total_failures.update(result.failures)
            total_skipped.update(result.skipped)
        else:
            outcomes = await runner.run_async(
                [PipelineItem(key=tdoc_id, value=tdoc_id) for tdoc_id in batch],
                lambda tdoc_id: services.tdoc_cr.prepare_extract(
                    tdoc_id,
                    force=force,
                ),
                persist=services.tdoc_cr.persist_prepared_extract,
                is_complete=lambda value: isinstance(value, (ExtractResult, FamilyExtractResult)),
                is_cancelled=cancel_event.is_set,
            )
            for outcome in outcomes:
                reason = outcome.reason or ""
                if outcome.status == "success":
                    total_successes[outcome.key] = outcome.value
                elif outcome.status == "skipped":
                    total_skipped[outcome.key] = reason
                else:
                    total_failures[outcome.key] = reason
        progress(
            f"batch {start // max_batch + 1}: {len(batch)} requested, "
            f"{len(total_successes)} ok, {len(total_failures)} failed, "
            f"{len(total_skipped)} skipped"
        )
    return {
        "requested": len(tdoc_ids),
        "successes": len(total_successes),
        "failures": len(total_failures),
        "skipped": len(total_skipped),
        "failure_details": dict(total_failures),
        "skipped_details": dict(total_skipped),
    }


async def _parse_tdoc_url(
    job: Job,
    services: ServiceContainer,
    settings: Settings,
    *,
    progress: ProgressFn,
    cancel_event: asyncio.Event,
) -> Mapping[str, JSONValue]:
    raw_url = job.params.get("url")
    try:
        url = validate_tdoc_ftp_url(raw_url)
    except ValueError as exc:
        raise ValueError(
            f"parse_tdoc_url job requires a valid 3GPP FTP 'url' parameter: {exc}"
        ) from exc
    force = bool(job.params.get("force", False))
    recursive = bool(job.params.get("recursive", False))
    max_depth_param = job.params.get("max_depth")
    if recursive:
        max_depth = settings.tdoc_parse.max_ftp_depth
    elif max_depth_param is not None:
        max_depth = max(int(max_depth_param), 0)
    else:
        max_depth = settings.tdoc_parse.max_ftp_depth

    if settings.sync.auto_sync:
        candidates = collect_tdoc_candidates_for_url(
            url,
            tdoc_service=services.tdoc_cr,
            max_depth=max_depth,
        )
        if candidates:
            progress(
                f"auto-sync: {len(candidates)} candidate tdoc_id(s) from URL — "
                f"running trigger_auto_sync"
            )
            try:
                trigger_auto_sync(
                    auto_sync_enabled=True,
                    meeting_service=services.meeting,
                    tdoc_sync_coordinator=services.tdoc_sync,
                    tdoc_ids=candidates,
                    on_progress=progress,
                )
            except Exception as exc:  # noqa: BLE001 - CLI parity: warn, do not abort
                logger.warning("auto_sync from URL %s failed: %s", url, exc)

    max_tdoc_size_bytes = (
        settings.tdoc_parse.max_tdoc_size_kb * 1024
        if settings.tdoc_parse.max_tdoc_size_kb > 0
        else 0
    )

    if cancel_event.is_set():
        raise asyncio.CancelledError()
    if services.pipeline is None:
        if is_3gpp_file_url(url):
            failures: dict[str, str] = {}
            try:
                result = await asyncio.to_thread(
                    services.tdoc_cr.extract_from_url,
                    url,
                    force=force,
                    max_tdoc_size_bytes=max_tdoc_size_bytes or None,
                )
            except Exception as exc:
                logger.warning("Failed to parse %s: %s", url, exc, exc_info=True)
                failures[url] = f"{type(exc).__name__}: {exc}"
                batch = DirectParseBatchResult(
                    results=[], failures=failures, skipped={}
                )
            else:
                batch = DirectParseBatchResult(
                    results=[result], failures={}, skipped={}
                )
        else:
            batch = await asyncio.to_thread(
                services.tdoc_cr.extract_from_url_batch,
                url,
                max_depth=max_depth,
                force=force,
                max_tdoc_size_bytes=max_tdoc_size_bytes or None,
                on_progress=progress,
                is_cancelled=cancel_event.is_set,
            )
    else:
        if is_3gpp_file_url(url):
            file_urls = [url]
        else:
            # Discovery remains a synchronous, deterministic breadth-first
            # operation. Run it off the event loop, then hand its source order
            # to the shared async runner for preparation/persistence.
            file_urls = await asyncio.to_thread(
                services.tdoc_cr.collect_3gpp_file_urls,
                url,
                max_depth=max_depth,
            )
        runner = TDocPipeline(services.pipeline)

        def report_pipeline(item_progress) -> None:
            progress(
                f"parsed {item_progress.completed}/{item_progress.total} files"
            )

        outcomes = await runner.run_async(
            [PipelineItem(key=file_url, value=file_url) for file_url in file_urls],
            lambda file_url: services.tdoc_cr.prepare_from_url(
                file_url,
                force=force,
                max_tdoc_size_bytes=max_tdoc_size_bytes or None,
            ),
            persist=services.tdoc_cr.persist_prepared_url,
            is_complete=lambda value: isinstance(value, DirectParseResult),
            is_cancelled=cancel_event.is_set,
            on_progress=(
                report_pipeline
                if not is_3gpp_file_url(url)
                else None
            ),
        )
        results: list[DirectParseResult] = []
        failures: dict[str, str] = {}
        skipped: dict[str, str] = {}
        for outcome in outcomes:
            reason = outcome.reason or ""
            if outcome.status == "success":
                if outcome.value is not None:
                    results.append(outcome.value)
            elif outcome.status == "skipped":
                skipped[outcome.key] = reason
            else:
                failures[outcome.key] = reason
        batch = DirectParseBatchResult(
            results=results,
            failures=failures,
            skipped=skipped,
        )
    if cancel_event.is_set():
        raise asyncio.CancelledError()

    files: list[dict[str, str]] = []
    for r in batch.results:
        if r.tdoc_id is None:
            continue
        files.append({
            "tdoc_id": r.tdoc_id,
            "ftp_url": r.source_url or "",
            "status": "ok" if r.persisted else "parsed-no-fk",
            "main_source_file": r.main_source_file,
            "documents": [
                {
                    "document_id": document.document_id,
                    "source_file": document.source_file,
                    "file_order": document.file_order,
                }
                for document in r.documents
            ],
        })

    document_count = sum(len(result.documents) for result in batch.results)
    document_label = "document" if document_count == 1 else "documents"
    progress(
        f"done: {len(batch.results)} parsed, {document_count} {document_label}, "
        f"{len(batch.failures)} failed, {len(batch.skipped)} skipped",
        force=True,
    )

    summary: dict[str, JSONValue] = {
        "requested": len(batch.results) + len(batch.failures) + len(batch.skipped),
        "successes": len(batch.results),
        "failures": len(batch.failures),
        "skipped": len(batch.skipped),
        "files": files,
    }
    if batch.failures:
        # The pipeline's failure messages carry the failing DOCX member path;
        # return them so queued jobs retain the same source-level detail as CLI.
        summary["failure_details"] = dict(batch.failures)
    return summary


async def _parse_spec_docs(
    job: Job,
    services: ServiceContainer,
    settings: Settings,
    *,
    progress: ProgressFn,
    cancel_event: asyncio.Event,
) -> Mapping[str, JSONValue]:
    spec_id = job.params.get("spec_id")
    if not isinstance(spec_id, str) or not spec_id.strip():
        raise ValueError(
            "parse_spec_docs job requires a non-empty 'spec_id' string parameter"
        )
    release = job.params.get("release")
    version = job.params.get("version")
    force = bool(job.params.get("force", False))
    release = release if isinstance(release, str) else None
    version = version if isinstance(version, str) else None
    try:
        validate_release_version_alignment(release, version)
    except SpecDocSelectionError as exc:
        raise ValueError(str(exc)) from exc

    service = getattr(services, "spec_doc", None)
    if service is None:
        raise RuntimeError("spec-doc parse is not available in this build")

    progress(f"parsing spec doc {spec_id}")

    def on_progress(event: str, data: Mapping[str, object]) -> None:
        if event == "spec_done":
            progress(f"spec {data.get('spec_id', '')}@{data.get('version', '')} done")

    if cancel_event.is_set():
        raise asyncio.CancelledError()
    if services.pipeline is None:
        result = await asyncio.to_thread(
            service.parse_many,
            [spec_id],
            release=release,
            version=version,
            force=force,
            on_progress=on_progress,
        )
    else:
        permit = await services.pipeline.reserve_async()
        try:
            prepared = await _await_future(
                services.pipeline.submit_prepare(
                    permit,
                    service.prepare_parse,
                    spec_id,
                    release=release,
                    version=version,
                    force=force,
                )
            )
            if cancel_event.is_set():
                raise asyncio.CancelledError()
            if isinstance(prepared, SpecDocSource):
                # prepare_parse only short-circuits on already-parsed pairs
                # without force; mirror parse_many's identity-skip reason.
                result = SpecDocBatchResult(
                    skipped={
                        spec_id: (
                            f"{spec_id}@{prepared.version} already parsed; "
                            "use --force to re-parse"
                        )
                    }
                )
            else:
                source = await _await_future(
                    services.pipeline.submit_persist(
                        permit,
                        service.persist_prepared_parse,
                        prepared,
                    )
                )
                result = SpecDocBatchResult(successes={spec_id: source})
                on_progress("spec_done", {"spec_id": spec_id, "version": source.version})
        except SpecDocTooLargeError as exc:
            result = SpecDocBatchResult(skipped={spec_id: str(exc)})
        except Exception as exc:  # noqa: BLE001 - preserve parse_many failure envelope
            result = SpecDocBatchResult(failures={spec_id: str(exc)})
        finally:
            permit.release()
    if cancel_event.is_set():
        raise asyncio.CancelledError()
    for sid, reason in result.failures.items():
        progress(f"spec {sid} failed: {reason}", force=True)
    progress(
        f"done: {len(result.successes)} ok, "
        f"{len(result.skipped)} skipped, {len(result.failures)} failed",
        force=True,
    )
    return {
        "requested": 1,
        "successes": len(result.successes),
        "skipped": len(result.skipped),
        "failures": len(result.failures),
        "failure_details": dict(result.failures),
    }


async def _rebuild_search(
    job: Job,
    services: ServiceContainer,
    settings: Settings,
    *,
    progress: ProgressFn,
    cancel_event: asyncio.Event,
) -> Mapping[str, JSONValue]:
    search = services.search
    if search is None:
        raise RuntimeError("search is not available in this build")
    stale_only = bool(job.params.get("stale_only", False))
    resume = bool(job.params.get("resume", False))
    batch_size = settings.search.rebuild_batch_size
    progress("rebuilding FTS5 search index")
    for update in search.rebuild(
        batch_size=batch_size,
        resume=resume,
        stale_only=stale_only,
        quiet=True,
    ):
        if cancel_event.is_set():
            raise asyncio.CancelledError()
        progress(
            f"rebuild {update.processed}/{update.total} "
            f"({update.current_tdoc_id})"
        )
    progress("search index rebuild complete", force=True)
    return {"processed": True}


def _index_request(params: Mapping[str, JSONValue]) -> IndexRequest:
    batch = params.get("batch")
    return IndexRequest(
        rebuild=bool(params.get("rebuild", False)),
        rebuild_embeddings=bool(params.get("rebuild_embeddings", False)),
        rebuild_all=bool(params.get("rebuild_all", False)),
        batch=int(batch) if batch is not None else None,
        resume=bool(params.get("resume", False)),
        stale_only=bool(params.get("stale_only", False)),
    )


async def _run_index(
    job: Job,
    services: ServiceContainer,
    settings: Settings,
    *,
    progress: ProgressFn,
    cancel_event: asyncio.Event,
    resource: str,
) -> Mapping[str, JSONValue]:
    service = getattr(services, resource)
    if service is None:
        raise RuntimeError(f"{resource.replace('_', ' ')} index is not available")
    request = _index_request(job.params)
    progress(f"starting {resource.replace('_', ' ')} index maintenance")
    if cancel_event.is_set():
        raise asyncio.CancelledError()

    def on_progress(message: str) -> None:
        if cancel_event.is_set():
            raise asyncio.CancelledError()
        progress(message, force=True)

    result = await asyncio.to_thread(
        service.rebuild,
        request,
        quiet=True,
        on_progress=on_progress,
    )
    if cancel_event.is_set():
        raise asyncio.CancelledError()
    if hasattr(result, "to_dict"):
        summary = result.to_dict()
    else:
        summary = {"status": "complete"}
    progress(f"{resource.replace('_', ' ')} index maintenance complete", force=True)
    return summary


async def _index_tdocs(
    job: Job,
    services: ServiceContainer,
    settings: Settings,
    *,
    progress: ProgressFn,
    cancel_event: asyncio.Event,
) -> Mapping[str, JSONValue]:
    return await _run_index(
        job,
        services,
        settings,
        progress=progress,
        cancel_event=cancel_event,
        resource="tdoc_index",
    )


async def _index_spec_docs(
    job: Job,
    services: ServiceContainer,
    settings: Settings,
    *,
    progress: ProgressFn,
    cancel_event: asyncio.Event,
) -> Mapping[str, JSONValue]:
    return await _run_index(
        job,
        services,
        settings,
        progress=progress,
        cancel_event=cancel_event,
        resource="spec_doc_index",
    )


async def _cache_purge(
    job: Job,
    services: ServiceContainer,
    settings: Settings,
    *,
    progress: ProgressFn,
    cancel_event: asyncio.Event,
) -> Mapping[str, JSONValue]:
    from doc3gpp.scraping.cache import TDocCache

    scope = job.params.get("scope", "markdown")
    if scope not in ("markdown", "zips", "all"):
        raise ValueError(
            f"cache_purge scope must be one of 'markdown'|'zips'|'all', got {scope!r}"
        )
    cache = TDocCache(
        root=settings.cache.dir,
        size_limit_bytes=settings.cache.size_limit_mb * 1024 * 1024,
    )
    if scope == "all":
        deleted = await asyncio.to_thread(cache.purge)
    else:
        deleted = await asyncio.to_thread(
            cache.purge_subdir,
            scope,  # type: ignore[arg-type]
        )
    if cancel_event.is_set():
        raise asyncio.CancelledError()
    progress(f"purged {deleted} file(s) from cache '{scope}'", force=True)
    return {"deleted": deleted}


class JobHandlers:
    """Registry mapping each :class:`JobKind` to its handler.

    New job kinds are added here — nowhere else. Handlers are selected
    by the worker via :data:`KIND_TO_HANDLER`.
    """

    KIND_TO_HANDLER: ClassVar[dict[JobKind, Handler]] = {
        JobKind.SYNC_MEETINGS: _sync_meetings,
        JobKind.SYNC_WIS: _sync_wis,
        JobKind.SYNC_TDOCS: _sync_tdocs,
        JobKind.SYNC_TDOCS_ALL: _sync_tdocs_all,
        JobKind.SYNC_SPECS: _sync_specs,
        JobKind.SYNC_TESTCASES: _sync_testcases,
        JobKind.PARSE_TDOCS: _parse_tdocs,
        JobKind.PARSE_TDOC_URL: _parse_tdoc_url,
        JobKind.PARSE_SPEC_DOCS: _parse_spec_docs,
        JobKind.INDEX_TDOCS: _index_tdocs,
        JobKind.INDEX_SPEC_DOCS: _index_spec_docs,
        JobKind.REBUILD_SEARCH: _rebuild_search,
        JobKind.CACHE_PURGE: _cache_purge,
    }


__all__ = [
    "Handler",
    "JobHandlers",
    "ProgressFn",
]
