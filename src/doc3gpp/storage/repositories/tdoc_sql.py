from __future__ import annotations

from sqlalchemy import func, select

from doc3gpp.models.tdoc import TDoc
from doc3gpp.storage.db.models import TDocORM, MeetingORM
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
                existing.meeting_id = tdoc.meeting_id
                existing.url = tdoc.url
                existing.source = tdoc.source
                existing.type = tdoc.type
                existing.status = tdoc.status
                existing.reservation_date = tdoc.reservation_date
                existing.uploaded_date = tdoc.uploaded_date
                existing.cr_cat = tdoc.cr_cat
                existing.is_revision_of = tdoc.is_revision_of
                existing.revised_to = tdoc.revised_to
                existing.release = tdoc.release
                existing.spec = tdoc.spec
                existing.version = tdoc.version
                existing.related_wis = tdoc.related_wis
                existing.cr_num = tdoc.cr_num
                existing.cr_pack = tdoc.cr_pack
            else:
                session.add(
                    TDocORM(
                        tdoc_id=tdoc.tdoc_id,
                        title=tdoc.title,
                        meeting_id=tdoc.meeting_id,
                        url=tdoc.url,
                        source=tdoc.source,
                        type=tdoc.type,
                        status=tdoc.status,
                        reservation_date=tdoc.reservation_date,
                        uploaded_date=tdoc.uploaded_date,
                        cr_cat=tdoc.cr_cat,
                        is_revision_of=tdoc.is_revision_of,
                        revised_to=tdoc.revised_to,
                        release=tdoc.release,
                        spec=tdoc.spec,
                        version=tdoc.version,
                        related_wis=tdoc.related_wis,
                        cr_num=tdoc.cr_num,
                        cr_pack=tdoc.cr_pack,
                    )
                )
            session.commit()

    def list(
        self,
        limit: int = 20,
        tsg: str | None = None,
        meeting_like: str | None = None,
        year: int | None = None,
    ) -> list[TDoc]:
        """Return recent TDoc records ordered by creation timestamp.

        Optional filters:
        - `tsg`: filter TDoc IDs that start with the given TSG prefix.
        - `meeting_like`: SQL LIKE pattern to apply to the meeting name.
        - `year`: two-digit year embedded in the TDoc identifier.
        """
        with self._session_factory() as session:
            stmt = select(TDocORM)

            if tsg:
                stmt = stmt.where(TDocORM.tdoc_id.ilike(f"{tsg}%"))

            if meeting_like:
                # join meetings to filter by meeting name
                stmt = stmt.join(MeetingORM, TDocORM.meeting_id == MeetingORM.meeting_id).where(
                    MeetingORM.name.like(meeting_like)
                )

            if year is not None:
                stmt = stmt.where(func.substr(TDocORM.tdoc_id, 4, 2) == f"{year:02d}")

            stmt = stmt.order_by(TDocORM.created_at.desc()).limit(limit)
            rows = session.scalars(stmt).all()

        # To include meeting_name for display, load names for any referenced meeting_id
        meeting_map: dict[int, str] = {}
        meeting_ids = {r.meeting_id for r in rows if r.meeting_id}
        if meeting_ids:
            stmt_m = select(MeetingORM).where(MeetingORM.meeting_id.in_(meeting_ids))
            meetings = session.scalars(stmt_m).all()
            meeting_map = {m.meeting_id: m.name for m in meetings}

        return [
            TDoc(
                tdoc_id=row.tdoc_id,
                title=row.title,
                meeting_id=row.meeting_id,
                meeting_name=meeting_map.get(row.meeting_id) if row.meeting_id else None,
                url=row.url,
                source=row.source,
                type=row.type,
                status=row.status,
                reservation_date=row.reservation_date,
                uploaded_date=row.uploaded_date,
                cr_cat=row.cr_cat,
                is_revision_of=row.is_revision_of,
                revised_to=row.revised_to,
                release=row.release,
                spec=row.spec,
                version=row.version,
                related_wis=row.related_wis,
                cr_num=row.cr_num,
                cr_pack=row.cr_pack,
            )
            for row in rows
        ]
