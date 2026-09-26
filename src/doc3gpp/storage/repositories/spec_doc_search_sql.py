"""SQLAlchemy implementation of :class:`SpecDocSearchRepository`.

Owns the FTS5 virtual table (``spec_doc_search``) + meta sidecar
(``spec_doc_search_meta``) created by
:func:`doc3gpp.storage.db.migrate._create_specdata_search_schema`. At
construction time it probes for FTS5 availability — raising
:class:`SearchUnavailableError` on non-sqlite or FTS5-less builds.

Mirrors :mod:`doc3gpp.storage.repositories.search_sql` minus the
``tdocs`` / ``meetings`` JOINs: the FTS5 row is a projection of one
``spec_doc_chunks`` row, re-joined at query time for the
``sections`` / ``tables`` / ``chunk_index`` / original-text fields
that have no FTS5 column. The repo takes the raw user query and builds
the ``MATCH`` expression internally via
:class:`doc3gpp.cli_filters.SearchQueryBuilder` (the same path
``SearchService.search`` uses) so services pass raw text.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from doc3gpp.cli_filters import (
    SearchQueryBuilder,
    is_not_null_token,
    is_null_token,
    split_not_like_prefix,
)
from doc3gpp.models.search import (
    SearchIndexCorruptError,
    SearchIndexStatus,
    SearchQueryError,
    SearchUnavailableError,
)
from doc3gpp.models.spec_doc import SpecDocChunk, SpecDocHit, SpecDocSearchFilters
from doc3gpp.repository.protocols import SpecDocSearchRepository
from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import _SPEC_DOC_SNIPPET_COLUMNS
from doc3gpp.storage.db.fts5_query import normalize_query
from doc3gpp.storage.db.session import get_specdata_engine
from doc3gpp.storage.repositories.spec_doc_sql import SQLAlchemySpecDocRepository

logger = logging.getLogger(__name__)


# FTS5 ``snippet()`` and ``bm25()`` take a 0-based ``cid``. The
# ``spec_doc_search`` virtual table declares 7 columns in this order:
# ``chunk_id`` (UNINDEXED, cid 0), then ``text`` (cid 1) ..
# ``release`` (cid 6). The 6 indexed columns therefore occupy
# cids 1..6.
def _snippet_column_cid(name: str) -> int:
    return _SPEC_DOC_SNIPPET_COLUMNS.index(name) + 1


def _check_fts5(engine: Engine) -> None:
    """Raise :class:`SearchUnavailableError` if FTS5 is not available."""
    if engine.dialect.name != "sqlite":
        raise SearchUnavailableError(
            "spec-doc search requires sqlite FTS5; current dialect is "
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


# FTS5 ``MATCH`` parse failures surface as ``OperationalError`` with a
# message like ``fts5: syntax error near ...``. Distinguish a malformed
# user query (a :class:`SearchQueryError`, exit code 2) from genuine
# index corruption (a :class:`SearchIndexCorruptError`, exit code 3).
_QUERY_ERROR_MARKERS = (
    "syntax error",
    "unable to parse",
    "no such column",
    "unbalanced",
    "unterminated",
)


def _classify_search_error(
    exc: OperationalError,
) -> SearchIndexCorruptError | SearchQueryError:
    """Translate an FTS5 ``OperationalError`` into a domain error."""
    msg = str(exc)
    if any(marker in msg for marker in _QUERY_ERROR_MARKERS):
        return SearchQueryError(msg)
    return SearchIndexCorruptError(msg)


def _fts5_text_filter(
    expr: str, value: str | None, param: str,
) -> tuple[str, Any] | None:
    """Build a rich-grammar filter against an FTS5 column.

    Same fragment shape as
    :func:`doc3gpp.storage.repositories.rich_filters.build_text_filter_sql`,
    but the bound ``LIKE`` pattern runs through
    :func:`normalize_query` first so a dotted filter (``38.331``)
    matches its normalized indexed form (``38_331``) — every FTS5
    column is normalized at index time while the query side is
    normalized by :class:`SearchQueryBuilder`, so the filter side
    must be normalized too. ``null`` / ``not-null`` / ``!`` keep
    their usual meaning; ``None`` is a pass-through.
    """
    if value is None:
        return None
    if is_null_token(value):
        return f"{expr} IS NULL", None
    if is_not_null_token(value):
        return f"{expr} IS NOT NULL", None
    negated, pattern = split_not_like_prefix(value)
    pattern = normalize_query(pattern)
    if negated:
        return f"NOT ({expr} LIKE :{param})", pattern
    return f"{expr} LIKE :{param}", pattern


class SQLAlchemySpecDocSearchRepository(SpecDocSearchRepository):
    """Concrete :class:`SpecDocSearchRepository` backed by FTS5."""

    def __init__(self) -> None:
        self._engine = get_specdata_engine()
        _check_fts5(self._engine)
        settings = get_settings()
        self._weights: tuple[float, ...] = tuple(settings.spec_doc.bm25_weights)
        self._snippet_tokens: int = settings.search.snippet_tokens

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------

    def upsert_for_version(self, spec_id: str, version: str) -> None:
        """Rebuild the FTS5 rows for one ``(spec_id, version)`` pair.

        Reads every chunk via
        :meth:`SQLAlchemySpecDocRepository.list_chunks` (paged, so no
        chunk is silently dropped past the page limit), DELETEs the
        pair's existing FTS5 rows, then INSERTs one row per chunk.
        ``sections`` and ``tables`` store the normalized combined metadata
        fields so their filters and snippets use the same FTS5 columns as
        the chunk contract.
        """
        chunk_repo = SQLAlchemySpecDocRepository()
        chunks: list[SpecDocChunk] = []
        offset = 0
        page_size = 500
        while True:
            batch = chunk_repo.list_chunks(
                spec_id, version=version, limit=page_size, offset=offset
            )
            chunks.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        # FTS5 ``spec_id`` / ``version`` columns hold the
        # ``normalize_query`` form (dots → underscores), so the
        # pair-DELETE must bind the normalized values — raw
        # ``38.331`` would match zero rows and leave stale FTS5 rows
        # behind.
        norm_spec = normalize_query(spec_id)
        norm_version = normalize_query(version)
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM spec_doc_search "
                    "WHERE spec_id = :s AND version = :v"
                ),
                {"s": norm_spec, "v": norm_version},
            )
            for chunk in chunks:
                conn.execute(
                    text(
                        """
                        INSERT INTO spec_doc_search (
                            chunk_id, text, sections, tables,
                            spec_id, version, release
                        ) VALUES (
                            :chunk_id, :text, :sections, :tables,
                            :spec_id, :version, :release
                        )
                        """
                    ),
                    {
                        "chunk_id": chunk.chunk_id,
                        "text": normalize_query(chunk.text or ""),
                        "sections": (
                            normalize_query(chunk.sections)
                            if chunk.sections is not None
                            else None
                        ),
                        "tables": (
                            normalize_query(chunk.tables)
                            if chunk.tables is not None
                            else None
                        ),
                        "spec_id": normalize_query(chunk.spec_id),
                        "version": normalize_query(chunk.version),
                        "release": (
                            normalize_query(chunk.release)
                            if chunk.release is not None
                            else None
                        ),
                    },
                )
            parsed_at = conn.execute(
                text(
                    "SELECT parsed_at FROM spec_doc_sources "
                    "WHERE spec_id = :s AND version = :v"
                ),
                {"s": spec_id, "v": version},
            ).scalar()
            if parsed_at is not None:
                conn.execute(
                    text(
                        """
                        INSERT INTO spec_doc_search_meta (key, value)
                        VALUES ('last_indexed_parsed_at', :v)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """
                    ),
                    {"v": str(parsed_at)},
                )

    def remove_for_version(self, spec_id: str, version: str) -> None:
        """Delete the FTS5 rows for ``(spec_id, version)``. No-op if absent."""
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM spec_doc_search "
                    "WHERE spec_id = :s AND version = :v"
                ),
                {"s": normalize_query(spec_id), "v": normalize_query(version)},
            )

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        filters: SpecDocSearchFilters,
        snippet_tokens: int | None = None,
    ) -> list[SpecDocHit]:
        """Run FTS5 ``MATCH`` + filters + ``bm25()`` scoring.

        Takes the raw user query and builds the ``MATCH`` expression
        via :class:`SearchQueryBuilder` internally, so a
        stopwords-only or empty query raises :class:`SearchQueryError`
        before any SQL runs. One ``snippet()`` per ``weight > 0``
        column; a column surfaces in ``previews`` only when its
        snippet carries a ``<<`` / ``>>`` match.
        """
        match_expr = SearchQueryBuilder(query).build()
        effective_tokens = (
            snippet_tokens if snippet_tokens is not None
            else self._snippet_tokens
        )
        snippet_columns: list[tuple[str, int]] = [
            (name, _snippet_column_cid(name))
            for name, weight in zip(
                _SPEC_DOC_SNIPPET_COLUMNS, self._weights, strict=True
            )
            if weight > 0
        ]
        sql = [
            "SELECT spec_doc_search.chunk_id,",
            "       bm25(spec_doc_search, :w0, :w1, :w2, :w3, :w4, :w5)"
            " AS score,",
        ]
        params: dict[str, Any] = {
            "query": match_expr,
            "tok": effective_tokens,
        }
        for i, weight in enumerate(self._weights):
            params[f"w{i}"] = weight
        for n, (_name, col_idx) in enumerate(snippet_columns):
            param = f"col_{n}"
            sql.append(
                f"       snippet(spec_doc_search, :{param}, '<<', '>>', '…', :tok)"
                f" AS snippet_{n},"
            )
            params[param] = col_idx
        sql.extend([
            "       c.spec_id, c.version, c.release,",
            "       c.sections, c.tables,",
            "       c.chunk_index, c.text",
            "  FROM spec_doc_search",
            "  JOIN spec_doc_chunks c"
            "    ON c.chunk_id = spec_doc_search.chunk_id",
            " WHERE spec_doc_search MATCH :query",
        ])
        for attr, column, param in (
            (filters.spec_id, "spec_doc_search.spec_id", "spec_id"),
            (filters.version, "spec_doc_search.version", "version"),
            (filters.release, "spec_doc_search.release", "release"),
            (filters.sections, "spec_doc_search.sections", "sections"),
            (filters.tables, "spec_doc_search.tables", "tables"),
        ):
            result = _fts5_text_filter(column, attr, param)
            if result is None:
                continue
            clause, bound = result
            if bound is not None:
                params[param] = bound
            sql.append(f"   AND {clause}")
        sql.append(
            " ORDER BY bm25(spec_doc_search, :w0, :w1, :w2, :w3, :w4, :w5)"
            " LIMIT :limit OFFSET :offset"
        )
        params["limit"] = max(filters.limit, 0)
        params["offset"] = max(filters.offset, 0)
        try:
            with self._engine.begin() as conn:
                rows = conn.execute(text("\n".join(sql)), params).all()
        except OperationalError as exc:
            raise _classify_search_error(exc) from exc
        hits: list[SpecDocHit] = []
        base = 2 + len(snippet_columns)
        for row in rows:
            previews: dict[str, str] = {}
            for n, (name, _col_idx) in enumerate(snippet_columns):
                value = row[2 + n]
                if value and "<<" in value and ">>" in value:
                    previews[name] = value
            hits.append(
                SpecDocHit(
                    chunk_id=row[0],
                    spec_id=row[base],
                    version=row[base + 1],
                    release=row[base + 2],
                    sections=row[base + 3],
                    tables=row[base + 4],
                    chunk_index=int(row[base + 5]),
                    text=row[base + 6],
                    score=row[1],
                    previews=previews,
                )
            )
        return hits

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def rebuild_batch(
        self,
        batch_size: int,
        after_id: str | None,
        stale_only: bool,
    ) -> Iterable[list[tuple[str, str]]]:
        """Yield ``(spec_id, version)`` batches from ``spec_doc_sources``.

        ``after_id`` is a ``"spec_id@version"`` resume cursor — pairs
        comparing strictly greater are returned. ``stale_only=True``
        returns only rows whose ``parsed_at`` is newer than the
        ``last_indexed_parsed_at`` watermark stamped by
        :meth:`upsert_for_version`.
        """
        last_id = after_id if after_id is not None else ""
        stale_cutoff: str | None = None
        if stale_only:
            with self._engine.begin() as conn:
                stale_cutoff = conn.execute(
                    text(
                        "SELECT value FROM spec_doc_search_meta "
                        "WHERE key = 'last_indexed_parsed_at'"
                    )
                ).scalar()
            stale_cutoff = str(stale_cutoff or "")
        while True:
            sql = [
                "SELECT spec_id, version FROM spec_doc_sources",
                " WHERE (spec_id || '@' || version) > :last_id",
            ]
            params: dict[str, Any] = {"last_id": last_id, "limit": batch_size}
            if stale_only:
                sql.append(" AND parsed_at > :stale_cutoff")
                params["stale_cutoff"] = stale_cutoff
            sql.append(" ORDER BY spec_id ASC, version ASC LIMIT :limit")
            with self._engine.begin() as conn:
                rows = conn.execute(text("\n".join(sql)), params).all()
            pairs = [(r[0], r[1]) for r in rows]
            if not pairs:
                return
            yield pairs
            last_id = f"{pairs[-1][0]}@{pairs[-1][1]}"

    def count_versions_to_index(
        self, stale_only: bool, after_id: str | None = None,
    ) -> int:
        sql = ["SELECT COUNT(*) FROM spec_doc_sources"]
        clauses: list[str] = []
        params: dict[str, object] = {}
        if after_id is not None:
            clauses.append("(spec_id || '@' || version) > :after_id")
            params["after_id"] = after_id
        if stale_only:
            clauses.append(
                "parsed_at > COALESCE((SELECT value FROM "
                "spec_doc_search_meta WHERE key = 'last_indexed_parsed_at'), '')"
            )
        if clauses:
            sql.append(" WHERE " + " AND ".join(clauses))
        with self._engine.begin() as conn:
            return int(
                conn.execute(text(" ".join(sql)), params).scalar() or 0,
            )

    def get_resume_cursor(self) -> str | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT value FROM spec_doc_search_meta "
                    "WHERE key = 'last_rebuild_last_chunk_id'"
                )
            ).first()
        if row is None or row[0] in (None, ""):
            return None
        return str(row[0])

    def set_resume_cursor(self, cursor: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO spec_doc_search_meta (key, value)
                    VALUES ('last_rebuild_last_chunk_id', :id)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """
                ),
                {"id": cursor},
            )

    def clear_resume_cursor(self) -> None:
        """Remove the resume cursor so the next rebuild starts fresh."""
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM spec_doc_search_meta "
                    "WHERE key = 'last_rebuild_last_chunk_id'"
                ),
            )

    def status(self) -> SearchIndexStatus:
        with self._engine.begin() as conn:
            row_count = int(
                conn.execute(text("SELECT COUNT(*) FROM spec_doc_search")).scalar()
                or 0
            )
            last_rebuild = conn.execute(
                text(
                    "SELECT value FROM spec_doc_search_meta "
                    "WHERE key = 'last_rebuild_at'"
                )
            ).first()
            last_indexed = conn.execute(
                text(
                    "SELECT value FROM spec_doc_search_meta "
                    "WHERE key = 'last_indexed_parsed_at'"
                )
            ).first()
            latest = conn.execute(
                text("SELECT MAX(parsed_at) FROM spec_doc_sources")
            ).first()
        last_rebuild_dt = _parse_iso(last_rebuild[0]) if last_rebuild else None
        last_indexed_dt = _parse_iso(last_indexed[0]) if last_indexed else None
        latest_dt = (
            _parse_iso(str(latest[0])) if latest and latest[0] else None
        )
        is_stale = (
            last_indexed_dt is not None
            and latest_dt is not None
            and latest_dt > last_indexed_dt
        )
        return SearchIndexStatus(
            enabled=True,
            row_count=row_count,
            last_rebuild_at=last_rebuild_dt,
            last_indexed_uploaded_date=last_indexed_dt,
            latest_tdocs_uploaded_date=latest_dt,
            is_stale=is_stale,
        )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
