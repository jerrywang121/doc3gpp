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

from doc3gpp.cli_auto_sync import (
    collect_tdoc_candidates_for_url,
    trigger_auto_sync,
)
from doc3gpp.models.jobs import Job, JobKind, JSONValue
from doc3gpp.parsers.direct_extractor import is_3gpp_ftp_url
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
    outcome = services.meeting.sync(url, tsg=tsg, force=force, on_progress=progress)
    progress(outcome.reason, force=True)
    return {
        "status": outcome.status,
        "reason": outcome.reason,
        "synced_count": outcome.synced_count,
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
    if meeting_id is not None:
        progress(f"syncing TDocs for meeting id {meeting_id}")
        outcome = coordinator.sync_for_meeting_id(int(meeting_id), force=force, on_progress=progress)
    elif meeting_name is not None:
        progress(f"syncing TDocs for meeting {meeting_name}")
        outcome = coordinator.sync_for_meeting_name(str(meeting_name), force=force, on_progress=progress)
    else:
        raise ValueError(
            "sync_tdocs job requires a 'meeting_id' or 'meeting_name' parameter"
        )
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
    outcome = services.tdoc_sync.sync_all_tracked_meetings(force=force, on_progress=progress)
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

    if spec_id is not None:
        outcome = services.spec.sync_spec(
            spec_id,
            force=force,
            per_version_details=per_version_details,
            on_progress=on_progress,
        )
    else:
        outcome = services.spec.sync(
            tsg,
            force=force,
            per_version_details=per_version_details,
            on_progress=on_progress,
        )
    progress(outcome.reason, force=True)
    return {
        "status": outcome.status,
        "reason": outcome.reason,
        "synced_count": outcome.synced_count,
        "version_count": outcome.version_count,
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
        raise ValueError("parse_tdocs job requires a 'filter' mapping parameter")
    force = bool(job.params.get("force", False))
    full = bool(job.params.get("full", False))
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
        k: v for k, v in filters.items() if k in _KNOWN_LIST_FILTERS
    }
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
        return {"requested": 0, "successes": 0, "failures": 0, "skipped": 0}

    progress(f"parsing {len(tdoc_ids)} TDocs in batches of {max_batch}")
    total_successes: dict[str, object] = {}
    total_failures: dict[str, str] = {}
    total_skipped: dict[str, str] = {}
    for start in range(0, len(tdoc_ids), max_batch):
        if cancel_event.is_set():
            raise asyncio.CancelledError()
        batch = tdoc_ids[start : start + max_batch]
        result = services.tdoc_cr.extract_many(batch, force=force, full=full)
        total_successes.update(result.successes)
        total_failures.update(result.failures)
        total_skipped.update(result.skipped)
        progress(
            f"batch {start // max_batch + 1}: {len(batch)} requested, "
            f"{len(result.successes)} ok, {len(result.failures)} failed"
        )
    return {
        "requested": len(tdoc_ids),
        "successes": len(total_successes),
        "failures": len(total_failures),
        "skipped": len(total_skipped),
    }


async def _parse_tdoc_url(
    job: Job,
    services: ServiceContainer,
    settings: Settings,
    *,
    progress: ProgressFn,
    cancel_event: asyncio.Event,
) -> Mapping[str, JSONValue]:
    url = job.params.get("url")
    if not isinstance(url, str) or not is_3gpp_ftp_url(url):
        raise ValueError(
            f"parse_tdoc_url job requires a 3GPP FTP 'url' parameter; got {url!r}"
        )
    force = bool(job.params.get("force", False))
    full = bool(job.params.get("full", False))
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
    batch = await asyncio.to_thread(
        services.tdoc_cr.extract_from_url_batch,
        url,
        max_depth=max_depth,
        force=force,
        full=full,
        max_tdoc_size_bytes=max_tdoc_size_bytes or None,
        on_progress=progress,
    )

    files: list[dict[str, str]] = []
    for r in batch.results:
        if r.tdoc_id is None:
            continue
        files.append({
            "tdoc_id": r.tdoc_id,
            "ftp_url": r.source_url or "",
            "status": "ok" if r.persisted else "parsed-no-fk",
        })

    progress(
        f"done: {len(batch.results)} parsed, {len(batch.failures)} failed, "
        f"{len(batch.skipped)} skipped",
        force=True,
    )

    return {
        "requested": len(batch.results) + len(batch.failures) + len(batch.skipped),
        "successes": len(batch.results),
        "failures": len(batch.failures),
        "skipped": len(batch.skipped),
        "files": files,
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
        deleted = cache.purge()
    else:
        deleted = cache.purge_subdir(scope)  # type: ignore[arg-type]
    progress(f"purged {deleted} file(s) from cache '{scope}'", force=True)
    return {"deleted": deleted}


class JobHandlers:
    """Registry mapping each :class:`JobKind` to its handler.

    New job kinds are added here — nowhere else. Handlers are selected
    by the worker via :data:`KIND_TO_HANDLER`.
    """

    KIND_TO_HANDLER: dict[JobKind, Handler] = {
        JobKind.SYNC_MEETINGS: _sync_meetings,
        JobKind.SYNC_TDOCS: _sync_tdocs,
        JobKind.SYNC_TDOCS_ALL: _sync_tdocs_all,
        JobKind.SYNC_SPECS: _sync_specs,
        JobKind.PARSE_TDOCS: _parse_tdocs,
        JobKind.PARSE_TDOC_URL: _parse_tdoc_url,
        JobKind.REBUILD_SEARCH: _rebuild_search,
        JobKind.CACHE_PURGE: _cache_purge,
    }


__all__ = [
    "Handler",
    "JobHandlers",
    "ProgressFn",
]
