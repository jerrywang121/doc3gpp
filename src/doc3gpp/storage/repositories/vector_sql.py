"""SQLAlchemy implementation of :class:`VectorIndexRepository`.

Owns the ``vec_tdoc_embeddings`` virtual table (sqlite-vec ``vec0``)
+ meta sidecar (``vec_meta``) created by
:func:`doc3gpp.storage.db.migrate._create_vector_schema`. At
construction time it probes for sqlite-vec availability — raising
:class:`VectorIndexUnavailableError` on non-sqlite or sqlite-vec-less
builds so :func:`doc3gpp.services.factory.build_semantic_search_service`
can catch it once at startup.

Embed text is built by a small fan of SQL queries against ``tdocs``,
``meetings``, ``wis``, and the three sidecar tables, with the gzip JSON
blobs decompressed in Python via
:func:`doc3gpp.storage.compression.decompress_json`. The ``wis`` join
is a comma-separated LIKE match on ``tdocs.related_wis``; matched
``wis.name`` rows are joined with ``; `` and appended after the
meeting title.
"""

from __future__ import annotations

import json as _json
import logging
from collections.abc import Iterable, Sequence

import numpy as np
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from doc3gpp.models.search import SearchFilters, SearchIndexStatus, TDocMeta
from doc3gpp.models.semantic_search import VectorIndexUnavailableError
from doc3gpp.repository.protocols import VectorIndexRepository
from doc3gpp.storage.compression import decompress_json
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.rich_filters import (
    build_text_filter_or_params,
    build_text_filter_sql,
)

logger = logging.getLogger(__name__)

DEFAULT_DIM = 384
MAX_CHUNKS_PER_TDOC = 32


def _check_sqlite_vec(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        raise VectorIndexUnavailableError(
            f"vector index requires sqlite; current dialect is "
            f"{engine.dialect.name!r}"
        )
    try:
        import sqlite_vec
    except ImportError as exc:
        raise VectorIndexUnavailableError(
            "sqlite-vec is not installed; run `pip install doc3gpp[semantic]`"
        ) from exc
    with engine.begin() as conn:
        try:
            import sqlite_vec
            sqlite_vec.load(conn.connection.driver_connection)
        except Exception as exc:
            raise VectorIndexUnavailableError(
                f"sqlite-vec extension load failed: {exc}"
            ) from exc


class SQLAlchemyVectorIndexRepository(VectorIndexRepository):
    def __init__(self) -> None:
        self._engine = get_engine()
        _check_sqlite_vec(self._engine)
        self._dim = self._read_or_init_dim()

    def _read_or_init_dim(self) -> int:
        with self._engine.begin() as conn:
            try:
                row = conn.execute(
                    text("SELECT value FROM vec_meta WHERE key = 'embedding_dim'")
                ).scalar()
                if row is None:
                    conn.execute(
                        text(
                            "INSERT INTO vec_meta (key, value) VALUES ('embedding_dim', :d)"
                        ),
                        {"d": str(DEFAULT_DIM)},
                    )
                    return DEFAULT_DIM
                return int(row)
            except OperationalError as exc:
                raise VectorIndexUnavailableError(
                    "vector schema is not initialized; run `doc3gpp db init` "
                    f"or `doc3gpp db reset` first: {exc}"
                ) from exc

    def _check_dim(self, embeddings: list[np.ndarray]) -> None:
        for v in embeddings:
            if v.shape[-1] != self._dim:
                raise VectorIndexUnavailableError(
                    f"vector dim mismatch: stored={self._dim} "
                    f"requested={v.shape[-1]}; run "
                    f"`doc3gpp search index --rebuild-embeddings`"
                )

    def upsert_chunks(self, tdoc_id: str, embeddings: list[np.ndarray]) -> None:
        self._check_dim(embeddings)
        # Note: binds raw float32 bytes via sqlite-vec's BLOB conversion; equivalent to vec_bit(:emb).
        with self._engine.begin() as conn:
            conn.execute(
                text("DELETE FROM vec_tdoc_embeddings WHERE tdoc_id = :id"),
                {"id": tdoc_id},
            )
            for i, vec in enumerate(embeddings):
                conn.execute(
                    text(
                        "INSERT INTO vec_tdoc_embeddings "
                        "(chunk_id, tdoc_id, chunk_index, embedding) "
                        "VALUES (:cid, :tid, :ci, :emb)"
                    ),
                    {
                        "cid": f"{tdoc_id}#{i}",
                        "tid": tdoc_id,
                        "ci": i,
                        "emb": np.asarray(vec, dtype=np.float32).tobytes(),
                    },
                )

    def remove_for_tdoc(self, tdoc_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text("DELETE FROM vec_tdoc_embeddings WHERE tdoc_id = :id"),
                {"id": tdoc_id},
            )

    def knn(
        self, query_vec: np.ndarray, limit: int,
        filters: SearchFilters | None = None,
    ) -> list[tuple[str, str, int, float]]:
        if query_vec.shape[-1] != self._dim:
            raise VectorIndexUnavailableError(
                f"query dim mismatch: stored={self._dim} "
                f"requested={query_vec.shape[-1]}"
            )
        sql = [
            "SELECT chunk_id, vec_tdoc_embeddings.tdoc_id AS tdoc_id, "
            "chunk_index, distance",
            "  FROM vec_tdoc_embeddings",
        ]
        params: dict = {
            "q": np.asarray(query_vec, dtype=np.float32).tobytes(),
            "k": limit,
        }
        if filters is not None:
            sql.append("  JOIN tdocs t   ON t.tdoc_id = vec_tdoc_embeddings.tdoc_id")
            sql.append("  JOIN meetings m ON t.meeting_id = m.meeting_id")
            if filters.tdoc_id:
                sql.append("   AND t.tdoc_id = :tdoc_id")
                params["tdoc_id"] = filters.tdoc_id
            else:
                if filters.tsg:
                    # meetings.tsg is stored upper-case by sync, so callers
                    # may pass any case (mirrors meeting_sql / wi_sql).
                    sql.append("   AND m.tsg = :tsg")
                    params["tsg"] = filters.tsg.upper()
                if filters.meeting:
                    # Rich-filter grammar over name OR title: the results
                    # page shows m.title, but the CLI convention (meeting
                    # list --name, tdoc list text filters) is LIKE on the
                    # stored field.
                    meeting_clauses, meeting_params = build_text_filter_or_params(
                        ("m.name", "m.title"), filters.meeting, param="meeting",
                    )
                    sql.append(f"   AND ({meeting_clauses})")
                    params.update(meeting_params)
                if filters.meeting_id is not None:
                    sql.append("   AND m.meeting_id = :meeting_id")
                    params["meeting_id"] = filters.meeting_id
                if filters.release:
                    clause, bound = build_text_filter_sql("t.release", filters.release, param="release")
                    if bound is not None:
                        params["release"] = bound
                    sql.append(f"   AND {clause}")
                if filters.spec:
                    clause, bound = build_text_filter_sql("t.spec", filters.spec, param="spec")
                    if bound is not None:
                        params["spec"] = bound
                    sql.append(f"   AND {clause}")
                if filters.since:
                    sql.append("   AND t.uploaded_date >= :since")
                    params["since"] = filters.since
                if filters.until:
                    sql.append("   AND t.uploaded_date <= :until")
                    params["until"] = filters.until
        sql.append(" WHERE embedding MATCH :q AND k = :k")
        # Note: binds raw float32 bytes via sqlite-vec's BLOB conversion; equivalent to vec_bit(:q).
        sql.append("  ORDER BY distance IS NULL, distance ASC, chunk_id ASC")
        with self._engine.begin() as conn:
            rows = conn.execute(text("\n".join(sql)), params).all()
        return [(r[1], r[0], int(r[2]), float(r[3]) if r[3] is not None else float("inf")) for r in rows]

    def rebuild_batch(
        self, batch_size: int, after_id: str | None, stale_only: bool,
    ) -> Iterable[list[str]]:
        last_id = after_id if after_id is not None else ""
        while True:
            sql = ["SELECT tdoc_id FROM tdocs WHERE tdoc_id > :last_id"]
            params: dict[str, object] = {"last_id": last_id, "limit": batch_size}
            if stale_only:
                sql.append(
                    " AND uploaded_date > ("
                    " SELECT value FROM vec_meta "
                    " WHERE key = 'last_indexed_uploaded_date')"
                )
            sql.append(" ORDER BY tdoc_id ASC LIMIT :limit")
            with self._engine.begin() as conn:
                rows = conn.execute(text(" ".join(sql)), params).all()
            ids = [r[0] for r in rows]
            if not ids:
                return
            yield ids
            last_id = ids[-1]

    def count_tdocs_to_index(
        self, stale_only: bool, after_id: str | None = None,
    ) -> int:
        sql = ["SELECT COUNT(*) FROM tdocs"]
        params: dict[str, object] = {}
        clauses: list[str] = []
        if after_id is not None:
            clauses.append("tdoc_id > :after_id")
            params["after_id"] = after_id
        if stale_only:
            clauses.append(
                "uploaded_date > ("
                "SELECT value FROM vec_meta "
                " WHERE key = 'last_indexed_uploaded_date')"
            )
        if clauses:
            sql.append(" WHERE " + " AND ".join(clauses))
        with self._engine.begin() as conn:
            return int(
                conn.execute(text(" ".join(sql)), params).scalar() or 0,
            )

    def get_resume_cursor(self) -> str | None:
        with self._engine.begin() as conn:
            return conn.execute(
                text("SELECT value FROM vec_meta WHERE key='last_rebuild_last_tdoc_id'")
            ).scalar()

    def set_resume_cursor(self, tdoc_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO vec_meta (key, value) VALUES "
                    "('last_rebuild_last_tdoc_id', :v) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                ),
                {"v": tdoc_id},
            )

    def clear_resume_cursor(self) -> None:
        """Remove the resume cursor from ``vec_meta``.

        Called by :meth:`SemanticSearchService.rebuild_embeddings`
        when the operator runs ``search index --rebuild-embeddings``
        without ``--resume`` to force a fresh start from the very
        first TDoc. Mirrors
        :meth:`SQLAlchemySearchIndexRepository.clear_resume_cursor`.
        """
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM vec_meta "
                    "WHERE key = 'last_rebuild_last_tdoc_id'"
                ),
            )

    def status(self) -> SearchIndexStatus:
        with self._engine.begin() as conn:
            row_count = int(
                conn.execute(text("SELECT COUNT(*) FROM vec_tdoc_embeddings")).scalar() or 0
            )
            last_rebuild = conn.execute(
                text("SELECT value FROM vec_meta WHERE key='last_rebuild_at'")
            ).scalar()
            last_indexed = conn.execute(
                text("SELECT value FROM vec_meta WHERE key='last_indexed_uploaded_date'")
            ).scalar()
            latest = conn.execute(
                text("SELECT MAX(uploaded_date) FROM tdocs")
            ).scalar()
        from datetime import datetime as _dt
        return SearchIndexStatus(
            enabled=True,
            row_count=row_count,
            last_rebuild_at=_dt.fromisoformat(last_rebuild) if last_rebuild else None,
            last_indexed_uploaded_date=_dt.fromisoformat(last_indexed) if last_indexed else None,
            latest_tdocs_uploaded_date=_dt.fromisoformat(str(latest)) if latest else None,
            is_stale=bool(latest and (not last_indexed or str(latest) > last_indexed)),
        )

    def get_tdocs_metadata(
        self, tdoc_ids: list[str],
    ) -> dict[str, TDocMeta]:
        """Batch-fetch ``tdocs`` + ``meetings`` metadata.

        Returns an empty dict when ``tdoc_ids`` is empty or no row
        matches. Unknown tdoc_ids are silently omitted (they may
        have been deleted between index and query). TDoc ids are
        bound individually as named parameters — no string
        interpolation, so the call is SQL-injection safe.
        """
        out: dict[str, TDocMeta] = {}
        if not tdoc_ids:
            return out
        # Build ``(t.id_0, t.id_1, ...) = (:id_0, :id_1, ...)``
        # placeholders. SQLite allows ~999 bound params by default
        # so chunk the list defensively.
        chunk_size = 500
        with self._engine.begin() as conn:
            for start in range(0, len(tdoc_ids), chunk_size):
                chunk = tdoc_ids[start:start + chunk_size]
                placeholders = ", ".join(f":id_{i}" for i in range(len(chunk)))
                sql = (
                    "SELECT t.tdoc_id, t.title, t.ftp_url, t.related_wis AS wis,"
                    "       m.title AS meeting, m.tsg, t.uploaded_date "
                    "  FROM tdocs t "
                    "  LEFT JOIN meetings m ON t.meeting_id = m.meeting_id "
                    f" WHERE t.tdoc_id IN ({placeholders})"
                )
                params = {f"id_{i}": v for i, v in enumerate(chunk)}
                for row in conn.execute(text(sql), params):
                    tdoc_id = row[0]
                    out[tdoc_id] = TDocMeta(
                        tdoc_id=tdoc_id,
                        title=row[1] or "",
                        ftp_url=row[2],
                        wis=row[3],
                        meeting=row[4],
                        tsg=row[5],
                        uploaded_date=row[6],
                    )
        return out

    def get_min_distance_for_tdocs(
        self,
        tdoc_ids: Sequence[str],
        query_vec: Sequence[float],
    ) -> dict[str, tuple[float, str] | None]:
        if not tdoc_ids:
            return {}
        q = np.asarray(query_vec, dtype=np.float32)
        if q.shape[-1] != self._dim:
            raise VectorIndexUnavailableError(
                f"query dim mismatch: stored={self._dim} "
                f"requested={q.shape[-1]}"
            )
        # sqlite-vec KNN: per-row ``distance`` column, K=1 per tdoc_id.
        # We request K = number of distinct chunks for the asked tdoc_ids,
        # then group-by in Python (cheaper than a CTE, fewer sqlite-vec
        # version dependencies). Bounded to len(tdoc_ids) * 32 chunks
        # worst case; the common case is << that.
        from sqlalchemy import bindparam, text
        sql = [
            "SELECT tdoc_id, chunk_id, distance",
            "  FROM vec_tdoc_embeddings",
            " WHERE tdoc_id IN :tdoc_ids",
            "   AND embedding MATCH :q",
            "   AND k = :k",
            " ORDER BY distance IS NULL, distance ASC, chunk_id ASC",
        ]
        params: dict = {
            "tdoc_ids": tuple(tdoc_ids),
            "q": q.tobytes(),
            "k": len(tdoc_ids) * MAX_CHUNKS_PER_TDOC,
        }
        stmt = text("\n".join(sql)).bindparams(
            bindparam("tdoc_ids", expanding=True),
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt, params).all()
        # Pick the closest chunk per tdoc_id. Initialize all asked
        # ids to None so the caller can distinguish "no rows" from
        # "rows with inf distance" (sqlite-vec returns NULL when
        # the row is far enough to be effectively irrelevant).
        out: dict[str, tuple[float, str] | None] = {tid: None for tid in tdoc_ids}
        for tdoc_id, chunk_id, distance in rows:
            if out[tdoc_id] is not None:
                continue
            if distance is None:
                continue
            out[tdoc_id] = (float(distance), chunk_id)
        return out


def _build_embed_text(tdoc_id: str) -> str | None:
    """Build the concatenated embed text for ``tdoc_id``.

    Fans out a small set of SQL queries against ``tdocs``,
    ``meetings``, ``wis``, and the three sidecar tables, decompresses
    the gzip JSON blobs in Python, and concatenates the text fields
    with `` :: `` separator. Returns ``None`` when the ``tdoc_id`` is
    absent from ``tdocs``.

    The ``wis`` lookup joins ``tdocs.related_wis`` (a comma-separated
    string of acronyms) against ``wis.acronym`` via ``LIKE``; matched
    ``wis.name`` rows are joined with ``; `` and appended after the
    meeting title.
    """
    from doc3gpp.storage.db.session import get_engine as _ge
    engine = _ge()
    with engine.begin() as conn:
        tdoc = conn.execute(
            text("SELECT title FROM tdocs WHERE tdoc_id = :id"), {"id": tdoc_id},
        ).first()
        if tdoc is None:
            return None
        parts: list[str] = [tdoc[0] or ""]
        mtg = conn.execute(
            text(
                "SELECT m.title FROM meetings m "
                "JOIN tdocs t ON t.meeting_id = m.meeting_id "
                "WHERE t.tdoc_id = :id"
            ),
            {"id": tdoc_id},
        ).first()
        if mtg and mtg[0]:
            parts.append(mtg[0])
        wis_names = conn.execute(
            text(
                "SELECT w.name FROM wis w, tdocs t "
                "WHERE t.tdoc_id = :id "
                "  AND t.related_wis IS NOT NULL "
                "  AND (',' || t.related_wis || ',') LIKE "
                "      ('%,' || w.acronym || ',%')"
            ),
            {"id": tdoc_id},
        ).all()
        if wis_names:
            parts.append("; ".join(row[0] for row in wis_names if row[0]))
        cover = conn.execute(
            text(
                "SELECT title, summary_of_change "
                "FROM tdoc_cr_cover_page WHERE tdoc_id = :id"
            ),
            {"id": tdoc_id},
        ).first()
        if cover is not None:
            if cover[0]:
                parts.append(cover[0])
            if cover[1]:
                parts.append(cover[1])
        ls = conn.execute(
            text(
                """
                SELECT ls.title, ls.response_to_title, ls.to_groups,
                       ls.cc_groups
                  FROM tdoc_cr_ls_details ls
                  JOIN tdocs t ON t.tdoc_id = ls.tdoc_id
                 WHERE t.tdoc_id = :id
                   AND t.ftp_url IS NOT NULL
                   AND ls.ftp_url = t.ftp_url
                """
            ),
            {"id": tdoc_id},
        ).first()
        if ls is not None:
            ls_parts: list[str] = []
            if ls[0]:
                ls_parts.append(ls[0])
            if ls[1]:
                ls_parts.append(ls[1])
            if ls[2]:
                ls_parts.append(ls[2].replace("\n", " "))
            if ls[3]:
                ls_parts.append(ls[3].replace("\n", " "))
            parts.extend(ls_parts)
        ttcn = conn.execute(
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
        if ttcn is not None:
            flat = " ".join(str(v) for v in ttcn[:6] if v is not None)
            if flat:
                parts.append(flat)
            decoded = decompress_json(ttcn[6])
            if decoded is not None:
                parts.append(str(decoded)[:2000])
            if ttcn[7]:
                parts.append(ttcn[7].replace("\n", " "))
        changes = conn.execute(
            text(
                "SELECT clauses, changes FROM tdoc_cr_change_details "
                "WHERE tdoc_id = :id"
            ),
            {"id": tdoc_id},
        ).first()
        if changes is not None:
            if changes[0]:
                parts.append(changes[0].replace("\n", " "))
            decoded = decompress_json(changes[1])
            if isinstance(decoded, list):
                for c in decoded:
                    parts.append(_json.dumps(c, ensure_ascii=False))
            elif decoded is not None:
                parts.append(str(decoded)[:2000])
    return " :: ".join(p for p in parts if p)
