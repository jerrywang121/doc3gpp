from __future__ import annotations

from doc3gpp.models.tdoc import TDoc
from doc3gpp.services.tdoc_service import TDocService
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository


def test_tdoc_repository_upsert_and_list(sqlite_env) -> None:
    create_schema()
    repo = SQLAlchemyTDocRepository()

    repo.upsert(TDoc(tdoc_id="R1-000001", title="First", meeting="RAN1#100", url="https://x/1"))
    repo.upsert(TDoc(tdoc_id="R1-000002", title="Second"))
    repo.upsert(TDoc(tdoc_id="R1-000001", title="First updated", meeting="RAN1#100", url="https://x/1a"))

    rows = repo.list(limit=10)

    assert len(rows) == 2
    by_id = {r.tdoc_id: r for r in rows}
    assert by_id["R1-000001"].title == "First updated"
    assert by_id["R1-000002"].title == "Second"


def test_tdoc_service_save_and_list(sqlite_env) -> None:
    create_schema()
    service = TDocService(SQLAlchemyTDocRepository())

    service.save(TDoc(tdoc_id="R2-000010", title="Agenda"))
    service.save(TDoc(tdoc_id="R2-000011", title="CR pack", meeting="RAN2#130"))

    rows = service.list_recent(limit=5)
    ids = {r.tdoc_id for r in rows}

    assert ids == {"R2-000010", "R2-000011"}
