"""SQLAlchemy-backed implementation of :class:`ListservRepository`."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from doc3gpp.cli_filters import (
    is_not_null_token,
    is_null_token,
    split_not_like_prefix,
)
from doc3gpp.models.listserv import (
    ArchivePage,
    DateInterval,
    DateSet,
    ListservAttachment,
    ListservDetail,
    ListservFilters,
    ListservList,
    ListservMessage,
    ListservRecord,
    normalize_list_id,
    normalize_tdoc_ids,
    union_coverage,
)
from doc3gpp.models.listserv_catalog import infer_listserv_tsg
from doc3gpp.repository.listserv import ListservRepository
from doc3gpp.storage.compression import compress_json, decompress_json
from doc3gpp.storage.db.models import (
    ListservArchivePageORM,
    ListservDetailORM,
    ListservListORM,
    ListservMessageORM,
    TsgORM,
)
from doc3gpp.storage.db.session import get_session_factory
from doc3gpp.storage.repositories.rich_filters import apply_text_filter

_GET_MANY_BATCH_SIZE = 250


class SQLAlchemyListservRepository(ListservRepository):
    """SQLAlchemy implementation of :class:`ListservRepository` on the main DB."""

    def __init__(self, session_factory: sessionmaker | None = None) -> None:
        self._session_factory = session_factory or get_session_factory()

    def commit_page(
        self,
        page: ArchivePage,
        messages: Sequence[ListservMessage],
        coverage: DateSet,
        next_urls: Sequence[str],
    ) -> None:
        """Atomically upsert page messages, union coverage, and persist pagination URLs."""
        with self._session_factory() as session:
            if messages:
                self._register_list_if_absent(session, page.list_id)
            self._upsert_messages(session, messages)
            self._upsert_coverage_and_next_urls(session, page, coverage, next_urls)
            session.commit()

    def _register_list_if_absent(self, session: Session, list_id: str) -> None:
        normalized_list_id = normalize_list_id(list_id)
        if session.get(ListservListORM, normalized_list_id) is not None:
            return

        candidate_tsg = infer_listserv_tsg(normalized_list_id)
        tsg = (
            candidate_tsg
            if candidate_tsg is not None
            and session.get(TsgORM, candidate_tsg) is not None
            else None
        )
        values = {
            "list_id": normalized_list_id,
            "tsg": tsg,
            "description": None,
        }
        dialect_name = session.get_bind().dialect.name
        if dialect_name == "sqlite":
            statement = (
                sqlite_insert(ListservListORM)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[ListservListORM.list_id]
                )
            )
            session.execute(statement)
        elif dialect_name == "postgresql":
            statement = (
                postgresql_insert(ListservListORM)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[ListservListORM.list_id]
                )
            )
            session.execute(statement)
        else:
            try:
                with session.begin_nested():
                    session.add(ListservListORM(**values))
                    session.flush()
            except IntegrityError:
                if session.get(ListservListORM, normalized_list_id) is None:
                    raise

    def _upsert_messages(
        self, session: Session, messages: Sequence[ListservMessage]
    ) -> None:
        if not messages:
            return

        # Collapse repeated URLs before comparing with persisted state. The final
        # occurrence is authoritative, so one page can advance a source revision
        # at most once and an intermediate value cannot create a false change.
        messages_by_url: dict[str, ListservMessage] = {}
        for message in messages:
            messages_by_url[message.url] = message

        existing_rows = session.scalars(
            select(ListservMessageORM).where(
                ListservMessageORM.url.in_(list(messages_by_url))
            )
        ).all()
        existing_by_url = {row.url: row for row in existing_rows}

        for msg in messages_by_url.values():
            local_dt, local_d, tz_offset, sort_ts = _decompose_message_datetime(
                msg.date, msg.date_precision
            )
            subj_tdocs_str = ",".join(msg.subject_tdocs)
            row = existing_by_url.get(msg.url)
            if row is None:
                row = ListservMessageORM(
                    url=msg.url,
                    list_id=msg.list_id,
                    subject=msg.subject,
                    sender=msg.sender,
                    local_datetime=local_dt,
                    local_date=local_d,
                    tz_offset_seconds=tz_offset,
                    date_precision=msg.date_precision,
                    sort_timestamp=sort_ts,
                    week=msg.week,
                    subject_tdocs=subj_tdocs_str,
                    source_revision=1,
                )
                session.add(row)
                existing_by_url[msg.url] = row
                continue

            changed = (
                row.list_id != msg.list_id
                or row.subject != msg.subject
                or row.sender != msg.sender
                or row.local_datetime != local_dt
                or row.local_date != local_d
                or row.tz_offset_seconds != tz_offset
                or row.date_precision != msg.date_precision
                or row.sort_timestamp != sort_ts
                or row.week != msg.week
                or row.subject_tdocs != subj_tdocs_str
            )
            if changed:
                row.list_id = msg.list_id
                row.subject = msg.subject
                row.sender = msg.sender
                row.local_datetime = local_dt
                row.local_date = local_d
                row.tz_offset_seconds = tz_offset
                row.date_precision = msg.date_precision
                row.sort_timestamp = sort_ts
                row.week = msg.week
                row.subject_tdocs = subj_tdocs_str
                row.source_revision = int(row.source_revision) + 1

    def _upsert_coverage_and_next_urls(
        self,
        session: Session,
        page: ArchivePage,
        coverage: DateSet,
        next_urls: Sequence[str],
    ) -> None:
        row = session.get(ListservArchivePageORM, (page.list_id, page.url))
        if row is None:
            merged_cov = coverage
            merged_next = _dedup_urls((), next_urls)
            row = ListservArchivePageORM(
                list_id=page.list_id,
                page_url=page.url,
                period=page.period,
                coverage=_encode_date_set(merged_cov),
                next_urls=json.dumps(list(merged_next)),
            )
            session.add(row)
        else:
            existing_cov = _decode_date_set(row.coverage)
            merged_cov = union_coverage(existing_cov, coverage)
            existing_next = _decode_next_urls(row.next_urls)
            merged_next = _dedup_urls(existing_next, next_urls)
            row.period = page.period
            row.coverage = _encode_date_set(merged_cov)
            row.next_urls = json.dumps(list(merged_next))

    def get_coverage(self, page: ArchivePage) -> DateSet:
        """Return the stored date coverage for ``page`` (empty set when unknown)."""
        with self._session_factory() as session:
            row = session.get(ListservArchivePageORM, (page.list_id, page.url))
            if row is None:
                return DateSet()
            return _decode_date_set(row.coverage)

    def get_next_urls(self, page: ArchivePage) -> tuple[str, ...]:
        """Return discovered pagination URLs stored for ``page``."""
        with self._session_factory() as session:
            row = session.get(ListservArchivePageORM, (page.list_id, page.url))
            if row is None:
                return ()
            return _decode_next_urls(row.next_urls)

    def save_detail(self, detail: ListservDetail) -> None:
        """Atomically persist parsed message detail and bump ``source_revision`` on change."""
        attachments_json = _encode_attachments(detail.attachments)
        tdocs_str = ",".join(detail.tdocs)
        compressed_content = compress_json(detail.content)
        parsed_at_utc = _ensure_utc_datetime(detail.parsed_at)

        with self._session_factory() as session:
            msg_row = session.get(ListservMessageORM, detail.url)
            existing = session.get(ListservDetailORM, detail.url)
            if existing is None:
                new_row = ListservDetailORM(
                    url=detail.url,
                    attachments=attachments_json,
                    tdocs=tdocs_str,
                    content=compressed_content,
                    content_truncated=detail.content_truncated,
                    parser_version=detail.parser_version,
                    parsed_at=parsed_at_utc,
                )
                session.add(new_row)
                if msg_row is not None:
                    msg_row.source_revision = int(msg_row.source_revision) + 1
            else:
                existing_content = decompress_json(existing.content)
                if not isinstance(existing_content, str):
                    existing_content = ""
                changed = (
                    existing.attachments != attachments_json
                    or existing.tdocs != tdocs_str
                    or existing_content != detail.content
                    or bool(existing.content_truncated) != bool(detail.content_truncated)
                    or existing.parser_version != detail.parser_version
                )
                existing.attachments = attachments_json
                existing.tdocs = tdocs_str
                existing.content = compressed_content
                existing.content_truncated = detail.content_truncated
                existing.parser_version = detail.parser_version
                existing.parsed_at = parsed_at_utc
                if changed and msg_row is not None:
                    msg_row.source_revision = int(msg_row.source_revision) + 1
            session.commit()

    def get(self, url: str) -> ListservRecord | None:
        """Return the stored message record for canonical detail ``url``, or ``None``."""
        with self._session_factory() as session:
            stmt = (
                select(ListservMessageORM, ListservDetailORM)
                .outerjoin(
                    ListservDetailORM,
                    ListservMessageORM.url == ListservDetailORM.url,
                )
                .where(ListservMessageORM.url == url)
            )
            row = session.execute(stmt).first()
            if row is None:
                return None
            msg_orm, detail_orm = row
            return _rows_to_record(msg_orm, detail_orm)

    def get_many(self, urls: Sequence[str]) -> dict[str, ListservRecord]:
        """Return stored message records keyed by canonical detail URL for ``urls``."""
        unique_urls = list(dict.fromkeys(urls))
        if not unique_urls:
            return {}
        result: dict[str, ListservRecord] = {}
        with self._session_factory() as session:
            for start in range(0, len(unique_urls), _GET_MANY_BATCH_SIZE):
                batch = unique_urls[start : start + _GET_MANY_BATCH_SIZE]
                stmt = (
                    select(ListservMessageORM, ListservDetailORM)
                    .outerjoin(
                        ListservDetailORM,
                        ListservMessageORM.url == ListservDetailORM.url,
                    )
                    .where(ListservMessageORM.url.in_(batch))
                )
                rows = session.execute(stmt).all()
                for msg_orm, detail_orm in rows:
                    result[msg_orm.url] = _rows_to_record(msg_orm, detail_orm)
        return result

    def revisions(self) -> dict[str, int]:
        """Return ``{url: source_revision}`` for every stored message (SQL-only)."""
        with self._session_factory() as session:
            rows = session.execute(
                select(ListservMessageORM.url, ListservMessageORM.source_revision)
            ).all()
        return {str(url): int(rev) for url, rev in rows}

    def has_list(self, list_id: str) -> bool:
        """Return whether ``list_id`` exists in the LISTSERV catalog."""
        normalized_list_id = normalize_list_id(list_id)
        with self._session_factory() as session:
            return session.get(ListservListORM, normalized_list_id) is not None

    def list_lists(self) -> list[ListservList]:
        """Return catalog entries ordered by list ID."""
        with self._session_factory() as session:
            stmt = select(ListservListORM).order_by(ListservListORM.list_id.asc())
            rows = session.scalars(stmt).all()
        return [
            ListservList(
                list_id=row.list_id,
                tsg=row.tsg,
                description=row.description,
            )
            for row in rows
        ]

    def list_ids(self) -> list[str]:
        """Return catalog list IDs in ascending order."""
        return [record.list_id for record in self.list_lists()]

    def select(self, filters: ListservFilters) -> list[ListservRecord]:
        """Return matching stored records ordered newest first with URL tie-break."""
        if filters.limit == 0:
            return []
        with self._session_factory() as session:
            stmt = select(ListservMessageORM, ListservDetailORM).outerjoin(
                ListservDetailORM,
                ListservMessageORM.url == ListservDetailORM.url,
            )
            stmt = _apply_listserv_filters(stmt, filters)
            stmt = (
                stmt.order_by(
                    ListservMessageORM.sort_timestamp.desc(),
                    ListservMessageORM.url.asc(),
                )
                .offset(filters.offset)
                .limit(filters.limit)
            )
            rows = session.execute(stmt).all()
            return [_rows_to_record(m, d) for m, d in rows]

    def iter_for_parse(
        self,
        list_id: str,
        dates: DateSet | None,
        force: bool,
    ) -> Iterator[ListservRecord]:
        """Iterate all matching records for ``list_id`` (independent of search pagination)."""
        norm_list_id = normalize_list_id(list_id)
        with self._session_factory() as session:
            stmt = (
                select(ListservMessageORM, ListservDetailORM)
                .outerjoin(
                    ListservDetailORM,
                    ListservMessageORM.url == ListservDetailORM.url,
                )
                .where(ListservMessageORM.list_id == norm_list_id)
            )
            if dates is not None:
                stmt = _apply_date_set_filter(stmt, dates)
            if not force:
                stmt = stmt.where(ListservDetailORM.url.is_(None))
            stmt = stmt.order_by(
                ListservMessageORM.sort_timestamp.desc(),
                ListservMessageORM.url.asc(),
            )
            rows = session.execute(stmt).all()
            records = [_rows_to_record(m, d) for m, d in rows]
        yield from records


def _decompose_message_datetime(
    dt: datetime, precision: str
) -> tuple[datetime, date, int | None, datetime]:
    """Extract ``(local_datetime, local_date, tz_offset_seconds, sort_timestamp)``."""
    if precision == "date":
        local_dt = datetime(dt.year, dt.month, dt.day, 0, 0, 0)
        return local_dt, local_dt.date(), None, local_dt

    offset = dt.utcoffset() if dt.tzinfo is not None else None
    local_dt = dt.replace(tzinfo=None)
    local_d = local_dt.date()
    if offset is None:
        return local_dt, local_d, None, local_dt
    offset_seconds = int(offset.total_seconds())
    utc_dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return local_dt, local_d, offset_seconds, utc_dt


def _reconstruct_message_datetime(
    local_dt: datetime, tz_offset_seconds: int | None, precision: str
) -> datetime:
    local_naive = local_dt.replace(tzinfo=None)
    if precision == "date" or tz_offset_seconds is None:
        return local_naive
    tz = timezone(timedelta(seconds=int(tz_offset_seconds)))
    return local_naive.replace(tzinfo=tz)


def _ensure_utc_datetime(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _rows_to_record(
    msg_orm: ListservMessageORM, detail_orm: ListservDetailORM | None
) -> ListservRecord:
    precision = "date" if msg_orm.date_precision == "date" else "datetime"
    dt = _reconstruct_message_datetime(
        msg_orm.local_datetime, msg_orm.tz_offset_seconds, precision
    )
    subject_tdocs = _split_tdocs(msg_orm.subject_tdocs)
    msg = ListservMessage(
        url=msg_orm.url,
        list_id=msg_orm.list_id,
        subject=msg_orm.subject,
        sender=msg_orm.sender,
        date=dt,
        date_precision=precision,  # type: ignore[arg-type]
        week=msg_orm.week,
        subject_tdocs=subject_tdocs,
    )
    if detail_orm is None:
        return ListservRecord(
            message=msg,
            detail=None,
            tdocs=subject_tdocs,
            source_revision=int(msg_orm.source_revision),
        )

    attachments = _decode_attachments(detail_orm.attachments)
    detail_tdocs = _split_tdocs(detail_orm.tdocs)
    raw_content = decompress_json(detail_orm.content)
    content = raw_content if isinstance(raw_content, str) else ""
    parsed_at = _ensure_utc_datetime(detail_orm.parsed_at)
    detail = ListservDetail(
        url=detail_orm.url,
        attachments=attachments,
        tdocs=detail_tdocs,
        content=content,
        content_truncated=bool(detail_orm.content_truncated),
        parser_version=detail_orm.parser_version,
        parsed_at=parsed_at,
    )
    combined_tdocs = normalize_tdoc_ids((*subject_tdocs, *detail_tdocs))
    return ListservRecord(
        message=msg,
        detail=detail,
        tdocs=combined_tdocs,
        source_revision=int(msg_orm.source_revision),
    )


def _split_tdocs(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return normalize_tdoc_ids(raw.split(","))


def _encode_attachments(attachments: Sequence[ListservAttachment]) -> str:
    return json.dumps(
        [{"name": att.name, "url": att.url} for att in attachments],
        ensure_ascii=False,
    )


def _decode_attachments(raw: str | None) -> tuple[ListservAttachment, ...]:
    if not raw:
        return ()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(items, list):
        return ()
    out: list[ListservAttachment] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("url"), str):
            out.append(ListservAttachment(name=item["name"], url=item["url"]))
    return tuple(out)


def _encode_date_set(dates: DateSet) -> str:
    payload = [
        [
            iv.start.isoformat() if iv.start is not None else None,
            iv.end.isoformat() if iv.end is not None else None,
        ]
        for iv in dates.intervals
    ]
    return json.dumps(payload)


def _decode_date_set(raw: str | None) -> DateSet:
    if not raw:
        return DateSet()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return DateSet()
    if not isinstance(items, list):
        return DateSet()
    intervals: list[DateInterval] = []
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        s_raw, e_raw = item
        s_val = date.fromisoformat(s_raw) if isinstance(s_raw, str) else None
        e_val = date.fromisoformat(e_raw) if isinstance(e_raw, str) else None
        intervals.append(DateInterval(start=s_val, end=e_val))
    return DateSet(tuple(intervals))


def _decode_next_urls(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(items, list):
        return ()
    return _dedup_urls((), [x for x in items if isinstance(x, str)])


def _dedup_urls(existing: Sequence[str], additions: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for url in (*existing, *additions):
        u = url.strip()
        if not u or u in seen:
            continue
        seen.add(u)
        result.append(u)
    return tuple(result)


def _apply_listserv_filters(stmt: Select, filters: ListservFilters) -> Select:
    if filters.list_id is not None:
        val = filters.list_id
        if not is_null_token(val) and not is_not_null_token(val):
            negated, pattern = split_not_like_prefix(val)
            val = f"!{pattern.upper()}" if negated else pattern.upper()
        stmt = apply_text_filter(stmt, ListservMessageORM.list_id, val)
    if filters.sender is not None:
        stmt = apply_text_filter(stmt, ListservMessageORM.sender, filters.sender)
    if filters.subject is not None:
        stmt = apply_text_filter(stmt, ListservMessageORM.subject, filters.subject)
    if filters.tdocs is not None:
        stmt = _apply_tdocs_filter(stmt, filters.tdocs)
    if filters.dates is not None:
        stmt = _apply_date_set_filter(stmt, filters.dates)
    return stmt


def _apply_tdocs_filter(stmt: Select, value: str) -> Select:
    empty_pred = and_(
        or_(
            ListservMessageORM.subject_tdocs.is_(None),
            ListservMessageORM.subject_tdocs == "",
        ),
        or_(ListservDetailORM.tdocs.is_(None), ListservDetailORM.tdocs == ""),
    )
    if is_null_token(value):
        return stmt.where(empty_pred)
    if is_not_null_token(value):
        return stmt.where(~empty_pred)
    negated, pattern = split_not_like_prefix(value)
    upper_pattern = pattern.upper()
    subj_expr = func.upper(func.coalesce(ListservMessageORM.subject_tdocs, ""))
    detail_expr = func.upper(func.coalesce(ListservDetailORM.tdocs, ""))
    positive_pred = or_(
        subj_expr.like(upper_pattern),
        detail_expr.like(upper_pattern),
    )
    if negated:
        return stmt.where(~positive_pred)
    return stmt.where(positive_pred)


def _apply_date_set_filter(stmt: Select, dates: DateSet) -> Select:
    if not dates.intervals:
        return stmt.where(ListservMessageORM.url.is_(None))
    clauses = []
    for iv in dates.intervals:
        if iv.start is not None and iv.end is not None:
            clauses.append(
                and_(
                    ListservMessageORM.local_date >= iv.start,
                    ListservMessageORM.local_date <= iv.end,
                )
            )
        elif iv.start is not None:
            clauses.append(ListservMessageORM.local_date >= iv.start)
        elif iv.end is not None:
            clauses.append(ListservMessageORM.local_date <= iv.end)
        else:
            return stmt
    return stmt.where(or_(*clauses))


__all__ = ["SQLAlchemyListservRepository"]
