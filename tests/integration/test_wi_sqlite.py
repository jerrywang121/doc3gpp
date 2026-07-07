"""Integration tests for WI sync/list using a real SQLite engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from doc3gpp.models.wi import Wi
from doc3gpp.services.tsg_service import TsgService
from doc3gpp.services.wi_service import WiService
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository
from doc3gpp.storage.repositories.wi_sql import SQLAlchemyWiRepository

FIXTURE = Path("tests/fixtures/wi_pages/R5.html")


def _seed_tsgs() -> None:
    """Seed the TSG reference table so the FK from wis -> tsgs is satisfied."""
    TsgService(SQLAlchemyTsgRepository()).seed_defaults()


def test_create_schema_includes_wis_table(sqlite_env) -> None:
    """``create_schema`` creates the new ``wis`` table."""
    create_schema()
    engine = get_engine()
    with engine.connect() as conn:
        from sqlalchemy import text

        name = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='wis'")
        ).first()
    assert name is not None
    assert name[0] == "wis"


def test_sync_persists_rows_via_service(sqlite_env, monkeypatch) -> None:
    create_schema()
    _seed_tsgs()

    captured: dict[str, object] = {}

    def fake_fetch(tsg: str) -> str:
        captured["tsg"] = tsg
        return FIXTURE.read_text(encoding="utf-8")

    import doc3gpp.services.wi_service as wi_service_module
    monkeypatch.setattr(wi_service_module, "fetch_wis", fake_fetch)

    service = WiService(SQLAlchemyWiRepository())
    stored = service.sync("R5")  # uses uppercase already
    assert captured["tsg"] == "R5"
    assert stored >= 1

    # ``sync`` upper-cases user input before fetching.
    captured.clear()
    service.sync("r5")
    assert captured["tsg"] == "R5"

    rows = service.list_recent(tsg="R5")
    assert rows
    assert {row.tsg_short for row in rows} == {"R5"}


def test_upsert_is_idempotent(sqlite_env, monkeypatch) -> None:
    """Re-syncing the same TSG must not duplicate rows."""
    create_schema()
    _seed_tsgs()

    import doc3gpp.services.wi_service as wi_service_module
    monkeypatch.setattr(wi_service_module, "fetch_wis", lambda tsg: FIXTURE.read_text(encoding="utf-8"))

    service = WiService(SQLAlchemyWiRepository())
    service.sync("R5")
    first_count = len(service.list_recent(tsg="R5", limit=500))
    assert first_count > 0

    service.sync("R5")
    second_count = len(service.list_recent(tsg="R5", limit=500))
    assert second_count == first_count


def test_foreign_key_blocks_unknown_tsg(sqlite_env) -> None:
    """Inserting a WI whose ``tsg_short`` is not in ``tsgs`` is rejected."""
    from sqlalchemy.exc import IntegrityError

    create_schema()
    _seed_tsgs()
    repo = SQLAlchemyWiRepository()

    unknown = Wi(
        wi_id=1,
        acronym="ORPHAN",
        release="Rel-19",
        name="Should not insert",
        tsg_short="R99",  # not in the canonical TSG list
    )
    with pytest.raises(IntegrityError):
        repo.upsert_many([unknown])


def test_list_filter_combinations(sqlite_env, monkeypatch) -> None:
    create_schema()
    _seed_tsgs()

    import doc3gpp.services.wi_service as wi_service_module
    monkeypatch.setattr(wi_service_module, "fetch_wis", lambda tsg: FIXTURE.read_text(encoding="utf-8"))

    service = WiService(SQLAlchemyWiRepository())
    service.sync("R5")

    rows = service.list_recent(tsg="R5")
    assert all(r.tsg_short == "R5" for r in rows)

    rows = service.list_recent(name_like="%L2 support%")
    assert isinstance(rows, list)

    rows = service.list_recent(acronym_like="%UEConTest%")
    assert rows
    assert all("UEConTest" in r.acronym for r in rows)

