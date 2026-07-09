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
            release="Rel-18",
            version="18.1.0",
            cr_num="3790",
            cr_pack="RP-220001",
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
            release="Rel-18",
            version="18.2.0",
            cr_num="3791",
            cr_pack="RP-220002",
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
            release=None,  # NULL release to exercise the not-null branch.
            version=None,
            cr_num=None,
            cr_pack=None,
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


def test_list_filter_tdoc_id_like(repo):
    res = repo.list(tdoc_id="R5%")
    assert len(res) == 2
    assert all(t.tdoc_id.startswith("R5") for t in res)


def test_list_filter_tdoc_id_exact(repo):
    res = repo.list(tdoc_id="S2-260100")
    assert len(res) == 1
    assert res[0].tdoc_id == "S2-260100"


def test_list_filter_tdoc_id_year_prefix(repo):
    """`%26%` matches every TDoc from the 2026 cycle regardless of TSG."""
    res = repo.list(tdoc_id="%26%")
    assert len(res) == 3
    assert all("26" in t.tdoc_id for t in res)


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


# ---------------------------------------------------------------------------
# New text-column filters: release / version / cr_num / cr_pack.
# Each accepts the full rich grammar (LIKE / null / not-null / NOT LIKE)
# shared with the other text columns. The fixture seeds the two R5
# rows with values and leaves the S2 row's columns NULL so the null
# / not-null branches have something to discriminate against.
# ---------------------------------------------------------------------------


def test_list_filter_release_like(repo):
    """`release="Rel-18"` matches the two RAN5 rows (literal LIKE
    without wildcards matches the exact value)."""
    res = repo.list(release="Rel-18")
    assert len(res) == 2
    assert {t.tdoc_id for t in res} == {"R5-260001", "R5-260002"}


def test_list_filter_release_pattern(repo):
    """`release="Rel-%"` matches the two RAN5 rows whose release
    starts with ``Rel-``."""
    res = repo.list(release="Rel-%")
    assert len(res) == 2
    assert all(t.release and t.release.startswith("Rel-") for t in res)


def test_list_filter_release_null_token(repo):
    """`release="null"` matches the one row with a NULL release
    (S2-260100). The other two rows are excluded."""
    res = repo.list(release="null")
    assert len(res) == 1
    assert res[0].tdoc_id == "S2-260100"


def test_list_filter_release_not_null_token(repo):
    """`release="not-null"` matches every row whose release is set;
    the two RAN5 rows qualify, the S2 row (NULL release) is excluded."""
    res = repo.list(release="not-null")
    assert len(res) == 2
    assert all(t.release is not None for t in res)


def test_list_filter_release_not_like(repo):
    """`release="!Rel-18"` emits ``NOT LIKE 'Rel-18'``; the two RAN5
    rows whose release is exactly ``Rel-18`` are excluded. SQL's
    three-valued logic excludes NULL rows from the result set, so
    only the S2 row is returned (but only if release were non-NULL —
    in this fixture release is NULL for the S2 row, so the negated
    filter returns zero rows)."""
    assert len(repo.list(release="Rel-18")) == 2
    assert repo.list(release="!Rel-18") == []


def test_list_filter_version_like(repo):
    """`version="18.1.0"` matches the one RAN5 row with that exact
    version literal."""
    res = repo.list(version="18.1.0")
    assert len(res) == 1
    assert res[0].tdoc_id == "R5-260001"
    assert res[0].version == "18.1.0"


def test_list_filter_version_pattern(repo):
    """`version="18.%"` matches the two RAN5 rows whose version
    starts with ``18.``."""
    res = repo.list(version="18.%")
    assert len(res) == 2
    assert all(t.version and t.version.startswith("18.") for t in res)


def test_list_filter_version_null_token(repo):
    """`version="null"` matches the one row with a NULL version."""
    res = repo.list(version="null")
    assert len(res) == 1
    assert res[0].tdoc_id == "S2-260100"


def test_list_filter_version_not_null_token(repo):
    """`version="not-null"` matches every row whose version is set."""
    res = repo.list(version="not-null")
    assert len(res) == 2
    assert all(t.version is not None for t in res)


def test_list_filter_version_not_like(repo):
    """`version="!18.%"` emits ``NOT LIKE '18.%'`` and excludes the
    two RAN5 rows whose version starts with ``18.``. The S2 row has
    a NULL version, which is excluded by SQL's three-valued logic,
    so the negated filter returns zero rows."""
    assert len(repo.list(version="18.%")) == 2
    assert repo.list(version="!18.%") == []


def test_list_filter_cr_num_like(repo):
    """`cr_num="3790"` matches the one RAN5 row with that exact
    cr_num literal."""
    res = repo.list(cr_num="3790")
    assert len(res) == 1
    assert res[0].tdoc_id == "R5-260001"
    assert res[0].cr_num == "3790"


def test_list_filter_cr_num_pattern(repo):
    """`cr_num="379%"` matches both RAN5 rows whose cr_num starts
    with ``379``."""
    res = repo.list(cr_num="379%")
    assert len(res) == 2
    assert all(t.cr_num and t.cr_num.startswith("379") for t in res)


def test_list_filter_cr_num_null_token(repo):
    """`cr_num="null"` matches the one row with a NULL cr_num."""
    res = repo.list(cr_num="null")
    assert len(res) == 1
    assert res[0].tdoc_id == "S2-260100"


def test_list_filter_cr_num_not_null_token(repo):
    """`cr_num="not-null"` matches every row whose cr_num is set."""
    res = repo.list(cr_num="not-null")
    assert len(res) == 2
    assert all(t.cr_num is not None for t in res)


def test_list_filter_cr_num_not_like(repo):
    """`cr_num="!379%"` emits ``NOT LIKE '379%'`` and excludes the
    two RAN5 rows. The S2 row's NULL cr_num is excluded by SQL's
    three-valued logic, so the negated filter returns zero rows."""
    assert len(repo.list(cr_num="379%")) == 2
    assert repo.list(cr_num="!379%") == []


def test_list_filter_cr_pack_like(repo):
    """`cr_pack="RP-%"` matches the two RAN5 rows whose cr_pack
    starts with ``RP-``."""
    res = repo.list(cr_pack="RP-%")
    assert len(res) == 2
    assert all(t.cr_pack and t.cr_pack.startswith("RP-") for t in res)


def test_list_filter_cr_pack_exact(repo):
    """`cr_pack="RP-220001"` (no wildcards) matches via SQL LIKE."""
    res = repo.list(cr_pack="RP-220001")
    assert len(res) == 1
    assert res[0].tdoc_id == "R5-260001"
    assert res[0].cr_pack == "RP-220001"


def test_list_filter_cr_pack_null_token(repo):
    """`cr_pack="null"` matches the one row with a NULL cr_pack."""
    res = repo.list(cr_pack="null")
    assert len(res) == 1
    assert res[0].tdoc_id == "S2-260100"


def test_list_filter_cr_pack_not_null_token(repo):
    """`cr_pack="not-null"` matches every row whose cr_pack is set."""
    res = repo.list(cr_pack="not-null")
    assert len(res) == 2
    assert all(t.cr_pack is not None for t in res)


def test_list_filter_cr_pack_not_like(repo):
    """`cr_pack="!RP-%"` emits ``NOT LIKE 'RP-%'`` and excludes the
    two RAN5 rows. SQL's three-valued logic excludes NULL cr_pack
    rows, so the negated filter returns zero rows."""
    assert len(repo.list(cr_pack="RP-%")) == 2
    assert repo.list(cr_pack="!RP-%") == []


def test_list_filter_combined_release_and_cr_num(repo):
    """Multiple new-filter parameters AND-combine on different
    columns (here: release and cr_num)."""
    res = repo.list(release="Rel-18", cr_num="3790")
    assert len(res) == 1
    assert res[0].tdoc_id == "R5-260001"
