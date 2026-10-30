"""SQLite repository coverage for spec-document ZIP inventories."""

import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import OperationalError

from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import SpecDocSourceORM
from doc3gpp.storage.db.session import get_engine, get_specdata_engine
from doc3gpp.storage.repositories.spec_doc_sql import SQLAlchemySpecDocRepository


def test_inventory_round_trip_and_isolation(sqlite_env):
    from doc3gpp.models.spec_doc_zip_inventory import SpecDocZipInventory
    from doc3gpp.storage.repositories.spec_doc_zip_inventory_sql import (
        SQLAlchemySpecDocZipInventoryRepository,
    )

    create_schema("specdata")
    repo = SQLAlchemySpecDocZipInventoryRepository()
    sources = SQLAlchemySpecDocRepository()
    sources.record_download("38.331", "18.5.0", release="Rel-18", ftp_url="old", docx_count=2)
    repo.upsert(
        SpecDocZipInventory("38.331", "18.5.0", "Rel-18", ("α.docx", "nested.zip", "α.docx"))
    )
    repo.upsert(SpecDocZipInventory("38.331", "19.0.0", None, ()))
    repo.upsert(SpecDocZipInventory("38.101", "18.5.0", "Rel-18", ("other.docx",)))

    assert repo.get("38.331", "18.5.0") == SpecDocZipInventory(
        "38.331", "18.5.0", "Rel-18", ("α.docx", "nested.zip", "α.docx")
    )
    assert repo.get("38.331", "19.0.0").zip_files == ()
    assert repo.get("38.331", "17.0.0") is None
    assert set(repo.get_for_versions("38.331", ["18.5.0", "19.0.0"])) == {"18.5.0", "19.0.0"}

    repo.upsert(SpecDocZipInventory("38.331", "18.5.0", None, ("updated.docx",)))
    assert repo.get("38.331", "18.5.0") == SpecDocZipInventory(
        "38.331", "18.5.0", None, ("updated.docx",)
    )
    assert repo.get("38.331", "19.0.0") == SpecDocZipInventory("38.331", "19.0.0", None, ())
    assert repo.get("38.101", "18.5.0") == SpecDocZipInventory(
        "38.101", "18.5.0", "Rel-18", ("other.docx",)
    )
    assert sources.get_source("38.101", "18.5.0") is None
    assert sources.get_source("38.331", "18.5.0").ftp_url == "old"


def test_concurrent_first_writes_both_succeed(sqlite_env):
    from doc3gpp.models.spec_doc_zip_inventory import SpecDocZipInventory
    from doc3gpp.storage.repositories.spec_doc_zip_inventory_sql import (
        SQLAlchemySpecDocZipInventoryRepository,
    )

    create_schema("specdata")
    repo = SQLAlchemySpecDocZipInventoryRepository()
    engine = get_specdata_engine()
    barrier = threading.Barrier(2)

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        sql = statement.lstrip().upper()
        if sql.startswith("INSERT") and "SPEC_DOC_ZIP_INVENTORY" in sql:
            barrier.wait(timeout=10)

    inventories = (
        SpecDocZipInventory("38.331", "18.5.0", "Rel-18", ("first.docx",)),
        SpecDocZipInventory("38.331", "18.5.0", None, ("second.docx",)),
    )
    event.listen(engine, "before_cursor_execute", before_execute)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(repo.upsert, inventory) for inventory in inventories]
            for future in futures:
                future.result(timeout=15)
    finally:
        event.remove(engine, "before_cursor_execute", before_execute)

    assert repo.get("38.331", "18.5.0") in inventories


def test_upsert_rejects_non_sqlite_before_any_sql(sqlite_env, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    from doc3gpp.models.spec_doc_zip_inventory import SpecDocZipInventory
    from doc3gpp.storage.repositories.spec_doc_zip_inventory_sql import (
        SQLAlchemySpecDocZipInventoryRepository,
    )

    create_schema("specdata")
    engine = get_specdata_engine()
    monkeypatch.setattr(engine.dialect, "name", "postgresql")
    repo = SQLAlchemySpecDocZipInventoryRepository(sessionmaker(bind=engine))
    statements = []

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        with pytest.raises(RuntimeError, match="SQLite.*postgresql"):
            repo.upsert(SpecDocZipInventory("38.331", "18.5.0", None, ("file.docx",)))
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)

    assert statements == []
    assert repo.get("38.331", "18.5.0") is None


@pytest.mark.parametrize(
    "reason",
    [
        "inventory rejected",
        (
            "UNIQUE constraint failed: spec_doc_zip_inventory.spec_id, "
            "spec_doc_zip_inventory.version"
        ),
    ],
)
def test_upsert_does_not_hide_unrelated_integrity_errors(sqlite_env, reason):
    from sqlalchemy.exc import IntegrityError

    from doc3gpp.models.spec_doc_zip_inventory import SpecDocZipInventory
    from doc3gpp.storage.repositories.spec_doc_zip_inventory_sql import (
        SQLAlchemySpecDocZipInventoryRepository,
    )

    create_schema("specdata")
    repo = SQLAlchemySpecDocZipInventoryRepository()
    good = SpecDocZipInventory("38.331", "18.5.0", "good", ("good.docx",))
    repo.upsert(good)
    engine = get_specdata_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TRIGGER reject_bad_inventory BEFORE INSERT ON spec_doc_zip_inventory "
                "WHEN NEW.release = 'bad' "
                f"BEGIN SELECT RAISE(ABORT, '{reason}'); END"
            )
        )
    bad = SpecDocZipInventory("38.331", "18.5.0", "bad", ("bad.docx",))
    with pytest.raises(IntegrityError, match=re.escape(reason)):
        repo.upsert(bad)
    assert repo.get("38.331", "18.5.0") == good


def test_inventory_empty_and_missing_are_distinct(sqlite_env):
    from doc3gpp.models.spec_doc_zip_inventory import SpecDocZipInventory
    from doc3gpp.storage.repositories.spec_doc_zip_inventory_sql import (
        SQLAlchemySpecDocZipInventoryRepository,
    )

    create_schema("specdata")
    repo = SQLAlchemySpecDocZipInventoryRepository()
    repo.upsert(SpecDocZipInventory("38.331", "19.0.0", None, ()))
    assert repo.get("38.331", "19.0.0").zip_files == ()
    assert repo.get("38.331", "17.0.0") is None
    assert repo.get_for_versions("38.331", ["19.0.0", "17.0.0"]) == {
        "19.0.0": SpecDocZipInventory("38.331", "19.0.0", None, ())
    }


def test_inventory_batch_uses_one_query_for_a_normal_page(sqlite_env):
    from doc3gpp.models.spec_doc_zip_inventory import SpecDocZipInventory
    from doc3gpp.storage.repositories.spec_doc_zip_inventory_sql import (
        SQLAlchemySpecDocZipInventoryRepository,
    )

    create_schema("specdata")
    repo = SQLAlchemySpecDocZipInventoryRepository()
    repo.upsert(SpecDocZipInventory("38.331", "18.5.0", "Rel-18", ("a",)))
    repo.upsert(SpecDocZipInventory("38.331", "19.0.0", None, ()))
    statements = []
    engine = get_specdata_engine()

    def count_query(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count_query)
    try:
        assert set(repo.get_for_versions("38.331", ["18.5.0", "19.0.0", "17.0.0"])) == {
            "18.5.0",
            "19.0.0",
        }
        assert len(statements) == 1
        assert repo.get_for_versions("38.331", []) == {}
        assert len(statements) == 1
    finally:
        event.remove(engine, "before_cursor_execute", count_query)


@pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "setlimit"), reason="SQLite runtime limits require Python 3.11"
)
def test_inventory_batch_respects_sqlite_bind_limit(sqlite_env):
    from doc3gpp.models.spec_doc_zip_inventory import SpecDocZipInventory
    from doc3gpp.storage.repositories.spec_doc_zip_inventory_sql import (
        SQLAlchemySpecDocZipInventoryRepository,
    )

    create_schema("specdata")
    repo = SQLAlchemySpecDocZipInventoryRepository()
    repo.upsert(SpecDocZipInventory("38.331", "0", None, ("first",)))
    repo.upsert(SpecDocZipInventory("38.331", "1100", None, ("last",)))
    engine = get_specdata_engine()
    with engine.connect() as conn:
        raw = conn.connection.driver_connection
        old_limit = raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 500)
    try:
        result = repo.get_for_versions("38.331", [str(i) for i in range(1101)])
        assert set(result) == {"0", "1100"}
        assert result["1100"].zip_files == ("last",)
    finally:
        with engine.connect() as conn:
            conn.connection.driver_connection.setlimit(
                sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, old_limit
            )


@pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "setlimit"), reason="SQLite runtime limits require Python 3.11"
)
def test_inventory_batch_respects_limit_without_getlimit(sqlite_env):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from doc3gpp.models.spec_doc_zip_inventory import SpecDocZipInventory
    from doc3gpp.storage.db.models import SpecDocZipInventoryORM
    from doc3gpp.storage.repositories.spec_doc_zip_inventory_sql import (
        SQLAlchemySpecDocZipInventoryRepository,
    )

    class LegacyConnection(sqlite3.Connection):
        @property
        def getlimit(self):
            raise AttributeError("getlimit unavailable")

    def connect():
        conn = sqlite3.connect(":memory:", factory=LegacyConnection)
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 500)
        return conn

    engine = create_engine("sqlite://", creator=connect)
    try:
        SpecDocZipInventoryORM.__table__.create(engine)
        repo = SQLAlchemySpecDocZipInventoryRepository(sessionmaker(bind=engine))
        repo.upsert(SpecDocZipInventory("38.331", "0", None, ("first",)))
        repo.upsert(SpecDocZipInventory("38.331", "1100", None, ("last",)))
        result = repo.get_for_versions("38.331", [str(i) for i in range(1101)])
        assert set(result) == {"0", "1100"}
        assert result["1100"].zip_files == ("last",)
    finally:
        engine.dispose()


def test_missing_table_only_is_unknown_and_writes_explain_upgrade(sqlite_env):
    from doc3gpp.models.spec_doc_zip_inventory import SpecDocZipInventory
    from doc3gpp.storage.repositories.spec_doc_zip_inventory_sql import (
        SpecDocInventorySchemaError,
        SQLAlchemySpecDocZipInventoryRepository,
    )

    SpecDocSourceORM.__table__.create(get_specdata_engine())
    repo = SQLAlchemySpecDocZipInventoryRepository()
    assert repo.get("38.331", "18.5.0") is None
    assert repo.get_for_versions("38.331", ["18.5.0"]) == {}
    assert not inspect(get_specdata_engine()).has_table("spec_doc_zip_inventory")
    with pytest.raises(SpecDocInventorySchemaError, match="db init --scope specdata"):
        repo.upsert(SpecDocZipInventory("38.331", "18.5.0", None, ()))


def test_other_database_errors_are_not_hidden(sqlite_env):
    from doc3gpp.models.spec_doc_zip_inventory import SpecDocZipInventory
    from doc3gpp.storage.repositories.spec_doc_zip_inventory_sql import (
        SQLAlchemySpecDocZipInventoryRepository,
    )

    engine = get_specdata_engine()
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE spec_doc_zip_inventory (spec_id TEXT, version TEXT, release TEXT)")
        )
    repo = SQLAlchemySpecDocZipInventoryRepository()
    with pytest.raises(OperationalError, match="no such column"):
        repo.get("38.331", "18.5.0")
    with pytest.raises(OperationalError, match="no such column"):
        repo.get_for_versions("38.331", ["18.5.0"])
    with pytest.raises(OperationalError, match="no column named zip_files"):
        repo.upsert(SpecDocZipInventory("38.331", "18.5.0", None, ()))


def test_unrelated_missing_table_is_not_hidden(sqlite_env):
    from doc3gpp.storage.repositories.spec_doc_zip_inventory_sql import (
        SQLAlchemySpecDocZipInventoryRepository,
    )

    engine = get_specdata_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE VIEW spec_doc_zip_inventory AS "
                "SELECT spec_id, version, release, zip_files FROM missing_spec_doc_zip_inventory"
            )
        )
    repo = SQLAlchemySpecDocZipInventoryRepository()
    with pytest.raises(OperationalError, match="missing_spec_doc_zip_inventory"):
        repo.get("38.331", "18.5.0")
    with pytest.raises(OperationalError, match="missing_spec_doc_zip_inventory"):
        repo.get_for_versions("38.331", ["18.5.0"])


def test_specdata_upgrade_is_additive_and_idempotent(sqlite_env):
    SpecDocSourceORM.__table__.create(get_specdata_engine())
    sources = SQLAlchemySpecDocRepository()
    sources.record_download("38.331", "18.5.0", release="Rel-18", ftp_url="old", docx_count=1)
    create_schema("main")

    create_schema("specdata")
    create_schema("specdata")
    assert inspect(get_specdata_engine()).has_table("spec_doc_zip_inventory")
    assert not inspect(get_engine()).has_table("spec_doc_zip_inventory")
    source = sources.get_source("38.331", "18.5.0")
    assert source.ftp_url == "old"
    assert source.release == "Rel-18"
    assert source.docx_count == 1
