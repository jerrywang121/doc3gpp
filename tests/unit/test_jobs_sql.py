"""Tests for :class:`SQLAlchemyJobRepository` against an in-memory sqlite engine."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from doc3gpp.models.jobs import Job, JobKind, JobStatus
from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.models import JobORM
from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository


def _make_repo() -> SQLAlchemyJobRepository:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    repo = SQLAlchemyJobRepository()
    repo._session_factory = Session
    return repo


_UUID4_HEX = re.compile(r"^[0-9a-f]{32}$")


def test_create_returns_id() -> None:
    """``create`` returns a Job with a UUID4 ``id``; round-trip via ``get``."""
    repo = _make_repo()

    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "R5"})

    assert isinstance(job, Job)
    assert isinstance(job.id, str)
    assert _UUID4_HEX.match(job.id), f"unexpected id shape: {job.id!r}"
    assert job.kind is JobKind.SYNC_MEETINGS
    assert job.status is JobStatus.QUEUED
    assert job.params == {"tsg": "R5"}
    assert job.log_lines == ()
    assert job.result_summary is None
    assert job.error is None
    assert job.created_at is not None
    assert job.started_at is None
    assert job.finished_at is None

    fetched = repo.get(job.id)
    assert fetched == job


def test_mark_running_sets_started_at() -> None:
    """``mark_running`` populates ``started_at`` and appends the message."""
    repo = _make_repo()
    job = repo.create(JobKind.PARSE_TDOCS, {})

    running = repo.mark_running(job.id, message="worker picked it up")

    assert running.status is JobStatus.RUNNING
    assert running.started_at is not None
    assert running.finished_at is None
    assert running.log_lines == ("worker picked it up",)
    assert running.id == job.id


def test_append_log_caps_at_50() -> None:
    """Appending more than 50 lines keeps only the most recent 50 (FIFO)."""
    repo = _make_repo()
    job = repo.create(JobKind.REBUILD_SEARCH, {})
    repo.mark_running(job.id)

    for i in range(60):
        repo.append_log(job.id, line=f"line-{i:03d}")

    fetched = repo.get(job.id)
    assert fetched is not None
    assert len(fetched.log_lines) == 50
    assert fetched.log_lines[0] == "line-010"
    assert fetched.log_lines[-1] == "line-059"


def test_mark_succeeded_sets_finished_at_and_summary() -> None:
    """``mark_succeeded`` stamps ``finished_at`` and writes ``summary``."""
    repo = _make_repo()
    job = repo.create(JobKind.SYNC_TDOCS, {"meeting_id": 123})
    repo.mark_running(job.id)

    done = repo.mark_succeeded(job.id, summary={"rows": 42, "ok": True})

    assert done.status is JobStatus.SUCCEEDED
    assert done.finished_at is not None
    assert done.result_summary == {"rows": 42, "ok": True}
    assert done.error is None


def test_mark_failed_sets_error() -> None:
    """``mark_failed`` stamps ``finished_at`` and writes ``error``."""
    repo = _make_repo()
    job = repo.create(JobKind.PARSE_TDOCS, {})
    repo.mark_running(job.id)

    failed = repo.mark_failed(job.id, error="boom")

    assert failed.status is JobStatus.FAILED
    assert failed.finished_at is not None
    assert failed.error == "boom"
    assert failed.result_summary is None


def test_mark_cancelled_sets_finished_at() -> None:
    """``mark_cancelled`` stamps ``finished_at`` but leaves ``error`` alone."""
    repo = _make_repo()
    job = repo.create(JobKind.CACHE_PURGE, {})
    repo.mark_running(job.id)

    cancelled = repo.mark_cancelled(job.id)

    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.finished_at is not None
    assert cancelled.error is None


def test_list_filters_by_status() -> None:
    """``list(status=...)`` only returns rows in the matching state."""
    repo = _make_repo()
    queued = repo.create(JobKind.SYNC_MEETINGS, {})
    other_queued = repo.create(JobKind.SYNC_TDOCS, {})
    running = repo.create(JobKind.PARSE_TDOCS, {})
    repo.mark_running(running.id)
    succeeded = repo.create(JobKind.SYNC_TDOCS_ALL, {})
    repo.mark_running(succeeded.id)
    repo.mark_succeeded(succeeded.id, summary={"rows": 1})

    queued_rows = repo.list(status=JobStatus.QUEUED)
    running_rows = repo.list(status=JobStatus.RUNNING)
    succeeded_rows = repo.list(status=JobStatus.SUCCEEDED)

    queued_ids = {row.id for row in queued_rows}
    running_ids = {row.id for row in running_rows}
    succeeded_ids = {row.id for row in succeeded_rows}

    assert queued.id in queued_ids
    assert other_queued.id in queued_ids
    assert queued.id not in running_ids
    assert running.id in running_ids
    assert succeeded.id in succeeded_ids


def test_delete_older_than_removes_only_terminal_jobs() -> None:
    """``delete_older_than`` drops terminal jobs older than the cutoff."""
    repo = _make_repo()
    Session = repo._session_factory

    cutoff = datetime(2026, 8, 1, tzinfo=timezone.utc)

    old_succeeded = repo.create(JobKind.SYNC_MEETINGS, {})
    repo._force_terminal(
        Session,
        old_succeeded.id,
        JobStatus.SUCCEEDED.value,
        cutoff - timedelta(days=2),
    )

    old_failed = repo.create(JobKind.PARSE_TDOCS, {})
    repo._force_terminal(
        Session,
        old_failed.id,
        JobStatus.FAILED.value,
        cutoff - timedelta(days=1),
    )

    old_cancelled = repo.create(JobKind.CACHE_PURGE, {})
    repo._force_terminal(
        Session,
        old_cancelled.id,
        JobStatus.CANCELLED.value,
        cutoff - timedelta(hours=1),
    )

    recent_succeeded = repo.create(JobKind.SYNC_TDOCS, {})
    repo._force_terminal(
        Session,
        recent_succeeded.id,
        JobStatus.SUCCEEDED.value,
        cutoff + timedelta(hours=1),
    )

    queued_untouched = repo.create(JobKind.SYNC_MEETINGS, {})
    running_untouched = repo.create(JobKind.PARSE_TDOCS, {})
    repo.mark_running(running_untouched.id)

    deleted = repo.delete_older_than(cutoff)

    assert deleted == 3
    surviving = {row.id for row in repo.list(limit=100)}
    assert surviving == {
        recent_succeeded.id,
        queued_untouched.id,
        running_untouched.id,
    }


# ---------------------------------------------------------------------------
# Internal helpers (test-only utilities).
# ---------------------------------------------------------------------------


def _force_terminal(
    self: SQLAlchemyJobRepository,
    Session: sessionmaker,
    job_id: str,
    status_value: str,
    when: datetime,
) -> None:
    """Stamp a row's ``status`` and ``finished_at`` for retention tests."""
    with Session() as session:
        row = session.get(JobORM, job_id)
        assert row is not None
        row.status = status_value
        row.finished_at = when
        session.commit()


SQLAlchemyJobRepository._force_terminal = _force_terminal  # type: ignore[attr-defined]