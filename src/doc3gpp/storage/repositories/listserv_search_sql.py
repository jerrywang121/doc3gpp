"""SQLite FTS5 repository for ETSI LISTSERV archives."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.orm import sessionmaker

from doc3gpp.cli_filters import (
    SearchQueryBuilder,
    is_not_null_token,
    is_null_token,
    split_not_like_prefix,
)
from doc3gpp.models.listserv import (
    DateSet,
    ListservFilters,
    ListservRecord,
    ListservSearchResult,
)
from doc3gpp.models.listserv_payloads import (
    effective_record_tdocs,
    indexable_fields,
    indexable_text,
    record_to_search_result,
    strip_urls,
)
from doc3gpp.models.search import (
    SearchError,
    SearchIndexCorruptError,
    SearchQueryError,
    SearchUnavailableError,
)
from doc3gpp.repository.listserv import ListservRepository
from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import Settings
from doc3gpp.storage.db.fts5_query import normalize_query
from doc3gpp.storage.db.models import ListservDetailORM, ListservMessageORM
from doc3gpp.storage.db.session import get_engine, get_session_factory
from doc3gpp.storage.repositories.listserv_sql import (
    SQLAlchemyListservRepository,
    _rows_to_record,
)
from doc3gpp.storage.repositories.rich_filters import build_text_filter_sql

_INDEXED_COLUMNS: tuple[str, ...] = (
    "list_id",
    "subject",
    "sender",
    "date",
    "week",
    "tdocs",
    "attachments",
    "content",
)

# Priority order when picking the primary matched_source and snippet for a hit.
# Putting subject/content/attachments ahead of tdocs ensures that when a TDoc ID
# appears in prose or subject, the richer prose snippet is chosen; when a TDoc ID
# only comes from an attachment URL (or truncated-off body tail), "tdocs" matches.
_PROVENANCE_PRIORITY: tuple[str, ...] = (
    "subject",
    "content",
    "attachments",
    "tdocs",
    "sender",
    "list_id",
    "date",
    "week",
)

_QUERY_ERROR_MARKERS = (
    "syntax error",
    "unable to parse",
    "no such column",
    "unbalanced",
    "unterminated",
    "fts5:",
)

_DELETE_BATCH_SIZE = 250
_DEFAULT_FILTERS = ListservFilters()

_PRE_NORMALIZED_TDOC_RE = re.compile(
    r'"?([RSC][1-9P][-sw]\d{6,7})"?\s+"?(\1(?:r\d+)?)"?',
    re.IGNORECASE,
)
_DUPLICATE_HIGHLIGHT_TDOC_RE = re.compile(
    r"<<([RSC][1-9P][-sw]\d{6,7})\s+(\1(?:r\d+)?)>>",
    re.IGNORECASE,
)


def _check_fts5(engine: Engine) -> None:
    """Raise :class:`SearchUnavailableError` if SQLite FTS5 is not available."""
    if engine.dialect.name != "sqlite":
        raise SearchUnavailableError(
            "LISTSERV FTS5 search requires sqlite; current dialect is "
            f"{engine.dialect.name!r}"
        )
    with engine.begin() as conn:
        try:
            opts = conn.execute(text("PRAGMA compile_options")).all()
        except Exception as exc:
            raise SearchUnavailableError(
                f"could not probe sqlite compile_options: {exc}"
            ) from exc
    if not any(row[0] == "ENABLE_FTS5" for row in opts):
        raise SearchUnavailableError(
            "sqlite was built without ENABLE_FTS5; install a build with FTS5 enabled"
        )


def _classify_search_error(exc: DatabaseError) -> SearchError:
    """Translate an FTS5 database error into the domain search error hierarchy."""
    msg = str(exc)
    lowered = msg.lower()
    if "no such table: listserv_search" in lowered:
        return SearchUnavailableError("LISTSERV FTS5 search table is not available")
    if any(marker in lowered for marker in _QUERY_ERROR_MARKERS):
        return SearchQueryError(msg)
    return SearchIndexCorruptError(msg)


def _build_match_expression(query: str) -> str:
    """Build a normalized FTS5 MATCH expression idempotently via :class:`SearchQueryBuilder`."""
    collapsed = _PRE_NORMALIZED_TDOC_RE.sub(r"\2", query)
    return SearchQueryBuilder(collapsed).build()


def _clean_snippet(raw_snippet: str) -> str:
    """Collapse duplicate index-time TDoc tokens inside ``<<...>>`` markers."""
    return _DUPLICATE_HIGHLIGHT_TDOC_RE.sub(r"<<\2>>", raw_snippet)


def _delete_urls(conn: Connection, urls: Sequence[str]) -> None:
    for start in range(0, len(urls), _DELETE_BATCH_SIZE):
        batch = urls[start : start + _DELETE_BATCH_SIZE]
        placeholders = ", ".join(f":u{idx}" for idx in range(len(batch)))
        conn.execute(
            text(f"DELETE FROM listserv_search WHERE url IN ({placeholders})"),
            {f"u{idx}": u for idx, u in enumerate(batch)},
        )


def _fts_row_from_record(record: ListservRecord) -> dict[str, Any]:
    fields = indexable_fields(record)
    return {
        "url": record.message.url,
        "list_id": normalize_query(fields["list_id"]),
        "subject": normalize_query(fields["subject"]),
        "sender": normalize_query(fields["sender"]),
        "date": normalize_query(fields["date"]),
        "week": normalize_query(fields["week"]),
        "tdocs": normalize_query(fields["tdocs"]),
        "attachments": normalize_query(fields["attachments"]),
        "content": normalize_query(fields["content"]),
        "chunk_index": 0,
        "source_revision": str(record.source_revision),
    }


def _build_filter_clauses(
    filters: ListservFilters,
) -> tuple[list[str], dict[str, Any]]:
    """Build parameterized SQL WHERE clauses against ``listserv_messages m`` and ``listserv_details d``."""
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if filters.list_id is not None:
        val = filters.list_id
        if is_null_token(val):
            clauses.append("m.list_id IS NULL")
        elif is_not_null_token(val):
            clauses.append("m.list_id IS NOT NULL")
        else:
            negated, pattern = split_not_like_prefix(val)
            params["f_list_id"] = pattern.upper()
            if negated:
                clauses.append("NOT (m.list_id LIKE :f_list_id)")
            else:
                clauses.append("m.list_id LIKE :f_list_id")

    if filters.sender is not None:
        res = build_text_filter_sql("m.sender", filters.sender, param="f_sender")
        if res is not None:
            frag, bound = res
            clauses.append(frag)
            if bound is not None:
                params["f_sender"] = bound

    if filters.subject is not None:
        res = build_text_filter_sql("m.subject", filters.subject, param="f_subject")
        if res is not None:
            frag, bound = res
            clauses.append(frag)
            if bound is not None:
                params["f_subject"] = bound

    if filters.tdocs is not None:
        tdoc_val = filters.tdocs
        empty_sql = (
            "((m.subject_tdocs IS NULL OR m.subject_tdocs = '') "
            "AND (d.tdocs IS NULL OR d.tdocs = ''))"
        )
        if is_null_token(tdoc_val):
            clauses.append(empty_sql)
        elif is_not_null_token(tdoc_val):
            clauses.append(f"NOT {empty_sql}")
        else:
            negated, pattern = split_not_like_prefix(tdoc_val)
            params["f_tdocs"] = pattern.upper()
            positive_sql = (
                "(UPPER(COALESCE(m.subject_tdocs, '')) LIKE :f_tdocs "
                "OR UPPER(COALESCE(d.tdocs, '')) LIKE :f_tdocs)"
            )
            if negated:
                clauses.append(f"NOT {positive_sql}")
            else:
                clauses.append(positive_sql)

    if filters.dates is not None:
        date_clause, date_params = _build_date_set_sql(filters.dates)
        if date_clause:
            clauses.append(date_clause)
            params.update(date_params)

    return clauses, params


def _build_date_set_sql(dates: DateSet) -> tuple[str, dict[str, Any]]:
    if not dates.intervals:
        return "1 = 0", {}
    interval_clauses: list[str] = []
    params: dict[str, Any] = {}
    for idx, iv in enumerate(dates.intervals):
        if iv.start is not None and iv.end is not None:
            s_key = f"f_date_start_{idx}"
            e_key = f"f_date_end_{idx}"
            params[s_key] = iv.start.isoformat()
            params[e_key] = iv.end.isoformat()
            interval_clauses.append(
                f"(m.local_date >= :{s_key} AND m.local_date <= :{e_key})"
            )
        elif iv.start is not None:
            s_key = f"f_date_start_{idx}"
            params[s_key] = iv.start.isoformat()
            interval_clauses.append(f"m.local_date >= :{s_key}")
        elif iv.end is not None:
            e_key = f"f_date_end_{idx}"
            params[e_key] = iv.end.isoformat()
            interval_clauses.append(f"m.local_date <= :{e_key}")
        else:
            return "", {}
    return "(" + " OR ".join(interval_clauses) + ")", params


_INSERT_FTS_SQL = text(
    """
    INSERT INTO listserv_search (
        url, list_id, subject, sender, date, week,
        tdocs, attachments, content, chunk_index, source_revision
    ) VALUES (
        :url, :list_id, :subject, :sender, :date, :week,
        :tdocs, :attachments, :content, :chunk_index, :source_revision
    )
    """
)


class SQLAlchemyListservSearchRepository:
    """FTS5 index and search repository for ETSI LISTSERV messages on the main DB."""

    def __init__(
        self,
        *,
        engine: Engine | None = None,
        session_factory: sessionmaker | None = None,
        source_repository: ListservRepository | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._engine = engine or get_engine()
        _check_fts5(self._engine)
        self._session_factory = session_factory or get_session_factory()
        self._source_repository = source_repository or SQLAlchemyListservRepository(
            session_factory=self._session_factory
        )
        resolved_settings = settings if settings is not None else get_settings()
        self._snippet_tokens: int = resolved_settings.search.snippet_tokens

    def upsert(self, record_or_url: ListservRecord | str) -> None:
        """Index or replace the FTS5 entry for one :class:`ListservRecord` or canonical URL."""
        self.upsert_many((record_or_url,))

    def upsert_many(
        self,
        records_or_urls: Iterable[ListservRecord | str],
        *,
        stamp: bool = True,
    ) -> None:
        """Index or replace FTS5 entries for a batch of records or canonical URLs.

        ``stamp=False`` leaves ``last_indexed_at`` untouched (used by rebuilds, which
        stamp completion only after they finish).
        """
        items = list(records_or_urls)
        if not items:
            return

        target_urls: list[str] = []
        records_to_insert: list[ListservRecord] = []
        seen_urls: set[str] = set()

        for item in items:
            if isinstance(item, str):
                url = item
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                target_urls.append(url)
                loaded = self._source_repository.get(url)
                if loaded is not None:
                    records_to_insert.append(loaded)
            else:
                url = item.message.url
                if url in seen_urls:
                    records_to_insert = [
                        r for r in records_to_insert if r.message.url != url
                    ]
                else:
                    seen_urls.add(url)
                    target_urls.append(url)
                records_to_insert.append(item)

        fts_rows = [_fts_row_from_record(rec) for rec in records_to_insert]
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with self._engine.begin() as conn:
            _delete_urls(conn, target_urls)
            if fts_rows:
                conn.execute(_INSERT_FTS_SQL, fts_rows)
            if stamp:
                conn.execute(
                    text(
                        """
                        INSERT INTO listserv_search_meta (key, value)
                        VALUES ('last_indexed_at', :val)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """
                    ),
                    {"val": now_iso},
                )

    def indexed_revisions(self) -> dict[str, int]:
        """Return ``{url: source_revision}`` for every indexed message."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT url, source_revision FROM listserv_search")
            ).all()
        return {str(url): int(rev) for url, rev in rows}

    def meta(self) -> dict[str, str]:
        """Return the stored index metadata (``last_rebuild_at``/``last_indexed_at``)."""
        with self._engine.connect() as conn:
            return {
                str(k): str(v)
                for k, v in conn.execute(
                    text("SELECT key, value FROM listserv_search_meta")
                ).all()
            }

    def clear(self) -> None:
        """Delete every FTS row and completion timestamp before a full rebuild."""
        with self._engine.begin() as conn:
            conn.execute(text("DELETE FROM listserv_search"))
            conn.execute(
                text(
                    "DELETE FROM listserv_search_meta "
                    "WHERE key IN ('last_rebuild_at', 'last_indexed_at')"
                )
            )

    def mark_rebuilt(self) -> None:
        """Stamp rebuild completion timestamps after a successful rebuild."""
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._engine.begin() as conn:
            for key in ("last_rebuild_at", "last_indexed_at"):
                conn.execute(
                    text(
                        """
                        INSERT INTO listserv_search_meta (key, value)
                        VALUES (:key, :val)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """
                    ),
                    {"key": key, "val": now_iso},
                )

    def index_records(self, records: Iterable[ListservRecord]) -> None:
        """Index a sequence of :class:`ListservRecord` items into FTS5."""
        self.upsert_many(records)

    def remove(self, url: str) -> None:
        """Remove FTS5 rows for canonical detail ``url``."""
        with self._engine.begin() as conn:
            _delete_urls(conn, (url,))

    def rebuild(self) -> int:
        """Rebuild the entire LISTSERV FTS5 index from stored SQL source tables."""
        with self._session_factory() as session:
            stmt = (
                select(ListservMessageORM, ListservDetailORM)
                .outerjoin(
                    ListservDetailORM,
                    ListservMessageORM.url == ListservDetailORM.url,
                )
                .order_by(
                    ListservMessageORM.sort_timestamp.desc(),
                    ListservMessageORM.url.asc(),
                )
            )
            orm_rows = session.execute(stmt).all()
            records = [_rows_to_record(m, d) for m, d in orm_rows]

        fts_rows = [_fts_row_from_record(rec) for rec in records]
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._engine.begin() as conn:
            conn.execute(text("DELETE FROM listserv_search"))
            if fts_rows:
                conn.execute(_INSERT_FTS_SQL, fts_rows)
            for key in ("last_rebuild_at", "last_indexed_at"):
                conn.execute(
                    text(
                        """
                        INSERT INTO listserv_search_meta (key, value)
                        VALUES (:key, :val)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """
                    ),
                    {"key": key, "val": now_iso},
                )
        return len(fts_rows)

    def search(
        self,
        query: str,
        filters: ListservFilters = _DEFAULT_FILTERS,
        *,
        snippet_tokens: int | None = None,
    ) -> list[ListservSearchResult]:
        """Run FTS5 search with SQL source filters, deduplication by URL, and pagination."""
        match_expr = _build_match_expression(query)
        if filters.limit == 0:
            return []

        effective_tokens = (
            snippet_tokens if snippet_tokens is not None else self._snippet_tokens
        )
        snippet_selects: list[str] = []
        params: dict[str, Any] = {
            "query": match_expr,
            "tok": effective_tokens,
        }
        for idx, _col_name in enumerate(_INDEXED_COLUMNS):
            cid = idx + 1  # cid 0 is url UNINDEXED
            param_name = f"cid_{idx}"
            params[param_name] = cid
            snippet_selects.append(
                f"snippet(listserv_search, :{param_name}, '<<', '>>', '…', :tok) AS snip_{idx}"
            )

        where_clauses = ["listserv_search MATCH :query"]
        filter_clauses, filter_params = _build_filter_clauses(filters)
        where_clauses.extend(filter_clauses)
        params.update(filter_params)
        where_sql = " AND ".join(where_clauses)

        sql = f"""
            SELECT listserv_search.url,
                   bm25(listserv_search) AS score,
                   {", ".join(snippet_selects)}
              FROM listserv_search
              JOIN listserv_messages m ON m.url = listserv_search.url
              LEFT JOIN listserv_details d ON d.url = m.url
             WHERE {where_sql}
             ORDER BY bm25(listserv_search) ASC,
                      m.sort_timestamp DESC,
                      m.url ASC
        """
        try:
            with self._engine.begin() as conn:
                raw_rows = conn.execute(text(sql), params).all()
        except (OperationalError, DatabaseError) as exc:
            raise _classify_search_error(exc) from exc

        # Deduplicate by message URL preserving ranked order before offset/limit
        deduped_hits: list[tuple[str, float, str | None, str | None]] = []
        seen_urls: set[str] = set()
        for row in raw_rows:
            url = str(row[0])
            if url in seen_urls:
                continue
            seen_urls.add(url)
            score = float(row[1])
            snippets_by_col: dict[str, str] = {}
            for idx, col_name in enumerate(_INDEXED_COLUMNS):
                val = row[2 + idx]
                if isinstance(val, str) and "<<" in val and ">>" in val:
                    snippets_by_col[col_name] = _clean_snippet(val)

            matched_source: str | None = None
            chosen_snippet: str | None = None
            for candidate_col in _PROVENANCE_PRIORITY:
                if candidate_col in snippets_by_col:
                    matched_source = candidate_col
                    chosen_snippet = snippets_by_col[candidate_col]
                    break

            deduped_hits.append((url, score, chosen_snippet, matched_source))

        paged_hits = deduped_hits[filters.offset : filters.offset + filters.limit]
        if not paged_hits:
            return []

        page_urls = [item[0] for item in paged_hits]
        records_by_url = self._load_records_by_url(page_urls)

        results: list[ListservSearchResult] = []
        for url, score, snippet, matched_source in paged_hits:
            record = records_by_url.get(url)
            if record is None:
                continue
            results.append(
                record_to_search_result(
                    record,
                    score=score,
                    snippet=snippet,
                    matched_source=matched_source,
                )
            )
        return results

    def _load_records_by_url(
        self, urls: Sequence[str]
    ) -> dict[str, ListservRecord]:
        if not urls:
            return {}
        with self._session_factory() as session:
            stmt = (
                select(ListservMessageORM, ListservDetailORM)
                .outerjoin(
                    ListservDetailORM,
                    ListservMessageORM.url == ListservDetailORM.url,
                )
                .where(ListservMessageORM.url.in_(urls))
            )
            rows = session.execute(stmt).all()
            return {
                m.url: _rows_to_record(m, d)
                for m, d in rows
            }


__all__ = [
    "SQLAlchemyListservSearchRepository",
    "effective_record_tdocs",
    "indexable_fields",
    "indexable_text",
    "strip_urls",
]
