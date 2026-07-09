from datetime import date, datetime

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
            ftp_url="tsg_ran/WG5_RL5/TSGR5_124/R5-260001.zip",
            source="Qualcomm",
            type="CR",
            status="Agreed",
            reservation_date=date(2026, 1, 10),
            uploaded_date=date(2026, 2, 1),
            cr_cat="F",
            is_revision_of=None,
            revised_to="R5-260050",
            spec="38.331",
            related_wis="NR_ext",
        ),
        TDocORM(
            tdoc_id="R5-260002",
            title="TTCN-3 test case",
            meeting_id=1,
            ftp_url="tsg_ran/WG5_RL5/TSGR5_124/R5-260002.zip",
            source="Huawei",
            type="CR",
            status="Noted",
            reservation_date=date(2026, 1, 12),
            uploaded_date=date(2026, 3, 15),
            cr_cat="B",
            is_revision_of=None,
            revised_to=None,
            spec="38.523-1",
            related_wis="NR_core",
        ),
        TDocORM(
            tdoc_id="S2-260100",
            title="Sidelink relay",
            meeting_id=2,
            ftp_url=None,  # NULL ftp_url to exercise the not-null branch.
            source="Ericsson",
            type="Discussion Paper",
            status="Withdrawn",
            reservation_date=date(2026, 2, 5),
            uploaded_date=None,  # NULL uploaded_date for the date tests.
            cr_cat=None,
            is_revision_of=None,
            revised_to=None,
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
    res = repo.list(source="Qualcomm")
    assert len(res) == 1
    assert res[0].source == "Qualcomm"

    res = repo.list(source="%uawei%")
    assert len(res) == 1
    assert res[0].source == "Huawei"


def test_list_filter_spec(repo):
    res = repo.list(spec="38.331")
    assert len(res) == 1
    assert res[0].spec == "38.331"

    res = repo.list(spec="38.%")
    assert len(res) == 2


def test_list_filter_wi(repo):
    res = repo.list(wi="%ext%")
    assert len(res) == 1
    assert "NR_ext" in res[0].related_wis


def test_list_filter_title(repo):
    res = repo.list(title="%RedCap%")
    assert len(res) == 1
    assert "RedCap" in res[0].title


def test_list_filter_cat(repo):
    res = repo.list(cr_cat="F")
    assert len(res) == 1
    assert res[0].cr_cat == "F"


def test_list_filter_status(repo):
    res = repo.list(status="Agreed")
    assert len(res) == 1
    assert res[0].status == "Agreed"


def test_list_filter_type(repo):
    res = repo.list(tdoc_type="%Discussion%")
    assert len(res) == 1
    assert "Discussion" in res[0].type


# Rich filter surface (text nullability + date operators) used by
# `tdoc parse --meeting-id`. The un-suffixed params accept the grammar
# from `cli_filters.py`: `null` / `not-null` match nullability, a
# leading `!` flips to `NOT LIKE`, anything else is a `LIKE` pattern.
# Date-specific cases (`uploaded_date` ops + invalid form) live below.


def test_list_filter_status_null_token(repo):
    """`status="not-null"` matches every row whose status is set; the
    S2 row has status="Withdrawn" (NOT NULL) so all three rows match."""
    res = repo.list(status="not-null")
    assert len(res) == 3


def test_list_filter_cr_cat_null_token(repo):
    """`cr_cat="null"` matches the one row with a NULL cr_cat."""
    res = repo.list(cr_cat="null")
    assert len(res) == 1
    assert res[0].tdoc_id == "S2-260100"


@pytest.mark.parametrize(
    ("param_name", "expected_null", "expected_notnull", "not_like_value", "expected_notlike"),
    [
        # Only `cr_cat` has a NULL in the fixture; the rest are fully populated.
        ("status", 0, 3, "!Agreed", 2),
        ("cr_cat", 1, 2, "!F", 1),  # S2 row's NULL is excluded by NOT LIKE.
        ("spec", 0, 3, "!38.331", 2),
        ("wi", 0, 3, "!NR_ext", 2),
        ("title", 0, 3, "!%Sidelink%", 2),
        ("source", 0, 3, "!Ericsson", 2),
        ("tdoc_type", 0, 3, "!CR", 1),  # matches "Discussion Paper" only.
    ],
)
def test_list_filter_rich_grammar_per_column(
    repo, param_name, expected_null, expected_notnull, not_like_value, expected_notlike
):
    """Each un-suffixed text-filter param accepts the full `cli_filters`
    grammar: `null` / `not-null` for nullability, a leading `!` to
    negate as `NOT LIKE`. The `!` is consumed before the pattern is
    bound, and SQL's three-valued logic means `NULL NOT LIKE 'x'` is
    NULL (treated as false), so the negated filter excludes NULL rows
    from the result set.
    """
    assert len(repo.list(**{param_name: "null"})) == expected_null
    assert len(repo.list(**{param_name: "not-null"})) == expected_notnull
    assert len(repo.list(**{param_name: not_like_value})) == expected_notlike


def test_list_filter_revision_of_like_pattern(repo):
    """The un-suffixed `revision_of` accepts a LIKE pattern."""
    res = repo.list(revision_of="R5-%")
    assert res == []


def test_list_filter_revised_to_like_pattern(repo):
    """`revised_to="R5-260050"` matches via SQL LIKE without wildcards
    (LIKE without % matches the literal value)."""
    res = repo.list(revised_to="R5-260050")
    assert len(res) == 1
    assert res[0].tdoc_id == "R5-260001"


def test_list_filter_ftp_url_null_token(repo):
    """`ftp_url="null"` matches the S2 row whose ftp_url is NULL; the
    other two rows have explicit ftp_url values and are excluded."""
    res = repo.list(ftp_url="null")
    assert len(res) == 1
    assert res[0].tdoc_id == "S2-260100"


def test_list_filter_ftp_url_like(repo):
    """`ftp_url` LIKE pattern matches the two RAN5 rows whose ftp_url
    starts with `tsg_ran/WG5`."""
    res = repo.list(ftp_url="tsg_ran/WG5%")
    assert len(res) == 2
    assert all(t.tdoc_id.startswith("R5-") for t in res)


def test_list_filter_title_like_pattern(repo):
    """The un-suffixed `title` accepts a LIKE pattern."""
    res = repo.list(title="%Sidelink%")
    assert len(res) == 1
    assert res[0].tdoc_id == "S2-260100"


def test_list_filter_source_like_pattern(repo):
    """The un-suffixed `source` accepts a LIKE pattern."""
    res = repo.list(source="%ricsson")
    assert len(res) == 1
    assert res[0].tdoc_id == "S2-260100"


def test_list_filter_tdoc_type_like_pattern(repo):
    """`tdoc_type` accepts a LIKE pattern matching the `type` column."""
    res = repo.list(tdoc_type="Discussion Paper")
    assert len(res) == 1
    assert res[0].tdoc_id == "S2-260100"


def test_list_filter_uploaded_date_null(repo):
    """`uploaded_date="null"` matches the S2 row whose uploaded_date is NULL."""
    res = repo.list(uploaded_date="null")
    assert len(res) == 1
    assert res[0].tdoc_id == "S2-260100"


def test_list_filter_uploaded_date_not_null(repo):
    """`uploaded_date="not-null"` matches the two RAN5 rows with dates."""
    res = repo.list(uploaded_date="not-null")
    assert len(res) == 2
    assert all(t.tdoc_id.startswith("R5-") for t in res)


def test_list_filter_uploaded_date_eq(repo):
    """`uploaded_date="= '2026-02-01'"` matches the R5-260001 row."""
    res = repo.list(uploaded_date="= '2026-02-01'")
    assert len(res) == 1
    assert res[0].tdoc_id == "R5-260001"


def test_list_filter_uploaded_date_gte(repo):
    """`uploaded_date=">= '2026-03-01'"` matches only R5-260002
    (uploaded 2026-03-15). The R5-260001 row (2026-02-01) is older."""
    res = repo.list(uploaded_date=">= '2026-03-01'")
    assert len(res) == 1
    assert res[0].tdoc_id == "R5-260002"


def test_list_filter_uploaded_date_lt(repo):
    """`uploaded_date="< '2026-03-01'"` matches only R5-260001."""
    res = repo.list(uploaded_date="< '2026-03-01'")
    assert len(res) == 1
    assert res[0].tdoc_id == "R5-260001"


def test_list_filter_uploaded_date_invalid_raises(repo):
    """An operator the regex doesn't recognise surfaces as ValueError
    (the CLI layer catches it for a friendlier BadParameter)."""
    with pytest.raises(ValueError, match="Invalid date filter"):
        repo.list(uploaded_date="== '2026-02-31'")


def test_list_filter_combined_rich_params_and(repo):
    """Multiple rich-filter parameters AND-combine on different columns
    (here: meeting name, tdoc type, and status)."""
    res = repo.list(meeting_like="RAN5%", tdoc_type="CR", status="Agreed")
    assert len(res) == 1
    assert res[0].tdoc_id == "R5-260001"


def test_list_filter_not_like_excludes_matching_rows(repo):
    """`title="!%Sidelink%"` emits ``NOT LIKE '%Sidelink%'``; the
    Sidelink row is excluded and the two RAN5 rows remain."""
    res = repo.list(title="!%Sidelink%")
    assert len(res) == 2
    assert {t.tdoc_id for t in res} == {"R5-260001", "R5-260002"}


def test_list_filter_not_like_exact_match(repo):
    """`status="!Agreed"` excludes the Agreed row and returns the
    remaining two."""
    res = repo.list(status="!Agreed")
    assert len(res) == 2
    assert all(t.status != "Agreed" for t in res)


def test_list_filter_not_like_bang_is_consumed(repo):
    """The leading ``!`` is consumed before binding. If it were
    preserved, the pattern would be ``!%Sidelink%`` and would match
    zero rows (no title starts with ``!``). The fact that the negated
    filter returns the two RAN5 rows proves the bang was stripped."""
    res = repo.list(title="!%Sidelink%")
    assert all("Sidelink" not in t.title for t in res)


def test_list_filter_null_token_takes_precedence_over_not_like(repo):
    """The nullability check runs first: ``status="null"`` is treated
    as ``IS NULL`` (not ``NOT LIKE 'null'``). All fixture statuses
    are non-NULL, so the positive form returns zero rows.

    By contrast, ``status="!null"`` falls through to the NOT LIKE
    branch (the null-token check rejects the ``!``-prefixed value)
    and returns every row whose status is not the literal string
    ``"null"`` — all three fixture rows.
    """
    assert repo.list(status="null") == []
    assert len(repo.list(status="!null")) == 3


def test_list_filter_not_like_null_column_excluded(repo):
    """`ftp_url="!tsg_ran/%"` excludes the two R5 rows whose ftp_url
    matches the pattern. The S2 row has a NULL ftp_url, and SQL's
    three-valued logic means ``NULL NOT LIKE 'x'`` is NULL, which
    excludes the row from the result set — so the negated filter
    returns zero rows."""
    assert len(repo.list(ftp_url="tsg_ran/%")) == 2
    assert repo.list(ftp_url="!tsg_ran/%") == []
