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
    """Build an in-memory SQLite session factory matching production.

    Production's :func:`get_session_factory` uses ``autoflush=False``
    (see ``storage/db/session.py``). The SQLAlchemy default is
    ``autoflush=True``, which would hide the duplicate-key bug by
    auto-flushing pending inserts before ``session.get`` — the bug
    only surfaces when the duplicate is queued in the same session
    before flush. Pin the production setting here so the test
    surfaces real defects.
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
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
        rapporteurs="Ericsson LM",
    )
    repo.upsert(spec)
    got = repo.get("36.579-5")
    assert got is not None
    assert got.spec_id == "36.579-5"
    assert got.type == "TS"
    assert got.tsg == "R5"
    assert got.wis == "A,B"
    assert got.rapporteurs == "Ericsson LM"


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


def test_list_versions_orders_numerically_desc(session_factory) -> None:
    """``list_versions`` sorts by numeric version, newest first.

    ``18.10.1`` must rank above ``18.2.1`` even though a lexicographic
    string sort would place ``18.2.1`` first.
    """
    repo = SQLAlchemySpecRepository(session_factory)
    repo.upsert(Spec(spec_id="36.579-5", type="TS", title="T"))
    versions = [
        SpecVersion(spec_id="36.579-5", version="18.2.1", ftp_url="u1"),
        SpecVersion(spec_id="36.579-5", version="18.10.1", ftp_url="u2"),
        SpecVersion(spec_id="36.579-5", version="18.3.0", ftp_url="u3"),
    ]
    repo.upsert_versions(versions)
    got = repo.list_versions("36.579-5")
    assert [v.version for v in got] == ["18.10.1", "18.3.0", "18.2.1"]

def test_list_versions_version_filter_and_paging(session_factory) -> None:
    """``list_versions`` filters by ``version`` (rich LIKE), orders
    numeric-DESC, then applies ``limit``/``offset``."""
    repo = SQLAlchemySpecRepository(session_factory)
    repo.upsert(Spec(spec_id="36.579-5", type="TS", title="T"))
    versions = [
        SpecVersion(spec_id="36.579-5", version="19.2.0", ftp_url="u1"),
        SpecVersion(spec_id="36.579-5", version="19.10.0", ftp_url="u2"),
        SpecVersion(spec_id="36.579-5", version="18.3.0", ftp_url="u3"),
        SpecVersion(spec_id="36.579-5", version="17.1.0", ftp_url="u4"),
    ]
    repo.upsert_versions(versions)

    got = repo.list_versions("36.579-5", version="19.%")
    assert [v.version for v in got] == ["19.10.0", "19.2.0"]

    got = repo.list_versions("36.579-5", version="19.%", limit=1, offset=1)
    assert [v.version for v in got] == ["19.2.0"]

    got = repo.list_versions("36.579-5", version="null")
    assert got == []


def test_list_versions_is_idempotent(session_factory) -> None:
    repo = SQLAlchemySpecRepository(session_factory)
    repo.upsert(Spec(spec_id="s1", type="TS", title="T"))
    v = SpecVersion(spec_id="s1", version="1.0.0", ftp_url="u")
    repo.upsert_versions([v])
    repo.upsert_versions([v])
    assert len(repo.list_versions("s1")) == 1


def test_upsert_then_upsert_stamps_last_synced_at(session_factory) -> None:
    """``upsert`` preserves ``last_synced_at`` when set, and clears it
    when omitted — pins the two-step stamp pattern
    :meth:`SpecService._sync_one_spec` uses (first upsert without the
    timestamp, second upsert with it after the version rows land).
    """
    from datetime import datetime, timezone

    repo = SQLAlchemySpecRepository(session_factory)
    spec = Spec(spec_id="36.579-5", type="TS", title="NR conformance")

    # First upsert: no timestamp.
    repo.upsert(spec)
    assert repo.get("36.579-5").last_synced_at is None

    # Second upsert with a timestamp: lands in the column.
    stamped = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)
    spec.last_synced_at = stamped
    repo.upsert(spec)
    got = repo.get("36.579-5")
    assert got.last_synced_at is not None
    assert got.last_synced_at == stamped


def test_upsert_versions_dedupes_within_single_batch(session_factory) -> None:
    """``upsert_versions`` collapses duplicate ``(spec_id, version)`` rows
    inside a single batch.

    The 3GPP DynaReport spec detail page lists the same version twice
    when it has been re-uploaded (different ``upload_date`` / comment /
    ``version_id``). The PK is ``(spec_id, version)`` and the
    semantically-correct row is the most recent re-upload.
    """
    from datetime import date

    repo = SQLAlchemySpecRepository(session_factory)
    repo.upsert(Spec(spec_id="11.10-3", type="TS", title="T"))
    versions = [
        SpecVersion(
            spec_id="11.10-3", version="5.0.0", ftp_url="u-old",
            release="Rel-5", upload_date=date(1996, 4, 12),
            version_id=13186,
        ),
        SpecVersion(
            spec_id="11.10-3", version="5.0.0", ftp_url="u-new",
            release="Rel-5", upload_date=date(1999, 11, 11),
        ),
    ]
    repo.upsert_versions(versions)
    rows = repo.list_versions("11.10-3")
    assert len(rows) == 1
    # Newer upload_date wins.
    assert rows[0].upload_date == date(1999, 11, 11)
    assert rows[0].ftp_url == "u-new"


def test_upsert_versions_dedupes_when_upload_date_is_none(session_factory) -> None:
    """``upsert_versions`` collapses duplicates even when ``upload_date``
    is ``None`` on every row — falls back to last-write-wins so the
    batch commit succeeds instead of raising IntegrityError."""
    repo = SQLAlchemySpecRepository(session_factory)
    repo.upsert(Spec(spec_id="s2", type="TS", title="T"))
    versions = [
        SpecVersion(spec_id="s2", version="1.0.0", ftp_url="u1"),
        SpecVersion(spec_id="s2", version="1.0.0", ftp_url="u2"),
    ]
    repo.upsert_versions(versions)
    rows = repo.list_versions("s2")
    assert len(rows) == 1
    # Last write wins when upload_date can't break the tie.
    assert rows[0].ftp_url == "u2"


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


def test_list_rapporteurs_filter(session_factory) -> None:
    repo = SQLAlchemySpecRepository(session_factory)
    repo.upsert(Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5", rapporteurs="Ericsson LM"))
    repo.upsert(Spec(spec_id="38.760-1", type="TR", title="Study", tsg="R5", rapporteurs="Nokia"))
    repo.upsert(Spec(spec_id="38.761-1", type="TR", title="Other", tsg="R5"))
    # LIKE
    assert [s.spec_id for s in repo.list(rapporteurs="%Ericsson%")] == ["36.579-5"]
    # negated (NULL rows are excluded: NULL NOT LIKE ... is NULL in SQL)
    assert [s.spec_id for s in repo.list(rapporteurs="!%Nokia%")] == ["36.579-5"]
    # not-null
    assert [s.spec_id for s in repo.list(rapporteurs="not-null")] == ["36.579-5", "38.760-1"]
    # null
    assert [s.spec_id for s in repo.list(rapporteurs="null")] == ["38.761-1"]
