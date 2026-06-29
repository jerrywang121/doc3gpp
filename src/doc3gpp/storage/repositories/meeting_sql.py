from __future__ import annotations

from sqlalchemy import select

from doc3gpp.models.meeting import Meeting
from doc3gpp.storage.db.models import MeetingORM
from doc3gpp.storage.db.session import get_session_factory


class SQLAlchemyMeetingRepository:
    """SQLAlchemy-backed implementation of MeetingRepository.

    This repository maps Meeting dataclass objects to the MeetingORM SQLAlchemy
    model and implements basic persistence operations used by the service layer.
    """

    def __init__(self) -> None:
        self._session_factory = get_session_factory()

    def upsert_many(self, meetings: list[Meeting]) -> int:
        """Upsert multiple meeting records in a single transaction."""
        with self._session_factory() as session:
            for item in meetings:
                session.merge(
                    MeetingORM(
                        meeting_id=item.meeting_id,
                        name=item.name,
                        title=item.title,
                        location=item.location,
                        start_date=item.start_date,
                        end_date=item.end_date,
                        ftp_url=item.ftp_url,
                        start_doc=item.start_doc,
                        end_doc=item.end_doc,
                        updated_at=item.updated_at,
                    )
                )
            session.commit()
        return len(meetings)

    def list(self, limit: int = 50) -> list[Meeting]:
        """List the most recent meeting records, ordered by start date."""
        with self._session_factory() as session:
            stmt = select(MeetingORM).order_by(MeetingORM.start_date.desc()).limit(limit)
            rows = session.scalars(stmt).all()

        return [
            Meeting(
                meeting_id=row.meeting_id,
                name=row.name,
                title=row.title,
                location=row.location,
                start_date=row.start_date,
                end_date=row.end_date,
                ftp_url=row.ftp_url,
                start_doc=row.start_doc,
                end_doc=row.end_doc,
                updated_at=row.updated_at,
            )
            for row in rows
        ]

    def get_by_id(self, meeting_id: int) -> Meeting | None:
        """Retrieve a single meeting by its numeric ID."""
        with self._session_factory() as session:
            row = session.get(MeetingORM, meeting_id)
            if row is None:
                return None

            return Meeting(
                meeting_id=row.meeting_id,
                name=row.name,
                title=row.title,
                location=row.location,
                start_date=row.start_date,
                end_date=row.end_date,
                ftp_url=row.ftp_url,
                start_doc=row.start_doc,
                end_doc=row.end_doc,
                updated_at=row.updated_at,
            )
