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

from sqlalchemy import text
from sqlalchemy.engine import Engine

from doc3gpp.models.search import (
    SearchFilters,
    SearchHit,
    SearchIndexStatus,
)
from doc3gpp.repository.protocols import SearchIndexRepository
from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import _SNIPPET_COLUMN_NAMES
from doc3gpp.storage.compression import decompress_json
from doc3gpp.storage.db.fts5_query import normalize_query
from doc3gpp.storage.db.session import get_engine

logger = logging.getLogger(__name__)


# FTS5 ``snippet()`` and ``bm25()`` take a 0-based ``cid``. The
# ``tdoc_search`` virtual table declares 9 columns in this order:
# ``tdoc_id`` (UNINDEXED, cid 0), then ``title`` (cid 1) ..
# ``ttcn_text`` (cid 8). The 8 indexed columns therefore occupy
# cids 1..8.
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
from doc3gpp.models.search import SearchUnavailableError  # noqa: E402


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
        text_payload = self._build_index_text(tdoc_id)
        if text_payload is None:
            self.remove(tdoc_id)
            return
        with self._engine.begin() as conn:
            conn.execute(
                text("DELETE FROM tdoc_search WHERE tdoc_id = :id"),
                {"id": tdoc_id},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO tdoc_search (
                        tdoc_id, title, ftp_url, meeting_title,
                        meeting_location, wis, cover_text,
                        change_text, ttcn_text
                    ) VALUES (
                        :tdoc_id, :title, :ftp_url, :meeting_title,
                        :meeting_location, :wis, :cover_text,
                        :change_text, :ttcn_text
                    )
                    """
                ),
                {"tdoc_id": tdoc_id, **text_payload},
            )
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
        """Run the FTS5 ``MATCH`` + filters + scoring + rerank.

        ``snippet_tokens`` is an optional per-call override for the
        ``Settings.search.snippet_tokens`` cached value. When ``None``
        (the default) the cached value is used; when provided, it
        is forwarded to the ``snippet(...)`` call so the CLI's
        ``--snippet-tokens`` flag can retune the preview length for
        a single invocation without mutating settings.

        The SELECT emits one ``snippet(tdoc_search, col_idx, ...)``
        per column whose ``bm25_weights[i] > 0`` so each weight>0
        column surfaces its own highlighted span in the result
        ``previews`` mapping. Weight=0 columns are skipped entirely
        (no extra snippet() call, no placeholder).
        """
        effective_tokens = (
            snippet_tokens if snippet_tokens is not None
            else self._snippet_tokens
        )
        # Identify the weight>0 columns and bind a ``:col_i_<n>``
        # parameter to each so the SQL is fully parameterised
        # (sqlite sees ``?`` placeholders for the column index).
        snippet_columns: list[tuple[str, int]] = [
            (name, _snippet_column_cid(name))
            for name, weight in zip(
                _SNIPPET_COLUMN_NAMES, self._weights, strict=True
            )
            if weight > 0
        ]
        sql = [
            "SELECT tdoc_search.tdoc_id,",
            "       bm25(tdoc_search, :w0, :w1, :w2, :w3, :w4, :w5, :w6, :w7)"
            " AS score,",
        ]
        params: dict[str, Any] = {
            "query": query,
            "tok": effective_tokens,
        }
        for i, weight in enumerate(self._weights):
            params[f"w{i}"] = weight
        # One snippet() per weight>0 column. Bind each col idx as a
        # named param so the order is independent of the dict
        # iteration order.
        for n, (_name, col_idx) in enumerate(snippet_columns):
            param = f"col_{n}"
            sql.append(
                f"       snippet(tdoc_search, :{param}, '<<', '>>', '…', :tok)"
                f" AS snippet_{n},"
            )
            params[param] = col_idx
        sql.extend([
            "       t.title, m.title AS meeting, m.tsg AS tsg,",
            "       t.uploaded_date,",
            "       tdoc_search.ftp_url, tdoc_search.wis",
            "  FROM tdoc_search",
            "  JOIN tdocs t   ON t.tdoc_id = tdoc_search.tdoc_id",
            "  JOIN meetings m ON t.meeting_id = m.meeting_id",
            " WHERE tdoc_search MATCH :query",
        ])
        if filters.tsg:
            sql.append("   AND m.tsg = :tsg")
            params["tsg"] = filters.tsg
        if filters.meeting:
            sql.append("   AND m.name = :meeting")
            params["meeting"] = filters.meeting
        if filters.meeting_id is not None:
            sql.append("   AND m.meeting_id = :meeting_id")
            params["meeting_id"] = filters.meeting_id
        if filters.tdoc_id:
            sql.append("   AND t.tdoc_id = :tdoc_id")
            params["tdoc_id"] = filters.tdoc_id
        if filters.release:
            sql.append("   AND t.release = :release")
            params["release"] = filters.release
        if filters.spec:
            sql.append("   AND t.spec = :spec")
            params["spec"] = filters.spec
        if filters.since:
            sql.append("   AND t.uploaded_date >= :since")
            params["since"] = filters.since
        if filters.until:
            sql.append("   AND t.uploaded_date <= :until")
            params["until"] = filters.until
        sql.append(
            " ORDER BY bm25(tdoc_search, :w0, :w1, :w2, :w3, :w4, "
            ":w5, :w6, :w7) LIMIT :limit"
        )
        params["limit"] = max(filters.limit, 0)
        with self._engine.begin() as conn:
            rows = conn.execute(text("\n".join(sql)), params).all()
        hits: list[SearchHit] = []
        for row in rows:
            previews: dict[str, str] = {}
            for n, (name, _col_idx) in enumerate(snippet_columns):
                value = row[2 + n]
                # A column belongs in ``previews`` only when its
                # FTS5 ``snippet()`` actually surfaced a match —
                # the contract is ``weight>0 AND matching snippet``.
                # FTS5 returns the closest context for the column
                # even when the column itself has no match; the
                # only reliable signal of a real match is the
                # presence of the snippet markers (``<<`` and
                # ``>>``) emitted around the matched tokens.
                if value and "<<" in value and ">>" in value:
                    previews[name] = value
            hits.append(
                SearchHit(
                    tdoc_id=row[0],
                    score=row[1],
                    previews=previews,
                    title=row[2 + len(snippet_columns)],
                    meeting=row[3 + len(snippet_columns)],
                    tsg=row[4 + len(snippet_columns)],
                    uploaded_date=row[5 + len(snippet_columns)],
                    ftp_url=row[6 + len(snippet_columns)],
                    wis=row[7 + len(snippet_columns)],
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
    ) -> Iterable[list[str]]:
        last_id = after_id if after_id is not None else ""
        while True:
            sql = [
                "SELECT tdoc_id FROM tdocs",
                " WHERE tdoc_id > :last_id",
                " ORDER BY tdoc_id ASC LIMIT :limit",
            ]
            params: dict[str, Any] = {"last_id": last_id, "limit": batch_size}
            if stale_only:
                sql.append(" AND uploaded_date > COALESCE((")
                sql.append(
                    "   SELECT value FROM tdoc_search_meta "
                    "   WHERE key = 'last_indexed_uploaded_date'"
                    " ), '')"
                )
            with self._engine.begin() as conn:
                rows = conn.execute(text("\n".join(sql)), params).all()
            ids = [r[0] for r in rows]
            if not ids:
                return
            yield ids
            last_id = ids[-1]

    def count_tdocs_to_index(
        self, stale_only: bool, after_id: str | None = None,
    ) -> int:
        sql = ["SELECT COUNT(*) FROM tdocs"]
        clauses: list[str] = []
        params: dict[str, object] = {}
        if after_id is not None:
            clauses.append("tdoc_id > :after_id")
            params["after_id"] = after_id
        if stale_only:
            clauses.append(
                "uploaded_date > COALESCE((SELECT value FROM "
                "tdoc_search_meta WHERE key = 'last_indexed_uploaded_date'), '')"
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

    def status(self) -> SearchIndexStatus:
        with self._engine.begin() as conn:
            row_count = int(
                conn.execute(text("SELECT COUNT(*) FROM tdoc_search")).scalar() or 0
            )
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

    # ------------------------------------------------------------------
    # Internal: build concatenated text for one tdoc_id
    # ------------------------------------------------------------------

    def _build_index_text(self, tdoc_id: str) -> dict[str, str] | None:
        """Return the column→text mapping for ``tdoc_id``.

        Runs a single JOIN across ``tdocs`` + ``meetings`` + the
        three sidecars and decompresses the gzip JSON blobs in
        Python. Returns ``None`` when no ``tdocs`` row exists (the
        caller translates that into a :meth:`remove`).
        """
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
        title, ftp_url, related_wis, meeting_title, meeting_location = row

        wis_text = _wis_to_text(related_wis)
        cover_text = _cover_text(conn=self._engine, tdoc_id=tdoc_id)
        change_text = _change_text(conn=self._engine, tdoc_id=tdoc_id)
        ttcn_text = _ttcn_text(conn=self._engine, tdoc_id=tdoc_id)

        return {
            "title": normalize_query(title or ""),
            "ftp_url": ftp_url or "",
            "meeting_title": meeting_title or "",
            "meeting_location": meeting_location or "",
            "wis": wis_text,
            "cover_text": normalize_query(cover_text),
            "change_text": normalize_query(change_text),
            "ttcn_text": normalize_query(ttcn_text),
        }


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
    return " ".join([flat, req, cf])


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
