from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.models import TDocORM, MeetingORM
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository


def _make_engine():
    return create_engine("sqlite+pysqlite:///:memory:", future=True)


def insert_data(session):
    # Need a meeting to join against for meeting_like filters
    m1 = MeetingORM(
        meeting_id=1,
        name="RAN5#111",
        title="Meeting 111",
        location="Online",
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 1, 5),
    )
    m2 = MeetingORM(
        meeting_id=2,
        name="SA2#150",
        title="Meeting 150",
        location="Paris",
        start_date=datetime(2026, 2, 1),
        end_date=datetime(2026, 2, 5),
    )
    session.add_all([m1, m2])

    rows = [
        TDocORM(
            tdoc_id="R5-260001",
            title="RedCap CR to 38.331",
            meeting_id=1,
            source="Qualcomm",
            type="CR",
            status="Agreed",
            cr_cat="F",
            spec="38.331",
            related_wis="NR_ext",
        ),
        TDocORM(
            tdoc_id="R5-260002",
            title="TTCN-3 test case",
            meeting_id=1,
            source="Huawei",
            type="CR",
            status="Noted",
            cr_cat="B",
            spec="38.523-1",
            related_wis="NR_core",
        ),
        TDocORM(
            tdoc_id="S2-260100",
            title="Sidelink relay",
            meeting_id=2,
            source="Ericsson",
            type="Discussion Paper",
            status="Withdrawn",
            cr_cat=None,
            spec="23.501",
            related_wis="SL_enh",
        ),
    ]

    session.add_all(rows)
    session.commit()


@pytest.fixture
def repo():
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as s:
        insert_data(s)

    r = SQLAlchemyTDocRepository()
    r._session_factory = Session
    return r


def test_list_filter_tsg(repo):
    res = repo.list(tsg="R5")
    assert len(res) == 2
    assert all(t.tdoc_id.startswith("R5") for t in res)


def test_list_filter_year(repo):
    res = repo.list(year=26)
    assert len(res) == 3


def test_list_filter_meeting(repo):
    res = repo.list_with_meeting(meeting_like="%111%")
    assert len(res) == 2
    assert res[0].meeting_name == "RAN5#111"


def test_list_filter_meeting_id(repo):
    res = repo.list(meeting_id=1)
    assert len(res) == 2
    assert all(t.meeting_id == 1 for t in res)

    res = repo.list(meeting_id=2)
    assert len(res) == 1
    assert res[0].tdoc_id == "S2-260100"


def test_list_filter_meeting_id_with_meeting_like(repo):
    res = repo.list_with_meeting(meeting_id=1, meeting_like="%RAN5%")
    assert len(res) == 2
    assert res[0].meeting_name == "RAN5#111"

    res = repo.list_with_meeting(meeting_id=1, meeting_like="%SA2%")
    assert len(res) == 0


def test_list_filter_meeting_id_no_match(repo):
    assert repo.list(meeting_id=999) == []


def test_list_filter_source(repo):
    res = repo.list(source_like="Qualcomm")
    assert len(res) == 1
    assert res[0].source == "Qualcomm"

    res = repo.list(source_like="%uawei%")
    assert len(res) == 1
    assert res[0].source == "Huawei"


def test_list_filter_spec(repo):
    res = repo.list(spec_like="38.331")
    assert len(res) == 1
    assert res[0].spec == "38.331"

    res = repo.list(spec_like="38.%")
    assert len(res) == 2


def test_list_filter_wi(repo):
    res = repo.list(wi_like="%ext%")
    assert len(res) == 1
    assert "NR_ext" in res[0].related_wis


def test_list_filter_title(repo):
    res = repo.list(title_like="%RedCap%")
    assert len(res) == 1
    assert "RedCap" in res[0].title


def test_list_filter_cat(repo):
    res = repo.list(cat_like="F")
    assert len(res) == 1
    assert res[0].cr_cat == "F"


def test_list_filter_status(repo):
    res = repo.list(status_like="Agreed")
    assert len(res) == 1
    assert res[0].status == "Agreed"


def test_list_filter_type(repo):
    res = repo.list(type_like="%Discussion%")
    assert len(res) == 1
    assert "Discussion" in res[0].type
