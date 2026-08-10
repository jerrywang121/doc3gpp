"""Integration tests for the SQLAlchemy-backed SpecRepository."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from doc3gpp.models.spec import Spec, SpecVersion
from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db import models as m  # noqa: F401
from doc3gpp.storage.repositories.spec_sql import SQLAlchemySpecRepository


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        session.add(m.TsgORM(tsg_name="RAN TSG", short_name="R5", description=""))
        session.commit()
    return Session


def test_upsert_and_get(session_factory) -> None:
    repo = SQLAlchemySpecRepository(session_factory)
    spec = Spec(
        spec_id="36.579-5", type="TS", title="NR conformance",
        status="Under change control", radio_tech="LTE,5G",
        initial_release="Rel-20", tsg="R5", wis="A,B",
    )
    repo.upsert(spec)
    got = repo.get("36.579-5")
    assert got is not None
    assert got.spec_id == "36.579-5"
    assert got.type == "TS"
    assert got.tsg == "R5"
    assert got.wis == "A,B"


def test_upsert_versions_round_trip(session_factory) -> None:
    repo = SQLAlchemySpecRepository(session_factory)
    repo.upsert(Spec(spec_id="36.579-5", type="TS", title="T"))
    versions = [
        SpecVersion(
            spec_id="36.579-5", version="18.3.0",
            ftp_url="https://www.3gpp.org/ftp/Specs/archive/36_series/36.579-5/36579-5-i30.zip",
            release="Rel-18", meeting_id=108, meeting_name="RAN#108",
            upload_date=date(2025, 6, 1), version_id=92276,
        ),
        SpecVersion(
            spec_id="36.579-5", version="17.1.0",
            ftp_url="https://www.3gpp.org/ftp/Specs/archive/36_series/36.579-5/36579-5-g10.zip",
            release="Rel-17", meeting_id=100, meeting_name="RAN#100",
        ),
    ]
    assert repo.upsert_versions(versions) == 2
    got = repo.list_versions("36.579-5")
    assert len(got) == 2
    assert got[0].version == "18.3.0"
    assert got[1].version == "17.1.0"


def test_list_versions_is_idempotent(session_factory) -> None:
    repo = SQLAlchemySpecRepository(session_factory)
    repo.upsert(Spec(spec_id="s1", type="TS", title="T"))
    v = SpecVersion(spec_id="s1", version="1.0.0", ftp_url="u")
    repo.upsert_versions([v])
    repo.upsert_versions([v])
    assert len(repo.list_versions("s1")) == 1


def test_list_rich_filters(session_factory) -> None:
    repo = SQLAlchemySpecRepository(session_factory)
    repo.upsert(Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5", status="Under change control"))
    repo.upsert(Spec(spec_id="38.760-1", type="TR", title="Study on something", tsg="R5", status="Draft"))
    assert [s.spec_id for s in repo.list(tsg="R5")] == ["36.579-5", "38.760-1"]
    assert [s.spec_id for s in repo.list(type="TR")] == ["38.760-1"]
    assert [s.spec_id for s in repo.list(title="%NR%")] == ["36.579-5"]
    assert [s.spec_id for s in repo.list(spec_id="36.579-5")] == ["36.579-5"]
    assert [s.spec_id for s in repo.list(status="Draft")] == ["38.760-1"]
    assert [s.spec_id for s in repo.list(initial_release="Rel-20")] == []
