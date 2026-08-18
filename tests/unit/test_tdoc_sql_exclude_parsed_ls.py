"""LS-aware ``exclude_parsed`` filter on TDocRepository.list.

``exclude_parsed=True`` must drop rows whose ``tdoc_id`` appears in
either ``tdoc_cr_cover_page`` (CR sidecar) or ``tdoc_cr_ls_details``
(LS sidecar) — a row is "parsed" if either sidecar exists.
"""

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.models import MeetingORM, TDocCrDetailOrm, TDocCrLSDetailOrm, TDocORM
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository


@pytest.fixture
def repo():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        session.add(
            MeetingORM(
                meeting_id=1,
                name="RAN5#124",
                title="Meeting 124",
                location="Online",
                start_date=datetime(2026, 1, 1),
                end_date=datetime(2026, 1, 5),
            )
        )
        session.add_all(
            [
                TDocORM(
                    tdoc_id="R5-260001",
                    title="LS on sidelink relay",
                    meeting_id=1,
                    ftp_url="tsg_ran/WG5_RL5/TSGR5_124/R5-260001.zip",
                    source="Ericsson",
                    type="LS",
                    status="Agreed",
                    reservation_date=date(2026, 1, 10),
                    uploaded_date=date(2026, 2, 1),
                ),
                TDocORM(
                    tdoc_id="R5-260002",
                    title="RedCap CR to 38.331",
                    meeting_id=1,
                    ftp_url="tsg_ran/WG5_RL5/TSGR5_124/R5-260002.zip",
                    source="Qualcomm",
                    type="CR",
                    status="Agreed",
                    reservation_date=date(2026, 1, 11),
                    uploaded_date=date(2026, 2, 2),
                ),
                TDocORM(
                    tdoc_id="R5-260003",
                    title="Not yet parsed",
                    meeting_id=1,
                    ftp_url="tsg_ran/WG5_RL5/TSGR5_124/R5-260003.zip",
                    source="Huawei",
                    type="CR",
                    status="Noted",
                    reservation_date=date(2026, 1, 12),
                    uploaded_date=date(2026, 2, 3),
                ),
            ]
        )
        session.commit()
    # Sidecars reference tdocs.tdoc_id — commit them after their parents
    # exist (FKs are enforced on sqlite via the backends/sqlite pragma).
    with Session() as session:
        session.add(TDocCrDetailOrm(ftp_url="x/02", tdoc_id="R5-260002"))
        session.add(
            TDocCrLSDetailOrm(
                ftp_url="x/01",
                tdoc_id="R5-260001",
                parser_version="test",
            )
        )
        session.commit()

    r = SQLAlchemyTDocRepository()
    r._session_factory = Session
    return r


def test_exclude_parsed_skips_ls_sidecar_rows(repo):
    """An LS row already in ``tdoc_cr_ls_details`` must be excluded."""
    rows = repo.list(limit=20, offset=0, exclude_parsed=True)
    assert [t.tdoc_id for t in rows] == ["R5-260003"]


def test_exclude_parsed_skips_both_sidecar_families(repo):
    """Cover-page and LS sidecar rows are both excluded by the filter."""
    rows = repo.list(limit=20, offset=0, exclude_parsed=True)
    assert all(t.tdoc_id not in {"R5-260001", "R5-260002"} for t in rows)
    assert len(rows) == 1


def test_exclude_parsed_false_keeps_ls_rows(repo):
    """Default / ``exclude_parsed=False`` keeps LS rows visible."""
    rows = repo.list(limit=20, offset=0)
    assert {t.tdoc_id for t in rows} == {"R5-260001", "R5-260002", "R5-260003"}
