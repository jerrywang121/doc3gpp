"""Transactional upgrades for legacy LISTSERV tables."""

from __future__ import annotations

import importlib
import importlib.util
import sqlite3
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from doc3gpp.storage.compression import compress_json
from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.listserv_catalog import ensure_listserv_catalog
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine

_MESSAGE_URL = "https://list.etsi.org/archives/legacy/2026-01/msg00001.html"
_MESSAGE_LIST_ID = "LEGACY-MESSAGE"
_PAGE_LIST_ID = "LEGACY-PAGE-ONLY"
_PAGE_URL = "https://list.etsi.org/archives/legacy/2026-01/"
_NEXT_URL = "https://list.etsi.org/archives/legacy/2026-01/2.html"
_DATA_TABLES = (
    "listserv_messages",
    "listserv_details",
    "listserv_archive_pages",
    "listserv_message_audit",
    "listserv_search",
    "listserv_search_meta",
    "vec_listserv_embeddings",
    "vec_listserv_meta",
    "vec_listserv_chunk_text",
)
_INDEXED_TABLES = (
    "listserv_messages",
    "listserv_details",
    "listserv_archive_pages",
)


def _migration_module():
    module_name = "doc3gpp.storage.db.listserv_catalog_migration"
    assert importlib.util.find_spec(module_name) is not None, (
        "the transactional LISTSERV catalog migration module is missing"
    )
    return importlib.import_module(module_name)


def _create_legacy_database(
    db_path: Path,
    *,
    extra_message_column: bool = False,
    with_sidecars: bool = False,
) -> None:
    """Build the pre-catalog schema, including the detail-to-message FK."""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        connection.executescript(
            """
            CREATE TABLE listserv_messages (
                url VARCHAR(1024) NOT NULL,
                list_id VARCHAR(128) NOT NULL,
                subject TEXT NOT NULL,
                sender TEXT NOT NULL,
                local_datetime DATETIME NOT NULL,
                local_date DATE NOT NULL,
                tz_offset_seconds INTEGER,
                date_precision VARCHAR(16) NOT NULL,
                sort_timestamp DATETIME NOT NULL,
                week VARCHAR(16) NOT NULL,
                subject_tdocs TEXT NOT NULL,
                source_revision INTEGER DEFAULT 1 NOT NULL,
                PRIMARY KEY (url)
            );
            CREATE INDEX ix_listserv_messages_list_id
                ON listserv_messages (list_id);
            CREATE INDEX ix_listserv_messages_local_date
                ON listserv_messages (local_date);
            CREATE INDEX idx_listserv_messages_list_sort
                ON listserv_messages (list_id, sort_timestamp);
            CREATE INDEX idx_listserv_messages_sort_url
                ON listserv_messages (sort_timestamp, url);
            CREATE INDEX idx_listserv_messages_legacy_sender
                ON listserv_messages (sender);

            CREATE TABLE listserv_details (
                url VARCHAR(1024) NOT NULL,
                attachments TEXT NOT NULL,
                tdocs TEXT NOT NULL,
                content BLOB NOT NULL,
                content_truncated BOOLEAN NOT NULL,
                parser_version VARCHAR(32) NOT NULL,
                parsed_at DATETIME NOT NULL,
                PRIMARY KEY (url),
                FOREIGN KEY (url) REFERENCES listserv_messages (url)
                    ON DELETE CASCADE
            );

            CREATE TABLE listserv_archive_pages (
                list_id VARCHAR(128) NOT NULL,
                page_url VARCHAR(1024) NOT NULL,
                period VARCHAR(32) NOT NULL,
                coverage TEXT NOT NULL,
                next_urls TEXT NOT NULL,
                PRIMARY KEY (list_id, page_url)
            );
            CREATE INDEX idx_listserv_archive_pages_period
                ON listserv_archive_pages (period);

            CREATE TABLE listserv_message_audit (
                url TEXT NOT NULL,
                old_subject TEXT NOT NULL,
                new_subject TEXT NOT NULL
            );
            CREATE TRIGGER trg_listserv_message_subject_audit
            AFTER UPDATE OF subject ON listserv_messages
            BEGIN
                INSERT INTO listserv_message_audit (url, old_subject, new_subject)
                VALUES (OLD.url, OLD.subject, NEW.subject);
            END;
            """
        )
        if extra_message_column:
            connection.execute(
                "ALTER TABLE listserv_messages ADD COLUMN legacy_marker TEXT"
            )
            connection.execute(
                "CREATE INDEX idx_listserv_messages_legacy_marker "
                "ON listserv_messages (legacy_marker)"
            )

        message_columns = (
            "url, list_id, subject, sender, local_datetime, local_date, "
            "tz_offset_seconds, date_precision, sort_timestamp, week, "
            "subject_tdocs, source_revision"
        )
        message_values: tuple[object, ...] = (
            _MESSAGE_URL,
            _MESSAGE_LIST_ID,
            "Legacy migration message",
            "legacy@example.test",
            "2026-01-03 04:05:06",
            "2026-01-03",
            0,
            "datetime",
            "2026-01-03 04:05:06",
            "2026-01-W1",
            "RAN-260001",
            7,
        )
        if extra_message_column:
            message_columns += ", legacy_marker"
            message_values += ("preserve-me",)
        placeholders = ", ".join("?" for _ in message_values)
        connection.execute(
            f"INSERT INTO listserv_messages ({message_columns}) "
            f"VALUES ({placeholders})",
            message_values,
        )
        connection.execute(
            """
            INSERT INTO listserv_details (
                url, attachments, tdocs, content, content_truncated,
                parser_version, parsed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _MESSAGE_URL,
                '[{"name":"legacy.pdf","url":"https://list.etsi.org/legacy.pdf"}]',
                "RAN-260001",
                compress_json("compressed legacy message body"),
                0,
                "legacy-parser",
                "2026-01-03 04:06:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO listserv_archive_pages (
                list_id, page_url, period, coverage, next_urls
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                _PAGE_LIST_ID,
                _PAGE_URL,
                "2026-01",
                '[["2026-01-01","2026-01-07"]]',
                f'["{_NEXT_URL}"]',
            ),
        )

        if with_sidecars:
            options = {
                row[0] for row in connection.execute("PRAGMA compile_options").fetchall()
            }
            if "ENABLE_FTS5" in options:
                connection.execute(
                    """
                    CREATE VIRTUAL TABLE listserv_search USING fts5(
                        url UNINDEXED, list_id, subject, sender, date, week, tdocs,
                        attachments, content, chunk_index UNINDEXED,
                        source_revision UNINDEXED
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO listserv_search (
                        url, list_id, subject, sender, date, week, tdocs,
                        attachments, content, chunk_index, source_revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _MESSAGE_URL,
                        _MESSAGE_LIST_ID,
                        "legacy fts subject",
                        "legacy@example.test",
                        "2026-01-03",
                        "2026-01-W1",
                        "RAN-260001",
                        "legacy.pdf",
                        "legacy indexed content",
                        0,
                        7,
                    ),
                )
                connection.execute(
                    "CREATE TABLE listserv_search_meta "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO listserv_search_meta (key, value) "
                    "VALUES ('legacy-marker', 'keep-fts-sidecar')"
                )

            try:
                from doc3gpp.storage.backends.sqlite import load_sqlite_vec

                load_sqlite_vec(connection)
            except ImportError:
                pass
            else:
                connection.execute(
                    """
                    CREATE VIRTUAL TABLE vec_listserv_embeddings USING vec0(
                        chunk_id TEXT PRIMARY KEY,
                        url TEXT,
                        chunk_index INTEGER,
                        source_revision INTEGER,
                        embedding FLOAT[384] distance_metric=cosine
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO vec_listserv_embeddings (
                        chunk_id, url, chunk_index, source_revision, embedding
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-vector-chunk",
                        _MESSAGE_URL,
                        0,
                        7,
                        struct.pack("<384f", *([0.125] * 384)),
                    ),
                )
                connection.execute(
                    "CREATE TABLE vec_listserv_meta "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO vec_listserv_meta (key, value) "
                    "VALUES ('legacy-marker', 'keep-vector-sidecar')"
                )
                connection.execute(
                    """
                    CREATE TABLE vec_listserv_chunk_text (
                        chunk_id TEXT PRIMARY KEY,
                        url TEXT NOT NULL,
                        chunk_index INTEGER NOT NULL,
                        source_revision INTEGER NOT NULL,
                        fingerprint TEXT NOT NULL,
                        text TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX idx_vec_listserv_chunk_text_url "
                    "ON vec_listserv_chunk_text (url)"
                )
                connection.execute(
                    """
                    INSERT INTO vec_listserv_chunk_text (
                        chunk_id, url, chunk_index, source_revision, fingerprint, text
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-vector-chunk",
                        _MESSAGE_URL,
                        0,
                        7,
                        "legacy-fingerprint",
                        "legacy vector text",
                    ),
                )
        connection.commit()
    finally:
        connection.close()


def _initialize_catalog(engine) -> None:
    Base.metadata.create_all(bind=engine)
    ensure_listserv_catalog(engine)


def _snapshot(engine):
    with engine.connect() as connection:
        try:
            from doc3gpp.storage.backends.sqlite import load_sqlite_vec

            load_sqlite_vec(connection.connection.driver_connection)
        except ImportError:
            pass

        data: dict[str, tuple[tuple[object, ...], ...]] = {}
        for table in _DATA_TABLES:
            exists = connection.execute(
                text(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = :name"
                ),
                {"name": table},
            ).first()
            if exists:
                rows = connection.execute(text(f'SELECT * FROM "{table}"')).all()
                data[table] = tuple(tuple(row) for row in rows)

        indexed_names = ", ".join(f"'{table}'" for table in _INDEXED_TABLES)
        schema_objects = tuple(
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    f"WHERE type IN ('index', 'trigger') AND tbl_name IN ({indexed_names}) "
                    "ORDER BY tbl_name, type, name"
                )
            ).all()
        )
    return data, schema_objects


def _assert_catalog_upgrade(engine) -> None:
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        message_fks = connection.exec_driver_sql(
            "PRAGMA foreign_key_list('listserv_messages')"
        ).all()
        page_fks = connection.exec_driver_sql(
            "PRAGMA foreign_key_list('listserv_archive_pages')"
        ).all()
        detail_fks = connection.exec_driver_sql(
            "PRAGMA foreign_key_list('listserv_details')"
        ).all()
        assert any(
            row[2:5] == ("listserv_list", "list_id", "list_id")
            for row in message_fks
        )
        assert any(
            row[2:5] == ("listserv_list", "list_id", "list_id") for row in page_fks
        )
        assert any(
            row[2:5] == ("listserv_messages", "url", "url") and row[6] == "CASCADE"
            for row in detail_fks
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

        parents = {
            row[0]: tuple(row[1:])
            for row in connection.execute(
                text(
                    "SELECT list_id, tsg, description FROM listserv_list "
                    "WHERE list_id IN (:message_id, :page_id, '3GPP_TSG_CT')"
                ),
                {"message_id": _MESSAGE_LIST_ID, "page_id": _PAGE_LIST_ID},
            ).all()
        }
        assert parents == {
            _MESSAGE_LIST_ID: (None, None),
            _PAGE_LIST_ID: (None, None),
            "3GPP_TSG_CT": ("CP", "3GPP_TSG_CT - Core Network and Terminals"),
        }

        with pytest.raises(IntegrityError), engine.begin() as transaction:
            transaction.execute(
                text(
                    "INSERT INTO listserv_messages ("
                    "url, list_id, subject, sender, local_datetime, local_date, "
                    "date_precision, sort_timestamp, week, subject_tdocs, "
                    "source_revision"
                    ") VALUES ("
                    "'https://example.test/orphan-message', 'UNKNOWN_LIST', 's', "
                    "'s', '2026-01-01 00:00:00', '2026-01-01', 'date', "
                    "'2026-01-01 00:00:00', '2026-01-W1', '', 1"
                    ")"
                )
            )
        with pytest.raises(IntegrityError), engine.begin() as transaction:
            transaction.execute(
                text(
                    "INSERT INTO listserv_archive_pages ("
                    "list_id, page_url, period, coverage, next_urls"
                    ") VALUES ("
                    "'UNKNOWN_LIST', 'https://example.test/archive', '2026-01', "
                    "'[]', '[]'"
                    ")"
                )
            )

        updated_blob = compress_json("updated compressed detail")
        with engine.begin() as transaction:
            result = transaction.execute(
                text(
                    "UPDATE listserv_details SET content = :content, "
                    "parser_version = 'updated-parser' WHERE url = :url"
                ),
                {"content": updated_blob, "url": _MESSAGE_URL},
            )
            assert result.rowcount == 1
            assert transaction.execute(
                text("SELECT content FROM listserv_details WHERE url = :url"),
                {"url": _MESSAGE_URL},
            ).scalar_one() == updated_blob

    updated_subject = "Updated after table rebuild"
    with engine.begin() as transaction:
        transaction.execute(
            text("UPDATE listserv_messages SET subject = :subject WHERE url = :url"),
            {"subject": updated_subject, "url": _MESSAGE_URL},
        )
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT url, old_subject, new_subject FROM listserv_message_audit"
            )
        ).one() == (_MESSAGE_URL, "Legacy migration message", updated_subject)


def test_create_schema_upgrades_legacy_listserv_tables_without_data_loss(
    sqlite_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_legacy_database(sqlite_env, with_sidecars=True)
    engine = get_engine()
    original_rows, original_indexes = _snapshot(engine)

    create_schema("main")

    assert _snapshot(engine) == (original_rows, original_indexes)

    migration = _migration_module()

    def fail_if_rebuilt(*_args) -> None:
        pytest.fail("already-upgraded LISTSERV tables must not be rebuilt")

    monkeypatch.setattr(migration, "_prepare_rebuild", fail_if_rebuilt)
    create_schema("main")

    assert _snapshot(engine) == (original_rows, original_indexes)
    _assert_catalog_upgrade(engine)


def test_upgrade_rolls_back_after_old_message_table_drop(
    sqlite_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_legacy_database(sqlite_env)
    engine = get_engine()
    _initialize_catalog(engine)
    migration = _migration_module()
    before = _snapshot(engine)
    original_rename = migration._rename_staging_table

    def fail_after_message_drop(connection, staging_name, table_name) -> None:
        if table_name == "listserv_messages":
            raise RuntimeError("injected failure after old message table drop")
        original_rename(connection, staging_name, table_name)

    monkeypatch.setattr(migration, "_rename_staging_table", fail_after_message_drop)

    with pytest.raises(RuntimeError, match="injected failure after old message table drop"):
        migration.upgrade_listserv_catalog(engine)

    assert _snapshot(engine) == before
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql(
            "PRAGMA foreign_key_list('listserv_messages')"
        ).all() == []
        assert connection.exec_driver_sql(
            "PRAGMA foreign_key_list('listserv_archive_pages')"
        ).all() == []
        assert connection.execute(
            text("SELECT COUNT(*) FROM listserv_list WHERE list_id IN (:message_id, :page_id)"),
            {"message_id": _MESSAGE_LIST_ID, "page_id": _PAGE_LIST_ID},
        ).scalar_one() == 0
        assert connection.execute(
            text(
                "SELECT content FROM listserv_details WHERE url = :url"
            ),
            {"url": _MESSAGE_URL},
        ).scalar_one() == before[0]["listserv_details"][0][3]
        assert connection.execute(
            text(
                "SELECT coverage, next_urls FROM listserv_archive_pages "
                "WHERE list_id = :list_id"
            ),
            {"list_id": _PAGE_LIST_ID},
        ).one() == ('[["2026-01-01","2026-01-07"]]', f'["{_NEXT_URL}"]')
        assert connection.execute(
            text(
                "SELECT 1 FROM sqlite_master WHERE name LIKE "
                "'__listserv_catalog_upgrade_%'"
            )
        ).first() is None


def test_concurrent_catalog_upgrade_serializes(sqlite_env) -> None:
    _create_legacy_database(sqlite_env)
    engine = get_engine()
    _initialize_catalog(engine)
    migration = _migration_module()
    checkout_barrier = threading.Barrier(2)
    checked_out: list[int] = []

    def synchronize_connections(dbapi_connection, _connection_record, _connection_proxy):
        checked_out.append(id(dbapi_connection))
        checkout_barrier.wait(timeout=10)

    event.listen(engine, "checkout", synchronize_connections)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(migration.upgrade_listserv_catalog, engine)
                for _ in range(2)
            ]
            for future in futures:
                future.result(timeout=20)
    finally:
        event.remove(engine, "checkout", synchronize_connections)

    assert len(set(checked_out)) == 2
    create_schema("main")
    _assert_catalog_upgrade(engine)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM listserv_list WHERE list_id = :list_id"),
            {"list_id": _MESSAGE_LIST_ID},
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT COUNT(*) FROM listserv_list WHERE list_id = :list_id"),
            {"list_id": _PAGE_LIST_ID},
        ).scalar_one() == 1


def test_upgrade_rejects_unsupported_legacy_shape_without_data_loss(sqlite_env) -> None:
    _create_legacy_database(sqlite_env, extra_message_column=True)
    engine = get_engine()
    original_rows, original_indexes = _snapshot(engine)

    with pytest.raises(RuntimeError, match="unsupported.*legacy_marker"):
        create_schema("main")

    assert _snapshot(engine) == (original_rows, original_indexes)
    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info('listserv_messages')"
            )
        }
        assert "legacy_marker" in columns
        assert connection.execute(
            text(
                "SELECT legacy_marker FROM listserv_messages WHERE url = :url"
            ),
            {"url": _MESSAGE_URL},
        ).scalar_one() == "preserve-me"
        assert connection.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE name = 'idx_listserv_messages_legacy_marker'"
            )
        ).first() is not None
        assert connection.exec_driver_sql(
            "PRAGMA foreign_key_list('listserv_messages')"
        ).all() == []


def test_non_sqlite_legacy_tables_missing_fks_fail_explicitly(
    sqlite_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_legacy_database(sqlite_env)
    engine = get_engine()
    _initialize_catalog(engine)
    migration = _migration_module()
    original_rows, original_indexes = _snapshot(engine)
    monkeypatch.setattr(engine.dialect, "name", "postgresql")

    with pytest.raises(RuntimeError, match="legacy LISTSERV.*only supported for SQLite"):
        migration.upgrade_listserv_catalog(engine)

    assert _snapshot(engine) == (original_rows, original_indexes)


def test_temp_staging_table_collision_fails_safely_without_losing_main_messages(
    sqlite_env,
) -> None:
    _create_legacy_database(sqlite_env)
    engine = get_engine()
    _initialize_catalog(engine)
    with engine.begin() as connection:
        deleted = connection.execute(
            text("DELETE FROM listserv_details WHERE url = :url"),
            {"url": _MESSAGE_URL},
        )
        assert deleted.rowcount == 1

    # Leave only one pooled connection so the TEMP table is guaranteed to be
    # present on the same DBAPI connection used by the migration.
    engine.dispose()
    checked_out_ids: list[int] = []

    def record_checkout(dbapi_connection, _connection_record, _connection_proxy) -> None:
        checked_out_ids.append(id(dbapi_connection))

    event.listen(engine, "checkout", record_checkout)
    migration_error: RuntimeError | None = None
    try:
        with engine.connect() as connection:
            collision_connection_id = id(connection.connection.driver_connection)
            connection.exec_driver_sql(
                """
                CREATE TEMP TABLE __listserv_catalog_upgrade_messages (
                    url VARCHAR(1024) NOT NULL,
                    list_id VARCHAR(128) NOT NULL,
                    subject TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    local_datetime DATETIME NOT NULL,
                    local_date DATE NOT NULL,
                    tz_offset_seconds INTEGER,
                    date_precision VARCHAR(16) NOT NULL,
                    sort_timestamp DATETIME NOT NULL,
                    week VARCHAR(16) NOT NULL,
                    subject_tdocs TEXT NOT NULL,
                    source_revision INTEGER DEFAULT 1 NOT NULL,
                    PRIMARY KEY (url),
                    FOREIGN KEY (list_id) REFERENCES listserv_list (list_id)
                )
                """
            )

        migration = _migration_module()
        try:
            migration.upgrade_listserv_catalog(engine)
        except RuntimeError as exc:
            migration_error = exc

        with engine.connect() as connection:
            main_table_exists = connection.execute(
                text(
                    "SELECT 1 FROM main.sqlite_master "
                    "WHERE type = 'table' AND name = 'listserv_messages'"
                )
            ).scalar_one_or_none()
            assert main_table_exists == 1
            assert connection.execute(
                text(
                    "SELECT url, list_id, subject FROM main.listserv_messages "
                    "WHERE url = :url"
                ),
                {"url": _MESSAGE_URL},
            ).one() == (
                _MESSAGE_URL,
                _MESSAGE_LIST_ID,
                "Legacy migration message",
            )
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.execute(
                text(
                    "SELECT 1 FROM sqlite_temp_master "
                    "WHERE type = 'table' "
                    "AND name = '__listserv_catalog_upgrade_messages'"
                )
            ).first() is not None

        assert checked_out_ids[:2] == [collision_connection_id, collision_connection_id]
        assert migration_error is not None
        assert "TEMP" in str(migration_error)
    finally:
        event.remove(engine, "checkout", record_checkout)


def test_repeated_upgrade_skips_writer_lock_for_current_catalog_fks(
    sqlite_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_legacy_database(sqlite_env)
    engine = get_engine()
    create_schema("main")
    _assert_catalog_upgrade(engine)
    migration = _migration_module()

    def fail_if_migration_work_runs(*_args) -> None:
        pytest.fail("a fully upgraded schema must skip the migration transaction")

    monkeypatch.setattr(
        migration, "_upgrade_sqlite_transaction", fail_if_migration_work_runs
    )

    def disable_busy_wait(dbapi_connection, _connection_record, _connection_proxy) -> None:
        dbapi_connection.execute("PRAGMA busy_timeout=0")

    event.listen(engine, "checkout", disable_busy_wait)
    writer = sqlite3.connect(sqlite_env, timeout=0)
    try:
        writer.execute("BEGIN IMMEDIATE")
        migration.upgrade_listserv_catalog(engine)
    finally:
        if writer.in_transaction:
            writer.rollback()
        writer.close()
        event.remove(engine, "checkout", disable_busy_wait)


def _add_legacy_views(engine) -> tuple[tuple[object, ...], ...]:
    with engine.begin() as connection:
        statements = (
            "CREATE VIEW message_view AS SELECT url, subject FROM listserv_messages",
            "CREATE VIEW nested_message_view AS SELECT url, subject FROM message_view",
            "CREATE VIEW page_view AS SELECT list_id, page_url, period FROM listserv_archive_pages",
            "CREATE VIEW nested_page_view AS SELECT list_id, page_url, period FROM page_view",
            (
                "CREATE TRIGGER update_message_view INSTEAD OF UPDATE ON nested_message_view "
                "BEGIN UPDATE listserv_messages SET subject=NEW.subject WHERE url=OLD.url; END"
            ),
            (
                "CREATE TRIGGER update_page_view INSTEAD OF UPDATE ON nested_page_view "
                "BEGIN UPDATE listserv_archive_pages SET period=NEW.period "
                "WHERE list_id=OLD.list_id AND page_url=OLD.page_url; END"
            ),
        )
        for statement in statements:
            connection.exec_driver_sql(statement)
        return _view_schema(connection)


def _view_schema(connection) -> tuple[tuple[object, ...], ...]:
    return tuple(tuple(row) for row in connection.exec_driver_sql(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE type='view' OR name IN ('update_message_view', 'update_page_view') "
        "ORDER BY type, name"
    ))


def _set_pooled_pragmas(engine, value: int, foreign_keys: int):
    connection = engine.raw_connection()
    dbapi = connection.driver_connection
    dbapi.execute(f"PRAGMA foreign_keys={foreign_keys}")
    assert dbapi.execute("PRAGMA foreign_keys").fetchone() == (foreign_keys,)
    dbapi.execute(f"PRAGMA legacy_alter_table={value}")
    if dbapi.execute("PRAGMA legacy_alter_table").fetchone() is None:
        connection.close()
        pytest.skip("SQLite does not support PRAGMA legacy_alter_table")
    assert dbapi.execute("PRAGMA legacy_alter_table").fetchone() == (value,)
    connection.close()
    return dbapi


@pytest.mark.parametrize("legacy_setting", [0, 1])
@pytest.mark.parametrize("foreign_keys", [0, 1])
def test_upgrade_preserves_view_chains_triggers_and_connection_settings(
    sqlite_env, legacy_setting: int, foreign_keys: int
) -> None:
    _create_legacy_database(sqlite_env, with_sidecars=True)
    engine = get_engine()
    _initialize_catalog(engine)
    views = _add_legacy_views(engine)
    before = _snapshot(engine)
    dbapi = _set_pooled_pragmas(engine, legacy_setting, foreign_keys)

    create_schema("main")
    create_schema("main")

    assert _snapshot(engine) == before
    with engine.connect() as connection:
        assert connection.connection.driver_connection is dbapi
        assert connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar_one() == legacy_setting
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == foreign_keys
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert _view_schema(connection) == views
        assert connection.exec_driver_sql("SELECT subject FROM nested_message_view").scalar_one() == (
            "Legacy migration message"
        )
        assert connection.exec_driver_sql("SELECT period FROM nested_page_view").scalar_one() == "2026-01"
    with engine.begin() as connection:
        connection.exec_driver_sql("UPDATE nested_message_view SET subject='changed via view'")
        connection.exec_driver_sql("UPDATE nested_page_view SET period='changed via view'")
        assert connection.exec_driver_sql("SELECT subject FROM listserv_messages").scalar_one() == (
            "changed via view"
        )
        assert connection.exec_driver_sql("SELECT period FROM listserv_archive_pages").scalar_one() == (
            "changed via view"
        )
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM listserv_message_audit").scalar_one() == 1


@pytest.mark.parametrize("legacy_setting", [0, 1])
@pytest.mark.parametrize("foreign_keys", [0, 1])
def test_upgrade_failure_restores_views_data_and_connection_pragmas(
    sqlite_env, monkeypatch: pytest.MonkeyPatch, legacy_setting: int, foreign_keys: int
) -> None:
    _create_legacy_database(sqlite_env)
    engine = get_engine()
    _initialize_catalog(engine)
    views = _add_legacy_views(engine)
    before = _snapshot(engine)
    dbapi = _set_pooled_pragmas(engine, legacy_setting, foreign_keys)
    migration = _migration_module()
    original_rename = migration._rename_staging_table

    def fail_on_second_rebuild(cursor, staging_name, table_name):
        if table_name == "listserv_archive_pages":
            raise RuntimeError("injected second rebuild failure")
        original_rename(cursor, staging_name, table_name)

    monkeypatch.setattr(migration, "_rename_staging_table", fail_on_second_rebuild)
    with pytest.raises(RuntimeError, match="injected second rebuild failure"):
        migration.upgrade_listserv_catalog(engine)

    assert _snapshot(engine) == before
    with engine.connect() as connection:
        assert connection.connection.driver_connection is dbapi
        assert connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar_one() == legacy_setting
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == foreign_keys
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert _view_schema(connection) == views
        assert connection.exec_driver_sql("SELECT subject FROM nested_message_view").scalar_one() == (
            "Legacy migration message"
        )
        assert connection.exec_driver_sql("SELECT period FROM nested_page_view").scalar_one() == "2026-01"
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM listserv_list WHERE list_id IN (?, ?)",
            (_MESSAGE_LIST_ID, _PAGE_LIST_ID),
        ).scalar_one() == 0


@pytest.mark.parametrize(
    ("denied", "injected"),
    [("legacy_alter_table", False), ("legacy_alter_table", True),
     ("foreign_keys", True), ("rollback", True)],
)
def test_failed_cleanup_invalidates_connection_and_preserves_original_error(
    sqlite_env, monkeypatch: pytest.MonkeyPatch, denied: str, injected: bool
) -> None:
    _create_legacy_database(sqlite_env)
    engine = get_engine()
    _initialize_catalog(engine)
    views = _add_legacy_views(engine)
    before = _snapshot(engine)
    dbapi = _set_pooled_pragmas(engine, 0, 1)
    migration = _migration_module()
    transaction_started = False

    def authorizer(action, arg1, arg2, _database, _source):
        nonlocal transaction_started
        if action == sqlite3.SQLITE_TRANSACTION and arg1 == "BEGIN":
            transaction_started = True
        if (
            action == sqlite3.SQLITE_PRAGMA
            and arg1 == denied
            and arg2 == {"legacy_alter_table": "0", "foreign_keys": "1"}.get(denied)
            and (denied != "foreign_keys" or transaction_started)
        ):
            return sqlite3.SQLITE_DENY
        if denied == "rollback" and action == sqlite3.SQLITE_TRANSACTION and arg1 == "ROLLBACK":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    dbapi.set_authorizer(authorizer)
    if injected:
        def fail_rebuild(_cursor, _staging_name, _table_name):
            raise RuntimeError("original rebuild failure")
        monkeypatch.setattr(migration, "_rename_staging_table", fail_rebuild)
        expected_error = RuntimeError
        message = "original rebuild failure"
    else:
        expected_error = sqlite3.DatabaseError
        message = "not authorized"
    with pytest.raises(expected_error, match=message):
        migration.upgrade_listserv_catalog(engine)

    with engine.connect() as connection:
        assert connection.connection.driver_connection is not dbapi
        assert connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar_one() == 0
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert _view_schema(connection) == views
        assert connection.exec_driver_sql(
            "PRAGMA foreign_key_list('listserv_messages')"
        ).all() == []
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM listserv_list WHERE list_id IN (?, ?)",
            (_MESSAGE_LIST_ID, _PAGE_LIST_ID),
        ).scalar_one() == 0
    assert _snapshot(engine) == before


def test_legacy_verification_failure_occurs_before_commit_and_rolls_back(
    sqlite_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_legacy_database(sqlite_env)
    engine = get_engine()
    _initialize_catalog(engine)
    views = _add_legacy_views(engine)
    before = _snapshot(engine)
    dbapi = _set_pooled_pragmas(engine, 0, 1)
    migration = _migration_module()
    original_restore = migration._restore_legacy_alter
    calls = []

    def fail_second_verification(cursor, original):
        calls.append(cursor.connection.in_transaction)
        if len(calls) == 2:
            raise RuntimeError("injected second legacy verification failure")
        return original_restore(cursor, original)

    monkeypatch.setattr(migration, "_restore_legacy_alter", fail_second_verification)
    with pytest.raises(RuntimeError, match="injected second legacy verification failure"):
        migration.upgrade_listserv_catalog(engine)
    assert calls[:2] == [True, True]
    assert _snapshot(engine) == before
    with engine.connect() as connection:
        assert connection.connection.driver_connection is dbapi
        assert _view_schema(connection) == views
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA foreign_key_list('listserv_messages')").all() == []
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM listserv_list WHERE list_id IN (?, ?)",
            (_MESSAGE_LIST_ID, _PAGE_LIST_ID),
        ).scalar_one() == 0


def test_post_commit_foreign_key_restore_failure_reports_commit_and_invalidates(sqlite_env) -> None:
    _create_legacy_database(sqlite_env)
    engine = get_engine()
    _initialize_catalog(engine)
    views = _add_legacy_views(engine)
    before = _snapshot(engine)
    dbapi = _set_pooled_pragmas(engine, 0, 1)
    commit_seen = False

    def authorizer(action, arg1, arg2, _database, _source):
        nonlocal commit_seen
        if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
            commit_seen = True
        if (commit_seen and action == sqlite3.SQLITE_PRAGMA
                and arg1 == "foreign_keys" and arg2 == "1"):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    dbapi.set_authorizer(authorizer)
    with pytest.raises(Exception) as caught:
        _migration_module().upgrade_listserv_catalog(engine)
    assert isinstance(caught.value, _migration_module().ListservCatalogUpgradeCommittedError)
    assert "committed" in str(caught.value).lower()
    assert isinstance(caught.value.__cause__, sqlite3.DatabaseError)
    assert commit_seen
    assert _snapshot(engine) == before
    with engine.connect() as connection:
        assert connection.connection.driver_connection is not dbapi
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar_one() == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert _view_schema(connection) == views
        assert any(row[2:5] == ("listserv_list", "list_id", "list_id") for row in
                   connection.exec_driver_sql("PRAGMA foreign_key_list('listserv_messages')"))
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM listserv_list WHERE list_id IN (?, ?)",
            (_MESSAGE_LIST_ID, _PAGE_LIST_ID),
        ).scalar_one() == 2


def test_foreign_key_restoration_is_preflighted_before_migration(sqlite_env) -> None:
    _create_legacy_database(sqlite_env)
    engine = get_engine()
    _initialize_catalog(engine)
    before = _snapshot(engine)
    dbapi = _set_pooled_pragmas(engine, 0, 1)
    transactions = []

    def authorizer(action, arg1, arg2, _database, _source):
        if action == sqlite3.SQLITE_TRANSACTION:
            transactions.append(arg1)
        if action == sqlite3.SQLITE_PRAGMA and arg1 == "foreign_keys" and arg2 == "1":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    dbapi.set_authorizer(authorizer)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        _migration_module().upgrade_listserv_catalog(engine)
    assert "COMMIT" not in transactions
    assert _snapshot(engine) == before
    with engine.connect() as connection:
        assert connection.connection.driver_connection is not dbapi
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA foreign_key_list('listserv_messages')").all() == []
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM listserv_list WHERE list_id IN (?, ?)",
            (_MESSAGE_LIST_ID, _PAGE_LIST_ID),
        ).scalar_one() == 0


def test_cleanup_failure_is_reported_when_original_exception_has_no_add_note(
    sqlite_env, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    class OriginalError(RuntimeError):
        @property
        def add_note(self):
            raise AttributeError("notes unavailable")

    _create_legacy_database(sqlite_env)
    engine = get_engine()
    _initialize_catalog(engine)
    before = _snapshot(engine)
    dbapi = _set_pooled_pragmas(engine, 0, 1)
    migration = _migration_module()

    def authorizer(action, arg1, arg2, _database, _source):
        if (action == sqlite3.SQLITE_PRAGMA and arg1 == "legacy_alter_table" and arg2 == "0"):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def fail_rebuild(_cursor, _staging_name, _table_name):
        raise OriginalError("original rebuild failure")

    dbapi.set_authorizer(authorizer)
    monkeypatch.setattr(migration, "_rename_staging_table", fail_rebuild)
    with pytest.raises(OriginalError, match="original rebuild failure"):
        migration.upgrade_listserv_catalog(engine)
    assert "SQLite connection cleanup failed" in caplog.text
    assert "not authorized" in caplog.text
    assert _snapshot(engine) == before
    with engine.connect() as connection:
        assert connection.connection.driver_connection is not dbapi
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar_one() == 0


@pytest.mark.parametrize(
    ("fault", "deny_restore"),
    [("after_commit", True), ("after_commit", False), ("before_commit", False),
     ("sqlite_commit_error", False), ("sqlite_error_after_commit", True),
     ("interrupt_after_rollback", True)],
)
def test_commit_interruption_reports_durable_state_and_preserves_errors(
    sqlite_env, fault: str, deny_restore: bool
) -> None:
    from sqlalchemy import create_engine

    class FaultCursor(sqlite3.Cursor):
        def execute(self, sql, parameters=()):
            if sql == "COMMIT" and self.connection.commit_fault:
                if self.connection.commit_fault == "before_commit":
                    raise KeyboardInterrupt("injected before commit")
                if self.connection.commit_fault == "sqlite_commit_error":
                    super().execute("ROLLBACK")
                    return super().execute("COMMIT")
                if self.connection.commit_fault == "interrupt_after_rollback":
                    super().execute("ROLLBACK")
                    self.connection.commit_executed = True
                    raise KeyboardInterrupt("injected interrupt after rollback")
                super().execute(sql, parameters)
                self.connection.commit_executed = True
                if self.connection.commit_fault == "sqlite_error_after_commit":
                    raise sqlite3.OperationalError("injected SQLite error after commit")
                raise KeyboardInterrupt("injected after commit")
            return super().execute(sql, parameters)

    class FaultConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.commit_fault = None
            self.commit_executed = False

        def cursor(self, *args, **kwargs):
            kwargs.setdefault("factory", FaultCursor)
            return super().cursor(*args, **kwargs)

    _create_legacy_database(sqlite_env)
    engine = create_engine(
        f"sqlite+pysqlite:///{sqlite_env}",
        connect_args={"factory": FaultConnection, "check_same_thread": False},
    )

    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    event.listen(engine, "connect", enable_foreign_keys)
    _initialize_catalog(engine)
    views = _add_legacy_views(engine)
    before = _snapshot(engine)
    dbapi = _set_pooled_pragmas(engine, 0, 1)
    dbapi.commit_fault = fault

    def authorizer(action, arg1, arg2, _database, _source):
        if (deny_restore and dbapi.commit_executed and action == sqlite3.SQLITE_PRAGMA
                and arg1 == "foreign_keys" and arg2 == "1"):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    dbapi.set_authorizer(authorizer)
    migration = _migration_module()
    caught = None
    try:
        migration.upgrade_listserv_catalog(engine)
    except BaseException as error:  # noqa: BLE001 - test captures the real interrupt
        caught = error
    assert caught is not None

    if fault == "after_commit" and deny_restore:
        assert isinstance(caught, migration.ListservCatalogUpgradeCommittedError)
        assert isinstance(caught.__cause__, KeyboardInterrupt)
        assert "injected after commit" in str(caught.__cause__)
        assert "not authorized" in str(caught)
    elif fault == "sqlite_error_after_commit":
        assert isinstance(caught, migration.ListservCatalogUpgradeCommittedError)
        assert isinstance(caught.__cause__, sqlite3.OperationalError)
        assert "injected SQLite error after commit" in str(caught.__cause__)
        assert "not authorized" in str(caught)
    elif fault == "sqlite_commit_error":
        assert isinstance(caught, sqlite3.OperationalError)
        assert "no transaction is active" in str(caught)
    else:
        assert isinstance(caught, KeyboardInterrupt)
    assert _snapshot(engine) == before
    with engine.connect() as connection:
        assert (connection.connection.driver_connection is dbapi) == (not deny_restore)
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar_one() == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert _view_schema(connection) == views
        message_fks = connection.exec_driver_sql("PRAGMA foreign_key_list('listserv_messages')").all()
        assert any(row[2:5] == ("listserv_list", "list_id", "list_id") for row in message_fks) == (
            fault in {"after_commit", "sqlite_error_after_commit"}
        )
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM listserv_list WHERE list_id IN (?, ?)",
            (_MESSAGE_LIST_ID, _PAGE_LIST_ID),
        ).scalar_one() == (2 if fault in {"after_commit", "sqlite_error_after_commit"} else 0)
    engine.dispose()
