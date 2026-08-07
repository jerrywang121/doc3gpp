from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.models import MeetingORM, TsgORM
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository


def _make_engine():
    return create_engine("sqlite+pysqlite:///:memory:", future=True)


def insert_rows(session):
    # Seed TSG reference rows first; meetings.tsg is an FK to tsgs.short_name
    session.add_all(
        [
            TsgORM(
                tsg_name="RAN WG5",
                short_name="R5",
                description="Mobile terminal conformance testing",
                url=None,
            ),
            TsgORM(
                tsg_name="SA WG2",
                short_name="S2",
                description="System Architecture",
                url=None,
            ),
        ]
    )
    session.flush()
    rows = [
        MeetingORM(
            meeting_id=1,
            name="R5-100",
            title="Test 100",
            location="Online",
            start_date=date(2025, 1, 10),
            end_date=date(2025, 1, 14),
            tsg="R5",
        ),
        MeetingORM(
            meeting_id=2,
            name="R5-101",
            title="Test 101",
            location="Rome",
            start_date=date(2026, 5, 20),
            end_date=date(2026, 5, 24),
            tsg="R5",
        ),
        MeetingORM(
            meeting_id=3,
            name="RAN5-TTCN Workshop",
            title="Workshop",
            location="Online",
            start_date=date(2026, 7, 2),
            end_date=date(2026, 7, 2),
            tsg="R5",
        ),
        # TTCN email meeting: starts Dec 2025 and runs through Dec 2026.
        # The TDoc numbering on its FTP server uses the end_date year (2026),
        # so the --year filter must key off end_date.year to match those TDocs.
        MeetingORM(
            meeting_id=4,
            name="RAN5-TTCN-WS#75",
            title="TTCN email meeting",
            location="Online",
            start_date=date(2025, 12, 1),
            end_date=date(2026, 12, 1),
            tsg="R5",
        ),
        # Cross-TSG row to assert the FK filter excludes it.
        MeetingORM(
            meeting_id=5,
            name="S2-150",
            title="Test 150",
            location="Vienna",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 5),
            tsg="S2",
        ),
        # Legacy row without an owning TSG — excluded by the FK filter.
        MeetingORM(
            meeting_id=6,
            name="LEGACY-no-tsg",
            title="Imported before column was added",
            location="Online",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 2),
        ),
    ]

    session.add_all(rows)
    session.commit()


def test_list_filters_by_tsg_and_year_and_like():
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    # seed
    with Session() as s:
        insert_rows(s)

    repo = SQLAlchemyMeetingRepository()
    # inject our session factory
    repo._session_factory = Session

    # FK LIKE filter; SA WG2 row and legacy null-tsg row must be excluded.
    r5 = repo.list(limit=10, tsg="r5")
    assert {m.meeting_id for m in r5} == {1, 2, 3, 4}

    # year filter (end_date year): TTCN row ends Dec 2026, so it joins the 2026 bucket
    y2026 = repo.list(limit=10, year=2026)
    assert {m.meeting_id for m in y2026} == {2, 3, 4, 5, 6}

    # TTCN row starts in 2025 but must NOT match year=2025 because its end_date is 2026
    y2025 = repo.list(limit=10, year=2025)
    assert {m.meeting_id for m in y2025} == {1}

    # name_like filter (SQL LIKE, match Workshop); TTCN row name has no "Workshop" substring
    w = repo.list(limit=10, name_like="%Workshop%")
    assert len(w) == 1
    assert w[0].meeting_id == 3


def test_list_filters_by_tsg_like_pattern():
    """``tsg`` is a SQL LIKE pattern, not an exact equality."""
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as s:
        insert_rows(s)

    repo = SQLAlchemyMeetingRepository()
    repo._session_factory = Session

    # Prefix wildcard: every R* TSG (fixture only has R5 rows).
    r_prefix = repo.list(limit=10, tsg="R%")
    assert {m.meeting_id for m in r_prefix} == {1, 2, 3, 4}

    # Single-character wildcard matches S2.
    s_wildcard = repo.list(limit=10, tsg="S_")
    assert {m.meeting_id for m in s_wildcard} == {5}

    # Match-all pattern returns every row that owns a TSG; NULL is excluded.
    all_tsgs = repo.list(limit=10, tsg="%")
    assert {m.meeting_id for m in all_tsgs} == {1, 2, 3, 4, 5}

    # Pattern that matches nothing.
    none = repo.list(limit=10, tsg="X%")
    assert none == []

    # Lowercase input is upper-cased before the lookup.
    r_lower = repo.list(limit=10, tsg="r%")
    assert {m.meeting_id for m in r_lower} == {1, 2, 3, 4}


def test_list_filters_by_tsg_negated_and_null_grammar():
    """``tsg`` honours the rich ``!pattern`` and ``null`` / ``not-null`` grammar."""
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as s:
        insert_rows(s)

    repo = SQLAlchemyMeetingRepository()
    repo._session_factory = Session

    # Negated: every row whose tsg is NOT R*.
    not_r = repo.list(limit=10, tsg="!R%")
    assert {m.meeting_id for m in not_r} == {5}

    # not-null excludes the NULL-tsg row.
    nn = repo.list(limit=10, tsg="not-null")
    assert {m.meeting_id for m in nn} == {1, 2, 3, 4, 5}

    # null matches only the NULL-tsg row.
    nul = repo.list(limit=10, tsg="null")
    assert {m.meeting_id for m in nul} == {6}


def _insert_doc_range_rows(session):
    """Seed a fixture covering every branch of the ``--tdoc`` range check."""
    session.add_all(
        [
            TsgORM(
                tsg_name="RAN WG5",
                short_name="R5",
                description="Mobile terminal conformance testing",
                url=None,
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            MeetingORM(
                meeting_id=100,
                name="M100-bracketed",
                title="M100",
                location="Online",
                start_date=date(2025, 1, 1),
                end_date=date(2025, 1, 5),
                start_doc="R5-260001",
                end_doc="R5-260050",
                tsg="R5",
            ),
            MeetingORM(
                meeting_id=101,
                name="M101-open-ended",
                title="M101",
                location="Online",
                start_date=date(2025, 2, 1),
                end_date=date(2025, 2, 5),
                start_doc="R5-260040",
                end_doc=None,
                tsg="R5",
            ),
            MeetingORM(
                meeting_id=102,
                name="M102-no-start-doc",
                title="M102",
                location="Online",
                start_date=date(2025, 3, 1),
                end_date=date(2025, 3, 5),
                start_doc=None,
                end_doc="R5-260050",
                tsg="R5",
            ),
            MeetingORM(
                meeting_id=103,
                name="M103-prefix-mismatch",
                title="M103",
                location="Online",
                start_date=date(2025, 4, 1),
                end_date=date(2025, 4, 5),
                start_doc="R5s260001",
                end_doc="R5s260050",
                tsg="R5",
            ),
            MeetingORM(
                meeting_id=104,
                name="M104-end-prefix-mismatch",
                title="M104",
                location="Online",
                start_date=date(2025, 5, 1),
                end_date=date(2025, 5, 5),
                start_doc="R5-260040",
                end_doc="C6-260050",
                tsg="R5",
            ),
            MeetingORM(
                meeting_id=105,
                name="M105-malformed",
                title="M105",
                location="Online",
                start_date=date(2025, 6, 1),
                end_date=date(2025, 6, 5),
                start_doc="garbage",
                end_doc="also-garbage",
                tsg="R5",
            ),
        ]
    )
    session.commit()


def test_list_tdoc_id_in_closed_range():
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        _insert_doc_range_rows(s)

    repo = SQLAlchemyMeetingRepository()
    repo._session_factory = Session

    matches = repo.list(limit=20, tdoc_id=("R5-", 260025))
    assert {m.meeting_id for m in matches} == {100}

    upper = repo.list(limit=20, tdoc_id=("R5-", 260050))
    assert {m.meeting_id for m in upper} == {100, 101}

    lower = repo.list(limit=20, tdoc_id=("R5-", 260001))
    assert {m.meeting_id for m in lower} == {100}


def test_list_tdoc_id_excludes_out_of_range_and_other_branches():
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        _insert_doc_range_rows(s)

    repo = SQLAlchemyMeetingRepository()
    repo._session_factory = Session

    below = repo.list(limit=20, tdoc_id=("R5-", 260000))
    assert below == []

    no_start = repo.list(limit=20, tdoc_id=("R5-", 260025))
    assert {m.meeting_id for m in no_start} == {100}

    cross_prefix = repo.list(limit=20, tdoc_id=("R5-", 260025))
    assert 103 not in {m.meeting_id for m in cross_prefix}
    assert 104 not in {m.meeting_id for m in cross_prefix}

    repo.list(limit=20, tdoc_id=("R5-", 260025))


def test_list_tdoc_id_open_ended_range_matches_when_number_in_start():
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        _insert_doc_range_rows(s)

    repo = SQLAlchemyMeetingRepository()
    repo._session_factory = Session

    # M101 has start_doc=R5-260040, no end_doc. Query 260050 must match.
    matches = repo.list(limit=20, tdoc_id=("R5-", 260050))
    assert 101 in {m.meeting_id for m in matches}

    # Query 260039 (just below start) must NOT match M101 (>= rule on start_doc).
    below_start = repo.list(limit=20, tdoc_id=("R5-", 260039))
    assert 101 not in {m.meeting_id for m in below_start}


def test_list_tdoc_id_combines_with_other_filters():
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        _insert_doc_range_rows(s)

    repo = SQLAlchemyMeetingRepository()
    repo._session_factory = Session

    # Combine tdoc_id with tsg: only M100..M105 carry tsg=R5, so the
    # intersection still comes from the doc-range rows. Adding a foreign
    # TSG with a matching prefix must not leak in (no such row exists
    # here, but the combination exercises the AND logic).
    matches = repo.list(limit=20, tdoc_id=("R5-", 260025), tsg="R5")
    assert {m.meeting_id for m in matches} == {100}

    # Wrong TSG -> no rows even when the doc range would match.
    matches_none = repo.list(limit=20, tdoc_id=("R5-", 260025), tsg="C6")
    assert matches_none == []


def test_list_tdoc_id_prefix_match_is_case_insensitive():
    """``r5s``, ``R5S``, ``r5S`` must all match a stored ``R5s...`` row."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker as sm

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sm(bind=engine)
    with Session() as s:
        s.add(
            TsgORM(
                tsg_name="RAN WG5",
                short_name="R5",
                description="Mobile terminal conformance testing",
                url=None,
            )
        )
        s.flush()
        # Stored in mixed case on purpose.
        s.add(
            MeetingORM(
                meeting_id=200,
                name="M200-mixed-case",
                title="M200",
                location="Online",
                start_date=date(2025, 7, 1),
                end_date=date(2025, 7, 5),
                start_doc="R5s260001",
                end_doc="R5s260050",
                tsg="R5",
            )
        )
        s.commit()

    repo = SQLAlchemyMeetingRepository()
    repo._session_factory = Session

    for prefix in ("r5s", "R5S", "r5S", "R5s"):
        matches = repo.list(limit=20, tdoc_id=(prefix, 260025))
        assert {m.meeting_id for m in matches} == {200}, prefix


def test_list_tdoc_id_in_range_accepts_ran4_7digit_shapes():
    """RAN4 has used 7-digit sequence numbers since 2016 (``R4-2607922``);
    stored ``start_doc`` / ``end_doc`` are 10 characters long instead of
    9. The range check must accept both shapes."""
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        s.add(
            TsgORM(
                tsg_name="RAN WG4",
                short_name="R4",
                description="Radio performance",
                url=None,
            )
        )
        s.flush()
        s.add_all(
            [
                MeetingORM(
                    meeting_id=300,
                    name="R4-119-7digit",
                    title="R4-119",
                    location="China",
                    start_date=date(2026, 5, 18),
                    end_date=date(2026, 5, 22),
                    start_doc="R4-2605200",
                    end_doc="R4-2607999",
                    tsg="R4",
                ),
                MeetingORM(
                    meeting_id=301,
                    name="R4-119-open-ended",
                    title="R4-119",
                    location="China",
                    start_date=date(2026, 5, 18),
                    end_date=date(2026, 5, 22),
                    start_doc="R4-2607000",
                    end_doc=None,
                    tsg="R4",
                ),
                MeetingORM(
                    meeting_id=302,
                    name="R4-still-6digit-R5-shape",
                    title="placeholder",
                    location="Online",
                    start_date=date(2026, 5, 18),
                    end_date=date(2026, 5, 22),
                    start_doc="R4-260010",
                    end_doc="R4-260099",
                    tsg="R4",
                ),
            ]
        )
        s.commit()

    repo = SQLAlchemyMeetingRepository()
    repo._session_factory = Session

    matches = repo.list(limit=20, tdoc_id=("R4-", 2607922))
    assert {m.meeting_id for m in matches} == {300, 301}

    below_both = repo.list(limit=20, tdoc_id=("R4-", 2605100))
    assert {m.meeting_id for m in below_both} == set()

    above_closed = repo.list(limit=20, tdoc_id=("R4-", 2608000))
    assert {m.meeting_id for m in above_closed} == {301}

    six_digit = repo.list(limit=20, tdoc_id=("R4-", 260050))
    assert {m.meeting_id for m in six_digit} == {302}
