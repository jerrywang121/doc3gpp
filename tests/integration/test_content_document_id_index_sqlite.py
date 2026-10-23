"""SQLite coverage for the ``document_id`` index on the TDoc content tables.

``document_id`` joins the four per-type content tables in the FTS union
and freshness paths, so each table indexes it. ``Base.metadata.create_all``
does not add indexes to tables that already exist, so
``_migrate_content_document_id_index`` backfills the index on older
databases.
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine

CONTENT_TABLES = (
    "tdoc_cr_annexes",
    "tdoc_wid_contents",
    "tdoc_ls_contents",
    "tdoc_discussion_contents",
)


def _document_id_indexes(table: str) -> list[dict]:
    return [
        index
        for index in inspect(get_engine()).get_indexes(table)
        if index["column_names"] == ["document_id"]
    ]


def _drop_document_id_indexes(tables: tuple[str, ...]) -> None:
    with get_engine().begin() as conn:
        for table in tables:
            conn.execute(text(f"DROP INDEX IF EXISTS ix_{table}_document_id"))


def test_fresh_db_has_document_id_index(sqlite_env) -> None:
    create_schema("main")

    for table in CONTENT_TABLES:
        assert _document_id_indexes(table), table


def test_upgrade_adds_document_id_index(sqlite_env) -> None:
    create_schema("main")
    _drop_document_id_indexes(CONTENT_TABLES)
    for table in CONTENT_TABLES:
        assert not _document_id_indexes(table), table

    create_schema("main")

    for table in CONTENT_TABLES:
        assert _document_id_indexes(table), table


def test_migration_is_idempotent(sqlite_env) -> None:
    from doc3gpp.storage.db.migrate import _migrate_content_document_id_index

    create_schema("main")

    _migrate_content_document_id_index()
    _migrate_content_document_id_index()

    for table in CONTENT_TABLES:
        assert len(_document_id_indexes(table)) == 1, table


def test_missing_table_is_skipped(sqlite_env) -> None:
    from doc3gpp.storage.db.migrate import _migrate_content_document_id_index

    create_schema("main")
    _drop_document_id_indexes(CONTENT_TABLES)
    with get_engine().begin() as conn:
        conn.execute(text("DROP TABLE tdoc_ls_contents"))

    _migrate_content_document_id_index()

    assert not inspect(get_engine()).has_table("tdoc_ls_contents")
    for table in CONTENT_TABLES:
        if table == "tdoc_ls_contents":
            continue
        assert len(_document_id_indexes(table)) == 1, table
