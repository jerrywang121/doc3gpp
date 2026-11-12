"""Coverage-aware sync orchestration for ETSI LISTSERV archives."""

from __future__ import annotations

import logging
import os
import re
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import (
     ALL_COMPLETED,
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    wait,
)
from contextlib import nullcontext
from datetime import date, datetime, timezone
from typing import Literal, Protocol

import httpx

from doc3gpp.models.listserv import (
    ArchivePage,
    ListservBatchResult,
    ListservMessage,
    ListservRecord,
    coverage_contains,
    date_matches,
    default_sync_dates,
    normalize_list_id,
    parse_date_set,
)
from doc3gpp.parsers.listserv_archive import (
    ListservArchiveParseError,
    parse_archive_index,
    parse_archive_page,
)
from doc3gpp.repository.listserv import ListservRepository
from doc3gpp.scraping.listserv_source import (
    FetchedPage,
    ListservAuthRequiredError,
    ListservRedirectError,
    ListservSource,
    ListservSourceError,
    archive_index_url,
)
from doc3gpp.settings.schema import Settings

logger = logging.getLogger(__name__)

_PERIOD_KEY_RE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])(?:-W(?P<week>[1-5]))?$"
)

_SAFE_STATIC_PARSE_MESSAGES = frozenset(
    {
        "Archive index HTML is empty or requires login.",
        "No recognizable LISTSERV archive links found in index.",
        "Missing LISTSERV message listing table with Subject/From/Date headers.",
        "Header row not located in listing table.",
        "Message row has an empty Date cell.",
        "Message row has an empty Subject.",
        "Message row has an empty From cell.",
        "Archive listing HTML is empty or requires login.",
    }
)


class ListservIndexUpdater(Protocol):
    """Protocol for the post-commit derived-index update hook."""

    def update_records(
        self, records: Sequence[ListservRecord], *, operation: Literal["sync", "parse"]
    ) -> dict[str, str]: ...


class ListservPageSource(Protocol):
    """Protocol for fetching validated ETSI LISTSERV pages."""

    def fetch(self, url: str) -> FetchedPage: ...


def _period_sort_key(period: str) -> tuple[int, int, int]:
    match = _PERIOD_KEY_RE.match(period)
    if match is None:
        return (0, 0, 0)
    year = int(match.group("year"))
    month = int(match.group("month"))
    week_raw = match.group("week")
    week = int(week_raw) if week_raw is not None else 0
    return (year, month, week)


def _is_newest_period(period: str, newest_key: tuple[int, int, int]) -> bool:
    year, month, week = _period_sort_key(period)
    max_year, max_month, max_week = newest_key
    if (year, month, week) == (max_year, max_month, max_week):
        return True
    # Month-only archive in the latest year-month refreshes conservatively
    return (year, month, week) == (max_year, max_month, 0)


def _safe_failure_description(exc: Exception) -> str:
    """Return a failure summary that never includes response or message body text."""
    if isinstance(exc, ListservAuthRequiredError):
        status = (
            exc.response.status_code
            if getattr(exc, "response", None) is not None
            else "401/403"
        )
        return (
            f"Authentication required (HTTP {status}); "
            "only public ETSI LISTSERV archives are supported."
        )
    if isinstance(exc, ListservRedirectError):
        return "Redirect validation failed"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return f"HTTP transport error ({type(exc).__name__})"
    if isinstance(exc, ListservSourceError):
        return "LISTSERV source validation error"
    if isinstance(exc, ListservArchiveParseError):
        raw = str(exc)
        if raw in _SAFE_STATIC_PARSE_MESSAGES:
            return raw
        if raw.startswith("Malformed row in LISTSERV listing table"):
            return "Malformed row in LISTSERV listing table"
        if raw.startswith("Message row is missing a valid subject detail link"):
            return "Message row is missing a valid subject detail link"
        if raw.startswith(
            (
                "Invalid ISO date",
                "Invalid calendar date",
                "Invalid ISO datetime",
                "Unrecognized LISTSERV message date",
            )
        ):
            return "Invalid or unrecognized LISTSERV message date"
        if raw.startswith("Unsupported archive page period"):
            return "Unsupported archive page period"
        return "LISTSERV archive parse error"
    return f"Sync failure ({type(exc).__name__})"


def _emit_progress(
    on_progress: Callable[[str], None] | None,
    message: str,
) -> None:
    if on_progress is not None:
        on_progress(message)


class ListservSyncService:
    """Orchestrates coverage-aware ETSI LISTSERV archive discovery and sync."""

    def __init__(
        self,
        repository: ListservRepository,
        source: ListservPageSource | None = None,
        *,
        max_workers: int | None = None,
        today_fn: Callable[[], date] | None = None,
        clock: Callable[[], date | datetime] | None = None,
        settings: Settings | None = None,
        index_service: ListservIndexUpdater | None = None,
        owns_index_service: bool = False,
    ) -> None:
        if max_workers is not None and max_workers < 1:
            raise ValueError(f"max_workers must be at least 1, got {max_workers}.")
        self._repository = repository
        self._source = source
        self._max_workers = max_workers
        self._today_fn = today_fn
        self._clock = clock
        self._settings = settings
        self._index_service = index_service
        self._owns_index_service = owns_index_service
        self._closed = False

    def _update_indexes(
        self, urls: Sequence[str], failures: dict[str, str]
    ) -> None:
        """Best-effort derived-index update after a committed page; never raises."""
        if self._index_service is None or not urls:
            return
        try:
            records = list(self._repository.get_many(list(urls)).values())
            failures.update(
                self._index_service.update_records(records, operation="sync")
            )
        except Exception as exc:  # noqa: BLE001
            reason = f"Index update failed ({type(exc).__name__})"
            logger.warning("LISTSERV index update failed: %s", reason)
            for url in urls:
                failures[url] = reason

    def _resolve_today(self) -> date:
        if self._today_fn is not None:
            return self._today_fn()
        if self._clock is not None:
            value = self._clock()
            return value.date() if isinstance(value, datetime) else value
        return datetime.now(timezone.utc).date()

    @staticmethod
    def _fetch_and_parse_page(
        source: ListservPageSource,
        page: ArchivePage,
    ) -> tuple[list[ListservMessage], list[str]]:
        fetched = source.fetch(page.url)
        parse_target = (
            page
            if fetched.final_url == page.url
            else ArchivePage(
                list_id=page.list_id,
                url=fetched.final_url,
                period=page.period,
            )
        )
        messages, next_urls = parse_archive_page(fetched.text, parse_target)
        if fetched.final_url != page.url:
            next_urls = [url for url in next_urls if url != page.url]
        return messages, next_urls

    def sync(
        self,
        list_id: str,
        *,
        dates: Sequence[str] = (),
        force: bool = False,
        on_progress: Callable[[str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ListservBatchResult:
        """Sync LISTSERV message listings for ``list_id`` within ``dates``."""
        # 1. Validate inputs before any network or DB access
        normalized_list_id = normalize_list_id(list_id)
        index_url = archive_index_url(normalized_list_id)
        date_exprs: Sequence[str] = (dates,) if isinstance(dates, str) else dates
        today = self._resolve_today()
        requested_dates = (
            parse_date_set(date_exprs)
            if date_exprs
            else default_sync_dates(today)
        )

        cancel_latched = False

        def is_cancelled() -> bool:
            nonlocal cancel_latched
            if cancel_latched:
                return True
            if cancelled is not None and bool(cancelled()):
                cancel_latched = True
                return True
            return False

        if is_cancelled():
            _emit_progress(
                on_progress, f"Cancelled LISTSERV sync for {normalized_list_id}"
            )
            return ListservBatchResult(processed=0, skipped=0, failures={})

        workers = self._max_workers or min(6, (os.cpu_count() or 4) + 4)
        source_ctx = (
            nullcontext(self._source)
            if self._source is not None
            else ListservSource(settings=self._settings)
        )

        processed = 0
        skipped = 0
        failures: dict[str, str] = {}

        with source_ctx as active_source:
            fetched_index = active_source.fetch(index_url)
            index_pages = parse_archive_index(
                fetched_index.text,
                fetched_index.final_url,
                normalized_list_id,
            )

            newest_key = max(
                (_period_sort_key(p.period) for p in index_pages),
                default=(0, 0, 0),
            )

            pending: deque[ArchivePage] = deque()
            seen_urls: set[str] = set()
            for page in index_pages:
                if page.url not in seen_urls:
                    seen_urls.add(page.url)
                    pending.append(page)

            in_flight: dict[
                Future[tuple[list[ListservMessage], list[str]]],
                tuple[ArchivePage, tuple[str, ...]],
            ] = {}

            with ThreadPoolExecutor(max_workers=workers) as executor:
                while (not is_cancelled() and pending) or in_flight:
                    while (
                        not is_cancelled()
                        and pending
                        and len(in_flight) < workers
                    ):
                        page = pending.popleft()
                        should_fetch = (
                            force
                            or _is_newest_period(page.period, newest_key)
                            or not coverage_contains(
                                self._repository.get_coverage(page),
                                requested_dates,
                            )
                        )
                        if not should_fetch:
                            skipped += 1
                            for next_url in self._repository.get_next_urls(page):
                                if next_url not in seen_urls:
                                    seen_urls.add(next_url)
                                    pending.append(
                                        ArchivePage(
                                            list_id=normalized_list_id,
                                            url=next_url,
                                            period=page.period,
                                        )
                                    )
                            _emit_progress(on_progress, f"Skipped {page.url}")
                            continue

                        persisted_next = self._repository.get_next_urls(page)
                        fut = executor.submit(
                            self._fetch_and_parse_page, active_source, page
                        )
                        in_flight[fut] = (page, persisted_next)

                    if not in_flight:
                        break

                    return_when = (
                        ALL_COMPLETED if is_cancelled() else FIRST_COMPLETED
                    )
                    wait(tuple(in_flight.keys()), return_when=return_when)

                    done_futures = [f for f in list(in_flight.keys()) if f.done()]
                    for fut in done_futures:
                        page, persisted_next = in_flight.pop(fut)
                        try:
                            messages, parsed_next_urls = fut.result()
                        except Exception as exc:  # noqa: BLE001
                            reason = _safe_failure_description(exc)
                            failures[page.url] = reason
                            logger.warning(
                                "LISTSERV sync failed for %s: %s",
                                page.url,
                                reason,
                            )
                            if not is_cancelled():
                                for next_url in persisted_next:
                                    if next_url not in seen_urls:
                                        seen_urls.add(next_url)
                                        pending.append(
                                            ArchivePage(
                                                list_id=normalized_list_id,
                                                url=next_url,
                                                period=page.period,
                                            )
                                        )
                            _emit_progress(
                                on_progress, f"Failed {page.url}: {reason}"
                            )
                            continue

                        # Apply the date filter to each URL's final page occurrence.
                        messages_by_url = {msg.url: msg for msg in messages}
                        matching_messages = [
                            msg
                            for msg in messages_by_url.values()
                            if date_matches(requested_dates, msg.date.date())
                        ]
                        try:
                            self._repository.commit_page(
                                page,
                                matching_messages,
                                requested_dates,
                                parsed_next_urls,
                            )
                        except Exception as exc:  # noqa: BLE001
                            reason = _safe_failure_description(exc)
                            failures[page.url] = reason
                            logger.warning(
                                "LISTSERV commit failed for %s: %s",
                                page.url,
                                reason,
                            )
                            if not is_cancelled():
                                for next_url in persisted_next:
                                    if next_url not in seen_urls:
                                        seen_urls.add(next_url)
                                        pending.append(
                                            ArchivePage(
                                                list_id=normalized_list_id,
                                                url=next_url,
                                                period=page.period,
                                            )
                                        )
                            _emit_progress(
                                on_progress, f"Failed {page.url}: {reason}"
                            )
                            continue

                        processed += 1
                        self._update_indexes(
                            [msg.url for msg in matching_messages], failures
                        )
                        if not is_cancelled():
                            for next_url in (*persisted_next, *parsed_next_urls):
                                if next_url not in seen_urls:
                                    seen_urls.add(next_url)
                                    pending.append(
                                        ArchivePage(
                                            list_id=normalized_list_id,
                                            url=next_url,
                                            period=page.period,
                                        )
                                    )
                        _emit_progress(on_progress, f"Processed {page.url}")

        if cancel_latched:
            _emit_progress(
                on_progress, f"Cancelled LISTSERV sync for {normalized_list_id}"
            )

        return ListservBatchResult(
            processed=processed,
            skipped=skipped,
            failures=failures,
        )

    def close(self) -> None:
        """Close owned index service and source resources once."""
        if self._closed:
            return
        self._closed = True
        if self._owns_index_service:
            index_close = getattr(self._index_service, "close", None)
            if callable(index_close):
                index_close()
        source_close = getattr(self._source, "close", None)
        if callable(source_close):
            source_close()


__all__ = ["ListservPageSource", "ListservSyncService"]
