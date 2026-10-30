"""SQLAlchemy repository for spec-document outer-ZIP inventories."""

from __future__ import annotations

import json
import sqlite3

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from doc3gpp.models.spec_doc_zip_inventory import (
    SpecDocInventorySchemaError,
    SpecDocZipInventory,
)
from doc3gpp.storage.db.models import SpecDocZipInventoryORM
from doc3gpp.storage.db.session import get_specdata_session_factory

# SQLite's historical default is 999 variables, including the spec_id bind.
_VERSION_BATCH_SIZE = 900


def _is_missing_inventory_table(exc: OperationalError) -> bool:
    return str(exc.orig).lower() in {
        "no such table: spec_doc_zip_inventory",
        "no such table: main.spec_doc_zip_inventory",
    }


def _orm_to_domain(row: SpecDocZipInventoryORM) -> SpecDocZipInventory:
    return SpecDocZipInventory(
        spec_id=row.spec_id,
        version=row.version,
        release=row.release,
        zip_files=tuple(json.loads(row.zip_files)),
    )


class SQLAlchemySpecDocZipInventoryRepository:
    """Persist inventories independently of the spec-document source ledger."""

    def __init__(self, session_factory: sessionmaker | None = None) -> None:
        self._session_factory = session_factory or get_specdata_session_factory()

    def get(self, spec_id: str, version: str) -> SpecDocZipInventory | None:
        try:
            with self._session_factory() as session:
                row = session.get(SpecDocZipInventoryORM, (spec_id, version))
                return _orm_to_domain(row) if row is not None else None
        except OperationalError as exc:
            if _is_missing_inventory_table(exc):
                return None
            raise

    def get_for_versions(self, spec_id: str, versions: list[str]) -> dict[str, SpecDocZipInventory]:
        if not versions:
            return {}
        try:
            result: dict[str, SpecDocZipInventory] = {}
            with self._session_factory() as session:
                connection = session.connection()
                batch_size = _VERSION_BATCH_SIZE
                if connection.dialect.name == "sqlite":
                    raw = connection.connection.driver_connection
                    if hasattr(raw, "getlimit"):
                        batch_size = min(
                            batch_size,
                            max(1, raw.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) - 1),
                        )
                start = 0
                while start < len(versions):
                    stmt = select(SpecDocZipInventoryORM).where(
                        SpecDocZipInventoryORM.spec_id == spec_id,
                        SpecDocZipInventoryORM.version.in_(versions[start : start + batch_size]),
                    )
                    try:
                        rows = session.scalars(stmt).all()
                    except OperationalError as exc:
                        # Python 3.10 does not expose SQLite getlimit. Retry only
                        # this specific bind-limit error with smaller batches.
                        if (
                            connection.dialect.name == "sqlite"
                            and str(exc.orig).lower() == "too many sql variables"
                            and batch_size > 1
                        ):
                            batch_size = max(1, batch_size // 2)
                            continue
                        raise
                    for row in rows:
                        result[row.version] = _orm_to_domain(row)
                    start += batch_size
            return result
        except OperationalError as exc:
            if _is_missing_inventory_table(exc):
                return {}
            raise

    def upsert(self, inventory: SpecDocZipInventory) -> None:
        """Atomically store an inventory on the supported SQLite backend."""
        try:
            with self._session_factory() as session:
                dialect = session.get_bind().dialect.name
                if dialect != "sqlite":
                    raise RuntimeError(
                        "Spec-document ZIP inventory upsert requires SQLite; "
                        f"got {dialect!r}."
                    )
                zip_files = json.dumps(
                    list(inventory.zip_files), ensure_ascii=False, separators=(",", ":")
                )
                stmt = sqlite_insert(SpecDocZipInventoryORM).values(
                    spec_id=inventory.spec_id,
                    version=inventory.version,
                    release=inventory.release,
                    zip_files=zip_files,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["spec_id", "version"],
                    set_={
                        "release": stmt.excluded.release,
                        "zip_files": stmt.excluded.zip_files,
                    },
                )
                session.execute(stmt)
                session.commit()
        except OperationalError as exc:
            if _is_missing_inventory_table(exc):
                raise SpecDocInventorySchemaError(
                    "Spec-document ZIP inventory table is missing; "
                    "run 'doc3gpp db init --scope specdata' to upgrade."
                ) from exc
            raise
