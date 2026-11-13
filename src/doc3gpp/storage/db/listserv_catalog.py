"""Insert canonical LISTSERV catalog and required TSG reference rows."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.sql.schema import Table

from doc3gpp.models.listserv_catalog import LISTSERV_CATALOG_SEEDS
from doc3gpp.models.tsg import Tsg
from doc3gpp.models.tsg_defaults import default_tsgs
from doc3gpp.storage.db.models import ListservListORM, TsgORM


def ensure_listserv_catalog(engine: Engine) -> None:
    """Ensure default TSG identities and reference LISTSERV rows exist.

    Existing TSG and catalog metadata is retained. A conflicting TSG name/code
    identity aborts initialization so catalog rows are never written against a
    different reference definition.
    """
    defaults = default_tsgs()
    with engine.begin() as connection:
        _ensure_default_tsgs(connection, defaults)
        _ensure_catalog_rows(connection)


def _ensure_default_tsgs(connection: Connection, defaults: tuple[Tsg, ...]) -> None:
    missing, conflicts = _default_tsg_status(connection, defaults)
    if conflicts:
        raise ValueError("conflicting TSG reference identity: " + "; ".join(conflicts))

    if missing:
        rows = [
            {
                "tsg_name": tsg.tsg_name,
                "short_name": tsg.short_name,
                "description": tsg.description,
                "url": tsg.url,
            }
            for tsg in missing
        ]
        _insert_if_missing(connection, TsgORM.__table__, rows)

    missing, conflicts = _default_tsg_status(connection, defaults)
    if conflicts:
        raise ValueError("conflicting TSG reference identity: " + "; ".join(conflicts))
    if missing:
        missing_codes = ", ".join(tsg.short_name for tsg in missing)
        raise RuntimeError(f"could not ensure required TSG references: {missing_codes}")


def _default_tsg_status(
    connection: Connection,
    defaults: tuple[Tsg, ...],
) -> tuple[list[Tsg], list[str]]:
    rows = connection.execute(
        select(TsgORM.short_name, TsgORM.tsg_name).order_by(TsgORM.short_name)
    ).all()
    by_short_name: dict[str, list[tuple[str, str]]] = {}
    by_tsg_name: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        by_short_name.setdefault(row[0].casefold(), []).append(row)
        by_tsg_name.setdefault(row[1].casefold(), []).append(row)

    missing: list[Tsg] = []
    conflicts: list[str] = []
    for expected in defaults:
        candidates = {
            short_name: (short_name, tsg_name)
            for short_name, tsg_name in (
                *by_short_name.get(expected.short_name.casefold(), ()),
                *by_tsg_name.get(expected.tsg_name.casefold(), ()),
            )
        }
        if not candidates:
            missing.append(expected)
            continue

        if len(candidates) != 1:
            found = ", ".join(
                f"{tsg_name!r} ({short_name})"
                for short_name, tsg_name in candidates.values()
            )
            conflicts.append(
                f"{expected.tsg_name!r} ({expected.short_name}) matches multiple rows: {found}"
            )
            continue

        existing_short_name, existing_tsg_name = next(iter(candidates.values()))
        if existing_short_name != expected.short_name:
            conflicts.append(
                f"{expected.tsg_name!r} ({expected.short_name}) conflicts with "
                f"{existing_tsg_name!r} ({existing_short_name})"
            )

    return missing, conflicts


def _ensure_catalog_rows(connection: Connection) -> None:
    available_tsgs = set(connection.scalars(select(TsgORM.short_name)).all())
    required_tsgs = {tsg for _, tsg, _ in LISTSERV_CATALOG_SEEDS}
    missing_tsgs = sorted(required_tsgs - available_tsgs)
    if missing_tsgs:
        raise ValueError(
            "LISTSERV catalog seeds refer to missing TSG short names: "
            + ", ".join(missing_tsgs)
        )

    rows = [
        {"list_id": list_id, "tsg": tsg, "description": description}
        for list_id, tsg, description in LISTSERV_CATALOG_SEEDS
    ]
    _insert_if_missing(connection, ListservListORM.__table__, rows)


def _insert_if_missing(
    connection: Connection,
    table: Table,
    rows: list[dict[str, object]],
) -> None:
    if not rows:
        return
    if connection.dialect.name == "sqlite":
        connection.execute(sqlite_insert(table).values(rows).on_conflict_do_nothing())
        return

    # These reference tables use single-column primary keys. Other SQLAlchemy
    # dialects do not share one portable conflict-ignore INSERT syntax, so
    # insert only keys not already present rather than updating existing rows.
    primary_key_columns = tuple(table.primary_key.columns)
    if len(primary_key_columns) != 1:
        raise ValueError(
            f"{table.name} insert-if-missing requires a single-column primary key"
        )
    primary_key = primary_key_columns[0]
    keys = {row[primary_key.name] for row in rows}
    existing_keys = set(
        connection.scalars(
            select(primary_key).where(primary_key.in_(keys))
        ).all()
    )
    missing_rows = [
        row for row in rows if row[primary_key.name] not in existing_keys
    ]
    if missing_rows:
        connection.execute(table.insert(), missing_rows)


def _backfill_legacy_listserv_ids(
    cursor: sqlite3.Cursor, list_ids: Iterable[str]
) -> None:
    """Add exact legacy child IDs without replacing existing catalog metadata.

    This SQLite-only helper is called inside the migration's ``BEGIN IMMEDIATE``
    transaction; callers on other dialects must use their own migration path.
    """
    unique_ids = list(dict.fromkeys(list_ids))
    if not unique_ids:
        return
    cursor.executemany(
        "INSERT INTO listserv_list (list_id, tsg, description) "
        "VALUES (?, NULL, NULL) ON CONFLICT(list_id) DO NOTHING",
        ((list_id,) for list_id in unique_ids),
    )


__all__ = ["ensure_listserv_catalog"]
