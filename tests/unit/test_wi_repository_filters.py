"""Unit tests for the WI SQLAlchemy repository's filter behaviour."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.models import TsgORM, WiORM
from doc3gpp.storage.repositories.wi_sql import SQLAlchemyWiRepository


def _make_engine():
    return create_engine("sqlite+pysqlite:///:memory:", future=True)


def _seed(session) -> None:
    """Insert a TSG reference row and a handful of WI fixtures."""
    session.add(
        TsgORM(
            tsg_name="RAN WG5",
            short_name="R5",
            description="Mobile terminal conformance testing",
            url="https://www.3gpp.org/3gpp-groups/radio-access-networks-ran/ran-wg5",
        )
    )
    session.add(
        TsgORM(
            tsg_name="SA WG2",
            short_name="S2",
            description="System Architecture and Services",
            url="https://www.3gpp.org/3gpp-groups/service-system-aspects-sa/sa-wg2",
        )
    )
    session.add_all(
        [
            WiORM(
                wi_id=1031076,
                acronym="LTE_TN_NR_NTN_mob-Core",
                release="Rel-19",
                name="Building Block: Core part: Inter-RAT mode mobility support from E-UTRAN TN to NR NTN",
                tsg_short="R5",
                updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            WiORM(
                wi_id=1100035,
                acronym="NR_LPWUS-UEConTest",
                release="Rel-20",
                name="Building Block: UE Conformance - Low-power wake-up signal and receiver for NR",
                tsg_short="R5",
                updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            ),
            WiORM(
                wi_id=60067,
                acronym="OSA",
                release="R99",
                name="Building Block: Open Service Access",
                tsg_short="S2",
                updated_at=datetime(2025, 12, 30, tzinfo=timezone.utc),
            ),
        ]
    )
    session.commit()


@pytest.fixture
def repo():
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        _seed(s)

    r = SQLAlchemyWiRepository()
    r._session_factory = Session
    return r


def test_list_default_limit(repo) -> None:
    rows = repo.list()
    # 3 rows total; default limit is 20, all of them should come back.
    assert len(rows) == 3


def test_list_filter_tsg_uppercase_is_normalized(repo) -> None:
    """``--tsg r5`` (lowercase input) matches the stored uppercase canonical name."""
    rows = repo.list(tsg="r5")
    assert {r.tsg_short for r in rows} == {"R5"}
    assert {r.wi_id for r in rows} == {1031076, 1100035}


def test_list_filter_acronym_like(repo) -> None:
    rows = repo.list(acronym_like="%NTN%")
    assert len(rows) == 1
    assert rows[0].acronym == "LTE_TN_NR_NTN_mob-Core"
    assert rows[0].wi_id == 1031076


def test_list_filter_release_like(repo) -> None:
    rows = repo.list(release_like="Rel-__")
    # Matches "Rel-" + exactly two chars: Rel-19 and Rel-20 match; R99 does not.
    assert {r.release for r in rows} == {"Rel-19", "Rel-20"}

    # Single-char wildcard: Rel-1_ matches only release whose digits start with Rel-1.
    rows = repo.list(release_like="Rel-1_")
    assert {r.release for r in rows} == {"Rel-19"}


def test_list_filter_name_like(repo) -> None:
    rows = repo.list(name_like="%UE Conformance%")
    assert len(rows) == 1
    assert rows[0].acronym == "NR_LPWUS-UEConTest"


def test_list_combined_filters(repo) -> None:
    """Combined tsg + release filters narrow the result set."""
    rows = repo.list(tsg="R5", release_like="Rel-99")
    assert rows == []  # R5 has no Rel-99 rows.

    rows = repo.list(tsg="R5", release_like="Rel-20")
    assert {r.wi_id for r in rows} == {1100035}


def test_list_orders_by_updated_at_desc(repo) -> None:
    """Newest ``updated_at`` first; ties broken by ``wi_id`` desc."""
    rows = repo.list(limit=10)
    # R5 entries: 2026-01-02 then 2026-01-01; S2 entry last.
    assert rows[0].wi_id == 1100035
    assert rows[1].wi_id == 1031076
    assert rows[-1].wi_id == 60067


def test_upsert_many_inserts_and_refreshes(repo) -> None:
    """Re-inserting the same (wi_id, tsg_short) updates in place without duplicating."""
    from doc3gpp.models.wi import Wi

    initial = repo.list(tsg="R5")
    assert len(initial) == 2

    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    new_rows = [
        Wi(
            wi_id=1031076,
            acronym="LTE_TN_NR_NTN_mob-Core",
            release="Rel-19",
            name="Renamed title",
            tsg_short="R5",
            updated_at=now,
        ),
        Wi(
            wi_id=9100111,
            acronym="FRESH_WI",
            release="Rel-21",
            name="Brand new WI",
            tsg_short="R5",
            updated_at=now,
        ),
    ]
    stored = repo.upsert_many(new_rows)
    assert stored == 2

    after = repo.list(tsg="R5")
    titles = {row.wi_id: row.name for row in after}
    assert titles[1031076] == "Renamed title"  # updated in place
    assert titles[9100111] == "Brand new WI"  # newly inserted
    assert len(after) == 3  # one insertion plus one update, no duplication of (1031076, R5)
