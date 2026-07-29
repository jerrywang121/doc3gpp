"""Integration test: tdoc_cr_change_details table is created."""

from __future__ import annotations

from sqlalchemy import text

from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine


def test_table_is_created_with_expected_columns() -> None:
    create_schema()
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(tdoc_cr_change_details)")).all()
    cols = {row[1] for row in rows}
    assert "ftp_url" in cols
    assert "tdoc_id" in cols
    assert "clauses" in cols
    assert "changes" in cols


def test_upsert_and_get_round_trip(sqlite_env) -> None:
    from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
        SQLAlchemyTDocCrChangeDetailsRepository,
    )

    create_schema()
    repo = SQLAlchemyTDocCrChangeDetailsRepository()
    details = TDocCRChangeDetails(
        ftp_url="tsg_wg1/CR123.zip",
        tdoc_id="R5-999999",
        clauses=("5.2.3", "5.2.3-1"),
        changes=(("line A", "line B"), ("line C",)),
    )
    # FK needs the parent tdoc row; create one. Note: TDocORM has
    # no `tsg` column — that lives on the parent meeting row.
    from doc3gpp.storage.db.session import get_session_factory
    from doc3gpp.storage.db.models import TDocORM
    sf = get_session_factory()
    with sf() as s:
        s.add(TDocORM(tdoc_id="R5-999999", ftp_url="tsg_wg1/CR123.zip",
                      meeting_id=None))
        s.commit()
    repo.upsert(details)

    fetched = repo.get_by_url("tsg_wg1/CR123.zip")
    assert fetched is not None
    assert fetched.tdoc_id == "R5-999999"
    assert fetched.clauses == ("5.2.3", "5.2.3-1")
    assert fetched.changes == (("line A", "line B"), ("line C",))

    by_id = repo.get_for_tdoc_id("R5-999999")
    assert len(by_id) == 1
    assert by_id[0].ftp_url == "tsg_wg1/CR123.zip"


def test_get_by_url_returns_none_on_miss(sqlite_env) -> None:
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
        SQLAlchemyTDocCrChangeDetailsRepository,
    )

    create_schema()
    assert SQLAlchemyTDocCrChangeDetailsRepository().get_by_url("nope") is None


def test_cascade_delete_with_parent_tdoc(sqlite_env) -> None:
    from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_session_factory
    from doc3gpp.storage.db.models import TDocORM
    from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
        SQLAlchemyTDocCrChangeDetailsRepository,
    )

    create_schema()
    repo = SQLAlchemyTDocCrChangeDetailsRepository()
    sf = get_session_factory()
    with sf() as s:
        s.add(TDocORM(tdoc_id="R5-CASCADE", ftp_url="x/y.zip",
                      meeting_id=None))
        s.commit()
    repo.upsert(TDocCRChangeDetails(
        ftp_url="x/y.zip", tdoc_id="R5-CASCADE",
        clauses=("1.0",), changes=(("a",),),
    ))
    with sf() as s:
        s.query(TDocORM).filter_by(tdoc_id="R5-CASCADE").delete()
        s.commit()
    assert repo.get_by_url("x/y.zip") is None