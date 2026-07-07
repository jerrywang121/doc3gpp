from __future__ import annotations

from sqlalchemy import select

from doc3gpp.models.tdoc import TDoc, TDocWithMeeting
from doc3gpp.parsers.tdoc_parser import tdoc_id_year
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
        are inserted. Returns the number of input rows processed.

        A single ``SELECT ... IN (...)`` resolves all existing rows up front
        so the per-row branch is a dict lookup rather than a fresh query,
        and a single ``commit`` covers the whole batch — important for
        meetings with hundreds of TDocs where the per-row variant opened
        hundreds of transactions.
        """
        if not tdocs:
            return 0

        with self._session_factory() as session:
            ids = [tdoc.tdoc_id for tdoc in tdocs]
            existing_rows = session.scalars(
                select(TDocORM).where(TDocORM.tdoc_id.in_(ids))
            ).all()
            existing_by_id = {row.tdoc_id: row for row in existing_rows}

            for tdoc in tdocs:
                target = existing_by_id.get(tdoc.tdoc_id)
                if target is None:
                    target = TDocORM(tdoc_id=tdoc.tdoc_id)
                    session.add(target)
                self._copy_fields(target, tdoc)

            session.commit()
        return len(tdocs)

    @staticmethod
    def _copy_fields(target: TDocORM, tdoc: TDoc) -> None:
        """Copy dataclass fields onto an ORM instance (existing or new)."""
        target.title = tdoc.title
        target.meeting_id = tdoc.meeting_id
        target.ftp_url = tdoc.ftp_url
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
        """Return recent TDoc records ordered by descending ``tdoc_id``.

        ``tdoc_id`` encodes the source-year and sequence (e.g.
        ``R5s260001``), so a lexicographic descending order is a stable
        approximation of "newest first" within a single TSG. Pure
        persistence shape — no joined meeting metadata. Callers that
        need ``meeting_name`` should use :meth:`list_with_meeting`.

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
                # Decode the year in Python so the SQL doesn't depend on
                # CR_ID_RE's exact shape. Project just the id column for the
                # candidate scan; this is cheap on the indexed unique column.
                candidate_ids = session.scalars(select(TDocORM.tdoc_id)).all()
                matching_ids = [tid for tid in candidate_ids if tdoc_id_year(tid) == year]
                if not matching_ids:
                    return []
                stmt = stmt.where(TDocORM.tdoc_id.in_(matching_ids))

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

            stmt = stmt.order_by(TDocORM.tdoc_id.desc()).limit(limit)
            rows = session.scalars(stmt).all()

        return [_orm_to_domain(row) for row in rows]

    def list_tdoc_ids_for_meeting(self, meeting_id: int) -> list[str]:
        """Return the TDoc IDs currently stored for ``meeting_id``."""
        with self._session_factory() as session:
            stmt = select(TDocORM.tdoc_id).where(TDocORM.meeting_id == meeting_id)
            return list(session.scalars(stmt).all())

    def get_by_id(self, tdoc_id: str) -> TDoc | None:
        """Return a TDoc record by its canonical ``tdoc_id`` (PK lookup).

        Used by :class:`doc3gpp.services.tdoc_cr_service.TDocCrService` to
        validate that a requested id exists and to check ``type == "CR"``
        before triggering a download. Returns ``None`` when the row is
        absent so callers can distinguish "not found" from a real error.
        """
        with self._session_factory() as session:
            row = session.get(TDocORM, tdoc_id)
        if row is None:
            return None
        return _orm_to_domain(row)

    def list_with_meeting(
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
    ) -> list[TDocWithMeeting]:
        """Like :meth:`list` but wraps each row with its parent meeting's name.

        Performs an extra batched lookup against ``meetings`` to populate
        ``TDocWithMeeting.meeting_name``. Used by the CLI / export code paths.
        """
        tdocs = self.list(
            limit=limit,
            tsg=tsg,
            meeting_like=meeting_like,
            year=year,
            source_like=source_like,
            spec_like=spec_like,
            wi_like=wi_like,
            title_like=title_like,
            cat_like=cat_like,
            status_like=status_like,
            type_like=type_like,
        )
        if not tdocs:
            return []
        with self._session_factory() as session:
            meeting_ids = {tdoc.meeting_id for tdoc in tdocs if tdoc.meeting_id}
            if not meeting_ids:
                return [TDocWithMeeting(tdoc=tdoc, meeting_name=None) for tdoc in tdocs]
            meetings = session.scalars(
                select(MeetingORM).where(MeetingORM.meeting_id.in_(meeting_ids))
            ).all()
            meeting_map = {m.meeting_id: m.name for m in meetings}
        return [
            TDocWithMeeting(
                tdoc=tdoc,
                meeting_name=meeting_map.get(tdoc.meeting_id) if tdoc.meeting_id else None,
            )
            for tdoc in tdocs
        ]


def _orm_to_domain(row: TDocORM) -> TDoc:
    """Map an ORM row to a TDoc dataclass (no joined metadata)."""
    return TDoc(
        tdoc_id=row.tdoc_id,
        title=row.title,
        meeting_id=row.meeting_id,
        ftp_url=row.ftp_url,
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