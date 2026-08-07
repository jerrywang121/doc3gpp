"""SQLAlchemy-backed implementation of :class:`WiRepository`."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from doc3gpp.models.wi import Wi
from doc3gpp.storage.db.models import WiORM
from doc3gpp.storage.db.session import get_session_factory
from doc3gpp.storage.repositories.rich_filters import apply_text_filter


class SQLAlchemyWiRepository:
    """SQLAlchemy implementation that stores WI rows in the ``wis`` table.

    Identity within the table is the composite of ``wi_id`` and
    ``tsg_short``. The same numeric WI identifier can appear under multiple
    TSG WGs on the upstream DynaReport pages, so upserts use the pair to
    refresh the acronym/release/name fields rather than relying on
    ``wi_id`` alone.
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
        update the acronym, release and name columns in place, while
        non-matches become new rows.

        Returns:
            The number of input rows that were written (insert or update).
        """
        if not wis:
            return 0

        with self._session_factory() as session:
            _persist(session, wis)
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
        """Return WI rows ordered by descending ``wi_id``.

        ``wi_id`` is a monotonically increasing DynaReport identifier, so
        descending ``wi_id`` is a stable approximation of "newest first".
        Optional filters:
        - ``tsg``: case-insensitive match against ``tsg_short``.
        - ``name_like``, ``acronym_like``, ``release_like``: rich-filter
          grammar applied to the corresponding text columns (``null`` /
          ``not-null`` / ``!pattern`` / plain LIKE).
        """
        with self._session_factory() as session:
            stmt = select(WiORM)

            if tsg:
                stmt = stmt.where(WiORM.tsg_short == tsg.upper())

            if name_like:
                stmt = apply_text_filter(stmt, WiORM.name, name_like)

            if acronym_like:
                stmt = apply_text_filter(stmt, WiORM.acronym, acronym_like)

            if release_like:
                stmt = apply_text_filter(stmt, WiORM.release, release_like)

            stmt = stmt.order_by(WiORM.wi_id.desc()).limit(limit)
            rows = session.scalars(stmt).all()

        return [
            Wi(
                wi_id=row.wi_id,
                acronym=row.acronym,
                release=row.release,
                name=row.name,
                tsg_short=row.tsg_short,
            )
            for row in rows
        ]


def _persist(session: Session, wis: list[Wi]) -> None:
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
        else:
            session.add(
                WiORM(
                    wi_id=item.wi_id,
                    acronym=item.acronym,
                    release=item.release,
                    name=item.name,
                    tsg_short=item.tsg_short,
                )
            )
