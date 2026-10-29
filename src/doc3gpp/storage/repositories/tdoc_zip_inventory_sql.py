"""SQLAlchemy repository for TDoc outer-ZIP file inventories."""

from __future__ import annotations

import json

from sqlalchemy.orm import sessionmaker

from doc3gpp.models.tdoc_identity import normalize_ftp_path
from doc3gpp.models.tdoc_zip_inventory import TDocZipInventory
from doc3gpp.storage.db.models import TDocZipInventoryOrm
from doc3gpp.storage.db.session import get_session_factory


class SQLAlchemyTDocZipInventoryRepository:
    """Persist inventories by normalized immutable ZIP URL."""

    def __init__(self, session_factory: sessionmaker | None = None) -> None:
        self._session_factory = session_factory or get_session_factory()

    def get_by_url(self, ftp_url: str) -> TDocZipInventory | None:
        """Return the inventory for a full or relative URL, if it is known."""
        normalized_url = normalize_ftp_path(ftp_url)
        if not normalized_url:
            return None

        with self._session_factory() as session:
            row = session.get(TDocZipInventoryOrm, normalized_url)
            if row is None:
                return None
            return _orm_to_domain(row)

    def upsert(self, inventory: TDocZipInventory) -> None:
        """Insert or replace a complete inventory for its normalized URL."""
        normalized_url = normalize_ftp_path(inventory.ftp_url)
        if not normalized_url:
            raise ValueError("TDocZipInventory requires a non-empty ftp_url")
        zip_files_json = json.dumps(
            list(inventory.zip_files), ensure_ascii=False, separators=(",", ":")
        )

        with self._session_factory() as session:
            row = session.get(TDocZipInventoryOrm, normalized_url)
            if row is None:
                row = TDocZipInventoryOrm(
                    ftp_url=normalized_url,
                    tdoc_id=inventory.tdoc_id,
                    zip_files=zip_files_json,
                )
                session.add(row)
            else:
                row.tdoc_id = inventory.tdoc_id
                row.zip_files = zip_files_json
            session.commit()


def _orm_to_domain(row: TDocZipInventoryOrm) -> TDocZipInventory:
    """Map a SQL row to the ORM-free inventory value object."""
    return TDocZipInventory(
        ftp_url=row.ftp_url,
        tdoc_id=row.tdoc_id,
        zip_files=tuple(json.loads(row.zip_files)),
    )
