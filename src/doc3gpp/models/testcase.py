"""Domain models for RAN5 conformance testcases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class TestcaseSourceNotFoundError(LookupError):
    """Raised when no status-file name matches the History grammar."""


class TestcaseWorkbookNotFoundError(ValueError):
    """Raised when the status zip holds no *.xlsx workbook member."""


@dataclass(slots=True)
class TestCase:
    testcase_id: str
    group: str
    title: str | None = None
    ats: str | None = None
    feature: str | None = None
    release: str | None = None
    wis: str | None = None
    spec: str | None = None


@dataclass(slots=True)
class TestCaseStatus:
    testcase_id: str
    group: str
    path: str
    gcf_ptcrb: str | None = None
    ttcn_status: str | None = None


@dataclass(slots=True)
class TestCaseWithStatuses:
    testcase: TestCase
    statuses: dict[str, str | None]


@dataclass(slots=True)
class TestCaseDetail:
    testcase: TestCase
    statuses: list[TestCaseStatus]


@dataclass(slots=True)
class TestCaseSource:
    filename: str
    year: int
    week: int
    revision: int
    downloaded_at: datetime | None = None
    parsed_at: datetime | None = None
    testcase_count: int = 0
    status_count: int = 0
