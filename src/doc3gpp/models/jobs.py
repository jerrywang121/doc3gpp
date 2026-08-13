"""Domain model for background jobs exposed by the HTTP / MCP server.

This dataclass is the in-memory representation of a long-running job
row in the ``jobs`` SQL table. The shape is fixed (``slots=True,
frozen=True``) so the worker (T7), the HTTP routes (T8) and the MCP
mount (T8) can all pass the same value object across the boundary
without defensive copies. JSON values round-trip through the
repository layer via ``json.dumps`` / ``json.loads`` so the original
types survive a sqlite / postgres round-trip without depending on
the dialect-native ``JSON`` representation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


JSONValue = str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
"""JSON-compatible scalar / container type.

The recursive alias covers every value ``json.dumps`` can serialise.
``Mapping[str, JSONValue]`` is the contract for :attr:`Job.params` and
:attr:`Job.result_summary` so callers can pass any nested JSON-shaped
data without ``cast``-ing at the boundary.
"""


class JobKind(str, Enum):
    """Discriminator for the work a background job performs.

    String values match the URL slugs used by the HTTP / MCP routes
    (``POST /jobs/{kind}``) so the on-the-wire shape and the enum
    value stay in lock-step — :func:`str(enum_value)` is the slug.
    """

    SYNC_MEETINGS = "sync_meetings"
    SYNC_TDOCS = "sync_tdocs"
    SYNC_TDOCS_ALL = "sync_tdocs_all"
    SYNC_SPECS = "sync_specs"
    PARSE_TDOCS = "parse_tdocs"
    PARSE_TDOC_URL = "parse_tdoc_url"
    REBUILD_SEARCH = "rebuild_search"
    CACHE_PURGE = "cache_purge"


class JobStatus(str, Enum):
    """Lifecycle states of a background job.

    The state machine is::

        QUEUED -> RUNNING -> SUCCEEDED
                          -> FAILED
                          -> CANCELLED

    ``SUCCEEDED`` / ``FAILED`` / ``CANCELLED`` are terminal states
    eligible for retention cleanup. ``QUEUED`` / ``RUNNING`` rows are
    left alone by :meth:`JobRepository.delete_older_than` so the
    worker never observes a mid-flight row vanishing.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True, frozen=True)
class Job:
    """A single background job record.

    Attributes:
        id: UUID4 hex string identifying the job; serves as the PK on
            the ``jobs`` table.
        kind: What kind of work the job represents (see
            :class:`JobKind`).
        status: Lifecycle state — see :class:`JobStatus` for the
            transition diagram.
        params: User-supplied input parameters, JSON-shaped. The
            repository round-trips this through ``json.dumps`` /
            ``json.loads`` so the original types survive a sqlite
            round-trip.
        log_lines: Recent log lines for the ``log_tail`` preview,
            capped at 50 entries (FIFO eviction on
            :meth:`JobRepository.append_log`). Stored as a tuple so
            the dataclass stays immutable.
        result_summary: JSON-shaped summary returned by the handler on
            success, or ``None`` until the worker writes it.
        error: Error message recorded by the worker on failure,
            ``None`` for non-terminal states and for successful jobs.
        created_at: UTC timestamp when the job row was first written.
        started_at: UTC timestamp when the worker transitioned the
            row from ``QUEUED`` to ``RUNNING``; ``None`` while the
            job is still queued.
        finished_at: UTC timestamp when the worker transitioned the
            row to a terminal state; ``None`` for non-terminal
            states.
    """

    id: str
    kind: JobKind
    status: JobStatus
    params: Mapping[str, JSONValue]
    log_lines: tuple[str, ...]
    result_summary: Mapping[str, JSONValue] | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


__all__ = ["JSONValue", "Job", "JobKind", "JobStatus"]