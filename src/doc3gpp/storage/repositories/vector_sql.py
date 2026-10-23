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
meeting title. Content documents (CR annexes and family members) come
from the four content tables, in indexing order.
"""

from __future__ import annotations

import json as _json
import logging
from collections.abc import Collection, Iterable, Iterator, Sequence
from itertools import groupby

import numpy as np
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine, Row
from sqlalchemy.exc import OperationalError

from doc3gpp.models.search import SearchFilters, SearchIndexStatus, TDocMeta
from doc3gpp.models.semantic_chunking import plan_content_chunks
from doc3gpp.models.semantic_search import (
    EmbeddedChunk,
    VectorIndexUnavailableError,
    parse_content_chunk_id,
)
from doc3gpp.models.tdoc_content_families import TDOC_CONTENT_STORES
from doc3gpp.models.tdoc_document import ordered_sources
from doc3gpp.repository.protocols import VectorIndexRepository
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.compression import decompress_json
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.rich_filters import (
    build_text_filter_or_params,
    build_text_filter_sql,
)

logger = logging.getLogger(__name__)

DEFAULT_DIM = 384
MAX_CHUNKS_PER_TDOC = 32
# vec0 metadata columns cannot store NULL, so a missing value is stored as a
# sentinel: -1 for a meetingless TDoc, '' for a missing TSG or upload date.
_NO_MEETING_ID = -1
_TDOC_METADATA_SELECT = (
    "SELECT t.tdoc_id, t.meeting_id, m.tsg, t.uploaded_date "
    "  FROM tdocs t LEFT JOIN meetings m ON m.meeting_id = t.meeting_id"
)


def _check_sqlite_vec(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        raise VectorIndexUnavailableError(
            f"vector index requires sqlite; current dialect is "
            f"{engine.dialect.name!r}"
        )
    try:
        import sqlite_vec  # noqa: F401 - confirm optional extension is installed
    except ImportError as exc:
        raise VectorIndexUnavailableError(
            "sqlite-vec is not installed; run `pip install doc3gpp[semantic]`"
        ) from exc

    from doc3gpp.storage.backends.sqlite import load_sqlite_vec

    with engine.begin() as conn:
        try:
            load_sqlite_vec(conn.connection.driver_connection)
        except Exception as exc:
            raise VectorIndexUnavailableError(
                f"sqlite-vec extension load failed: {exc}"
            ) from exc


def _like_predicates(filters: SearchFilters) -> tuple[list[str], dict]:
    """Return the LIKE ``AND`` clauses for meeting name/title, release, and spec.

    vec0 rejects LIKE on a metadata column in a KNN query, so these never
    push into the window: :func:`_knn_predicates` joins for them and
    :meth:`SQLAlchemyVectorIndexRepository.matching_tdoc_ids` applies them.
    """
    clauses: list[str] = []
    params: dict = {}
    if filters.meeting:
        # Rich-filter grammar over name OR title: the results
        # page shows m.title, but the CLI convention (meeting
        # list --name, tdoc list text filters) is LIKE on the
        # stored field.
        meeting_clauses, meeting_params = build_text_filter_or_params(
            ("m.name", "m.title"), filters.meeting, param="meeting",
        )
        clauses.append(f"({meeting_clauses})")
        params.update(meeting_params)
    if filters.release:
        clause, bound = build_text_filter_sql("t.release", filters.release, param="release")
        if bound is not None:
            params["release"] = bound
        clauses.append(clause)
    if filters.spec:
        clause, bound = build_text_filter_sql("t.spec", filters.spec, param="spec")
        if bound is not None:
            params["spec"] = bound
        clauses.append(clause)
    return clauses, params


def _filter_predicates(filters: SearchFilters) -> tuple[list[str], dict]:
    """Return the ``AND`` clauses and bound params for ``filters``.

    The clauses reference ``tdocs t`` and ``meetings m``; callers join both
    tables before applying them. ``tdoc_id`` overrides the other predicates.
    """
    clauses: list[str] = []
    params: dict = {}
    if filters.tdoc_id:
        clauses.append("t.tdoc_id = :tdoc_id")
        params["tdoc_id"] = filters.tdoc_id
        return clauses, params
    if filters.tsg:
        # meetings.tsg is stored upper-case by sync, so callers
        # may pass any case (mirrors meeting_sql / wi_sql).
        clauses.append("m.tsg = :tsg")
        params["tsg"] = filters.tsg.upper()
    if filters.meeting_id is not None:
        clauses.append("m.meeting_id = :meeting_id")
        params["meeting_id"] = filters.meeting_id
    if filters.since:
        clauses.append("t.uploaded_date >= :since")
        params["since"] = filters.since
    if filters.until:
        clauses.append("t.uploaded_date <= :until")
        params["until"] = filters.until
    like_clauses, like_params = _like_predicates(filters)
    clauses.extend(like_clauses)
    params.update(like_params)
    return clauses, params


def _knn_predicates(filters: SearchFilters) -> tuple[list[str], dict, bool]:
    """Return the knn ``AND`` clauses, bound params, and whether to join ``tdocs``/``meetings``.

    Pushed clauses name ``vec_tdoc_embeddings`` metadata columns, so vec0
    applies them before ``k``. ``meeting_id >= 0`` drops meetingless TDocs,
    which are stored as ``-1``. A date range also requires
    ``uploaded_date != ''`` because a missing date is stored as ``''``.
    ``tdoc_id`` overrides the other predicates, as in :func:`_filter_predicates`.
    """
    vec = "vec_tdoc_embeddings"
    clauses = [f"{vec}.meeting_id >= 0"]
    params: dict = {}
    if filters.tdoc_id:
        clauses.append(f"{vec}.tdoc_id = :tdoc_id")
        params["tdoc_id"] = filters.tdoc_id
        return clauses, params, False
    if filters.tsg:
        clauses.append(f"{vec}.tsg = :tsg")
        params["tsg"] = filters.tsg.upper()
    if filters.meeting_id is not None:
        clauses.append(f"{vec}.meeting_id = :meeting_id")
        params["meeting_id"] = filters.meeting_id
    if filters.since or filters.until:
        clauses.append(f"{vec}.uploaded_date != ''")
    if filters.since:
        clauses.append(f"{vec}.uploaded_date >= :since")
        params["since"] = filters.since
    if filters.until:
        clauses.append(f"{vec}.uploaded_date <= :until")
        params["until"] = filters.until
    like_clauses, like_params = _like_predicates(filters)
    clauses.extend(like_clauses)
    params.update(like_params)
    return clauses, params, bool(like_clauses)


def _vec_metadata(
    meeting_id: int | None, tsg: str | None, uploaded_date: str | None,
) -> tuple[int, str, str]:
    """Return ``(meeting_id, tsg, uploaded_date)`` as vec0 stores them, sentinels for NULL."""
    return (
        _NO_MEETING_ID if meeting_id is None else int(meeting_id),
        tsg or "",
        "" if uploaded_date is None else str(uploaded_date),
    )


class SQLAlchemyVectorIndexRepository(VectorIndexRepository):
    def __init__(
        self,
        expected_model: str | None = None,
        expected_dim: int | None = None,
    ) -> None:
        self._engine = get_engine()
        _check_sqlite_vec(self._engine)
        self._dim, self._stored_model = self._read_or_init_dim_and_model()
        self._expected_model = expected_model
        self._expected_dim = expected_dim
        self._chunk_settings = get_settings().semantic_search

    def _read_or_init_dim_and_model(self) -> tuple[int, str | None]:
        with self._engine.begin() as conn:
            try:
                dim_row = conn.execute(
                    text("SELECT value FROM vec_meta WHERE key = 'embedding_dim'")
                ).scalar()
                model_row = conn.execute(
                    text("SELECT value FROM vec_meta WHERE key = 'embedding_model'")
                ).scalar()
                if dim_row is None:
                    conn.execute(
                        text(
                            "INSERT INTO vec_meta (key, value) VALUES ('embedding_dim', :d)"
                        ),
                        {"d": str(DEFAULT_DIM)},
                    )
                    return DEFAULT_DIM, model_row
                return int(dim_row), model_row
            except OperationalError as exc:
                raise VectorIndexUnavailableError(
                    "vector schema is not initialized; run `doc3gpp db init` "
                    f"or `doc3gpp db reset` first: {exc}"
                ) from exc

    def _check_compatible(self, dim: int, *, what: str) -> None:
        if self._expected_dim is not None and self._expected_dim != self._dim:
            raise VectorIndexUnavailableError(
                f"vector dim mismatch: stored={self._dim} "
                f"expected={self._expected_dim}; run "
                f"`doc3gpp tdoc index --rebuild-embeddings`"
            )
        if dim != self._dim:
            prefix = "query" if what == "query" else "vector"
            raise VectorIndexUnavailableError(
                f"{prefix} dim mismatch: stored={self._dim} "
                f"requested={dim}; run "
                f"`doc3gpp tdoc index --rebuild-embeddings`"
            )
        if self._expected_model is not None and self._stored_model != self._expected_model:
            raise VectorIndexUnavailableError(
                f"vector model mismatch: stored={self._stored_model!r} "
                f"expected={self._expected_model!r}; run "
                f"`doc3gpp tdoc index --rebuild-embeddings`"
            )

    def verify_compatible(self, dim: int, model: str | None) -> None:
        """Raise on live-vs-stored dim/model mismatch without writing.

        Resume-path guard for
        :meth:`SemanticSearchService.rebuild_embeddings`: a resumed
        rebuild must not silently mix rows from two different models
        (or dims) into one ``vec0`` table. ``model=None`` (duck-typed
        embedders without a ``model_name``) skips the model check.
        """
        if dim != self._dim:
            raise VectorIndexUnavailableError(
                f"vector dim mismatch: stored={self._dim} "
                f"requested={dim}; run "
                f"`doc3gpp tdoc index --rebuild-embeddings`"
            )
        if model is not None and self._stored_model != model:
            raise VectorIndexUnavailableError(
                f"vector model mismatch: stored={self._stored_model!r} "
                f"expected={model!r}; run "
                f"`doc3gpp tdoc index --rebuild-embeddings`"
            )

    def keeps_index(self, dim: int, model: str | None) -> bool:
        """Return True when the ``vec0`` table exists at ``dim`` and ``model``.

        The non-raising form of :meth:`verify_compatible`, used by a
        ``stale_only`` rebuild to keep the existing rows. A missing table or
        ``model=None`` (duck-typed embedders) returns False, so the caller
        falls back to :meth:`reset_for_rebuild`.
        """
        if model is None:
            return False
        try:
            self.verify_compatible(dim, model)
        except VectorIndexUnavailableError:
            return False
        with self._engine.begin() as conn:
            return conn.execute(
                text(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'vec_tdoc_embeddings'"
                )
            ).first() is not None

    def reset_for_rebuild(self, dim: int, model: str | None) -> None:
        """Drop + recreate ``vec0`` at ``dim`` and stamp ``vec_meta``.

        A dimension change requires rebuilding the virtual table because
        its dimension is fixed at CREATE time. Refreshes cached dimension
        and model metadata after recreating the index.
        """
        width = int(dim)
        try:
            import sqlite_vec  # noqa: F401 - confirm optional extension is installed
        except ImportError as exc:
            raise VectorIndexUnavailableError(
                "sqlite-vec is not installed; run `pip install doc3gpp[semantic]`"
            ) from exc
        from doc3gpp.storage.backends.sqlite import load_sqlite_vec

        with self._engine.begin() as conn:
            try:
                load_sqlite_vec(conn.connection.driver_connection)
            except Exception as exc:
                raise VectorIndexUnavailableError(
                    f"sqlite-vec extension load failed: {exc}"
                ) from exc
            conn.execute(text("DROP TABLE IF EXISTS vec_tdoc_embeddings"))
            conn.execute(
                text(
                    "CREATE VIRTUAL TABLE vec_tdoc_embeddings USING vec0(\n"
                    "    chunk_id TEXT PRIMARY KEY,\n"
                    "    tdoc_id TEXT,\n"
                    "    chunk_index INTEGER,\n"
                    "    meeting_id INTEGER,\n"
                    "    tsg TEXT,\n"
                    "    uploaded_date TEXT,\n"
                    f"    embedding FLOAT[{width}] distance_metric=cosine\n"
                    ")"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO vec_meta (key, value) VALUES ('embedding_dim', :v) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                ),
                {"v": str(width)},
            )
            if model is None:
                conn.execute(text("DELETE FROM vec_meta WHERE key = 'embedding_model'"))
            else:
                conn.execute(
                    text(
                        "INSERT INTO vec_meta (key, value) VALUES ('embedding_model', :v) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                    ),
                    {"v": model},
                )
        self._dim = width
        self._stored_model = model

    def _check_dim(self, embeddings: list[np.ndarray]) -> None:
        for v in embeddings:
            self._check_compatible(v.shape[-1], what="upsert")

    def upsert_chunks(self, tdoc_id: str, chunks: Sequence[EmbeddedChunk]) -> None:
        self._check_dim([chunk.embedding for chunk in chunks])
        # Note: binds raw float32 bytes via sqlite-vec's BLOB conversion; equivalent to vec_bit(:emb).
        with self._engine.begin() as conn:
            conn.execute(
                text("DELETE FROM vec_tdoc_embeddings WHERE tdoc_id = :id"),
                {"id": tdoc_id},
            )
            row = conn.execute(
                text(_TDOC_METADATA_SELECT + " WHERE t.tdoc_id = :id"),
                {"id": tdoc_id},
            ).first()
            meeting_id, tsg, uploaded_date = _vec_metadata(
                *(row[1:] if row is not None else (None, None, None)),
            )
            for chunk in chunks:
                conn.execute(
                    text(
                        "INSERT INTO vec_tdoc_embeddings "
                        "(chunk_id, tdoc_id, chunk_index, meeting_id, tsg, uploaded_date, embedding) "
                        "VALUES (:cid, :tid, :ci, :mid, :tsg, :udate, :emb)"
                    ),
                    {
                        "cid": chunk.chunk_id,
                        "tid": tdoc_id,
                        "ci": chunk.chunk_index,
                        "mid": meeting_id,
                        "tsg": tsg,
                        "udate": uploaded_date,
                        "emb": np.asarray(chunk.embedding, dtype=np.float32).tobytes(),
                    },
                )

    def content_signatures(self) -> dict[str, frozenset[tuple[str, str]]]:
        return self._indexed_state()[0]

    def _indexed_state(
        self,
    ) -> tuple[dict[str, frozenset[tuple[str, str]]], dict[str, set[tuple[int, str, str]]]]:
        """Return each TDoc's indexed content signatures and stored metadata in one scan."""
        with self._engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT tdoc_id, chunk_id, meeting_id, tsg, uploaded_date "
                    "FROM vec_tdoc_embeddings"
                )
            ).all()
        signatures: dict[str, set[tuple[str, str]]] = {}
        metadata: dict[str, set[tuple[int, str, str]]] = {}
        for tdoc_id, chunk_id, meeting_id, tsg, uploaded_date in rows:
            metadata.setdefault(tdoc_id, set()).add((meeting_id, tsg, uploaded_date))
            signature = parse_content_chunk_id(chunk_id)
            if signature is not None:
                signatures.setdefault(tdoc_id, set()).add(signature)
        return (
            {tdoc_id: frozenset(values) for tdoc_id, values in signatures.items()},
            metadata,
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
        self._check_compatible(int(query_vec.shape[-1]), what="query")
        sql = [
            (
                "SELECT chunk_id, vec_tdoc_embeddings.tdoc_id AS tdoc_id, "
                "chunk_index, distance"
            ),
            "  FROM vec_tdoc_embeddings",
        ]
        params: dict = {
            "q": np.asarray(query_vec, dtype=np.float32).tobytes(),
            "k": limit,
        }
        clauses: list[str] = []
        if filters is not None:
            clauses, filter_params, needs_join = _knn_predicates(filters)
            params.update(filter_params)
            if needs_join:
                sql.append("  JOIN tdocs t   ON t.tdoc_id = vec_tdoc_embeddings.tdoc_id")
                sql.append("  JOIN meetings m ON t.meeting_id = m.meeting_id")
        sql.append(" WHERE embedding MATCH :q AND k = :k")
        sql.extend(f"   AND {clause}" for clause in clauses)
        # Note: binds raw float32 bytes via sqlite-vec's BLOB conversion; equivalent to vec_bit(:q).
        sql.append("  ORDER BY distance IS NULL, distance ASC, chunk_id ASC")
        with self._engine.begin() as conn:
            rows = conn.execute(text("\n".join(sql)), params).all()
        return [(r[1], r[0], int(r[2]), float(r[3]) if r[3] is not None else float("inf")) for r in rows]

    def matching_tdoc_ids(
        self, tdoc_ids: Collection[str], filters: SearchFilters,
    ) -> frozenset[str]:
        ids = list(tdoc_ids)
        if not ids:
            return frozenset()
        clauses, params = _filter_predicates(filters)
        sql = [
            "SELECT t.tdoc_id",
            "  FROM tdocs t",
            "  JOIN meetings m ON t.meeting_id = m.meeting_id",
            " WHERE t.tdoc_id IN :tdoc_ids",
        ]
        sql.extend(f"   AND {clause}" for clause in clauses)
        stmt = text("\n".join(sql)).bindparams(bindparam("tdoc_ids", expanding=True))
        with self._engine.begin() as conn:
            rows = conn.execute(stmt, {**params, "tdoc_ids": ids}).all()
        return frozenset(row[0] for row in rows)

    def rebuild_batch(
        self, batch_size: int, after_id: str | None, stale_only: bool,
    ) -> Iterable[list[str]]:
        if batch_size < 1:
            return
        last_id = after_id if after_id is not None else ""
        if stale_only:
            stale_ids = [tdoc_id for tdoc_id in self._stale_tdoc_ids() if tdoc_id > last_id]
            for start in range(0, len(stale_ids), batch_size):
                yield stale_ids[start:start + batch_size]
            return
        while True:
            params: dict[str, object] = {"last_id": last_id, "limit": batch_size}
            with self._engine.begin() as conn:
                rows = conn.execute(
                    text(
                        "SELECT tdoc_id FROM tdocs WHERE tdoc_id > :last_id "
                        "ORDER BY tdoc_id ASC LIMIT :limit"
                    ),
                    params,
                ).all()
            ids = [r[0] for r in rows]
            if not ids:
                return
            yield ids
            last_id = ids[-1]

    def count_tdocs_to_index(
        self, stale_only: bool, after_id: str | None = None,
    ) -> int:
        if stale_only:
            return sum(
                1 for tdoc_id in self._stale_tdoc_ids()
                if after_id is None or tdoc_id > after_id
            )
        sql = ["SELECT COUNT(*) FROM tdocs"]
        params: dict[str, object] = {}
        if after_id is not None:
            sql.append(" WHERE tdoc_id > :after_id")
            params["after_id"] = after_id
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
        when the operator runs ``tdoc index --rebuild-embeddings``
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
            stored_dim = conn.execute(
                text("SELECT value FROM vec_meta WHERE key='embedding_dim'")
            ).scalar()
            stored_model = conn.execute(
                text("SELECT value FROM vec_meta WHERE key='embedding_model'")
            ).scalar()
        date_stale = bool(latest and (not last_indexed or str(latest) > last_indexed))
        is_stale = date_stale or bool(self._stale_content_tdoc_ids())
        from datetime import datetime as _dt
        return SearchIndexStatus(
            enabled=True,
            row_count=row_count,
            last_rebuild_at=_dt.fromisoformat(last_rebuild) if last_rebuild else None,
            last_indexed_uploaded_date=_dt.fromisoformat(last_indexed) if last_indexed else None,
            latest_tdocs_uploaded_date=_dt.fromisoformat(str(latest)) if latest else None,
            is_stale=is_stale,
            embedding_dim=int(stored_dim) if stored_dim is not None else None,
            embedding_model=stored_model,
        )

    def _stale_tdoc_ids(self) -> list[str]:
        """Return sorted ``tdoc_id``s that are uploaded-date stale or content stale."""
        with self._engine.begin() as conn:
            date_stale = conn.execute(
                text(
                    "SELECT tdoc_id FROM tdocs WHERE uploaded_date > ("
                    " SELECT value FROM vec_meta "
                    " WHERE key = 'last_indexed_uploaded_date')"
                )
            ).scalars().all()
        return sorted(set(date_stale) | self._stale_content_tdoc_ids())

    def _stale_content_tdoc_ids(self) -> set[str]:
        """Return ``tdoc_id``s whose indexed content or metadata differs from SQL.

        Metadata is copied into the vec table at index time and no hook updates
        it, so a TDoc whose stored ``meeting_id`` / ``tsg`` / ``uploaded_date``
        no longer matches its ``tdocs`` and ``meetings`` rows is stale. A TDoc with
        undecodable stored content is always stale, so one corrupt row
        is reported for its own TDoc and does not abort the other TDocs.
        """
        expected = self._expected_content_signatures()
        actual, indexed_metadata = self._indexed_state()
        with self._engine.begin() as conn:
            rows = conn.execute(text(_TDOC_METADATA_SELECT)).all()
        stale: set[str] = set()
        for tdoc_id, meeting_id, tsg, uploaded_date in rows:
            signature = expected.get(tdoc_id, frozenset())
            if signature is None or signature != actual.get(tdoc_id, frozenset()):
                stale.add(tdoc_id)
                continue
            stored = indexed_metadata.get(tdoc_id)
            if stored is not None and stored != {_vec_metadata(meeting_id, tsg, uploaded_date)}:
                stale.add(tdoc_id)
        return stale

    def _expected_content_signatures(
        self,
    ) -> dict[str, frozenset[tuple[str, str]] | None]:
        """Return the content signatures a fresh index holds, per TDoc.

        Uses the same plan as :meth:`SemanticSearchService.index_for_tdoc`, so a
        blank document or one past the content cap has no expected signature.
        A ``None`` value marks a TDoc whose stored content cannot be decoded.
        """
        settings = self._chunk_settings
        expected: dict[str, frozenset[tuple[str, str]] | None] = {}
        with self._engine.begin() as conn:
            rows = conn.execute(text(_CONTENT_ROWS_SQL))
            for tdoc_id, group in groupby(rows, key=lambda row: row.tdoc_id):
                try:
                    planned = plan_content_chunks(
                        _ordered_documents(list(group)),
                        settings.chunk_size,
                        settings.chunk_overlap,
                        settings.max_content_chunks_per_tdoc,
                    )
                    expected[tdoc_id] = frozenset(
                        (document_id, content_hash)
                        for document_id, content_hash, _chunk_texts in planned
                    )
                except UndecodableContentError:
                    expected[tdoc_id] = None
        return expected

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
                    "       m.title AS meeting, m.tsg, t.uploaded_date,"
                    "       t.type, t.status "
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
                        type=row[7],
                        status=row[8],
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
        self._check_compatible(int(q.shape[-1]), what="query")
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


def _content_rows_sql(*, single_tdoc: bool) -> str:
    where = " WHERE e.tdoc_id = :tdoc_id" if single_tdoc else ""
    selects = [
        "SELECT e.tdoc_id AS tdoc_id, c.ftp_url AS ftp_url, "
        "e.doc_filename AS main, c.doc_filename AS source_file, "
        "c.document_id AS document_id, c.content_hash AS content_hash, "
        "c.content AS content "
        f"FROM {store.table_name} c "
        f"JOIN tdoc_extracts e ON e.ftp_url = c.ftp_url{where}"
        for store in TDOC_CONTENT_STORES
    ]
    return "\nUNION ALL\n".join(selects) + "\nORDER BY tdoc_id, ftp_url, source_file"


_CONTENT_ROWS_SQL = _content_rows_sql(single_tdoc=False)
_CONTENT_ROWS_FOR_TDOC_SQL = _content_rows_sql(single_tdoc=True)


class UndecodableContentError(ValueError):
    """A stored content row cannot be decoded into Markdown."""


def _ordered_documents(rows: Sequence[Row]) -> Iterator[tuple[str, str, str]]:
    """Yield ``(document_id, content_hash, markdown)`` in indexing order.

    Rows are grouped by ZIP URL. Each URL's main document comes first and the
    other sources follow in path order. The CR main is never a row, so a CR
    yields its annexes in path order.
    """
    by_url: dict[str, dict[str, Row]] = {}
    mains: dict[str, str] = {}
    for row in rows:
        by_url.setdefault(row.ftp_url, {})[row.source_file] = row
        mains[row.ftp_url] = row.main
    for ftp_url, rows_by_source in by_url.items():
        for source in ordered_sources(tuple(rows_by_source), main=mains[ftp_url]):
            row = rows_by_source[source]
            markdown = decompress_json(row.content)
            if markdown is None:
                raise UndecodableContentError(
                    f"stored content for {source!r} cannot be decoded"
                )
            yield row.document_id, row.content_hash, markdown


def _build_content_documents(tdoc_id: str) -> list[tuple[str, str, str]]:
    """Return one TDoc's stored content documents in indexing order."""
    with get_engine().begin() as conn:
        rows = conn.execute(
            text(_CONTENT_ROWS_FOR_TDOC_SQL), {"tdoc_id": tdoc_id},
        ).all()
    return list(_ordered_documents(rows))
