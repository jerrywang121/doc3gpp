"""SQLAlchemy-backed implementation of :class:`WiRepository`."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from doc3gpp.models.wi import Wi
from doc3gpp.storage.db.models import WiORM
from doc3gpp.storage.db.session import get_session_factory


class SQLAlchemyWiRepository:
    """SQLAlchemy implementation that stores WI rows in the ``wis`` table.

    Identity within the table is the composite of ``wi_id`` and
    ``tsg_short``. The same numeric WI identifier can appear under multiple
    TSG WGs on the upstream DynaReport pages, so upserts use the pair to
    refresh the acronym/release/title/updated_at fields rather than relying
    on ``wi_id`` alone.
    """

    def __init__(self, session_factory: sessionmaker | None = None) -> None:
        """Initialize the repository.

        Args:
            session_factory: Optional pre-built ``sessionmaker``. When
                omitted the function falls back to
                :func:`doc3gpp.storage.db.session.get_session_factory`. The
                parameter is primarily used by unit tests that want to bind a
                repository to an in-memory SQLite engine.
        """
        self._session_factory = session_factory or get_session_factory()

    def upsert_many(self, wis: list[Wi]) -> int:
        """Insert or update multiple WI records.

        Each item is matched by the ``(wi_id, tsg_short)`` pair; matches
        update the acronym, release, name and ``updated_at`` columns in
        place, while non-matches become new rows. ``updated_at`` is stamped
        with the current UTC time on every write.

        Returns:
            The number of input rows that were written (insert or update).
        """
        if not wis:
            return 0

        now = datetime.now(tz=timezone.utc)
        with self._session_factory() as session:
            _persist(session, wis, now)
            session.commit()
        return len(wis)

    def list(
        self,
        limit: int = 20,
        tsg: str | None = None,
        name_like: str | None = None,
        acronym_like: str | None = None,
        release_like: str | None = None,
    ) -> list[Wi]:
        """Return WI rows ordered by most recently updated.

        Optional filters:
        - ``tsg``: case-insensitive match against ``tsg_short``.
        - ``name_like``, ``acronym_like``, ``release_like``: SQL ``LIKE``
          patterns applied to the corresponding text columns.
        """
        with self._session_factory() as session:
            stmt = select(WiORM)

            if tsg:
                stmt = stmt.where(WiORM.tsg_short == tsg.upper())

            if name_like:
                stmt = stmt.where(WiORM.name.like(name_like))

            if acronym_like:
                stmt = stmt.where(WiORM.acronym.like(acronym_like))

            if release_like:
                stmt = stmt.where(WiORM.release.like(release_like))

            stmt = stmt.order_by(
                WiORM.updated_at.desc().nullslast(),
                WiORM.wi_id.desc(),
            ).limit(limit)
            rows = session.scalars(stmt).all()

        return [
            Wi(
                wi_id=row.wi_id,
                acronym=row.acronym,
                release=row.release,
                name=row.name,
                tsg_short=row.tsg_short,
                updated_at=row.updated_at,
            )
            for row in rows
        ]


def _persist(session: Session, wis: list[Wi], updated_at: datetime) -> None:
    """Insert or refresh each row in-place on the given session."""
    for item in wis:
        stmt = select(WiORM).where(
            (WiORM.wi_id == item.wi_id) & (WiORM.tsg_short == item.tsg_short)
        )
        existing = session.scalar(stmt)
        if existing is not None:
            existing.acronym = item.acronym
            existing.release = item.release
            existing.name = item.name
            existing.updated_at = updated_at
        else:
            session.add(
                WiORM(
                    wi_id=item.wi_id,
                    acronym=item.acronym,
                    release=item.release,
                    name=item.name,
                    tsg_short=item.tsg_short,
                    updated_at=updated_at,
                )
            )
