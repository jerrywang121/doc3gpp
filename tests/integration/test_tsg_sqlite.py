from __future__ import annotations

from datetime import datetime, timezone

from doc3gpp.services.tsg_service import TsgService
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository


def test_db_init_seeds_nineteen_tsgs(sqlite_env) -> None:
    create_schema()
    service = TsgService(SQLAlchemyTsgRepository())
    seeded = service.seed_defaults()
    assert seeded == 19

    rows = service.list_all()
    assert len(rows) == 19

    short_names = {t.short_name for t in rows}
    assert short_names == {"R1", "R2", "R3", "R4", "R5", "RT", "S1", "S2", "S3",
                           "S4", "S5", "S6", "C1", "C3", "C4", "C6",
                           "RP", "SP", "CP"}

    # Spot-check: RAN AH1 maps to RT and the URL follows the AH family pattern
    rt = service.get_by_short_name("RT")
    assert rt is not None
    assert rt.tsg_name == "RAN AH1"
    assert rt.url == "https://www.3gpp.org/3gpp-groups/radio-access-networks-ran/ran-ah1"

    # All 19 rows have URLs composed from the project pattern
    for row in rows:
        assert row.url is not None
        assert row.url.startswith("https://www.3gpp.org/3gpp-groups/")

    # Persisted at the ORM layer too
    engine = get_engine()
    with engine.connect() as conn:
        from sqlalchemy import text
        db_count = conn.execute(text("SELECT COUNT(*) FROM tsgs")).scalar()
    assert db_count == 19


def test_seed_is_idempotent(sqlite_env) -> None:
    create_schema()
    service = TsgService(SQLAlchemyTsgRepository())
    service.seed_defaults()
    service.seed_defaults()  # second call should not duplicate rows
    assert service.list_all() and len(service.list_all()) == 19


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
    assert len(names) == 19
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
    # Still 19 rows, not 20
    assert len(service.list_all()) == 19


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


def test_update_spec_last_sync_sql(sqlite_env) -> None:
    create_schema()
    service = TsgService(SQLAlchemyTsgRepository())
    service.seed_defaults()

    repo = SQLAlchemyTsgRepository()
    now = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
    assert repo.update_spec_last_sync("R5", now) is True
    rec = repo.get_by_short_name("R5")
    assert rec is not None
    assert rec.spec_last_sync is not None


def test_update_spec_last_sync_unknown_returns_false(sqlite_env) -> None:
    create_schema()
    service = TsgService(SQLAlchemyTsgRepository())
    service.seed_defaults()

    repo = SQLAlchemyTsgRepository()
    assert repo.update_spec_last_sync("NOPE", datetime.now(timezone.utc)) is False
