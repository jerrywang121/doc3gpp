from __future__ import annotations

import importlib
import importlib.util

from sqlalchemy import Text, inspect, text

from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import TDocORM
from doc3gpp.storage.db.session import get_engine, get_session_factory


def _inventory_api():
    model_spec = importlib.util.find_spec("doc3gpp.models.tdoc_zip_inventory")
    assert model_spec is not None, "TDoc ZIP inventory model has not been implemented"
    repository_spec = importlib.util.find_spec(
        "doc3gpp.storage.repositories.tdoc_zip_inventory_sql"
    )
    assert repository_spec is not None, "TDoc ZIP inventory repository has not been implemented"

    model_module = importlib.import_module("doc3gpp.models.tdoc_zip_inventory")
    repository_module = importlib.import_module(
        "doc3gpp.storage.repositories.tdoc_zip_inventory_sql"
    )
    return model_module.TDocZipInventory, repository_module.SQLAlchemyTDocZipInventoryRepository


def _seed_tdoc(tdoc_id: str = "R5s260009") -> None:
    with get_session_factory()() as session:
        session.add(TDocORM(tdoc_id=tdoc_id))
        session.commit()


def test_inventory_round_trips_order_duplicates_and_normalized_urls(sqlite_env) -> None:
    Inventory, Repository = _inventory_api()
    create_schema("main")
    _seed_tdoc()
    repository = Repository()
    inventory = Inventory(
        ftp_url="https://www.3gpp.org/ftp/TSG_RAN/WG5/Archive.zip",
        tdoc_id="R5s260009",
        zip_files=("main.docx", "dup.pdf", "dup.pdf", "日本語/文書.pdf"),
    )

    repository.upsert(inventory)

    expected = Inventory(
        ftp_url="tsg_ran/wg5/archive.zip",
        tdoc_id="R5s260009",
        zip_files=("main.docx", "dup.pdf", "dup.pdf", "日本語/文書.pdf"),
    )
    assert repository.get_by_url(inventory.ftp_url) == expected
    assert repository.get_by_url("/TSG_RAN/WG5/ARCHIVE.ZIP") == expected


def test_inventory_revisions_empty_and_unknown_are_distinct(sqlite_env) -> None:
    Inventory, Repository = _inventory_api()
    create_schema("main")
    _seed_tdoc()
    repository = Repository()
    revision = Inventory(
        ftp_url="tsg_ran/wg5/old.zip",
        tdoc_id="R5s260009",
        zip_files=("old.doc",),
    )
    empty = Inventory(
        ftp_url="tsg_ran/wg5/empty.zip",
        tdoc_id="R5s260009",
        zip_files=(),
    )

    repository.upsert(revision)
    repository.upsert(empty)

    assert repository.get_by_url("tsg_ran/wg5/old.zip") == revision
    assert repository.get_by_url("tsg_ran/wg5/empty.zip") == empty
    assert repository.get_by_url("tsg_ran/wg5/unknown.zip") is None

    with get_session_factory()() as session:
        tdoc = session.get(TDocORM, "R5s260009")
        assert tdoc is not None
        session.delete(tdoc)
        session.commit()
    assert repository.get_by_url(revision.ftp_url) is None
    assert repository.get_by_url(empty.ftp_url) is None


def test_inventory_survives_cache_eviction(sqlite_env, tmp_path) -> None:
    Inventory, Repository = _inventory_api()
    create_schema("main")
    _seed_tdoc()
    repository = Repository()
    inventory = Inventory(
        ftp_url="tsg_ran/wg5/archive.zip",
        tdoc_id="R5s260009",
        zip_files=("main.docx", "support.pdf"),
    )
    repository.upsert(inventory)

    cached_zip = tmp_path / "cache" / "zips" / "archive.zip"
    cached_zip.parent.mkdir(parents=True)
    cached_zip.write_bytes(b"cached ZIP bytes")
    cached_zip.unlink()

    assert not cached_zip.exists()
    assert repository.get_by_url(inventory.ftp_url) == inventory


def test_main_schema_upgrade_is_additive_and_idempotent(sqlite_env) -> None:
    engine = get_engine()
    existing_tables = [
        table for table in Base.metadata.sorted_tables
        if table.name != "tdoc_zip_inventory"
    ]
    Base.metadata.create_all(bind=engine, tables=existing_tables)
    with get_session_factory()() as session:
        session.add(TDocORM(tdoc_id="R5s260009", title="retained"))
        session.commit()

    assert not inspect(engine).has_table("tdoc_zip_inventory")

    create_schema("main")
    inspector = inspect(engine)
    assert inspector.has_table("tdoc_zip_inventory")
    assert inspector.get_pk_constraint("tdoc_zip_inventory")["constrained_columns"] == ["ftp_url"]
    columns = {column["name"]: column for column in inspector.get_columns("tdoc_zip_inventory")}
    assert isinstance(columns["zip_files"]["type"], Text)
    assert columns["zip_files"]["nullable"] is False
    assert any(
        index["column_names"] == ["tdoc_id"]
        for index in inspector.get_indexes("tdoc_zip_inventory")
    )
    foreign_key = inspector.get_foreign_keys("tdoc_zip_inventory")[0]
    assert foreign_key["constrained_columns"] == ["tdoc_id"]
    assert foreign_key["referred_table"] == "tdocs"
    assert foreign_key["options"].get("ondelete") == "CASCADE"
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT title FROM tdocs WHERE tdoc_id = 'R5s260009'")
        ) == "retained"

    create_schema("main")
    assert inspect(engine).has_table("tdoc_zip_inventory")
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT title FROM tdocs WHERE tdoc_id = 'R5s260009'")
        ) == "retained"
