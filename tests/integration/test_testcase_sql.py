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
