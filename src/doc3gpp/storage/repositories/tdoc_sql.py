from __future__ import annotations

from datetime import datetime, timezone

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
        """Save or update a single TDoc record in the database.

        Delegates to :meth:`upsert_many` to keep field-copy logic in one place.
        """
        self.upsert_many([tdoc])

    def upsert_many(self, tdocs: list[TDoc]) -> int:
        """Insert or update TDoc records in a single transaction.

        Existing rows (matched by ``tdoc_id``) are updated in place; new rows
        are inserted. ``updated_at`` is stamped on every write so callers can
        tell when a row was last refreshed (created rows start with NULL).
        Returns the number of input rows processed.

        A single ``SELECT ... IN (...)`` resolves all existing rows up front
        so the per-row branch is a dict lookup rather than a fresh query,
        and a single ``commit`` covers the whole batch — important for
        meetings with hundreds of TDocs where the per-row variant opened
        hundreds of transactions.
        """
        if not tdocs:
            return 0

        now = datetime.now(tz=timezone.utc)
        with self._session_factory() as session:
            ids = [tdoc.tdoc_id for tdoc in tdocs]
            existing_rows = session.scalars(
                select(TDocORM).where(TDocORM.tdoc_id.in_(ids))
            ).all()
            existing_by_id = {row.tdoc_id: row for row in existing_rows}

            for tdoc in tdocs:
                target = existing_by_id.get(tdoc.tdoc_id)
                is_new = target is None
                if is_new:
                    target = TDocORM(tdoc_id=tdoc.tdoc_id)
                    session.add(target)
                self._copy_fields(target, tdoc)
                # ``updated_at`` tracks the last write; left NULL on insert so
                # a stale-but-never-refreshed row is distinguishable from one
                # that was touched by a re-sync.
                target.updated_at = now

            session.commit()
        return len(tdocs)

    @staticmethod
    def _copy_fields(target: TDocORM, tdoc: TDoc) -> None:
        """Copy dataclass fields onto an ORM instance (existing or new)."""
        target.title = tdoc.title
        target.meeting_id = tdoc.meeting_id
        target.url = tdoc.url
        target.source = tdoc.source
        target.type = tdoc.type
        target.status = tdoc.status
        target.reservation_date = tdoc.reservation_date
        target.uploaded_date = tdoc.uploaded_date
        target.cr_cat = tdoc.cr_cat
        target.is_revision_of = tdoc.is_revision_of
        target.revised_to = tdoc.revised_to
        target.release = tdoc.release
        target.spec = tdoc.spec
        target.version = tdoc.version
        target.related_wis = tdoc.related_wis
        target.cr_num = tdoc.cr_num
        target.cr_pack = tdoc.cr_pack

    def list(
        self,
        limit: int = 20,
        tsg: str | None = None,
        meeting_like: str | None = None,
        year: int | None = None,
        source_like: str | None = None,
        spec_like: str | None = None,
        wi_like: str | None = None,
        title_like: str | None = None,
        cat_like: str | None = None,
        status_like: str | None = None,
        type_like: str | None = None,
    ) -> list[TDoc]:
        """Return recent TDoc records ordered by creation timestamp.

        Optional filters:
        - `tsg`: filter TDoc IDs that start with the given TSG prefix.
        - `meeting_like`: SQL LIKE pattern to apply to the meeting name.
        - `year`: two-digit year embedded in the TDoc identifier.
        - `source_like`: SQL LIKE pattern to apply to the document source field.
        - `spec_like`: SQL LIKE pattern to filter by technical specification (spec field).
        - `wi_like`: SQL LIKE pattern to filter by related work items (related_wis field).
        - `title_like`: SQL LIKE pattern to filter by document title.
        - `cat_like`: SQL LIKE pattern to filter by CR category.
        - `status_like`: SQL LIKE pattern to filter by document status.
        - `type_like`: SQL LIKE pattern to filter by document type.
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

            if source_like:
                stmt = stmt.where(TDocORM.source.like(source_like))

            if spec_like:
                stmt = stmt.where(TDocORM.spec.like(spec_like))

            if wi_like:
                stmt = stmt.where(TDocORM.related_wis.like(wi_like))

            if title_like:
                stmt = stmt.where(TDocORM.title.like(title_like))

            if cat_like:
                stmt = stmt.where(TDocORM.cr_cat.like(cat_like))

            if status_like:
                stmt = stmt.where(TDocORM.status.like(status_like))

            if type_like:
                stmt = stmt.where(TDocORM.type.like(type_like))

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
                updated_at=row.updated_at,
            )
            for row in rows
        ]