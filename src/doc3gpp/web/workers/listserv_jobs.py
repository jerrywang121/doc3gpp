"""Shared request validation and result shaping for LISTSERV background jobs.

Used by the HTTP routes (and MCP) before a job is persisted, and by the job
handlers when it runs, so both sides apply the exact same list ID / date rules.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from doc3gpp.models.jobs import JSONValue
from doc3gpp.models.listserv import (
    ListservBatchResult,
    normalize_list_id,
    parse_date_set,
)
from doc3gpp.models.listserv_failures import INDEX_FAILURE_PREFIXES, is_index_failure
from doc3gpp.scraping.listserv_source import archive_index_url

INDEX_REPAIR_HINT = "run `doc3gpp listserv index --rebuild` to repair"


def validate_batch_request(
    list_id: object, dates: object = None
) -> tuple[str, list[str]]:
    """Validate an exact list ID and date expressions; return normalized values.

    Raises:
        ValueError: when the list ID or any date expression is invalid.
    """
    if not isinstance(list_id, str):
        raise ValueError("LISTSERV list_id is required.")  # noqa: TRY004
    if dates is None:
        date_list: list[str] = []
    elif isinstance(dates, str) or not isinstance(dates, Sequence):
        raise ValueError("LISTSERV dates must be a list of date expressions.")
    else:
        date_list = list(dates)
        if not all(isinstance(d, str) for d in date_list):
            raise ValueError("LISTSERV dates must be a list of date expressions.")
    normalized = normalize_list_id(list_id)
    archive_index_url(normalized)
    parse_date_set(date_list)
    return normalized, date_list


def summarize_batch(
    verb: str, list_id: str, result: ListservBatchResult
) -> dict[str, JSONValue]:
    """Build a JSON summary keeping source failures distinct from index failures."""
    source = {k: v for k, v in result.failures.items() if not is_index_failure(v)}
    index = {k: v for k, v in result.failures.items() if is_index_failure(v)}
    parts: list[str] = []
    if source:
        parts.append(f"{len(source)} source failure(s)")
    if index:
        parts.append(
            f"{len(index)} derived-index failure(s) "
            f"(source data was committed; {INDEX_REPAIR_HINT})"
        )
    prefix = f"LISTSERV {verb} for {list_id}: processed={result.processed} "
    prefix += f"skipped={result.skipped}"
    message = prefix + (" with " + "; ".join(parts) if parts else "")
    return {
        "list_id": list_id,
        "processed": result.processed,
        "skipped": result.skipped,
        "failed": len(source),
        "index_failed": len(index),
        "failures": dict(source),
        "index_failures": dict(index),
        "message": message,
    }


def failure_error(summary: Mapping[str, JSONValue]) -> str | None:
    """Return the job error text when ``summary`` records any failure."""
    if summary.get("failed") or summary.get("index_failed"):
        return str(summary.get("message", "LISTSERV job completed with failures"))
    return None


__all__ = [
    "INDEX_FAILURE_PREFIXES",
    "failure_error",
    "is_index_failure",
    "summarize_batch",
    "validate_batch_request",
]
