from __future__ import annotations

from sqlalchemy import select

from doc3gpp.models.tdoc import TDoc
from doc3gpp.storage.db.models import TDocORM
from doc3gpp.storage.db.session import get_session_factory


class SQLAlchemyTDocRepository:
    """SQLAlchemy-backed implementation of TDocRepository.

    This repository stores TDoc metadata observed in meeting FTP directories.
    """

    def __init__(self) -> None:
        self._session_factory = get_session_factory()

    def upsert(self, tdoc: TDoc) -> None:
        """Save or update a TDoc record in the database.

        Existing records are updated by TDoc ID, while new records are inserted.
        """
        with self._session_factory() as session:
            existing = session.scalar(select(TDocORM).where(TDocORM.tdoc_id == tdoc.tdoc_id))
            if existing:
                existing.title = tdoc.title
                existing.meeting = tdoc.meeting
                existing.url = tdoc.url
            else:
                session.add(
                    TDocORM(
                        tdoc_id=tdoc.tdoc_id,
                        title=tdoc.title,
                        meeting=tdoc.meeting,
                        url=tdoc.url,
                    )
                )
            session.commit()

    def list(self, limit: int = 20) -> list[TDoc]:
        """Return recent TDoc records ordered by creation timestamp."""
        with self._session_factory() as session:
            stmt = select(TDocORM).order_by(TDocORM.created_at.desc()).limit(limit)
            rows = session.scalars(stmt).all()

        return [
            TDoc(tdoc_id=row.tdoc_id, title=row.title, meeting=row.meeting, url=row.url) for row in rows
        ]
