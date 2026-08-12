from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, update

from doc3gpp.models.tsg import Tsg
from doc3gpp.storage.db.models import TsgORM
from doc3gpp.storage.db.session import get_session_factory


class SQLAlchemyTsgRepository:
    """SQLAlchemy-backed implementation of :class:`TsgRepository`.

    Maps :class:`Tsg` dataclass objects to the ``TsgORM`` SQLAlchemy model and
    implements the persistence operations used by the TSG service layer.
    """

    def __init__(self) -> None:
        self._session_factory = get_session_factory()

    def upsert_many(self, tsgs: list[Tsg]) -> int:
        """Insert or update multiple TSG records keyed by ``tsg_name``.

        Existing records (matched by ``tsg_name``, case-insensitive) are updated
        in place so callers can use this method to refresh descriptions or URLs.
        ``meeting_last_sync`` is intentionally preserved on update; it is
        managed exclusively by :meth:`update_meeting_last_sync`.
        """
        with self._session_factory() as session:
            for item in tsgs:
                stmt = select(TsgORM).where(
                    func.lower(TsgORM.tsg_name) == item.tsg_name.lower()
                )
                existing = session.scalar(stmt)
                if existing is not None:
                    existing.tsg_name = item.tsg_name
                    existing.short_name = item.short_name
                    existing.description = item.description
                    existing.url = item.url
                else:
                    session.add(
                        TsgORM(
                            tsg_name=item.tsg_name,
                            short_name=item.short_name,
                            description=item.description,
                            url=item.url,
                            meeting_last_sync=item.meeting_last_sync,
                        )
                    )
            session.commit()
        return len(tsgs)

    def list_all(self) -> list[Tsg]:
        """Return all TSG records ordered by ``tsg_name``."""
        with self._session_factory() as session:
            stmt = select(TsgORM).order_by(TsgORM.tsg_name)
            rows = session.scalars(stmt).all()
        return [_orm_to_domain(row) for row in rows]

    def get_by_short_name(self, short_name: str) -> Tsg | None:
        """Return a TSG by its short name, matching case-insensitively."""
        with self._session_factory() as session:
            stmt = select(TsgORM).where(
                func.lower(TsgORM.short_name) == short_name.lower()
            )
            row = session.scalar(stmt)
            if row is None:
                return None
            return _orm_to_domain(row)

    def get_by_tsg_name(self, tsg_name: str) -> Tsg | None:
        """Return a TSG by its full ``tsg_name``, matching case-insensitively."""
        with self._session_factory() as session:
            stmt = select(TsgORM).where(func.lower(TsgORM.tsg_name) == tsg_name.lower())
            row = session.scalar(stmt)
            if row is None:
                return None
            return _orm_to_domain(row)

    def count(self) -> int:
        """Return the number of stored TSG records."""
        with self._session_factory() as session:
            stmt = select(func.count()).select_from(TsgORM)
            return int(session.scalar(stmt) or 0)

    def update_meeting_last_sync(self, short_name: str, synced_at: datetime) -> bool:
        """Record when the meeting calendar was last synced for a TSG.

        Returns ``True`` when a matching row existed and was updated,
        ``False`` otherwise.
        """
        with self._session_factory() as session:
            stmt = (
                update(TsgORM)
                .where(func.lower(TsgORM.short_name) == short_name.lower())
                .values(meeting_last_sync=synced_at)
            )
            result = session.execute(stmt)
            session.commit()
        return int(result.rowcount or 0) > 0

    def update_spec_last_sync(self, short_name: str, synced_at: datetime) -> bool:
        """Record when the spec list was last synced for a TSG.

        Returns ``True`` when a matching row existed and was updated,
        ``False`` otherwise.
        """
        with self._session_factory() as session:
            stmt = (
                update(TsgORM)
                .where(func.lower(TsgORM.short_name) == short_name.lower())
                .values(spec_last_sync=synced_at)
            )
            result = session.execute(stmt)
            session.commit()
        return int(result.rowcount or 0) > 0

def _orm_to_domain(row: TsgORM) -> Tsg:
    """Map a TsgORM row into a Tsg dataclass."""
    return Tsg(
        tsg_name=row.tsg_name,
        short_name=row.short_name,
        description=row.description,
        url=row.url,
        meeting_last_sync=_as_utc(row.meeting_last_sync),
        spec_last_sync=_as_utc(row.spec_last_sync),
    )


def _as_utc(value: datetime | None) -> datetime | None:
    """Return ``value`` normalized to UTC, handling naive SQLite returns."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
