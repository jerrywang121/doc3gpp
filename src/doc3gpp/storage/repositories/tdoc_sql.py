from __future__ import annotations

from sqlalchemy import ColumnElement, Select, distinct, select

from doc3gpp.cli_filters import (
    DATE_FILTER_RE,
    is_not_null_token,
    is_null_token,
    split_not_like_prefix,
)
from doc3gpp.models.tdoc import TDoc, TDocWithMeeting
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
        offset: int = 0,
        tdoc_id: str | None = None,
        meeting_like: str | None = None,
        meeting_id: int | None = None,
        status: str | None = None,
        cr_cat: str | None = None,
        spec: str | None = None,
        wi: str | None = None,
        revision_of: str | None = None,
        revised_to: str | None = None,
        title: str | None = None,
        ftp_url: str | None = None,
        source: str | None = None,
        tdoc_type: str | None = None,
        uploaded_date: str | None = None,
        release: str | None = None,
        version: str | None = None,
        cr_num: str | None = None,
        cr_pack: str | None = None,
    ) -> list[TDoc]:
        """Return recent TDoc records ordered by descending ``tdoc_id``.

        ``tdoc_id`` encodes the source-year and sequence (e.g.
        ``R5s260001``), so a lexicographic descending order is a stable
        approximation of "newest first" within a single TSG. Pure
        persistence shape — no joined meeting metadata. Callers that
        need ``meeting_name`` should use :meth:`list_with_meeting`.

        Optional filters:
        - ``tdoc_id``: SQL ``LIKE`` pattern against ``tdocs.tdoc_id``
          (rich-filter grammar — ``null`` / ``not-null`` /
          ``!pattern`` / plain LIKE). The same parameter powers the
          ``--tdoc`` flag on both ``tdoc list`` and ``tdoc parse``.
        - ``meeting_like``: SQL LIKE pattern to apply to the meeting name.
        - ``meeting_id``: exact match on ``tdocs.meeting_id``. Combinable
          with ``meeting_like``; rows must satisfy both predicates.

        The remaining parameters (``status``, ``cr_cat``, ``spec``,
        ``wi``, ``revision_of``, ``revised_to``, ``title``, ``ftp_url``,
        ``source``, ``tdoc_type``, ``uploaded_date``, ``release``,
        ``version``, ``cr_num``, ``cr_pack``) accept the rich
        filter syntax described in :mod:`doc3gpp.cli_filters`:

        - the literal token ``null`` matches rows whose column is NULL;
        - ``not-null`` matches rows whose column is NOT NULL;
        - a leading ``!`` flips the comparison to ``NOT LIKE``; the
          ``!`` is consumed and the remainder is bound as the pattern
          (e.g. ``!%RAN5%`` → ``column NOT LIKE '%RAN5%'``);
        - any other value is treated as a SQL ``LIKE`` pattern;
        - ``uploaded_date`` additionally accepts ``"<op> 'YYYY-MM-DD'"``
          with ``<op>`` in ``=`` / ``!=`` / ``<`` / ``<=`` / ``>`` / ``>=``,
          producing a parameterised column comparison.

        Pagination:
        - ``offset``: number of rows to skip before applying ``limit``.
        """
        with self._session_factory() as session:
            stmt = select(TDocORM)

            if meeting_like:
                # join meetings to filter by meeting name
                stmt = stmt.join(MeetingORM, TDocORM.meeting_id == MeetingORM.meeting_id).where(
                    MeetingORM.name.like(meeting_like)
                )

            if meeting_id is not None:
                stmt = stmt.where(TDocORM.meeting_id == meeting_id)

            stmt = _apply_text_filter(stmt, TDocORM.tdoc_id, tdoc_id)
            stmt = _apply_text_filter(stmt, TDocORM.status, status)
            stmt = _apply_text_filter(stmt, TDocORM.cr_cat, cr_cat)
            stmt = _apply_text_filter(stmt, TDocORM.spec, spec)
            stmt = _apply_text_filter(stmt, TDocORM.related_wis, wi)
            stmt = _apply_text_filter(stmt, TDocORM.is_revision_of, revision_of)
            stmt = _apply_text_filter(stmt, TDocORM.revised_to, revised_to)
            stmt = _apply_text_filter(stmt, TDocORM.title, title)
            stmt = _apply_text_filter(stmt, TDocORM.ftp_url, ftp_url)
            stmt = _apply_text_filter(stmt, TDocORM.source, source)
            stmt = _apply_text_filter(stmt, TDocORM.type, tdoc_type)
            stmt = _apply_text_filter(stmt, TDocORM.release, release)
            stmt = _apply_text_filter(stmt, TDocORM.version, version)
            stmt = _apply_text_filter(stmt, TDocORM.cr_num, cr_num)
            stmt = _apply_text_filter(stmt, TDocORM.cr_pack, cr_pack)
            stmt = _apply_date_filter(stmt, TDocORM.uploaded_date, uploaded_date)

            stmt = stmt.order_by(TDocORM.tdoc_id.desc()).offset(offset).limit(limit)
            rows = session.scalars(stmt).all()

        return [_orm_to_domain(row) for row in rows]

    def list_tdoc_ids_for_meeting(self, meeting_id: int) -> list[str]:
        """Return the TDoc IDs currently stored for ``meeting_id``."""
        with self._session_factory() as session:
            stmt = select(TDocORM.tdoc_id).where(TDocORM.meeting_id == meeting_id)
            return list(session.scalars(stmt).all())

    def list_distinct_meeting_ids(self) -> list[int]:
        """Return the distinct, non-null meeting IDs stored in ``tdocs``."""
        with self._session_factory() as session:
            stmt = (
                select(distinct(TDocORM.meeting_id))
                .where(TDocORM.meeting_id.isnot(None))
                .order_by(TDocORM.meeting_id)
            )
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
        offset: int = 0,
        tdoc_id: str | None = None,
        meeting_like: str | None = None,
        meeting_id: int | None = None,
        status: str | None = None,
        cr_cat: str | None = None,
        spec: str | None = None,
        wi: str | None = None,
        revision_of: str | None = None,
        revised_to: str | None = None,
        title: str | None = None,
        ftp_url: str | None = None,
        source: str | None = None,
        tdoc_type: str | None = None,
        uploaded_date: str | None = None,
        release: str | None = None,
        version: str | None = None,
        cr_num: str | None = None,
        cr_pack: str | None = None,
    ) -> list[TDocWithMeeting]:
        """Like :meth:`list` but wraps each row with its parent meeting's name.

        Performs an extra batched lookup against ``meetings`` to populate
        ``TDocWithMeeting.meeting_name``. Used by the CLI / export code paths.
        Accepts the same rich filters and pagination as :meth:`list`.
        """
        tdocs = self.list(
            limit=limit,
            offset=offset,
            tdoc_id=tdoc_id,
            meeting_like=meeting_like,
            meeting_id=meeting_id,
            status=status,
            cr_cat=cr_cat,
            spec=spec,
            wi=wi,
            revision_of=revision_of,
            revised_to=revised_to,
            title=title,
            ftp_url=ftp_url,
            source=source,
            tdoc_type=tdoc_type,
            uploaded_date=uploaded_date,
            release=release,
            version=version,
            cr_num=cr_num,
            cr_pack=cr_pack,
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


def _apply_text_filter(
    stmt: Select, column: ColumnElement, value: str | None
) -> Select:
    """Filter by text column.

    ``None`` is a pass-through. ``null`` / ``not-null`` match the
    column's nullability. A leading ``!`` flips the comparison to
    ``NOT LIKE``; the ``!`` is consumed and the remainder is bound
    as the pattern. Any other value is bound as a ``LIKE`` pattern.
    """
    if value is None:
        return stmt
    if is_null_token(value):
        return stmt.where(column.is_(None))
    if is_not_null_token(value):
        return stmt.where(column.is_not(None))
    negated, pattern = split_not_like_prefix(value)
    if negated:
        return stmt.where(column.notlike(pattern))
    return stmt.where(column.like(pattern))


def _apply_date_filter(
    stmt: Select, column: ColumnElement, value: str | None
) -> Select:
    """Filter by date column: ``None`` → pass-through, ``null``/``not-null`` → nullability, else operator against ``'YYYY-MM-DD'``."""
    if value is None:
        return stmt
    if is_null_token(value):
        return stmt.where(column.is_(None))
    if is_not_null_token(value):
        return stmt.where(column.is_not(None))
    match = DATE_FILTER_RE.match(value)
    if match is None:
        raise ValueError(
            f"Invalid date filter {value!r}. Expected 'null', 'not-null', "
            f"or an expression like \">= 'YYYY-MM-DD'\" with one of "
            f"=, !=, <, <=, >, >=."
        )
    return stmt.where(column.op(match["op"])(match["date"]))


__all__ = ["SQLAlchemyTDocRepository", "_apply_text_filter", "_apply_date_filter", "DATE_FILTER_RE"]