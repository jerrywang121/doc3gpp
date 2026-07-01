from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from doc3gpp.models.tsg import Tsg
from doc3gpp.storage.db.base import Base
from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository


def _make_engine():
    return create_engine("sqlite+pysqlite:///:memory:", future=True)


def test_upsert_many_inserts_and_updates() -> None:
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    repo = SQLAlchemyTsgRepository()
    repo._session_factory = Session

    rows = [
        Tsg(tsg_name="RAN WG1", short_name="R1", description="Layer 1", url="https://x/r1"),
        Tsg(tsg_name="SA WG1", short_name="S1", description="Services", url=None),
    ]
    count = repo.upsert_many(rows)
    assert count == 2
    assert repo.count() == 2

    # Upsert refreshes existing rows in place (matched by tsg_name)
    rows_updated = [
        Tsg(
            tsg_name="RAN WG1",
            short_name="R1",
            description="Layer 1 (updated)",
            url="https://x/r1-v2",
        ),
    ]
    repo.upsert_many(rows_updated)
    assert repo.count() == 2  # still 2, not 3
    refreshed = repo.get_by_short_name("R1")
    assert refreshed is not None
    assert refreshed.description == "Layer 1 (updated)"
    assert refreshed.url == "https://x/r1-v2"


def test_get_by_short_name_is_case_insensitive() -> None:
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    repo = SQLAlchemyTsgRepository()
    repo._session_factory = Session
    repo.upsert_many(
        [Tsg(tsg_name="RAN WG5", short_name="R5", description="Conformance", url=None)]
    )

    assert repo.get_by_short_name("R5") is not None
    assert repo.get_by_short_name("r5") is not None
    assert repo.get_by_short_name("R55") is None


def test_get_by_tsg_name_is_case_insensitive() -> None:
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    repo = SQLAlchemyTsgRepository()
    repo._session_factory = Session
    repo.upsert_many(
        [Tsg(tsg_name="RAN AH1", short_name="RT", description="ITU-R Ad Hoc", url=None)]
    )

    assert repo.get_by_tsg_name("RAN AH1") is not None
    assert repo.get_by_tsg_name("ran ah1") is not None
    assert repo.get_by_tsg_name("CT WG1") is None


def test_list_all_returns_sorted_by_tsg_name() -> None:
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    repo = SQLAlchemyTsgRepository()
    repo._session_factory = Session
    repo.upsert_many(
        [
            Tsg(tsg_name="SA WG1", short_name="S1", description="Services", url=None),
            Tsg(tsg_name="CT WG1", short_name="C1", description="UE-CN", url=None),
            Tsg(tsg_name="RAN WG1", short_name="R1", description="Layer 1", url=None),
        ]
    )

    listed = repo.list_all()
    assert [t.tsg_name for t in listed] == ["CT WG1", "RAN WG1", "SA WG1"]


def test_count_zero_on_empty_table() -> None:
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    repo = SQLAlchemyTsgRepository()
    repo._session_factory = Session

    assert repo.count() == 0
