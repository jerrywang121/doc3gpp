"""Integration tests for the SQLAlchemy-backed TestCaseRepository.

Task 2 gate: table presence. Task 3 will APPEND more tests to this file
(upsert/get/list/status round-trip, stale-path cleanup, sources ledger).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db import models as m  # noqa: F401


@pytest.fixture()
def session_factory():
    """Build an in-memory SQLite session factory matching production.

    Mirrors ``tests/integration/test_spec_sql.py``: in-memory SQLite,
    ``Base.metadata.create_all``, ``sessionmaker(autoflush=False)``.
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return Session


def test_tables_exist(session_factory) -> None:
    from sqlalchemy import inspect

    with session_factory() as session:
        tables = set(inspect(session.get_bind()).get_table_names())
    assert {"testcases", "testcase_status", "testcase_sources"} <= tables


def test_upsert_get_list_status_round_trip(session_factory) -> None:
    from doc3gpp.models.testcase import TestCase, TestCaseStatus
    from doc3gpp.storage.repositories.testcase_sql import SQLAlchemyTestCaseRepository
    repo = SQLAlchemyTestCaseRepository(session_factory)
    assert repo.upsert_many([TestCase(testcase_id="TC_A", title="T", group="5G", spec="38.523-1")]) == 1
    repo.replace_statuses("TC_A", [TestCaseStatus(testcase_id="TC_A", path="FR1", gcf_ptcrb="Approved", ttcn_status="Approved")])
    got = repo.get("TC_A")
    assert got is not None and got.group == "5G"
    assert [(s.path, s.ttcn_status) for s in repo.list_statuses("TC_A")] == [("FR1", "Approved")]
    assert [c.testcase_id for c in repo.list(status="Approved")] == ["TC_A"]
    assert repo.list(status="null") == []


def test_replace_statuses_cleans_stale_paths(session_factory) -> None:
    from doc3gpp.models.testcase import TestCase, TestCaseStatus
    from doc3gpp.storage.repositories.testcase_sql import SQLAlchemyTestCaseRepository
    repo = SQLAlchemyTestCaseRepository(session_factory)
    repo.upsert_many([TestCase(testcase_id="TC_B", group="5G")])
    repo.replace_statuses("TC_B", [
        TestCaseStatus(testcase_id="TC_B", path="FR1", ttcn_status="Approved"),
        TestCaseStatus(testcase_id="TC_B", path="FR2", ttcn_status="Not approved"),
    ])
    repo.replace_statuses("TC_B", [TestCaseStatus(testcase_id="TC_B", path="FR1", ttcn_status="Approved")])
    assert [s.path for s in repo.list_statuses("TC_B")] == ["FR1"]


def test_sources_ledger(session_factory) -> None:
    from datetime import datetime, timezone
    from doc3gpp.models.testcase import TestCaseSource
    from doc3gpp.storage.repositories.testcase_sql import SQLAlchemyTestCaseRepository
    repo = SQLAlchemyTestCaseRepository(session_factory)
    now = datetime(2026, 9, 11, tzinfo=timezone.utc)
    repo.record_download(TestCaseSource(filename="TTCN CR Agreement Status 2024-wk32.zip", year=2024, week=32, revision=0, downloaded_at=now))
    assert repo.get_source("TTCN CR Agreement Status 2024-wk32.zip").parsed_at is None
    repo.record_parsed("TTCN CR Agreement Status 2024-wk32.zip", now, 2, 5)
    src = repo.get_source("TTCN CR Agreement Status 2024-wk32.zip")
    assert (src.parsed_at, src.testcase_count, src.status_count) == (now, 2, 5)
