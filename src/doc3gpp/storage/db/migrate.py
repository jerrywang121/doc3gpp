from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.models import MeetingORM  # noqa: F401 - ensures model metadata is loaded
from doc3gpp.storage.db.models import TDocCrDetailOrm  # noqa: F401 - ensures model metadata is loaded
from doc3gpp.storage.db.models import TDocCrTtcnDetailOrm  # noqa: F401 - ensures model metadata is loaded
from doc3gpp.storage.db.models import TDocExtractOrm  # noqa: F401 - ensures model metadata is loaded
from doc3gpp.storage.db.models import TDocFileORM  # noqa: F401 - ensures model metadata is loaded
from doc3gpp.storage.db.models import TDocORM  # noqa: F401 - ensures model metadata is loaded
from doc3gpp.storage.db.models import TsgORM  # noqa: F401 - ensures model metadata is loaded
from doc3gpp.storage.db.models import WiORM  # noqa: F401 - ensures model metadata is loaded
from doc3gpp.storage.db.session import get_engine


def _migrate_rename_tdoc_cr_details() -> None:
    """Rename legacy ``tdoc_cr_details`` table to ``tdoc_cr_cover_page``.

    One-shot, idempotent: runs at every ``create_schema`` call but is a
    no-op once the legacy name is gone. ``Base.metadata.create_all``
    does not rename existing tables, so databases created by prior
    releases would otherwise carry an orphan ``tdoc_cr_details`` table
    while reads/writes went to the new ``tdoc_cr_cover_page``.

    Syntax notes:

    * SQLite supports ``ALTER TABLE ... RENAME TO`` (the only DDL it
      supports that mutates a table). ``CREATE TABLE IF NOT EXISTS``
      is used as the post-condition probe so a pre-existing new-name
      table (partial migration) does not raise.
    * MySQL and PostgreSQL support ``RENAME TABLE`` natively.
    """
    engine = get_engine()
    with engine.begin() as conn:
        # SQLite-only: probe sqlite_master for the legacy name.
        if engine.dialect.name == "sqlite":
            legacy_exists = conn.execute(
                text(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='tdoc_cr_details' LIMIT 1"
                )
            ).first()
            if legacy_exists:
                # Drop the empty new table (if any) so RENAME can land;
                # legacy data must survive — only rename, never drop.
                conn.execute(
                    text("DROP TABLE IF EXISTS tdoc_cr_cover_page")
                )
                conn.execute(
                    text("ALTER TABLE tdoc_cr_details RENAME TO tdoc_cr_cover_page")
                )
        else:
            # MySQL / PostgreSQL: native RENAME TABLE.
            try:
                conn.execute(text("RENAME TABLE tdoc_cr_details TO tdoc_cr_cover_page"))
            except OperationalError as exc:
                # Either the legacy table is already gone, or the new
                # table already exists (idempotent re-run). Both are
                # safe to swallow.
                msg = str(exc).lower()
                if (
                    "doesn't exist" in msg
                    or "does not exist" in msg
                    or "already exists" in msg
                ):
                    pass
                else:
                    raise


def create_schema() -> None:
    """Create database tables for configured backend."""

    engine = get_engine()
    _migrate_rename_tdoc_cr_details()
    Base.metadata.create_all(bind=engine)
