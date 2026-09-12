"""SQLAlchemy-backed implementation of :class:`TestCaseRepository`."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from doc3gpp.cli_filters import (
    is_not_null_token,
    is_null_token,
    split_not_like_prefix,
)
from doc3gpp.models.testcase import TestCase, TestCaseSource, TestCaseStatus
from doc3gpp.storage.db.models import TestCaseORM, TestCaseSourceORM, TestCaseStatusORM
from doc3gpp.storage.db.session import get_session_factory
from doc3gpp.storage.repositories.rich_filters import apply_text_filter

_PATH_RANK = [
    "FR1",
    "FR2",
    "FR1+FR2",
    "FDD",
    "TDD",
    "IPCAN-4G",
    "EUTRA",
    "IPCAN-5G",
    "NR5GC",
    "default",
]


class SQLAlchemyTestCaseRepository:
    """SQLAlchemy implementation storing rows in ``testcases`` tables."""

    def __init__(self, session_factory: sessionmaker | None = None) -> None:
        self._session_factory = session_factory or get_session_factory()

    def upsert_many(self, cases: list[TestCase]) -> int:
        if not cases:
            return 0
        with self._session_factory() as session:
            for case in cases:
                existing = session.get(
                    TestCaseORM, (case.testcase_id, case.group)
                )
                if existing is not None:
                    existing.title = case.title
                    existing.ats = case.ats
                    existing.feature = case.feature
                    existing.release = case.release
                    existing.wis = case.wis
                    existing.spec = case.spec
                else:
                    session.add(
                        TestCaseORM(
                            testcase_id=case.testcase_id,
                            group=case.group,
                            title=case.title,
                            ats=case.ats,
                            feature=case.feature,
                            release=case.release,
                            wis=case.wis,
                            spec=case.spec,
                        )
                    )
            session.commit()
        return len(cases)

    def replace_statuses(
        self, testcase_id: str, group: str, rows: list[TestCaseStatus]
    ) -> None:
        with self._session_factory() as session:
            session.execute(
                delete(TestCaseStatusORM).where(
                    (TestCaseStatusORM.testcase_id == testcase_id)
                    & (TestCaseStatusORM.group == group)
                )
            )
            for row in rows:
                session.add(
                    TestCaseStatusORM(
                        testcase_id=row.testcase_id,
                        group=row.group,
                        path=row.path,
                        gcf_ptcrb=row.gcf_ptcrb,
                        ttcn_status=row.ttcn_status,
                    )
                )
            session.commit()

    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        testcase_id: str | None = None,
        title: str | None = None,
        ats: str | None = None,
        feature: str | None = None,
        release: str | None = None,
        wis: str | None = None,
        spec: str | None = None,
        group: str | None = None,
        status: str | None = None,
        gcf_status: str | None = None,
    ) -> list[TestCase]:
        with self._session_factory() as session:
            stmt = select(TestCaseORM)
            if testcase_id:
                stmt = apply_text_filter(stmt, TestCaseORM.testcase_id, testcase_id)
            if title:
                stmt = apply_text_filter(stmt, TestCaseORM.title, title)
            if ats:
                stmt = apply_text_filter(stmt, TestCaseORM.ats, ats)
            if feature:
                stmt = apply_text_filter(stmt, TestCaseORM.feature, feature)
            if release:
                stmt = apply_text_filter(stmt, TestCaseORM.release, release)
            if wis:
                stmt = apply_text_filter(stmt, TestCaseORM.wis, wis)
            if spec:
                stmt = apply_text_filter(stmt, TestCaseORM.spec, spec)
            if group:
                stmt = stmt.where(TestCaseORM.group == group.upper())
            if status:
                stmt = _apply_status_exists(
                    stmt, TestCaseStatusORM.ttcn_status, status
                )
            if gcf_status:
                stmt = _apply_status_exists(
                    stmt, TestCaseStatusORM.gcf_ptcrb, gcf_status
                )
            stmt = stmt.order_by(TestCaseORM.testcase_id).offset(offset).limit(limit)
            rows = session.scalars(stmt).all()
        return [_orm_to_case(r) for r in rows]

    def get(
        self, testcase_id: str, group: str | None = None
    ) -> TestCase | None:
        with self._session_factory() as session:
            if group is not None:
                row = session.get(TestCaseORM, (testcase_id, group))
            else:
                stmt = (
                    select(TestCaseORM)
                    .where(TestCaseORM.testcase_id == testcase_id)
                    .order_by(TestCaseORM.group)
                    .limit(1)
                )
                row = session.scalars(stmt).first()
        return _orm_to_case(row) if row is not None else None

    def list_statuses(
        self, testcase_id: str, group: str | None = None
    ) -> list[TestCaseStatus]:
        with self._session_factory() as session:
            stmt = select(TestCaseStatusORM).where(
                TestCaseStatusORM.testcase_id == testcase_id
            )
            if group is not None:
                stmt = stmt.where(TestCaseStatusORM.group == group)
            rows = session.scalars(stmt).all()
        statuses = [_orm_to_status(r) for r in rows]
        statuses.sort(key=_path_rank_key)
        return statuses

    def get_source(self, filename: str) -> TestCaseSource | None:
        with self._session_factory() as session:
            row = session.get(TestCaseSourceORM, filename)
        return _orm_to_source(row) if row is not None else None

    def record_download(self, source: TestCaseSource) -> None:
        with self._session_factory() as session:
            existing = session.get(TestCaseSourceORM, source.filename)
            if existing is not None:
                existing.year = source.year
                existing.week = source.week
                existing.revision = source.revision
                existing.downloaded_at = source.downloaded_at
                existing.testcase_count = source.testcase_count
                existing.status_count = source.status_count
                if source.parsed_at is not None:
                    existing.parsed_at = source.parsed_at
            else:
                session.add(
                    TestCaseSourceORM(
                        filename=source.filename,
                        year=source.year,
                        week=source.week,
                        revision=source.revision,
                        downloaded_at=source.downloaded_at,
                        parsed_at=source.parsed_at,
                        testcase_count=source.testcase_count,
                        status_count=source.status_count,
                    )
                )
            session.commit()

    def record_parsed(
        self,
        filename: str,
        parsed_at: datetime,
        testcase_count: int,
        status_count: int,
    ) -> None:
        with self._session_factory() as session:
            existing = session.get(TestCaseSourceORM, filename)
            if existing is not None:
                existing.parsed_at = parsed_at
                existing.testcase_count = testcase_count
                existing.status_count = status_count
            else:
                session.add(
                    TestCaseSourceORM(
                        filename=filename,
                        year=0,
                        week=0,
                        revision=0,
                        downloaded_at=None,
                        parsed_at=parsed_at,
                        testcase_count=testcase_count,
                        status_count=status_count,
                    )
                )
            session.commit()


def _apply_status_exists(stmt, column, value: str):
    """Filter ``stmt`` to testcases having a status row matching ``value``.

    Builds ``EXISTS (SELECT 1 FROM testcase_status s WHERE
    s.testcase_id == testcases.testcase_id AND s.group == testcases.group
    AND <rich predicate>)`` where the rich predicate is ``null`` →
    ``IS NULL``, ``not-null`` → ``IS NOT NULL``, ``!p`` → ``NOT LIKE``,
    else ``LIKE``.
    """
    if is_null_token(value):
        predicate = column.is_(None)
    elif is_not_null_token(value):
        predicate = column.is_not(None)
    else:
        negated, pattern = split_not_like_prefix(value)
        predicate = column.notlike(pattern) if negated else column.like(pattern)
    sub = (
        select(TestCaseStatusORM.testcase_id)
        .where(TestCaseStatusORM.testcase_id == TestCaseORM.testcase_id)
        .where(TestCaseStatusORM.group == TestCaseORM.group)
        .where(predicate)
        .exists()
    )
    return stmt.where(sub)


def _path_rank_key(status: TestCaseStatus) -> int:
    """Rank ``status.path`` by ``_PATH_RANK`` index, unknown paths last."""
    try:
        return _PATH_RANK.index(status.path)
    except ValueError:
        return len(_PATH_RANK)


def _orm_to_case(row: TestCaseORM) -> TestCase:
    return TestCase(
        testcase_id=row.testcase_id,
        group=row.group,
        title=row.title,
        ats=row.ats,
        feature=row.feature,
        release=row.release,
        wis=row.wis,
        spec=row.spec,
    )


def _orm_to_status(row: TestCaseStatusORM) -> TestCaseStatus:
    return TestCaseStatus(
        testcase_id=row.testcase_id,
        group=row.group,
        path=row.path,
        gcf_ptcrb=row.gcf_ptcrb,
        ttcn_status=row.ttcn_status,
    )


def _orm_to_source(row: TestCaseSourceORM) -> TestCaseSource:
    return TestCaseSource(
        filename=row.filename,
        year=row.year,
        week=row.week,
        revision=row.revision,
        downloaded_at=_as_utc(row.downloaded_at),
        parsed_at=_as_utc(row.parsed_at),
        testcase_count=row.testcase_count,
        status_count=row.status_count,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    """Return ``value`` normalized to UTC, handling naive SQLite returns."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
