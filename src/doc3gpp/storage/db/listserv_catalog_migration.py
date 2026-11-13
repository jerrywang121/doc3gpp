"""Transactional upgrades for existing LISTSERV catalog relationships."""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from doc3gpp.storage.db.listserv_catalog import _backfill_legacy_listserv_ids
from doc3gpp.storage.db.models import ListservArchivePageORM, ListservMessageORM

_LIST_TABLE = "listserv_list"
_CHILD_TABLES = {
    "listserv_messages": (ListservMessageORM.__table__, "list_id"),
    "listserv_archive_pages": (ListservArchivePageORM.__table__, "list_id"),
}
_STAGING_PREFIX = "__listserv_catalog_upgrade_"
_CREATE_TABLE_PREFIX = re.compile(
    r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<name>\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[^\s(]+)",
    re.IGNORECASE,
)
_NO_AUTOCOMMIT_ATTRIBUTE = object()


@dataclass(frozen=True, slots=True)
class _TableRebuildPlan:
    table_name: str
    staging_name: str
    create_sql: str
    columns: tuple[str, ...]
    schema_objects: tuple[str, ...]
    sequence_value: int | None


class ListservCatalogUpgradeCommittedError(RuntimeError):
    """The SQLite catalog upgrade committed, but connection cleanup failed."""


def upgrade_listserv_catalog(engine: Engine) -> None:
    """Add catalog foreign keys and backfill legacy IDs without data loss.

    SQLite upgrades use one dedicated connection and an explicit
    ``BEGIN IMMEDIATE`` transaction. Foreign-key enforcement is disabled only
    while rebuilding the parent message table, then restored to its original
    per-connection setting. Existing non-SQLite schemas are inspected, but a
    legacy table missing a catalog FK is rejected rather than being reported as
    upgraded; no cross-dialect migration or race-safety guarantee is implied.
    """
    if engine.dialect.name == "sqlite":
        _upgrade_sqlite(engine)
        return

    _verify_non_sqlite_schema(engine)


def _verify_non_sqlite_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    missing: list[str] = []
    for table_name, (_, column_name) in _CHILD_TABLES.items():
        if table_name not in existing_tables:
            continue
        if not any(
            foreign_key.get("referred_table") == _LIST_TABLE
            and foreign_key.get("constrained_columns") == [column_name]
            and foreign_key.get("referred_columns") == ["list_id"]
            for foreign_key in inspector.get_foreign_keys(table_name)
        ):
            missing.append(f"{table_name}.{column_name}")

    if missing:
        raise RuntimeError(
            "upgrading legacy LISTSERV catalog relationships is only supported "
            "for SQLite; existing non-SQLite tables are missing foreign keys: "
            + ", ".join(missing)
        )


def _catalog_foreign_keys_are_current(cursor: sqlite3.Cursor) -> bool:
    """Return whether every existing LISTSERV child FK is already upgraded."""
    existing_child_tables = [
        table_name
        for table_name in _CHILD_TABLES
        if _table_exists(cursor, table_name)
    ]
    if not existing_child_tables:
        return True
    if not _table_exists(cursor, _LIST_TABLE):
        return False
    if not all(
        _has_catalog_foreign_key(_foreign_keys(cursor, table_name), table_name)
        for table_name in existing_child_tables
    ):
        return False

    _validate_list_table(cursor)
    return True


def _upgrade_sqlite(engine: Engine) -> None:
    pooled_connection = engine.raw_connection()
    dbapi_connection = pooled_connection.driver_connection
    original_autocommit = _NO_AUTOCOMMIT_ATTRIBUTE
    cursor: sqlite3.Cursor | None = None
    original_foreign_keys: int | None = None
    original_legacy_alter: int | None = None
    transaction_started = False
    commit_started = False
    rebuild_needed = False
    committed = False
    primary_error: BaseException | None = None
    cleanup_errors: list[BaseException] = []

    def cleanup(action) -> None:
        try:
            action()
        except BaseException as error:  # noqa: BLE001 - cleanup must handle interruptions
            cleanup_errors.append(error)

    try:
        # Driver autocommit is required to change foreign_keys on Python 3.12+.
        original_autocommit = getattr(
            dbapi_connection, "autocommit", _NO_AUTOCOMMIT_ATTRIBUTE
        )
        if original_autocommit is not _NO_AUTOCOMMIT_ATTRIBUTE:
            dbapi_connection.autocommit = True

        cursor = dbapi_connection.cursor()
        # Current schemas need no write lock or migration work.
        if _catalog_foreign_keys_are_current(cursor):
            return

        foreign_key_row = cursor.execute("PRAGMA foreign_keys").fetchone()
        if foreign_key_row is None:
            raise RuntimeError("could not read SQLite foreign_keys setting")
        original_foreign_keys = int(foreign_key_row[0])
        legacy_row = cursor.execute("PRAGMA legacy_alter_table").fetchone()
        if legacy_row is None:
            raise RuntimeError("could not read SQLite legacy_alter_table setting")
        original_legacy_alter = int(legacy_row[0])
        # Foreign keys can only be toggled outside the transaction. Preflight
        # restoration before any migration work, then disable for the rebuild.
        _set_foreign_keys(cursor, 0)
        _set_foreign_keys(cursor, original_foreign_keys)
        _set_foreign_keys(cursor, 0)

        # SQLAlchemy's logical transaction alone does not force pysqlite to
        # begin before DDL. Lock and roll back all schema and row changes together.
        cursor.execute("BEGIN IMMEDIATE")
        transaction_started = True
        # Record the locked pre-upgrade state to distinguish a committed
        # rebuild from SQLite automatically rolling back a failed COMMIT.
        rebuild_needed = not _catalog_foreign_keys_are_current(cursor)
        _upgrade_sqlite_transaction(cursor)
        # A final legacy verification must happen while rollback is possible.
        _restore_legacy_alter(cursor, original_legacy_alter)
        commit_started = True
        cursor.execute("COMMIT")
        committed = True
        transaction_started = False
    except BaseException as error:
        primary_error = error
        if transaction_started and cursor is not None:
            try:
                transaction_open = dbapi_connection.in_transaction
            except BaseException as state_error:  # noqa: BLE001 - preserve the original error
                cleanup_errors.append(state_error)
                cleanup(lambda: cursor.execute("ROLLBACK"))
            else:
                if transaction_open:
                    cleanup(lambda: cursor.execute("ROLLBACK"))
                elif commit_started and not committed and (
                    rebuild_needed or not isinstance(error, sqlite3.Error)
                ):
                    # The transaction ending is not itself proof of COMMIT:
                    # SQLite can also roll back. Verify the durable upgrade
                    # postconditions before reporting a committed outcome. For
                    # SQLite errors require a previously unupgraded schema too.
                    try:
                        if _catalog_foreign_keys_are_current(cursor):
                            _check_integrity(cursor, list(_CHILD_TABLES))
                            committed = True
                    except BaseException as state_error:  # noqa: BLE001 - preserve the original error
                        cleanup_errors.append(state_error)
        raise
    finally:
        if cursor is not None:
            if original_legacy_alter is not None and not committed:
                cleanup(lambda: _restore_legacy_alter(cursor, original_legacy_alter))
            if original_foreign_keys is not None:
                cleanup(lambda: _set_foreign_keys(cursor, original_foreign_keys))
            cleanup(cursor.close)
        if original_autocommit is not _NO_AUTOCOMMIT_ATTRIBUTE:
            def restore_autocommit() -> None:
                dbapi_connection.autocommit = original_autocommit
                if dbapi_connection.autocommit != original_autocommit:
                    raise RuntimeError("could not restore the original SQLite autocommit setting")
            cleanup(restore_autocommit)

        if cleanup_errors:
            # Never return a connection with uncertain transaction or PRAGMA state.
            # Invalidation closes the DBAPI connection, rolling back if needed.
            try:
                pooled_connection.invalidate(cleanup_errors[0])
            except BaseException as error:  # noqa: BLE001 - cleanup must handle interruptions
                cleanup_errors.append(error)
                cleanup(dbapi_connection.close)
                cleanup(pooled_connection.close)
        else:
            try:
                pooled_connection.close()
            except BaseException as error:  # noqa: BLE001 - cleanup must handle interruptions
                cleanup_errors.append(error)
                try:
                    pooled_connection.invalidate(error)
                except BaseException as invalidation_error:  # noqa: BLE001 - close unusable connection
                    cleanup_errors.append(invalidation_error)
                    cleanup(dbapi_connection.close)

        if cleanup_errors:
            if committed:
                raise ListservCatalogUpgradeCommittedError(
                    "SQLite LISTSERV catalog upgrade committed, but connection "
                    "cleanup failed; the connection was invalidated: "
                    + "; ".join(str(error) for error in cleanup_errors)
                ) from (primary_error if primary_error is not None else cleanup_errors[0])
            if primary_error is None:
                raise cleanup_errors[0]
            message = (
                "SQLite connection cleanup failed; connection invalidated: "
                + "; ".join(str(error) for error in cleanup_errors)
            )
            if hasattr(primary_error, "add_note"):
                primary_error.add_note(message)
            else:
                logging.getLogger(__name__).warning("%s", message)


def _set_foreign_keys(cursor: sqlite3.Cursor, value: int) -> None:
    cursor.execute(f"PRAGMA foreign_keys={value}")
    row = cursor.execute("PRAGMA foreign_keys").fetchone()
    if row is None or int(row[0]) != value:
        raise RuntimeError(f"could not set SQLite foreign_keys to {value}")


def _restore_legacy_alter(cursor: sqlite3.Cursor, original: int) -> None:
    row = cursor.execute("PRAGMA legacy_alter_table").fetchone()
    if row is None:
        raise RuntimeError("could not read SQLite legacy_alter_table setting")
    if int(row[0]) != original:
        cursor.execute(f"PRAGMA legacy_alter_table={original}")
    row = cursor.execute("PRAGMA legacy_alter_table").fetchone()
    if row is None or int(row[0]) != original:
        raise RuntimeError("could not restore the original SQLite legacy_alter_table setting")


def _upgrade_sqlite_transaction(cursor: sqlite3.Cursor) -> None:
    existing_child_tables = [
        table_name
        for table_name in _CHILD_TABLES
        if _table_exists(cursor, table_name)
    ]
    if not existing_child_tables:
        return
    if not _table_exists(cursor, _LIST_TABLE):
        raise RuntimeError(
            "cannot upgrade legacy LISTSERV tables before listserv_list exists"
        )
    _validate_list_table(cursor)

    plans: list[_TableRebuildPlan] = []
    for table_name in existing_child_tables:
        foreign_keys = _foreign_keys(cursor, table_name)
        if _has_catalog_foreign_key(foreign_keys, table_name):
            continue
        _reject_conflicting_list_id_foreign_keys(foreign_keys, table_name)
        plans.append(_prepare_rebuild(cursor, table_name))

    legacy_ids: list[str] = []
    for table_name in existing_child_tables:
        if "list_id" not in _table_columns(cursor, table_name):
            raise RuntimeError(
                f"unsupported legacy LISTSERV schema for {table_name}: "
                "missing list_id column"
            )
        rows = cursor.execute(
            f"SELECT DISTINCT list_id FROM {_quote_identifier(table_name)} "
            "WHERE list_id IS NOT NULL"
        ).fetchall()
        for row in rows:
            list_id = row[0]
            if not isinstance(list_id, str):
                raise TypeError(
                    f"unsupported non-text list ID in legacy {table_name}: "
                    f"{list_id!r}"
                )
            legacy_ids.append(list_id)

    # This insert is conflict-ignore only: seeded metadata and any existing
    # catalog edits remain untouched. BEGIN IMMEDIATE serializes SQLite writers.
    _backfill_legacy_listserv_ids(cursor, legacy_ids)

    if plans:
        # SQLite normally validates every view during ALTER TABLE. The old
        # table is temporarily absent while its replacement is renamed, so
        # keep legacy rename semantics only for this rebuild window.
        legacy_row = cursor.execute("PRAGMA legacy_alter_table").fetchone()
        if legacy_row is None:
            raise RuntimeError("could not read SQLite legacy_alter_table setting")
        original_legacy_alter = int(legacy_row[0])
        try:
            cursor.execute("PRAGMA legacy_alter_table=ON")
            if int(cursor.execute("PRAGMA legacy_alter_table").fetchone()[0]) != 1:
                raise RuntimeError("could not enable SQLite legacy_alter_table for table rebuild")
            for plan in plans:
                _apply_rebuild(cursor, plan)
        except BaseException as error:
            try:
                _restore_legacy_alter(cursor, original_legacy_alter)
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve the original error
                # Preserve the migration error; the outer wrapper verifies the
                # setting again and invalidates the connection if necessary.
                message = f"SQLite legacy_alter_table restore failed: {cleanup_error}"
                if hasattr(error, "add_note"):
                    error.add_note(message)
                else:
                    logging.getLogger(__name__).warning("%s", message)
            raise
        else:
            _restore_legacy_alter(cursor, original_legacy_alter)

    for table_name in existing_child_tables:
        if not _has_catalog_foreign_key(_foreign_keys(cursor, table_name), table_name):
            raise RuntimeError(
                f"SQLite LISTSERV catalog upgrade did not establish the "
                f"{table_name}.list_id foreign key"
            )

    _check_integrity(cursor, existing_child_tables)


def _validate_list_table(cursor: sqlite3.Cursor) -> None:
    rows = cursor.execute(
        f"PRAGMA table_info({_quote_identifier(_LIST_TABLE)})"
    ).fetchall()
    by_name = {row[1]: row for row in rows}
    missing = {"list_id", "tsg", "description"} - set(by_name)
    if missing:
        raise RuntimeError(
            "unsupported LISTSERV catalog table; missing columns: "
            + ", ".join(sorted(missing))
        )
    if int(by_name["list_id"][5]) == 0:
        raise RuntimeError(
            "unsupported LISTSERV catalog table; list_id must be a primary key"
        )


def _prepare_rebuild(
    cursor: sqlite3.Cursor, table_name: str
) -> _TableRebuildPlan:
    orm_table, _column_name = _CHILD_TABLES[table_name]
    columns = _table_columns(cursor, table_name)
    expected_columns = set(orm_table.columns.keys())
    actual_columns = set(columns)
    unexpected = sorted(actual_columns - expected_columns)
    missing = sorted(expected_columns - actual_columns)
    if unexpected or missing:
        differences = []
        if unexpected:
            differences.append("unexpected columns " + ", ".join(unexpected))
        if missing:
            differences.append("missing columns " + ", ".join(missing))
        raise RuntimeError(
            f"unsupported legacy LISTSERV schema for {table_name}: "
            + "; ".join(differences)
        )

    staging_name = _STAGING_PREFIX + table_name.removeprefix("listserv_")
    if cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? COLLATE NOCASE "
        "UNION ALL SELECT 1 FROM sqlite_temp_master WHERE name = ? COLLATE NOCASE LIMIT 1",
        (staging_name, staging_name),
    ).fetchone():
        raise RuntimeError(
            f"cannot upgrade {table_name}: staging name {staging_name!r} "
            "already exists in main or TEMP schema"
        )

    create_sql_row = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    if create_sql_row is None or not create_sql_row[0]:
        raise RuntimeError(
            f"cannot safely rebuild legacy LISTSERV table {table_name}: "
            "CREATE TABLE SQL is unavailable"
        )
    create_sql = _create_staging_table_sql(
        str(create_sql_row[0]), table_name, staging_name
    )
    schema_objects = tuple(
        row[0]
        for row in cursor.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE tbl_name = ? AND type IN ('index', 'trigger') "
            "AND sql IS NOT NULL ORDER BY type, name",
            (table_name,),
        ).fetchall()
    )
    sequence_value = _read_sequence(cursor, table_name, str(create_sql_row[0]))
    return _TableRebuildPlan(
        table_name=table_name,
        staging_name=staging_name,
        create_sql=create_sql,
        columns=columns,
        schema_objects=schema_objects,
        sequence_value=sequence_value,
    )


def _apply_rebuild(cursor: sqlite3.Cursor, plan: _TableRebuildPlan) -> None:
    cursor.execute(plan.create_sql)
    quoted_columns = ", ".join(_quote_identifier(column) for column in plan.columns)
    cursor.execute(
        f"INSERT INTO {_quote_identifier(plan.staging_name)} ({quoted_columns}) "
        f"SELECT {quoted_columns} FROM {_quote_identifier(plan.table_name)}"
    )
    cursor.execute(f"DROP TABLE {_quote_identifier(plan.table_name)}")
    _rename_staging_table(cursor, plan.staging_name, plan.table_name)

    for statement in plan.schema_objects:
        cursor.execute(statement)
    _restore_sequence(cursor, plan.table_name, plan.sequence_value)


def _rename_staging_table(
    cursor: sqlite3.Cursor, staging_name: str, table_name: str
) -> None:
    """Rename a replacement table after its legacy table has been dropped."""
    cursor.execute(
        f"ALTER TABLE {_quote_identifier(staging_name)} "
        f"RENAME TO {_quote_identifier(table_name)}"
    )


def _create_staging_table_sql(
    create_sql: str, table_name: str, staging_name: str
) -> str:
    match = _CREATE_TABLE_PREFIX.match(create_sql)
    if match is None or _unquote_identifier(match.group("name")).casefold() != table_name:
        raise RuntimeError(
            f"unsupported CREATE TABLE SQL for legacy LISTSERV table {table_name}"
        )

    prefix = create_sql[: match.start("name")]
    prefix = re.sub(
        r"\s+IF\s+NOT\s+EXISTS\s*$", "", prefix, flags=re.IGNORECASE
    )
    rewritten = (
        prefix.rstrip()
        + " "
        + _quote_identifier(staging_name)
        + create_sql[match.end("name") :]
    )
    opening = _find_opening_parenthesis(
        rewritten, len(prefix.rstrip()) + 1 + len(_quote_identifier(staging_name))
    )
    closing = _find_matching_parenthesis(rewritten, opening)
    constraint = (
        f"FOREIGN KEY ({_quote_identifier('list_id')}) "
        f"REFERENCES {_quote_identifier(_LIST_TABLE)} "
        f"({_quote_identifier('list_id')})"
    )
    return rewritten[:closing] + ", " + constraint + rewritten[closing:]


def _find_opening_parenthesis(sql: str, start: int) -> int:
    index = start
    while index < len(sql):
        character = sql[index]
        if character in ("'", '"', "`"):
            index = _skip_quoted(sql, index, character)
            continue
        if character == "[":
            index = _skip_quoted(sql, index, "]")
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline == -1 else newline + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end == -1:
                raise RuntimeError("unterminated comment in legacy CREATE TABLE SQL")
            index = end + 2
            continue
        if character == "(":
            return index
        index += 1
    raise RuntimeError("unsupported legacy CREATE TABLE SQL: table body is missing")


def _find_matching_parenthesis(sql: str, opening: int) -> int:
    depth = 0
    index = opening
    while index < len(sql):
        character = sql[index]
        if character in ("'", '"', "`"):
            index = _skip_quoted(sql, index, character)
            continue
        if character == "[":
            index = _skip_quoted(sql, index, "]")
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline == -1 else newline + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end == -1:
                raise RuntimeError("unterminated comment in legacy CREATE TABLE SQL")
            index = end + 2
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise RuntimeError("unsupported legacy CREATE TABLE SQL: table body is unbalanced")


def _skip_quoted(sql: str, opening: int, closing_quote: str) -> int:
    index = opening + 1
    while index < len(sql):
        if sql[index] == closing_quote:
            if (
                closing_quote != "]"
                and index + 1 < len(sql)
                and sql[index + 1] == closing_quote
            ):
                index += 2
                continue
            return index + 1
        index += 1
    raise RuntimeError("unterminated quoted value in legacy CREATE TABLE SQL")


def _unquote_identifier(identifier: str) -> str:
    if identifier.startswith('"') and identifier.endswith('"'):
        return identifier[1:-1].replace('""', '"')
    if identifier.startswith("`") and identifier.endswith("`"):
        return identifier[1:-1].replace("``", "`")
    if identifier.startswith("[") and identifier.endswith("]"):
        return identifier[1:-1]
    return identifier


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    return (
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
            (table_name,),
        ).fetchone()
        is not None
    )


def _table_columns(cursor: sqlite3.Cursor, table_name: str) -> tuple[str, ...]:
    rows = cursor.execute(
        f"PRAGMA table_info({_quote_identifier(table_name)})"
    ).fetchall()
    return tuple(str(row[1]) for row in rows)


def _foreign_keys(cursor: sqlite3.Cursor, table_name: str) -> list[tuple[object, ...]]:
    return cursor.execute(
        f"PRAGMA foreign_key_list({_quote_identifier(table_name)})"
    ).fetchall()


def _has_catalog_foreign_key(
    foreign_keys: list[tuple[object, ...]], table_name: str
) -> bool:
    column_name = _CHILD_TABLES[table_name][1]
    return any(
        str(row[2]).casefold() == _LIST_TABLE
        and str(row[3]).casefold() == column_name
        and str(row[4]).casefold() == "list_id"
        for row in foreign_keys
    )


def _reject_conflicting_list_id_foreign_keys(
    foreign_keys: list[tuple[object, ...]], table_name: str
) -> None:
    column_name = _CHILD_TABLES[table_name][1]
    conflicts = [row for row in foreign_keys if str(row[3]).casefold() == column_name]
    if conflicts:
        raise RuntimeError(
            f"unsupported legacy LISTSERV schema for {table_name}: "
            f"list_id already has a different foreign key {conflicts!r}"
        )


def _read_sequence(
    cursor: sqlite3.Cursor, table_name: str, create_sql: str
) -> int | None:
    if "AUTOINCREMENT" not in create_sql.upper() or not _table_exists(
        cursor, "sqlite_sequence"
    ):
        return None
    row = cursor.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = ?", (table_name,)
    ).fetchone()
    return int(row[0]) if row is not None else None


def _restore_sequence(
    cursor: sqlite3.Cursor, table_name: str, sequence_value: int | None
) -> None:
    if sequence_value is None:
        return
    row = cursor.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = ?", (table_name,)
    ).fetchone()
    if row is None:
        cursor.execute(
            "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
            (table_name, sequence_value),
        )
    elif int(row[0]) < sequence_value:
        cursor.execute(
            "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
            (sequence_value, table_name),
        )


def _check_integrity(
    cursor: sqlite3.Cursor, existing_child_tables: list[str]
) -> None:
    checked_tables = {_LIST_TABLE, "listserv_details", *existing_child_tables}
    for table_name in sorted(checked_tables):
        if not _table_exists(cursor, table_name):
            continue
        results = cursor.execute(
            f"PRAGMA integrity_check({_quote_identifier(table_name)})"
        ).fetchall()
        if results != [("ok",)]:
            raise RuntimeError(
                f"SQLite integrity check failed for {table_name}: {results[:5]!r}"
            )
        violations = cursor.execute(
            f"PRAGMA foreign_key_check({_quote_identifier(table_name)})"
        ).fetchall()
        if violations:
            raise RuntimeError(
                f"SQLite foreign key check failed for {table_name}: "
                f"{violations[:5]!r}"
            )


__all__ = ["ListservCatalogUpgradeCommittedError", "upgrade_listserv_catalog"]
