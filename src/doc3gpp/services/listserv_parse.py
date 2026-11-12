"""Explicit message-detail parsing orchestration for ETSI LISTSERV archives."""

from __future__ import annotations

import logging
import os
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
from datetime import datetime, timezone

import httpx

from doc3gpp.config import get_settings
from doc3gpp.models.listserv import (
    ListservBatchResult,
    ListservDetail,
    ListservRecord,
    normalize_list_id,
    parse_date_set,
)
from doc3gpp.parsers.listserv_detail import (
    ListservDetailParseError,
    extract_tdocs,
    parse_detail_page,
    parse_message_body,
)
from doc3gpp.repository.listserv import ListservRepository
from doc3gpp.scraping.listserv_source import (
    ListservAuthRequiredError,
    ListservRedirectError,
    ListservSource,
    ListservSourceError,
    archive_index_url,
    validate_listserv_url,
)
from doc3gpp.services.listserv_sync import ListservIndexUpdater, ListservPageSource
from doc3gpp.settings.schema import Settings

logger = logging.getLogger(__name__)

LISTSERV_DETAIL_PARSER_VERSION = "1.0.0"

_SAFE_DETAIL_PARSE_MESSAGES = frozenset(
    {
        "Detail page HTML is empty or requires login.",
    }
)


def _safe_parse_failure_description(exc: Exception) -> str:
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
    if isinstance(exc, ListservDetailParseError):
        raw = str(exc)
        if raw in _SAFE_DETAIL_PARSE_MESSAGES:
            return raw
        return "LISTSERV detail parse error"
    if isinstance(exc, ValueError):
        raw = str(exc)
        if raw.startswith("Refusing to fetch attachment target"):
            return "Refusing to fetch attachment URL"
        if raw.startswith(
            (
                "Invalid LISTSERV URL",
                "Unsupported URL scheme",
                "Credentials are not permitted",
                "Invalid port",
                "Untrusted host or port",
                "Path traversal is not permitted",
            )
        ):
            return "Invalid or untrusted LISTSERV URL"
        return "LISTSERV URL validation error"
    return f"Parse failure ({type(exc).__name__})"


def _emit_progress(
    on_progress: Callable[[str], None] | None,
    message: str,
) -> None:
    if on_progress is not None:
        on_progress(message)


def _select_body_is_html(content_type: str) -> bool:
    media_type = content_type.partition(";")[0].strip().lower()
    if media_type == "text/plain":
        return False
    if media_type in ("text/html", "application/xhtml+xml", "multipart/related", ""):
        return True
    raise ListservSourceError(
        f"Refusing unsupported body content-type {media_type!r}."
    )


class ListservParseService:
    """Orchestrates explicit message-detail fetching, parsing, and persistence.

    Already-parsed rows are filtered before selection by
    :meth:`ListservRepository.iter_for_parse` when ``force=False`` so they
    trigger no network fetches and are excluded from selected-batch counts.
    ``ListservBatchResult.skipped`` counts only selected records observed in
    the current batch that were not scheduled because cancellation latched.
    """

    def __init__(
        self,
        repository: ListservRepository,
        source: ListservPageSource | None = None,
        *,
        max_workers: int | None = None,
        settings: Settings | None = None,
        max_content_chars: int | None = None,
        parser_version: str | None = None,
        clock: Callable[[], datetime] | None = None,
        index_service: ListservIndexUpdater | None = None,
        owns_index_service: bool = False,
    ) -> None:
        if max_workers is not None and max_workers < 1:
            raise ValueError(f"max_workers must be at least 1, got {max_workers}.")
        if max_content_chars is not None and max_content_chars < 1:
            raise ValueError(
                f"max_content_chars must be at least 1, got {max_content_chars}."
            )
        self._repository = repository
        self._source = source
        self._max_workers = max_workers
        self._settings = settings
        self._max_content_chars = max_content_chars
        self._parser_version = parser_version
        self._clock = clock
        self._index_service = index_service
        self._owns_index_service = owns_index_service
        self._closed = False

    def _update_indexes(
        self, record: ListservRecord, failures: dict[str, str]
    ) -> None:
        """Best-effort derived-index update after a committed detail; never raises."""
        if self._index_service is None:
            return
        url = record.message.url
        try:
            fresh = self._repository.get(url)
            if fresh is None:
                return
            failures.update(
                self._index_service.update_records([fresh], operation="parse")
            )
        except Exception as exc:  # noqa: BLE001
            reason = f"Index update failed ({type(exc).__name__})"
            logger.warning("LISTSERV index update failed: %s", reason)
            failures[url] = reason

    def _resolve_now(self) -> datetime:
        if self._clock is not None:
            now = self._clock()
            return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc)

    def _resolve_max_content_chars(self) -> int:
        if self._max_content_chars is not None:
            return self._max_content_chars
        resolved_settings = (
            self._settings if self._settings is not None else get_settings()
        )
        return resolved_settings.listserv.max_content_chars

    def _resolve_parser_version(self) -> str:
        return (
            self._parser_version
            if self._parser_version is not None
            else LISTSERV_DETAIL_PARSER_VERSION
        )

    @staticmethod
    def _fetch_and_parse_record(
        source: ListservPageSource,
        record: ListservRecord,
        *,
        max_chars: int,
        parser_version: str,
        parsed_at: datetime,
    ) -> ListservDetail:
        validated_detail_url = validate_listserv_url(record.message.url)
        fetched_detail = source.fetch(validated_detail_url)
        body_part_url, attachments, inline_fallback = parse_detail_page(
            fetched_detail.text,
            fetched_detail.final_url,
        )

        if body_part_url is not None:
            validated_body_url = validate_listserv_url(body_part_url)
            fetched_body = source.fetch(validated_body_url)
            is_html = _select_body_is_html(fetched_body.content_type)
            full_markdown = parse_message_body(fetched_body.text, is_html=is_html)
        else:
            full_markdown = inline_fallback if inline_fallback is not None else ""

        attachment_inputs = [
            item for att in attachments for item in (att.name, att.url)
        ]
        detail_tdocs = extract_tdocs(full_markdown, *attachment_inputs)

        if len(full_markdown) > max_chars:
            stored_content = full_markdown[:max_chars]
            content_truncated = True
        else:
            stored_content = full_markdown
            content_truncated = False

        return ListservDetail(
            url=record.message.url,
            attachments=attachments,
            tdocs=detail_tdocs,
            content=stored_content,
            content_truncated=content_truncated,
            parser_version=parser_version,
            parsed_at=parsed_at,
        )

    def parse(
        self,
        list_id: str,
        *,
        dates: Sequence[str] = (),
        force: bool = False,
        on_progress: Callable[[str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ListservBatchResult:
        """Parse stored message details for ``list_id`` without implicit sync."""
        normalized_list_id = normalize_list_id(list_id)
        # Validate list_id character safety using the shared archive index URL validator
        archive_index_url(normalized_list_id)
        date_exprs: Sequence[str] = (dates,) if isinstance(dates, str) else dates
        requested_dates = parse_date_set(date_exprs) if date_exprs else None

        cancel_latched = False

        def is_cancelled() -> bool:
            nonlocal cancel_latched
            if cancel_latched:
                return True
            if cancelled is not None and bool(cancelled()):
                cancel_latched = True
                return True
            return False

        pending: deque[ListservRecord] = deque(
            self._repository.iter_for_parse(
                normalized_list_id,
                requested_dates,
                force,
            )
        )
        if not pending:
            return ListservBatchResult(processed=0, skipped=0, failures={})

        if is_cancelled():
            skipped_count = len(pending)
            pending.clear()
            _emit_progress(
                on_progress, f"Cancelled LISTSERV parse for {normalized_list_id}"
            )
            return ListservBatchResult(
                processed=0, skipped=skipped_count, failures={}
            )

        workers = self._max_workers or min(6, (os.cpu_count() or 4) + 4)
        max_chars = self._resolve_max_content_chars()
        parser_version = self._resolve_parser_version()
        source_ctx = (
            nullcontext(self._source)
            if self._source is not None
            else ListservSource(settings=self._settings)
        )

        processed = 0
        skipped = 0
        failures: dict[str, str] = {}

        with source_ctx as active_source:
            in_flight: dict[Future[ListservDetail], ListservRecord] = {}

            with ThreadPoolExecutor(max_workers=workers) as executor:
                while (not is_cancelled() and pending) or in_flight:
                    while (
                        not is_cancelled()
                        and pending
                        and len(in_flight) < workers
                    ):
                        record = pending.popleft()
                        fut = executor.submit(
                            self._fetch_and_parse_record,
                            active_source,
                            record,
                            max_chars=max_chars,
                            parser_version=parser_version,
                            parsed_at=self._resolve_now(),
                        )
                        in_flight[fut] = record

                    if is_cancelled() and pending:
                        skipped += len(pending)
                        pending.clear()

                    if not in_flight:
                        break

                    return_when = (
                        ALL_COMPLETED if is_cancelled() else FIRST_COMPLETED
                    )
                    wait(tuple(in_flight.keys()), return_when=return_when)

                    if is_cancelled() and pending:
                        skipped += len(pending)
                        pending.clear()

                    done_futures = [f for f in list(in_flight.keys()) if f.done()]
                    for fut in done_futures:
                        record = in_flight.pop(fut)
                        msg_url = record.message.url
                        try:
                            detail = fut.result()
                        except Exception as exc:  # noqa: BLE001
                            reason = _safe_parse_failure_description(exc)
                            failures[msg_url] = reason
                            logger.warning(
                                "LISTSERV parse failed for %s: %s",
                                msg_url,
                                reason,
                            )
                            _emit_progress(
                                on_progress, f"Failed {msg_url}: {reason}"
                            )
                            continue

                        try:
                            self._repository.save_detail(detail)
                        except Exception as exc:  # noqa: BLE001
                            reason = _safe_parse_failure_description(exc)
                            failures[msg_url] = reason
                            logger.warning(
                                "LISTSERV detail save failed for %s: %s",
                                msg_url,
                                reason,
                            )
                            _emit_progress(
                                on_progress, f"Failed {msg_url}: {reason}"
                            )
                            continue

                        processed += 1
                        self._update_indexes(record, failures)
                        _emit_progress(on_progress, f"Processed {msg_url}")

                if is_cancelled() and pending:
                    skipped += len(pending)
                    pending.clear()

        if cancel_latched:
            _emit_progress(
                on_progress, f"Cancelled LISTSERV parse for {normalized_list_id}"
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


__all__ = [
    "LISTSERV_DETAIL_PARSER_VERSION",
    "ListservParseService",
]
