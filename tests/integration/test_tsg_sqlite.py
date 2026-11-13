from __future__ import annotations

from doc3gpp.services.tsg_service import TsgService
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository


def test_db_init_refreshes_renamed_tsg_without_breaking_catalog(sqlite_env) -> None:
    from sqlalchemy import text
    from typer.testing import CliRunner

    from doc3gpp.cli import app

    create_schema("main")
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE tsgs SET tsg_name='My renamed group', description='custom', "
            "url='https://example.test/local', meeting_last_sync='2026-03-01 12:34:56' "
            "WHERE short_name='R1'"
        ))
        catalog_before = connection.execute(text(
            "SELECT * FROM listserv_list ORDER BY list_id"
        )).all()
    result = CliRunner().invoke(app, ["db", "init", "--scope", "main"])
    assert result.exit_code == 0, repr(result.exception)
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT tsg_name, description, url, meeting_last_sync "
            "FROM tsgs WHERE short_name='R1'"
        )).one()
        assert row == (
            "RAN WG1", "Radio Layer 1 (Physical layer)",
            "https://www.3gpp.org/3gpp-groups/radio-access-networks-ran/ran-wg1",
            "2026-03-01 12:34:56",
        )
        assert connection.execute(text(
            "SELECT * FROM listserv_list ORDER BY list_id"
        )).all() == catalog_before
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_seed_defaults_rejects_case_insensitive_duplicate_names_without_changes(sqlite_env) -> None:
    import pytest
    from sqlalchemy import text

    from doc3gpp.storage.db.base import Base

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO tsgs (short_name, tsg_name, description) VALUES "
            "('R1', 'RAN WG1', 'first'), ('RX', 'ran wg1', 'second')"
        ))
        before = connection.execute(text("SELECT * FROM tsgs ORDER BY short_name")).all()
    with pytest.raises(ValueError, match="conflicting TSG"):
        TsgService(SQLAlchemyTsgRepository()).seed_defaults()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM tsgs ORDER BY short_name")).all() == before
        assert connection.execute(text("SELECT COUNT(*) FROM listserv_list")).scalar_one() == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_db_init_seeds_twenty_tsgs(sqlite_env) -> None:
    create_schema()
    service = TsgService(SQLAlchemyTsgRepository())
    seeded = service.seed_defaults()
    assert seeded == 20

    rows = service.list_all()
    assert len(rows) == 20

    short_names = {t.short_name for t in rows}
    assert short_names == {
        "R1", "R2", "R3", "R4", "R5", "R6", "RT", "S1", "S2", "S3",
        "S4", "S5", "S6", "C1", "C3", "C4", "C6", "RP", "SP", "CP",
    }

    # Spot-check: RAN AH1 maps to RT and the URL follows the AH family pattern
    rt = service.get_by_short_name("RT")
    assert rt is not None
    assert rt.tsg_name == "RAN AH1"
    assert rt.url == "https://www.3gpp.org/3gpp-groups/radio-access-networks-ran/ran-ah1"

    r6 = service.get_by_short_name("R6")
    assert r6 is not None
    assert (
        r6.tsg_name,
        r6.short_name,
        r6.description,
        r6.url,
    ) == (
        "RAN WG6",
        "R6",
        "(CLOSED) GSM/EDGE Radio Access Network (GERAN) and UMTS Radio Access (UTRA)",
        None,
    )

    # Other TSG URLs are still composed from the existing project pattern.
    for row in rows:
        if row.short_name == "R6":
            assert row.url is None
            continue
        assert row.url is not None
        assert row.url.startswith("https://www.3gpp.org/3gpp-groups/")

    # Persisted at the ORM layer too
    engine = get_engine()
    with engine.connect() as conn:
        from sqlalchemy import text
        db_count = conn.execute(text("SELECT COUNT(*) FROM tsgs")).scalar()
    assert db_count == 20


def test_seed_is_idempotent(sqlite_env) -> None:
    create_schema()
    service = TsgService(SQLAlchemyTsgRepository())
    service.seed_defaults()
    service.seed_defaults()  # second call should not duplicate rows

    rows = service.list_all()
    assert len(rows) == 20
    r6 = service.get_by_short_name("R6")
    assert r6 is not None
    assert r6.url is None


def test_get_by_short_name_handles_lowercase(sqlite_env) -> None:
    create_schema()
    service = TsgService(SQLAlchemyTsgRepository())
    service.seed_defaults()

    # Stored value is uppercased ("R5") and lookup matches case-insensitively
    assert service.is_known_short_name("r5")
    assert service.is_known_short_name("R5")
    assert service.get_by_short_name("r5").tsg_name == "RAN WG5"
    assert not service.is_known_short_name("r99")


def test_known_short_names_returns_all_canonical_codes(sqlite_env) -> None:
    create_schema()
    service = TsgService(SQLAlchemyTsgRepository())
    service.seed_defaults()

    names = service.known_short_names()
    assert len(names) == 20
    # All returned names are uppercased canonical codes
    for name in names:
        assert name == name.upper()
        assert len(name) in (2, 3)  # e.g. "R1" (2) or "RT" (2) or "S11" (3) — currently all 2


def test_upsert_updates_existing_row_description(sqlite_env) -> None:
    create_schema()
    service = TsgService(SQLAlchemyTsgRepository())
    service.seed_defaults()

    # Manually upsert with an updated description for an existing TSG
    from doc3gpp.models.tsg import Tsg
    repo = SQLAlchemyTsgRepository()
    repo.upsert_many(
        [Tsg(
            tsg_name="RAN WG5",
            short_name="R5",
            description="Mobile terminal conformance testing (updated)",
            url="https://www.3gpp.org/3gpp-groups/radio-access-networks-ran/ran-wg5",
        )]
    )

    refreshed = service.get_by_short_name("R5")
    assert refreshed.description.endswith("(updated)")
    # The explicit TSG seed still has twenty canonical rows.
    assert len(service.list_all()) == 20


def test_tsgorm_metadata_registered(sqlite_env) -> None:
    """The TsgORM model is registered with Base.metadata at import time."""
    # Importing models.py registers TsgORM with Base.metadata. ``create_schema``
    # then issues CREATE TABLE for it.
    create_schema()
    engine = get_engine()
    with engine.connect() as conn:
        from sqlalchemy import text
        result = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='tsgs'")
        ).first()
    assert result is not None
    assert result[0] == "tsgs"
