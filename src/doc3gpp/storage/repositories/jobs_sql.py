"""SQLAlchemy-backed implementation of :class:`JobRepository`."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.orm import sessionmaker

from doc3gpp.models.jobs import (
    Job,
    JobKind,
    JobStatus,
    JSONValue,
)
from doc3gpp.repository.protocols import JobRepository
from doc3gpp.storage.db.models import JobORM
from doc3gpp.storage.db.session import get_session_factory

logger = logging.getLogger(__name__)


_LOG_LINES_CAP = 50


class SQLAlchemyJobRepository(JobRepository):
    """SQLAlchemy implementation that stores background-job rows in ``jobs``.

    Identity is a UUID4 hex string minted by :meth:`create`; the
    :class:`Job` dataclass is the in-memory projection of the
    ``JobORM`` row. ``params`` / ``log_lines`` / ``result_summary``
    round-trip through ``json.dumps`` / ``json.loads`` so the
    cross-dialect storage stays portable — every supported dialect
    stores JSON-shaped payloads as TEXT in sqlite / postgres
    without depending on SQLAlchemy's dialect-aware ``JSON`` type.

    ``log_lines`` is a list of strings capped at
    :data:`_LOG_LINES_CAP` entries; the cap is enforced inside
    :meth:`append_log` rather than the dataclass so the value
    object remains a pure projection of the persisted state.
    """

    def __init__(self, session_factory: sessionmaker | None = None) -> None:
        """Initialize the repository.

        Args:
            session_factory: Optional pre-built ``sessionmaker``. When
                omitted the function falls back to
                :func:`doc3gpp.storage.db.session.get_session_factory`.
                The parameter is primarily used by unit tests that
                want to bind a repository to an in-memory SQLite
                engine.
        """
        self._session_factory = session_factory or get_session_factory()

    def create(self, kind: JobKind, params: Mapping[str, JSONValue]) -> Job:
        """Persist a fresh ``QUEUED`` row and return the resulting ``Job``."""
        params_json = _encode_json_object(params)
        job_id = uuid.uuid4().hex
        now = _utcnow()
        with self._session_factory() as session:
            row = JobORM(
                job_id=job_id,
                kind=kind.value,
                status=JobStatus.QUEUED.value,
                params=params_json,
                log_lines="[]",
                result_summary=None,
                error=None,
                created_at=now,
                started_at=None,
                finished_at=None,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
        return _orm_to_domain(row)

    def get(self, job_id: str) -> Job | None:
        """Return the job row for ``job_id``, or ``None`` when absent."""
        with self._session_factory() as session:
            row = session.get(JobORM, job_id)
        if row is None:
            return None
        return _orm_to_domain(row)

    def list(
        self,
        *,
        limit: int = 50,
        status: JobStatus | None = None,
    ) -> list[Job]:
        """Return recent job rows ordered by descending ``created_at``."""
        with self._session_factory() as session:
            stmt = select(JobORM)
            if status is not None:
                stmt = stmt.where(JobORM.status == status.value)
            stmt = stmt.order_by(JobORM.created_at.desc()).limit(limit)
            rows = session.scalars(stmt).all()
        return [_orm_to_domain(row) for row in rows]

    def mark_running(self, job_id: str, *, message: str = "starting") -> tuple[bool, Job]:
        """Transition ``job_id`` from ``QUEUED`` to ``RUNNING``.

        Returns a ``(claimed, job)`` pair. ``claimed`` is ``True``
        when this call performed the transition; ``False`` when the
        row was already ``RUNNING`` / terminal — the claim lost a
        race against another worker (or the job finished while it
        was queued). The ``WHERE status = 'queued'`` guard makes the
        losing write a no-op so two workers cannot both overwrite
        ``started_at`` / ``log_lines``; the caller uses ``claimed``
        to decide whether to run the handler.
        """
        now = _utcnow()
        existing_lines = self._read_log_lines(job_id)
        new_lines_json = _encode_json_array(_append_capped(existing_lines, message))
        with self._session_factory() as session:
            stmt = (
                update(JobORM)
                .where(JobORM.job_id == job_id)
                .where(JobORM.status == JobStatus.QUEUED.value)
                .values(
                    status=JobStatus.RUNNING.value,
                    started_at=now,
                    log_lines=new_lines_json,
                )
            )
            result = session.execute(stmt)
            session.commit()
            row = session.get(JobORM, job_id)
            claimed = bool(result.rowcount)
            if not claimed:
                # Row exists but is not ``QUEUED`` — the claim lost a
                # race (another worker already picked it up) or the
                # job is already terminal. Return the current row so
                # the caller can decide whether to skip or recover.
                logger.info(
                    "mark_running no-op: job %s already in status %s",
                    job_id, row.status,
                )
        return claimed, _orm_to_domain(row)

    def append_log(self, job_id: str, *, line: str) -> None:
        """Append ``line`` to ``log_lines``, capping the buffer at 50 entries."""
        existing_lines = self._read_log_lines(job_id)
        new_lines_json = _encode_json_array(_append_capped(existing_lines, line))
        with self._session_factory() as session:
            stmt = (
                update(JobORM)
                .where(JobORM.job_id == job_id)
                .values(log_lines=new_lines_json)
            )
            result = session.execute(stmt)
            session.commit()
            if not result.rowcount:
                raise KeyError(f"job_id {job_id!r} not found")

    def mark_succeeded(
        self,
        job_id: str,
        *,
        summary: Mapping[str, JSONValue],
    ) -> Job:
        """Transition ``job_id`` to ``SUCCEEDED`` with ``summary``."""
        summary_json = _encode_json_object(summary)
        now = _utcnow()
        with self._session_factory() as session:
            stmt = (
                update(JobORM)
                .where(JobORM.job_id == job_id)
                .values(
                    status=JobStatus.SUCCEEDED.value,
                    finished_at=now,
                    result_summary=summary_json,
                )
            )
            result = session.execute(stmt)
            session.commit()
            if not result.rowcount:
                raise KeyError(f"job_id {job_id!r} not found")
            row = session.get(JobORM, job_id)
        return _orm_to_domain(row)

    def mark_failed(self, job_id: str, *, error: str) -> Job:
        """Transition ``job_id`` to ``FAILED`` with ``error``."""
        now = _utcnow()
        with self._session_factory() as session:
            stmt = (
                update(JobORM)
                .where(JobORM.job_id == job_id)
                .values(
                    status=JobStatus.FAILED.value,
                    finished_at=now,
                    error=error,
                )
            )
            result = session.execute(stmt)
            session.commit()
            if not result.rowcount:
                raise KeyError(f"job_id {job_id!r} not found")
            row = session.get(JobORM, job_id)
        return _orm_to_domain(row)

    def mark_cancelled(self, job_id: str) -> Job:
        """Transition ``job_id`` to ``CANCELLED``."""
        now = _utcnow()
        with self._session_factory() as session:
            stmt = (
                update(JobORM)
                .where(JobORM.job_id == job_id)
                .values(
                    status=JobStatus.CANCELLED.value,
                    finished_at=now,
                )
            )
            result = session.execute(stmt)
            session.commit()
            if not result.rowcount:
                raise KeyError(f"job_id {job_id!r} not found")
            row = session.get(JobORM, job_id)
        return _orm_to_domain(row)

    def delete_older_than(self, cutoff: datetime) -> int:
        """Delete terminal jobs older than ``cutoff``; return the row count."""
        terminal_values = (
            JobStatus.SUCCEEDED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
        )
        with self._session_factory() as session:
            stmt = (
                delete(JobORM)
                .where(JobORM.status.in_(terminal_values))
                .where(JobORM.finished_at.isnot(None))
                .where(JobORM.finished_at < cutoff)
            )
            result = session.execute(stmt)
            session.commit()
        return int(result.rowcount or 0)

    def _read_log_lines(self, job_id: str) -> list[str]:
        """Return the current ``log_lines`` list for ``job_id``."""
        with self._session_factory() as session:
            row = session.get(JobORM, job_id)
        if row is None:
            raise KeyError(f"job_id {job_id!r} not found")
        return _decode_json_array(row.log_lines)


def _orm_to_domain(row: JobORM) -> Job:
    """Map a JobORM row to a Job dataclass."""
    log_lines = _decode_json_array(row.log_lines)
    return Job(
        id=row.job_id,
        kind=JobKind(row.kind),
        status=JobStatus(row.status),
        params=_decode_json_object(row.params),
        log_lines=tuple(str(line) for line in log_lines),
        result_summary=(
            _decode_json_object(row.result_summary)
            if row.result_summary is not None
            else None
        ),
        error=row.error,
        created_at=_as_utc(row.created_at),
        started_at=_as_utc(row.started_at),
        finished_at=_as_utc(row.finished_at),
    )


def _encode_json_object(value: Mapping[str, JSONValue]) -> str:
    """Serialise a JSON-shaped mapping to a JSON string."""
    return json.dumps(dict(value))


def _encode_json_array(value: list[str]) -> str:
    """Serialise a list of strings to a JSON array string."""
    return json.dumps([str(line) for line in value])


def _decode_json_object(value: str | None) -> dict[str, JSONValue]:
    """Decode a JSON string into a dict; ``None`` returns an empty dict."""
    if value is None:
        return {}
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError(f"expected JSON object, got {type(decoded).__name__}")
    return decoded


def _decode_json_array(value: str | None) -> list[str]:
    """Decode a JSON string into a list of strings; ``None`` returns ``[]``."""
    if value is None:
        return []
    decoded = json.loads(value)
    if not isinstance(decoded, list):
        raise ValueError(f"expected JSON array, got {type(decoded).__name__}")
    return [str(line) for line in decoded]


def _append_capped(existing: list[str], line: str) -> list[str]:
    """Append ``line`` to ``existing`` and slice the tail to the 50-entry cap."""
    combined = [*existing, line]
    if len(combined) > _LOG_LINES_CAP:
        return combined[-_LOG_LINES_CAP:]
    return combined


def _utcnow() -> datetime:
    """Return the current UTC time as a tz-aware datetime."""
    return datetime.now(tz=timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    """Return ``value`` normalized to UTC, handling naive SQLite returns."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = ["SQLAlchemyJobRepository"]