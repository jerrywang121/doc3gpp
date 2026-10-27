"""SQLAlchemy implementation of :class:`SearchIndexRepository`.

Owns the FTS5 virtual table (``tdoc_search``) + meta sidecar
(``tdoc_search_meta``) created by
:func:`doc3gpp.storage.db.migrate._create_search_schema`. At
construction time it probes for FTS5 availability — raising
:class:`SearchUnavailableError` on non-sqlite or FTS5-less builds
so :func:`doc3gpp.services.factory.build_search_service` can
catch it once at startup.

Index text is built by a single SQL JOIN across ``tdocs`` +
``meetings`` + ``wis`` + the three sidecar tables, with the gzip
JSON blobs decompressed in Python (sqlite has no ``gzip()`` SQL
builtin) and every text column run through
:func:`doc3gpp.storage.db.fts5_query.normalize_query` before
INSERT.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from doc3gpp.models.search import (
    MatchedDocument,
    SearchError,
    SearchFilters,
    SearchHit,
    SearchIndexStatus,
)
from doc3gpp.models.tdoc_content_families import TDOC_CONTENT_STORES
from doc3gpp.repository.protocols import SearchIndexRepository
from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import _SNIPPET_COLUMN_NAMES
from doc3gpp.storage.compression import decompress_json
from doc3gpp.storage.db.fts5_query import normalize_query
from doc3gpp.storage.db.models import CONTENT_ORM_BY_STORE_KEY, TDocExtractOrm
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.rich_filters import (
    build_text_filter_or_params,
    build_text_filter_sql,
)
from doc3gpp.storage.repositories.tdoc_document_sources import (
    tdoc_document_sources_sql,
)

logger = logging.getLogger(__name__)


# FTS5 snippet column IDs account for the unindexed tdoc_id column at cid 0.
# The nine idx text fields therefore occupy cids 1 through 9.
def _snippet_column_cid(name: str) -> int:
    return _SNIPPET_COLUMN_NAMES.index(name) + 1


def _check_fts5(engine: Engine) -> None:
    """Raise :class:`SearchUnavailableError` if FTS5 is not available."""
    if engine.dialect.name != "sqlite":
        raise SearchUnavailableError(
            f"search requires sqlite FTS5; current dialect is "
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
            "sqlite was built without ENABLE_FTS5; install a build "
            "with FTS5 enabled or upgrade Python's bundled sqlite"
        )


# Imported here (not at module top) to avoid a circular import: this
# module is imported by services/factory.py which itself imports
# SearchUnavailableError from models.search.
from doc3gpp.models.search import SearchUnavailableError

# FTS5 ``MATCH`` parse failures surface as ``OperationalError`` with a
# message like ``fts5: syntax error near ...``. Distinguish a malformed
# user query (a :class:`SearchQueryError`, exit code 2) from genuine
# index corruption (a :class:`SearchIndexCorruptError`, exit code 3) so
# the CLI / web / MCP surfaces can give the right guidance instead of a
# raw traceback.
_QUERY_ERROR_MARKERS = (
    "syntax error",
    "unable to parse",
    "no such column",
    "unbalanced",
    "unterminated",
)


def _classify_search_error(exc: OperationalError) -> SearchError:
    """Translate an FTS5 ``OperationalError`` into a domain error.

    Query-shaped failures (bad ``MATCH`` expression) become
    :class:`SearchQueryError`; anything else is treated as index
    corruption (:class:`SearchIndexCorruptError`).
    """
    from doc3gpp.models.search import (
        SearchIndexCorruptError,
        SearchQueryError,
    )

    msg = str(exc)
    if any(marker in msg for marker in _QUERY_ERROR_MARKERS):
        return SearchQueryError(msg)
    return SearchIndexCorruptError(msg)


_DOCUMENT_SOURCES_SQL = tdoc_document_sources_sql()

_TDOC_FRESHNESS_CTES_SQL = f"""
WITH document_sources AS (
{_DOCUMENT_SOURCES_SQL}
), expected_documents AS (
    SELECT e.tdoc_id, d.document_id, d.source_file, d.content_hash,
           e.extraction_version
    FROM tdoc_extracts e
    JOIN document_sources d ON d.ftp_url = e.ftp_url
), declared_counts AS (
    SELECT tdoc_id, SUM(document_count) AS declared_document_count
    FROM tdoc_extracts
    GROUP BY tdoc_id
), actual_counts AS (
    SELECT e.tdoc_id, COUNT(d.document_id) AS actual_document_count
    FROM tdoc_extracts e
    LEFT JOIN document_sources d ON d.ftp_url = e.ftp_url
    GROUP BY e.tdoc_id
), expected_counts AS (
    SELECT declared.tdoc_id,
           declared.declared_document_count,
           actual.actual_document_count
    FROM declared_counts declared
    JOIN actual_counts actual ON actual.tdoc_id = declared.tdoc_id
), indexed_rows AS (
    SELECT tdoc_id, row_kind, document_id, source_file, content_hash,
           CAST(extraction_version AS INTEGER) AS extraction_version
    FROM tdoc_search
), indexed_documents AS (
    SELECT tdoc_id, row_kind, document_id, source_file, content_hash,
           extraction_version
    FROM indexed_rows
    WHERE row_kind = 'document'
), indexed_counts AS (
    SELECT tdoc_id,
           COUNT(*) AS physical_row_count,
           SUM(CASE WHEN row_kind = 'metadata' THEN 1 ELSE 0 END)
               AS metadata_row_count
    FROM indexed_rows
    GROUP BY tdoc_id
), index_checkpoint AS (
    SELECT COALESCE(
        MAX(CASE WHEN key = 'last_indexed_uploaded_date' THEN value END),
        ''
    ) AS last_indexed_uploaded_date
    FROM tdoc_search_meta
), stale_tdocs AS (
    SELECT t.tdoc_id
    FROM tdocs t
    LEFT JOIN indexed_counts idx ON idx.tdoc_id = t.tdoc_id
    LEFT JOIN expected_counts expected ON expected.tdoc_id = t.tdoc_id
    CROSS JOIN index_checkpoint checkpoint
    WHERE t.uploaded_date > checkpoint.last_indexed_uploaded_date
       OR COALESCE(idx.metadata_row_count, 0) <> 1
       OR COALESCE(idx.physical_row_count, 0)
            <> 1 + COALESCE(expected.actual_document_count, 0)
       OR COALESCE(expected.declared_document_count, 0)
            <> COALESCE(expected.actual_document_count, 0)
    UNION
    SELECT expected.tdoc_id
    FROM expected_documents expected
    LEFT JOIN indexed_documents idx
      ON idx.tdoc_id = expected.tdoc_id
     AND idx.document_id = expected.document_id
     AND idx.source_file = expected.source_file
     AND idx.content_hash = expected.content_hash
     AND idx.extraction_version = expected.extraction_version
    WHERE idx.row_kind IS NULL
    UNION
    SELECT idx.tdoc_id
    FROM indexed_counts idx
    LEFT JOIN tdocs t ON t.tdoc_id = idx.tdoc_id
    WHERE t.tdoc_id IS NULL
)
"""

class SQLAlchemySearchIndexRepository(SearchIndexRepository):
    """Concrete :class:`SearchIndexRepository` backed by FTS5."""

    def __init__(self) -> None:
        self._engine = get_engine()
        _check_fts5(self._engine)
        settings = get_settings().search
        self._weights: tuple[float, ...] = tuple(settings.bm25_weights)
        self._snippet_tokens: int = settings.snippet_tokens

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------

    def upsert(self, tdoc_id: str) -> None:
        index_text = self._build_index_text(tdoc_id)
        if index_text is None:
            self.remove(tdoc_id)
            return
        metadata_text, document_rows = index_text
        insert_row = text(
            """
            INSERT INTO tdoc_search (
                tdoc_id, title, ftp_url, meeting_title, meeting_location,
                wis, cover_text, change_text, ttcn_text, content_text,
                row_kind, document_id, source_file, extraction_version,
                content_hash
            ) VALUES (
                :tdoc_id, :title, :ftp_url, :meeting_title, :meeting_location,
                :wis, :cover_text, :change_text, :ttcn_text, :content_text,
                :row_kind, :document_id, :source_file, :extraction_version,
                :content_hash
            )
            """
        )
        with self._engine.begin() as conn:
            conn.execute(
                text("DELETE FROM tdoc_search WHERE tdoc_id = :id"),
                {"id": tdoc_id},
            )
            metadata_values: dict[str, Any] = {
                column: "" for column in _SNIPPET_COLUMN_NAMES
            }
            metadata_values.update(metadata_text)
            metadata_values.update(
                {
                    "tdoc_id": tdoc_id,
                    "row_kind": "metadata",
                    "document_id": None,
                    "source_file": None,
                    "extraction_version": None,
                    "content_hash": None,
                }
            )
            conn.execute(insert_row, metadata_values)
            for (
                document_id,
                source_file,
                extraction_version,
                content_hash,
                content_text,
            ) in document_rows:
                document_values: dict[str, Any] = {
                    column: "" for column in _SNIPPET_COLUMN_NAMES
                }
                document_values.update(
                    {
                        "tdoc_id": tdoc_id,
                        "content_text": content_text,
                        "row_kind": "document",
                        "document_id": document_id,
                        "source_file": source_file,
                        "extraction_version": str(extraction_version),
                        "content_hash": content_hash,
                    }
                )
                conn.execute(insert_row, document_values)

            now_iso = datetime.now(UTC).isoformat(timespec="seconds")
            uploaded_date = conn.execute(
                text("SELECT uploaded_date FROM tdocs WHERE tdoc_id = :id"),
                {"id": tdoc_id},
            ).scalar()
            for key, value in (
                ("last_indexed_at", now_iso),
                (
                    "last_indexed_uploaded_date",
                    uploaded_date or "",
                ),
            ):
                conn.execute(
                    text(
                        """
                        INSERT INTO tdoc_search_meta (key, value)
                        VALUES (:key, :value)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """
                    ),
                    {"key": key, "value": value},
                )

    def remove(self, tdoc_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text("DELETE FROM tdoc_search WHERE tdoc_id = :id"),
                {"id": tdoc_id},
            )

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def search(
        self, query: str, filters: SearchFilters,
        snippet_tokens: int | None = None,
    ) -> list[SearchHit]:
        """Return grouped parent hits with matching document provenance.

        A streamed top-level FTS query finds the best rows for parent ranking;
        a second top-level query loads all matching rows for the selected
        parents. Keeping bm25 and snippet calls in top-level FTS queries avoids
        SQLite losing its auxiliary-function context in a reused CTE.
        """
        limit = max(filters.limit, 0)
        if limit == 0:
            return []
        effective_tokens = (
            snippet_tokens if snippet_tokens is not None
            else self._snippet_tokens
        )
        snippet_columns: list[tuple[str, int]] = [
            (name, _snippet_column_cid(name))
            for name, weight in zip(
                _SNIPPET_COLUMN_NAMES, self._weights, strict=True
            )
            if weight > 0
        ]
        weight_args = ", ".join(
            f":w{index}" for index in range(len(self._weights))
        )
        where_clauses = ["tdoc_search MATCH :query"]
        filter_params: dict[str, Any] = {"query": query}
        if filters.tsg:
            where_clauses.append("m.tsg = :tsg")
            filter_params["tsg"] = filters.tsg.upper()
        if filters.meeting:
            meeting_clauses, meeting_params = build_text_filter_or_params(
                ("m.name", "m.title"), filters.meeting, param="meeting",
            )
            where_clauses.append(f"({meeting_clauses})")
            filter_params.update(meeting_params)
        if filters.meeting_id is not None:
            where_clauses.append("m.meeting_id = :meeting_id")
            filter_params["meeting_id"] = filters.meeting_id
        if filters.tdoc_id:
            where_clauses.append("t.tdoc_id = :tdoc_id")
            filter_params["tdoc_id"] = filters.tdoc_id
        if filters.release:
            clause, bound = build_text_filter_sql(
                "t.release", filters.release, param="release",
            )
            where_clauses.append(clause)
            if bound is not None:
                filter_params["release"] = bound
        if filters.spec:
            clause, bound = build_text_filter_sql(
                "t.spec", filters.spec, param="spec",
            )
            where_clauses.append(clause)
            if bound is not None:
                filter_params["spec"] = bound
        if filters.since:
            where_clauses.append("t.uploaded_date >= :since")
            filter_params["since"] = filters.since
        if filters.until:
            where_clauses.append("t.uploaded_date <= :until")
            filter_params["until"] = filters.until
        where_sql = " AND ".join(where_clauses)
        from_sql = (
            "FROM tdoc_search "
            "JOIN tdocs t ON t.tdoc_id = tdoc_search.tdoc_id "
            "JOIN meetings m ON t.meeting_id = m.meeting_id"
        )
        weight_params = {
            f"w{index}": weight
            for index, weight in enumerate(self._weights)
        }
        rank_sql = text(
            f"""
            SELECT tdoc_search.tdoc_id,
                   bm25(tdoc_search, {weight_args}) AS score
              {from_sql}
             WHERE {where_sql}
             ORDER BY bm25(tdoc_search, {weight_args}) ASC,
                      tdoc_search.tdoc_id ASC,
                      COALESCE(tdoc_search.source_file, '') ASC,
                      COALESCE(tdoc_search.document_id, '') ASC
            """
        )
        selected_parent_ids: list[str] = []
        parent_scores: dict[str, float] = {}
        try:
            with self._engine.begin() as conn:
                ranked_rows = conn.execute(
                    rank_sql,
                    {**weight_params, **filter_params},
                ).yield_per(128)
                try:
                    for row in ranked_rows:
                        tdoc_id = row[0]
                        if tdoc_id in parent_scores:
                            continue
                        selected_parent_ids.append(tdoc_id)
                        parent_scores[tdoc_id] = float(row[1])
                        if len(selected_parent_ids) == limit:
                            break
                finally:
                    ranked_rows.close()
                if not selected_parent_ids:
                    return []

                sql = [
                    "SELECT tdoc_search.tdoc_id, tdoc_search.row_kind,",
                    "       tdoc_search.document_id, tdoc_search.source_file,",
                    f"       bm25(tdoc_search, {weight_args}) AS row_score,",
                ]
                params: dict[str, Any] = {
                    **weight_params,
                    **filter_params,
                    "tok": effective_tokens,
                }
                for index, (_name, column_id) in enumerate(snippet_columns):
                    parameter = f"col_{index}"
                    sql.append(
                        f"       snippet(tdoc_search, :{parameter}, '<<', '>>', '…', :tok) "
                        f"AS snippet_{index},"
                    )
                    params[parameter] = column_id
                sql.extend(
                    [
                        "       t.title, m.title AS meeting, m.tsg AS tsg,",
                        "       t.uploaded_date, t.ftp_url, t.related_wis,",
                        "       t.type, t.status, doc.ftp_url AS document_ftp_url",
                        f"  {from_sql}",
                        "  JOIN temp._doc3gpp_search_selected_tdocs selected",
                        "    ON selected.tdoc_id = tdoc_search.tdoc_id",
                        f"  LEFT JOIN ({_DOCUMENT_SOURCES_SQL}) doc",
                        "    ON doc.document_id = tdoc_search.document_id",
                        f" WHERE {where_sql}",
                        " ORDER BY selected.parent_rank ASC,",
                        f"          bm25(tdoc_search, {weight_args}) ASC,",
                        "          tdoc_search.tdoc_id ASC,",
                        "          COALESCE(doc.ftp_url, '') ASC,",
                        "          COALESCE(tdoc_search.source_file, '') ASC,",
                        "          COALESCE(tdoc_search.document_id, '') ASC",
                    ]
                )
                conn.execute(
                    text("DROP TABLE IF EXISTS temp._doc3gpp_search_selected_tdocs")
                )
                conn.execute(
                    text(
                        "CREATE TEMP TABLE _doc3gpp_search_selected_tdocs ("
                        "tdoc_id TEXT PRIMARY KEY, parent_rank INTEGER NOT NULL)"
                    )
                )
                try:
                    conn.execute(
                        text(
                            "INSERT INTO temp._doc3gpp_search_selected_tdocs "
                            "(tdoc_id, parent_rank) VALUES (:tdoc_id, :parent_rank)"
                        ),
                        [
                            {"tdoc_id": tdoc_id, "parent_rank": rank}
                            for rank, tdoc_id in enumerate(selected_parent_ids)
                        ],
                    )
                    rows = conn.execute(text("\n".join(sql)), params).all()
                finally:
                    conn.execute(
                        text("DROP TABLE IF EXISTS temp._doc3gpp_search_selected_tdocs")
                    )
        except OperationalError as exc:
            raise _classify_search_error(exc) from exc

        parent_rows: dict[str, dict[str, Any]] = {}
        documents_by_parent: dict[str, list[MatchedDocument]] = {
            tdoc_id: [] for tdoc_id in selected_parent_ids
        }
        preview_offset = 5 + len(snippet_columns)
        for row in rows:
            previews: dict[str, str] = {}
            for index, (name, _column_id) in enumerate(snippet_columns):
                value = row[5 + index]
                if value and "<<" in value and ">>" in value:
                    previews[name] = value

            tdoc_id = row[0]
            if tdoc_id not in parent_rows:
                parent_rows[tdoc_id] = {
                    "score": parent_scores[tdoc_id],
                    "title": row[preview_offset],
                    "meeting": row[preview_offset + 1],
                    "tsg": row[preview_offset + 2],
                    "uploaded_date": row[preview_offset + 3],
                    "ftp_url": row[preview_offset + 4],
                    "related_wis": row[preview_offset + 5],
                    "type": row[preview_offset + 6],
                    "status": row[preview_offset + 7],
                    "previews": {},
                }

            if row[1] == "metadata":
                parent_rows[tdoc_id]["previews"] = previews
            elif row[1] == "document" and row[preview_offset + 8] is not None:
                documents_by_parent[tdoc_id].append(
                    MatchedDocument(
                        document_id=row[2],
                        ftp_url=row[preview_offset + 8],
                        source_file=row[3],
                        score=float(row[3 + 1]),
                        previews=previews,
                    )
                )

        return [
            SearchHit(
                tdoc_id=tdoc_id,
                score=parent["score"],
                previews=parent["previews"],
                title=parent["title"],
                meeting=parent["meeting"],
                tsg=parent["tsg"],
                uploaded_date=parent["uploaded_date"],
                ftp_url=parent["ftp_url"],
                wis=_wis_to_text(parent["related_wis"]),
                type=parent["type"],
                status=parent["status"],
                matched_documents=tuple(documents_by_parent[tdoc_id]),
            )
            for tdoc_id in selected_parent_ids
            if tdoc_id in parent_rows
            for parent in (parent_rows[tdoc_id],)
        ]

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def rebuild_batch(
        self,
        batch_size: int,
        after_id: str | None,
        stale_only: bool,
    ) -> Iterable[list[str]]:
        if batch_size < 1:
            return
        last_id = after_id if after_id is not None else ""
        if stale_only:
            sql = (
                f"{_TDOC_FRESHNESS_CTES_SQL}"
                " SELECT t.tdoc_id FROM tdocs t"
                " JOIN stale_tdocs stale ON stale.tdoc_id = t.tdoc_id"
                " WHERE t.tdoc_id > :last_id"
                " ORDER BY t.tdoc_id ASC LIMIT :limit"
            )
            while True:
                with self._engine.begin() as conn:
                    ids = conn.execute(
                        text(sql),
                        {"last_id": last_id, "limit": batch_size},
                    ).scalars().all()
                if not ids:
                    return
                yield ids
                last_id = ids[-1]

        while True:
            with self._engine.begin() as conn:
                rows = conn.execute(
                    text(
                        "SELECT tdoc_id FROM tdocs "
                        "WHERE tdoc_id > :last_id "
                        "ORDER BY tdoc_id ASC LIMIT :limit"
                    ),
                    {"last_id": last_id, "limit": batch_size},
                ).all()
            ids = [row[0] for row in rows]
            if not ids:
                return
            yield ids
            last_id = ids[-1]

    def count_tdocs_to_index(
        self, stale_only: bool, after_id: str | None = None,
    ) -> int:
        params: dict[str, object] = {}
        after_clause = ""
        if after_id is not None:
            after_clause = " WHERE t.tdoc_id > :after_id"
            params["after_id"] = after_id
        if stale_only:
            sql = (
                f"{_TDOC_FRESHNESS_CTES_SQL}"
                " SELECT COUNT(*) FROM tdocs t"
                " JOIN stale_tdocs stale ON stale.tdoc_id = t.tdoc_id"
            ) + after_clause
        else:
            sql = "SELECT COUNT(*) FROM tdocs t" + after_clause
        with self._engine.begin() as conn:
            return int(conn.execute(text(sql), params).scalar() or 0)

    def get_resume_cursor(self) -> str | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT value FROM tdoc_search_meta "
                    "WHERE key = 'last_rebuild_last_tdoc_id'"
                )
            ).first()
        if row is None or row[0] in (None, ""):
            return None
        return str(row[0])

    def set_resume_cursor(self, tdoc_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO tdoc_search_meta (key, value)
                    VALUES ('last_rebuild_last_tdoc_id', :id)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """
                ),
                {"id": tdoc_id},
            )

    def clear_resume_cursor(self) -> None:
        """Remove the resume cursor from ``tdoc_search_meta``.

        Called by :meth:`SearchService.rebuild` when the operator
        runs ``tdoc index --rebuild`` without ``--resume`` to
        force a fresh start from the very first TDoc. After this
        call, :meth:`get_resume_cursor` returns ``None`` until the
        rebuild's first batch upserts a new cursor.
        """
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM tdoc_search_meta "
                    "WHERE key = 'last_rebuild_last_tdoc_id'"
                ),
            )

    def status(self) -> SearchIndexStatus:
        with self._engine.begin() as conn:
            freshness = conn.execute(
                text(
                    f"{_TDOC_FRESHNESS_CTES_SQL} "
                    "SELECT (SELECT COUNT(*) FROM indexed_counts), "
                    "EXISTS (SELECT 1 FROM stale_tdocs)"
                )
            ).one()
            row_count = int(freshness[0] or 0)
            is_stale = bool(freshness[1])
            last_rebuild = conn.execute(
                text(
                    "SELECT value FROM tdoc_search_meta "
                    "WHERE key = 'last_rebuild_at'"
                )
            ).first()
            last_indexed_uploaded_date = conn.execute(
                text(
                    "SELECT value FROM tdoc_search_meta "
                    "WHERE key = 'last_indexed_uploaded_date'"
                )
            ).first()
            latest_uploaded_date = conn.execute(
                text("SELECT MAX(uploaded_date) FROM tdocs")
            ).first()
        last_rebuild_dt = _parse_iso(last_rebuild[0]) if last_rebuild else None
        last_indexed_dt = (
            _parse_iso(last_indexed_uploaded_date[0])
            if last_indexed_uploaded_date
            else None
        )
        latest_dt = (
            _parse_iso(latest_uploaded_date[0]) if latest_uploaded_date else None
        )
        return SearchIndexStatus(
            enabled=True,
            row_count=row_count,
            last_rebuild_at=last_rebuild_dt,
            last_indexed_uploaded_date=last_indexed_dt,
            latest_tdocs_uploaded_date=latest_dt,
            is_stale=is_stale,
        )

    # ------------------------------------------------------------------
    # Internal: build concatenated text for one tdoc_id
    # ------------------------------------------------------------------

    def _build_index_text(
        self,
        tdoc_id: str,
    ) -> tuple[dict[str, str], list[tuple[str, str, int, str, str]]] | None:
        """Build metadata text and SQL-backed document rows for a TDoc."""
        with self._engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT t.title, t.ftp_url, t.related_wis,
                           m.title AS meeting_title,
                           m.location AS meeting_location
                      FROM tdocs t
                      LEFT JOIN meetings m ON t.meeting_id = m.meeting_id
                     WHERE t.tdoc_id = :id
                    """
                ),
                {"id": tdoc_id},
            ).first()
            if row is None:
                return None
            document_rows = []
            for store in TDOC_CONTENT_STORES:
                orm = CONTENT_ORM_BY_STORE_KEY[store.key]
                document_rows.extend(
                    conn.execute(
                        select(
                            orm.doc_filename,
                            orm.document_id,
                            TDocExtractOrm.extraction_version,
                            orm.content_hash,
                            orm.content,
                        )
                        .select_from(orm)
                        .join(TDocExtractOrm, TDocExtractOrm.ftp_url == orm.ftp_url)
                        .where(TDocExtractOrm.tdoc_id == tdoc_id)
                        .order_by(orm.ftp_url, orm.doc_filename)
                    ).all()
                )
        title, ftp_url, related_wis, meeting_title, meeting_location = row

        wis_text = _wis_to_text(related_wis)
        cover_text = _cover_text(conn=self._engine, tdoc_id=tdoc_id)
        change_text = _change_text(conn=self._engine, tdoc_id=tdoc_id)
        ttcn_text = _ttcn_text(conn=self._engine, tdoc_id=tdoc_id)
        documents = [
            (
                document_row.document_id,
                document_row.doc_filename,
                int(document_row.extraction_version),
                document_row.content_hash,
                normalize_query(
                    _content_markdown(document_row.doc_filename, document_row.content)
                ),
            )
            for document_row in document_rows
        ]

        metadata_text = {
            "title": normalize_query(title or ""),
            "ftp_url": ftp_url or "",
            "meeting_title": meeting_title or "",
            "meeting_location": meeting_location or "",
            "wis": wis_text,
            "cover_text": normalize_query(cover_text),
            "change_text": normalize_query(change_text),
            "ttcn_text": normalize_query(ttcn_text),
        }
        return metadata_text, documents


def _content_markdown(source_file: str, content: bytes) -> str:
    """Decode one stored content row with the same rule as the content repo."""
    markdown = decompress_json(content)
    if markdown is None:
        raise ValueError(f"stored content for {source_file!r} cannot be decoded")
    return markdown


def _wis_to_text(related_wis: str | None) -> str:
    """Turn ``related_wis`` into a space-joined text blob.

    ``tdocs.related_wis`` is comma-separated; this splits + strips +
    joins with spaces so FTS5 indexes every acronym + name as its
    own token.
    """
    if not related_wis:
        return ""
    return " ".join(part.strip() for part in related_wis.split(",") if part.strip())


def _cover_text(*, conn: Engine, tdoc_id: str) -> str:
    with conn.begin() as c:
        row = c.execute(
            text(
                """
                SELECT spec, cr_num, rev, version, title, source, tsg,
                       related_wis, date, cr_cat, release,
                       reason_for_change, consequences_if_not_approved,
                       summary_of_change,
                       clauses_affected, other_comments, revision_history,
                       extracted_tdoc_id
                  FROM tdoc_cr_cover_page
                 WHERE tdoc_id = :id
                """
            ),
            {"id": tdoc_id},
        ).first()
    if row is None:
        return ""
    return " ".join(str(v) for v in row if v is not None)


def _change_text(*, conn: Engine, tdoc_id: str) -> str:
    with conn.begin() as c:
        row = c.execute(
            text(
                "SELECT clauses, changes FROM tdoc_cr_change_details "
                "WHERE tdoc_id = :id"
            ),
            {"id": tdoc_id},
        ).first()
    if row is None:
        return ""
    clauses_text = row[0] or ""
    changes_obj = decompress_json(row[1])
    parts: list[str] = [clauses_text]
    if isinstance(changes_obj, list):
        for c in changes_obj:
            parts.append(json.dumps(c, ensure_ascii=False))
    return " ".join(parts)


def _ttcn_text(*, conn: Engine, tdoc_id: str) -> str:
    with conn.begin() as c:
        row = c.execute(
            text(
                """
                SELECT testcase, ue, ss, ats_version, ttcn_release,
                       test_suite, required_changes, changed_functions
                  FROM tdoc_cr_ttcn_details
                 WHERE tdoc_id = :id
                """
            ),
            {"id": tdoc_id},
        ).first()
    if row is None:
        return ""
    flat = " ".join(str(v) for v in row[:6] if v is not None)
    req = _blob_to_text(row[6])
    cf = (row[7] or "").replace("\n", " ") if row[7] else ""
    return f"{flat} {req} {cf}"


def _blob_to_text(blob: bytes | None) -> str:
    """Decode a gzip JSON blob into a space-joined string."""
    decoded = decompress_json(blob)
    if decoded is None:
        return ""
    if isinstance(decoded, (list, dict)):
        return json.dumps(decoded, ensure_ascii=False)
    return str(decoded)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
