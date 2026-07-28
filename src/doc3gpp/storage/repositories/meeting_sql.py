from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import distinct, func, select, extract, update
from sqlalchemy.orm import Session, sessionmaker

from doc3gpp.models.meeting import Meeting
from doc3gpp.storage.db.models import MeetingORM
from doc3gpp.storage.db.session import get_session_factory


class SQLAlchemyMeetingRepository:
    """SQLAlchemy-backed implementation of MeetingRepository.

    This repository maps Meeting dataclass objects to the MeetingORM SQLAlchemy
    model and implements basic persistence operations used by the service layer.
    """

    def __init__(self, session_factory: sessionmaker | None = None) -> None:
        """Initialize the repository.

        Args:
            session_factory: Optional pre-built ``sessionmaker``. When omitted
                the repository falls back to ``get_session_factory`` so test
                fixtures can substitute an in-memory SQLite engine.
        """
        self._session_factory = session_factory or get_session_factory()

    def upsert_many(self, meetings: list[Meeting]) -> int:
        """Upsert multiple meeting records in a single transaction.

        Existing rows (matched by ``meeting_id``) are updated in place;
        non-matches become new rows. Returns the number of input rows
        processed.
        """
        if not meetings:
            return 0

        with self._session_factory() as session:
            _persist(session, meetings)
            session.commit()
        return len(meetings)

    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        tsg: str | None = None,
        name_like: str | None = None,
        location_like: str | None = None,
        year: int | None = None,
        tdoc_id: tuple[str, int] | None = None,
    ) -> list[Meeting]:
        """List the most recent meeting records, ordered by start date.

        Optional filters:
        - `tsg`: SQL ``LIKE`` pattern applied to the ``meetings.tsg`` FK
          (case-insensitive via upper-case canonical form, populated by
          ``meeting sync --tsg``). Use ``%`` / ``_`` wildcards; a plain
          value with no wildcards still matches exactly.
        - `name_like`: SQL LIKE pattern to apply to the `name` column
        - `location_like`: SQL LIKE pattern to apply to the `location` column
        - `year`: integer year to match the `end_date`
        - `tdoc_id`: ``(prefix, number)`` tuple; narrows to meetings whose
          ``start_doc`` / ``end_doc`` range brackets the TDoc. See
          :meth:`MeetingRepository.list` for the matching semantics.
        - `offset`: number of rows to skip before applying `limit` (pagination)
        """
        with self._session_factory() as session:
            stmt = select(MeetingORM)

            if tsg:
                # LIKE on meetings.tsg; stored upper-case by sync so callers may pass any case.
                stmt = stmt.where(MeetingORM.tsg.like(tsg.upper()))

            if name_like:
                stmt = stmt.where(MeetingORM.name.like(name_like))

            if location_like:
                stmt = stmt.where(MeetingORM.location.like(location_like))

            if year is not None:
                stmt = stmt.where(extract("year", MeetingORM.end_date) == year)

            if tdoc_id is not None:
                prefix, number = tdoc_id
                canonical_prefix = prefix.upper()
                # Prefix-only predicate in SQL so the numeric range
                # comparison can stay in Python (avoids dialect-specific
                # text→int CAST). UPPER makes the prefix match case-
                # insensitive on every dialect.
                stmt = stmt.where(
                    MeetingORM.start_doc.isnot(None),
                    func.upper(func.substr(MeetingORM.start_doc, 1, 3))
                    == canonical_prefix,
                )

            stmt = stmt.order_by(
                MeetingORM.start_date.desc(),
                MeetingORM.meeting_id.desc(),
            ).offset(offset).limit(limit)
            rows = session.scalars(stmt).all()

        if tdoc_id is not None:
            prefix, number = tdoc_id
            rows = [
                row for row in rows
                if _tdoc_id_in_range(row, prefix.upper(), number)
            ]

        return [_orm_to_domain(row) for row in rows]

    def get_by_id(self, meeting_id: int) -> Meeting | None:
        """Retrieve a single meeting by its numeric ID."""
        with self._session_factory() as session:
            row = session.get(MeetingORM, meeting_id)
            if row is None:
                return None
            return _orm_to_domain(row)

    def get_by_name(self, meeting_name: str) -> Meeting | None:
        """Retrieve a single meeting by its exact name."""
        with self._session_factory() as session:
            stmt = select(MeetingORM).where(MeetingORM.name == meeting_name)
            row = session.scalar(stmt)
            if row is None:
                return None
            return _orm_to_domain(row)

    def update_tdoc_list_last_sync(self, meeting_id: int, synced_at: datetime) -> bool:
        """Record when the TDoc list was last synced for a meeting.

        Returns ``True`` when a matching row existed and was updated,
        ``False`` otherwise.
        """
        with self._session_factory() as session:
            stmt = (
                update(MeetingORM)
                .where(MeetingORM.meeting_id == meeting_id)
                .values(tdoc_list_last_sync=synced_at)
            )
            result = session.execute(stmt)
            session.commit()
        return int(result.rowcount or 0) > 0

    def list_distinct_tsgs(self) -> list[str]:
        """Return distinct, non-null TSG short names stored in ``meetings.tsg``.

        Results are ordered alphabetically so iteration is deterministic.
        Rows with a ``NULL`` ``tsg`` are ignored.
        """
        with self._session_factory() as session:
            stmt = (
                select(distinct(MeetingORM.tsg))
                .where(MeetingORM.tsg.isnot(None))
                .order_by(MeetingORM.tsg)
            )
            rows = session.scalars(stmt).all()
        return [str(row) for row in rows]


def _orm_to_domain(row: MeetingORM) -> Meeting:
    """Map an ORM row into a Meeting dataclass."""
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
        tsg=row.tsg,
        tdoc_list_last_sync=_as_utc(row.tdoc_list_last_sync),
    )


def _as_utc(value: datetime | None) -> datetime | None:
    """Return ``value`` normalized to UTC, handling naive SQLite returns."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _tdoc_id_in_range(row: MeetingORM, prefix: str, number: int) -> bool:
    """Return True iff ``row`` brackets the TDoc identified by ``(prefix, number)``.

    ``prefix`` must already be upper-cased (the caller is responsible
    for canonicalisation). The stored prefix side is upper-cased here
    so a stored ``r5s...`` row still matches a ``R5S`` query.
    See :meth:`MeetingRepository.list` for the matching contract.
    Returns ``False`` (rather than raising) on malformed stored values
    so a stray scraper artifact never breaks a list query.

    Accepts 9-char (6-digit) and 10-char (7-digit) start/end doc shapes.
    """
    start_doc = row.start_doc
    if start_doc is None or len(start_doc) not in (9, 10) or start_doc[:3].upper() != prefix:
        return False
    try:
        start_num = int(start_doc[3:])
    except ValueError:
        return False
    if start_num > number:
        return False

    end_doc = row.end_doc
    if end_doc is None:
        return True
    if len(end_doc) not in (9, 10) or end_doc[:3].upper() != prefix:
        return False
    try:
        end_num = int(end_doc[3:])
    except ValueError:
        return False
    return end_num >= number


def _persist(session: Session, meetings: list[Meeting]) -> None:
    """Insert or refresh each meeting row in-place on the given session.

    Performs a single bulk ``SELECT`` keyed on ``meeting_id``, then issues
    INSERT/UPDATE for each item. Mirrors ``wi_sql._persist``.

    ``tsg`` is written as-is from the domain object; the service layer is
    responsible for stamping the canonical short name before calling
    ``upsert_many`` so callers can update the owning TSG on a re-sync.
    ``tdoc_list_last_sync`` is preserved on existing rows because it is
    managed exclusively by :meth:`update_tdoc_list_last_sync`.
    """
    ids = [item.meeting_id for item in meetings]
    existing_rows = session.scalars(select(MeetingORM).where(MeetingORM.meeting_id.in_(ids))).all()
    existing_by_id = {row.meeting_id: row for row in existing_rows}

    for item in meetings:
        existing = existing_by_id.get(item.meeting_id)
        if existing is not None:
            existing.name = item.name
            existing.title = item.title
            existing.location = item.location
            existing.start_date = item.start_date
            existing.end_date = item.end_date
            existing.ftp_url = item.ftp_url
            existing.start_doc = item.start_doc
            existing.end_doc = item.end_doc
            existing.tsg = item.tsg
        else:
            session.add(
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
                    tsg=item.tsg,
                    tdoc_list_last_sync=item.tdoc_list_last_sync,
                )
            )
