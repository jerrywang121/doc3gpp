"""SQLAlchemy implementation of :class:`SpecDocVectorRepository`.

Owns the ``vec_spec_doc_embeddings`` virtual table (sqlite-vec
``vec0``) + meta sidecar (``vec_spec_doc_meta``) created by
:func:`doc3gpp.storage.db.migrate._create_specdata_vector_schema`. At
construction time it probes for sqlite-vec availability — raising
:class:`VectorIndexUnavailableError` on non-sqlite or sqlite-vec-less
builds.

Mirrors :mod:`doc3gpp.storage.repositories.vector_sql` minus the
``tdocs`` / ``meetings`` JOINs: one ``(spec_id, version)`` pair maps
to N chunk rows (``chunk_id = "{spec_id}@{version}#{index}"``), and
the optional section/release filters join back to
``spec_doc_chunks``. Every database connection used by this repo
loads the sqlite-vec extension (via the shared engine's
``connect`` listener when available) so KNN queries work on any
checked-out connection.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import numpy as np
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from doc3gpp.models.search import SearchIndexStatus
from doc3gpp.models.semantic_search import VectorIndexUnavailableError
from doc3gpp.repository.protocols import SpecDocVectorRepository
from doc3gpp.storage.db.session import get_specdata_engine
from doc3gpp.storage.repositories.rich_filters import build_text_filter_sql

if TYPE_CHECKING:
    from doc3gpp.models.spec_doc import SpecDocSearchFilters

logger = logging.getLogger(__name__)
_FAILED_PAIR_PREFIX = "failed_pair:"


def _ensure_vec_loaded(dbapi_connection) -> None:
    """Load sqlite-vec into a raw DBAPI connection.

    Idempotent: ``sqlite_vec.load`` re-registers the module functions
    on every new connection (the tdoc ``vector_sql`` sibling relies on
    the same call). Raises
    :class:`VectorIndexUnavailableError` when the extension is
    missing or fails to load so callers degrade gracefully.
    """
    try:
        from doc3gpp.storage.backends.sqlite import load_sqlite_vec

        load_sqlite_vec(dbapi_connection)
    except ImportError as exc:
        raise VectorIndexUnavailableError(
            "sqlite-vec is not installed; run `pip install doc3gpp[semantic]`"
        ) from exc
    except Exception as exc:
        raise VectorIndexUnavailableError(
            f"sqlite-vec extension load failed: {exc}"
        ) from exc


def _connect_vec(engine: Engine):
    """Return a context manager yielding a sqlite-vec-loaded connection."""
    return _VecConnection(engine)


class _VecConnection:
    """Wrap ``engine.begin()`` with a per-checkout ``sqlite_vec.load``.

    sqlite-vec registers its SQL functions per DBAPI connection, so
    every checkout needs the load before any ``vec0`` / KNN statement
    runs. A global ``connect`` listener (see
    :func:`_load_vec_on_connect`) covers future checkouts; this
    wrapper covers the current one directly.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._cm = None
        self._conn = None

    def __enter__(self):
        self._cm = self._engine.begin()
        self._conn = self._cm.__enter__()
        raw = self._conn.connection.driver_connection
        _ensure_vec_loaded(raw)
        return self._conn

    def __exit__(self, *exc_info):
        try:
            return self._cm.__exit__(*exc_info)
        finally:
            self._cm = None
            self._conn = None


def _check_sqlite_vec(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        raise VectorIndexUnavailableError(
            "spec-doc vector index requires sqlite; current dialect is "
            f"{engine.dialect.name!r}"
        )
    with engine.begin() as conn:
        raw = conn.connection.driver_connection
        _ensure_vec_loaded(raw)


class SQLAlchemySpecDocVectorRepository(SpecDocVectorRepository):
    """Concrete :class:`SpecDocVectorRepository` backed by sqlite-vec."""

    def __init__(
        self,
        expected_model: str | None = None,
        expected_dim: int | None = None,
    ) -> None:
        self._engine = get_specdata_engine()
        _check_sqlite_vec(self._engine)
        # Listen once per engine so every future checkout (any repo
        # method, any pooled connection) has sqlite-vec loaded before
        # the first vec0/KNN statement. ``event.contains`` keeps
        # repeated constructions from stacking duplicate listeners.
        if not event.contains(self._engine, "connect", _load_vec_on_connect):
            event.listen(self._engine, "connect", _load_vec_on_connect)
        self._dim, self._stored_model = self._read_or_init_dim_and_model()
        self._expected_model = expected_model
        self._expected_dim = expected_dim

    def _read_or_init_dim_and_model(self) -> tuple[int, str | None]:
        from sqlalchemy.exc import OperationalError

        with _VecConnection(self._engine) as conn:
            try:
                dim_row = conn.execute(
                    text(
                        "SELECT value FROM vec_spec_doc_meta "
                        "WHERE key = 'embedding_dim'"
                    )
                ).scalar()
                model_row = conn.execute(
                    text(
                        "SELECT value FROM vec_spec_doc_meta "
                        "WHERE key = 'embedding_model'"
                    )
                ).scalar()
                if dim_row is None:
                    conn.execute(
                        text(
                            "INSERT INTO vec_spec_doc_meta (key, value) "
                            "VALUES ('embedding_dim', :d)"
                        ),
                        {"d": "384"},
                    )
                    return 384, model_row
                return int(dim_row), model_row
            except OperationalError as exc:
                raise VectorIndexUnavailableError(
                    "spec-doc vector schema is not initialized; run "
                    f"`doc3gpp db init` first: {exc}"
                ) from exc

    def _check_compatible(self, dim: int, *, what: str) -> None:
        if self._expected_dim is not None and self._expected_dim != self._dim:
            raise VectorIndexUnavailableError(
                f"vector dim mismatch: stored={self._dim} "
                f"expected={self._expected_dim}; run "
                f"`doc3gpp spec doc index --rebuild-embeddings`"
            )
        if dim != self._dim:
            prefix = "query" if what == "query" else "vector"
            raise VectorIndexUnavailableError(
                f"{prefix} dim mismatch: stored={self._dim} "
                f"requested={dim}; run "
                f"`doc3gpp spec doc index --rebuild-embeddings`"
            )
        if self._expected_model is not None and self._stored_model != self._expected_model:
            raise VectorIndexUnavailableError(
                f"vector model mismatch: stored={self._stored_model!r} "
                f"expected={self._expected_model!r}; run "
                f"`doc3gpp spec doc index --rebuild-embeddings`"
            )

    def verify_compatible(self, dim: int, model: str | None) -> None:
        """Raise on live-vs-stored dim/model mismatch without writing."""
        if dim != self._dim:
            raise VectorIndexUnavailableError(
                f"vector dim mismatch: stored={self._dim} "
                f"requested={dim}; run "
                f"`doc3gpp spec doc index --rebuild-embeddings`"
            )
        if model is not None and self._stored_model != model:
            raise VectorIndexUnavailableError(
                f"vector model mismatch: stored={self._stored_model!r} "
                f"expected={model!r}; run "
                f"`doc3gpp spec doc index --rebuild-embeddings`"
            )

    def reset_for_rebuild(self, dim: int, model: str | None) -> None:
        """Drop + recreate ``vec_spec_doc_embeddings`` at ``dim``.

        Mirrors
        :meth:`SQLAlchemyVectorIndexRepository.reset_for_rebuild` on
        the specdata engine: the ``vec0`` dimension is fixed at
        ``CREATE VIRTUAL TABLE`` time, so a dim change requires a
        rebuild of the table itself. Stamps ``embedding_dim`` +
        ``embedding_model`` in ``vec_spec_doc_meta``.
        """
        width = int(dim)
        with _VecConnection(self._engine) as conn:
            conn.execute(text("DROP TABLE IF EXISTS vec_spec_doc_embeddings"))
            conn.execute(
                text(
                    "CREATE VIRTUAL TABLE vec_spec_doc_embeddings USING vec0(\n"
                    "    chunk_id TEXT PRIMARY KEY,\n"
                    "    spec_id TEXT,\n"
                    "    version TEXT,\n"
                    "    chunk_index INTEGER,\n"
                    f"    embedding FLOAT[{width}] distance_metric=cosine\n"
                    ")"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO vec_spec_doc_meta (key, value) "
                    "VALUES ('embedding_dim', :v) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                ),
                {"v": str(width)},
            )
            if model is None:
                conn.execute(
                    text(
                        "DELETE FROM vec_spec_doc_meta "
                        "WHERE key = 'embedding_model'"
                    ),
                )
            else:
                conn.execute(
                    text(
                        "INSERT INTO vec_spec_doc_meta (key, value) "
                        "VALUES ('embedding_model', :v) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                    ),
                    {"v": model},
                )
            conn.execute(
                text(
                    "DELETE FROM vec_spec_doc_meta "
                    "WHERE key = 'last_rebuild_last_chunk_id' "
                    "OR key = 'last_indexed_parsed_at' "
                    "OR key LIKE :prefix"
                ),
                {"prefix": f"{_FAILED_PAIR_PREFIX}%"},
            )
        self._dim = width
        self._stored_model = model

    def upsert_for_version(
        self, spec_id: str, version: str, embeddings: list[np.ndarray],
    ) -> None:
        """Replace all chunk rows for ``(spec_id, version)``.

        Deletes the pair's rows then inserts one row per embedding
        with ``chunk_id = "{spec_id}@{version}#{index}"`` (the Task 7
        shape, so the FTS5 side and the vector side agree on ids).
        """
        for vec in embeddings:
            self._check_compatible(int(vec.shape[-1]), what="upsert")
        with _VecConnection(self._engine) as conn:
            conn.execute(
                text(
                    "DELETE FROM vec_spec_doc_embeddings "
                    "WHERE spec_id = :s AND version = :v"
                ),
                {"s": spec_id, "v": version},
            )
            for i, vec in enumerate(embeddings):
                conn.execute(
                    text(
                        "INSERT INTO vec_spec_doc_embeddings "
                        "(chunk_id, spec_id, version, chunk_index, embedding) "
                        "VALUES (:cid, :sid, :ver, :ci, :emb)"
                    ),
                    {
                        "cid": f"{spec_id}@{version}#{i}",
                        "sid": spec_id,
                        "ver": version,
                        "ci": i,
                        "emb": np.asarray(vec, dtype=np.float32).tobytes(),
                    },
                )
            self._stamp_indexed_parsed_at(conn, spec_id, version)

    def remove_for_version(self, spec_id: str, version: str) -> None:
        """Delete all chunk rows for ``(spec_id, version)``. No-op if absent."""
        with _VecConnection(self._engine) as conn:
            conn.execute(
                text(
                    "DELETE FROM vec_spec_doc_embeddings "
                    "WHERE spec_id = :s AND version = :v"
                ),
                {"s": spec_id, "v": version},
            )
            self._stamp_indexed_parsed_at(conn, spec_id, version)

    def rebuild_batch(
        self,
        batch_size: int,
        after_id: str | None,
        stale_only: bool,
    ) -> Iterable[list[tuple[str, str]]]:
        """Yield batches of parsed source-version pairs in cursor order."""
        last_id = after_id if after_id is not None else ""
        failed_after = ""
        stale_cutoff = None
        if stale_only:
            with _VecConnection(self._engine) as conn:
                stale_cutoff = conn.execute(
                    text(
                        "SELECT value FROM vec_spec_doc_meta "
                        "WHERE key = 'last_indexed_parsed_at'"
                    )
                ).scalar()
            stale_cutoff = str(stale_cutoff or "")
        while True:
            sql = [
                "SELECT spec_id, version FROM spec_doc_sources",
                " WHERE parsed_at IS NOT NULL",
            ]
            params: dict[str, object] = {"limit": batch_size}
            if failed_after is not None:
                sql.append("   AND (spec_id || '@' || version) > :failed_after")
                sql.append(
                    "   AND EXISTS (SELECT 1 FROM vec_spec_doc_meta m "
                    "                 WHERE m.key = 'failed_pair:' || spec_doc_sources.spec_id "
                    "                   || '@' || spec_doc_sources.version)"
                )
                params["failed_after"] = failed_after
            else:
                sql.append("   AND (spec_id || '@' || version) > :last_id")
                sql.append(
                    "   AND NOT EXISTS (SELECT 1 FROM vec_spec_doc_meta m "
                    "                     WHERE m.key = 'failed_pair:' || spec_doc_sources.spec_id "
                    "                       || '@' || spec_doc_sources.version)"
                )
                params["last_id"] = last_id
                if stale_only:
                    sql.append("   AND parsed_at > :stale_cutoff")
                    params["stale_cutoff"] = stale_cutoff
            sql.append(" ORDER BY spec_id ASC, version ASC LIMIT :limit")
            with _VecConnection(self._engine) as conn:
                rows = conn.execute(text("\n".join(sql)), params).all()
            pairs = [(row[0], row[1]) for row in rows]
            if not pairs:
                if failed_after is not None:
                    failed_after = None
                    continue
                return
            yield pairs
            if failed_after is not None:
                failed_after = f"{pairs[-1][0]}@{pairs[-1][1]}"
            else:
                last_id = f"{pairs[-1][0]}@{pairs[-1][1]}"

    def count_versions_to_index(
        self, stale_only: bool, after_id: str | None = None
    ) -> int:
        """Count parsed source-version pairs selected by rebuild filters."""
        params: dict[str, object] = {}
        normal_clauses = [
            "parsed_at IS NOT NULL",
            (
                "NOT EXISTS (SELECT 1 FROM vec_spec_doc_meta m "
                "WHERE m.key = 'failed_pair:' || spec_doc_sources.spec_id "
                "  || '@' || spec_doc_sources.version)"
            ),
        ]
        if after_id is not None:
            normal_clauses.append("(spec_id || '@' || version) > :after_id")
            params["after_id"] = after_id
        if stale_only:
            normal_clauses.append(
                "parsed_at > COALESCE((SELECT value FROM vec_spec_doc_meta "
                "WHERE key = 'last_indexed_parsed_at'), '')"
            )
        normal_sql = (
            "SELECT COUNT(*) FROM spec_doc_sources WHERE "
            + " AND ".join(normal_clauses)
        )
        failed_sql = (
            "SELECT COUNT(*) FROM spec_doc_sources WHERE parsed_at IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM vec_spec_doc_meta m "
            "WHERE m.key = 'failed_pair:' || spec_doc_sources.spec_id "
            "  || '@' || spec_doc_sources.version)"
        )
        with _VecConnection(self._engine) as conn:
            normal_count = int(
                conn.execute(text(normal_sql), params).scalar() or 0
            )
            failed_count = int(conn.execute(text(failed_sql)).scalar() or 0)
        return normal_count + failed_count

    def get_resume_cursor(self) -> str | None:
        with _VecConnection(self._engine) as conn:
            value = conn.execute(
                text(
                    "SELECT value FROM vec_spec_doc_meta "
                    "WHERE key = 'last_rebuild_last_chunk_id'"
                )
            ).scalar()
        return str(value) if value not in (None, "") else None

    def set_resume_cursor(self, cursor: str) -> None:
        with _VecConnection(self._engine) as conn:
            conn.execute(
                text(
                    "INSERT INTO vec_spec_doc_meta (key, value) "
                    "VALUES ('last_rebuild_last_chunk_id', :v) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                ),
                {"v": cursor},
            )

    def clear_resume_cursor(self) -> None:
        with _VecConnection(self._engine) as conn:
            conn.execute(
                text(
                    "DELETE FROM vec_spec_doc_meta "
                    "WHERE key = 'last_rebuild_last_chunk_id'"
                )
            )

    def record_rebuild_failure(self, spec_id: str, version: str) -> None:
        with _VecConnection(self._engine) as conn:
            conn.execute(
                text(
                    "INSERT INTO vec_spec_doc_meta (key, value) "
                    "VALUES (:key, :value) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                ),
                {
                    "key": f"{_FAILED_PAIR_PREFIX}{spec_id}@{version}",
                    "value": "failed",
                },
            )

    def clear_rebuild_failure(self, spec_id: str, version: str) -> None:
        with _VecConnection(self._engine) as conn:
            conn.execute(
                text("DELETE FROM vec_spec_doc_meta WHERE key = :key"),
                {"key": f"{_FAILED_PAIR_PREFIX}{spec_id}@{version}"},
            )

    def touch_rebuild_at(self) -> None:
        with _VecConnection(self._engine) as conn:
            conn.execute(
                text(
                    "INSERT INTO vec_spec_doc_meta (key, value) "
                    "VALUES ('last_rebuild_at', :ts) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                ),
                {
                    "ts": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    )
                },
            )

    def status(self) -> SearchIndexStatus:
        with _VecConnection(self._engine) as conn:
            row_count = int(
                conn.execute(
                    text("SELECT COUNT(*) FROM vec_spec_doc_embeddings")
                ).scalar()
                or 0
            )
            last_rebuild = conn.execute(
                text(
                    "SELECT value FROM vec_spec_doc_meta "
                    "WHERE key = 'last_rebuild_at'"
                )
            ).scalar()
            last_indexed = conn.execute(
                text(
                    "SELECT value FROM vec_spec_doc_meta "
                    "WHERE key = 'last_indexed_parsed_at'"
                )
            ).scalar()
            latest = conn.execute(
                text(
                    "SELECT MAX(parsed_at) FROM spec_doc_sources "
                    "WHERE parsed_at IS NOT NULL"
                )
            ).scalar()
            failed = conn.execute(
                text(
                    "SELECT 1 FROM vec_spec_doc_meta m "
                    "JOIN spec_doc_sources s ON m.key = 'failed_pair:' || s.spec_id "
                    "  || '@' || s.version "
                    "WHERE m.key LIKE :prefix AND s.parsed_at IS NOT NULL LIMIT 1"
                ),
                {"prefix": f"{_FAILED_PAIR_PREFIX}%"},
            ).first()
            stored_dim = conn.execute(
                text(
                    "SELECT value FROM vec_spec_doc_meta "
                    "WHERE key = 'embedding_dim'"
                )
            ).scalar()
            stored_model = conn.execute(
                text(
                    "SELECT value FROM vec_spec_doc_meta "
                    "WHERE key = 'embedding_model'"
                )
            ).scalar()
        last_rebuild_dt = _parse_iso(last_rebuild)
        last_indexed_dt = _parse_iso(last_indexed)
        latest_dt = _parse_iso(latest)
        return SearchIndexStatus(
            enabled=True,
            row_count=row_count,
            last_rebuild_at=last_rebuild_dt,
            last_indexed_uploaded_date=last_indexed_dt,
            latest_tdocs_uploaded_date=latest_dt,
            is_stale=bool(
                latest_dt is not None
                and (last_indexed_dt is None or latest_dt > last_indexed_dt)
            ) or failed is not None,
            embedding_dim=int(stored_dim) if stored_dim is not None else None,
            embedding_model=stored_model,
        )

    def _stamp_indexed_parsed_at(self, conn, spec_id: str, version: str) -> None:
        parsed_at = conn.execute(
            text(
                "SELECT parsed_at FROM spec_doc_sources "
                "WHERE spec_id = :s AND version = :v"
            ),
            {"s": spec_id, "v": version},
        ).scalar()
        if parsed_at is None:
            return
        current = conn.execute(
            text(
                "SELECT value FROM vec_spec_doc_meta "
                "WHERE key = 'last_indexed_parsed_at'"
            )
        ).scalar()
        current_dt = _parse_iso(current)
        parsed_dt = _parse_iso(parsed_at)
        if current_dt is not None and parsed_dt is not None and parsed_dt <= current_dt:
            return
        conn.execute(
            text(
                "INSERT INTO vec_spec_doc_meta (key, value) "
                "VALUES ('last_indexed_parsed_at', :v) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            ),
            {"v": str(parsed_at)},
        )

    def knn(
        self,
        query_vec: np.ndarray,
        limit: int,
        filters: SpecDocSearchFilters | None = None,
    ) -> list[tuple[str, float]]:
        """KNN by cosine distance; returns ``(chunk_id, distance)``.

        The chunk index is recoverable from the ``chunk_id`` suffix
        (``{spec_id}@{version}#{index}``). Optional ``filters`` narrow
        by ``spec_id`` / ``version`` on the vec table and by
        ``release`` / ``sections`` / ``tables`` via a JOIN to
        ``spec_doc_chunks``.
        """
        q = np.asarray(query_vec, dtype=np.float32)
        self._check_compatible(int(q.shape[-1]), what="query")
        sql = [
            "SELECT v.chunk_id, v.distance AS distance",
            "  FROM vec_spec_doc_embeddings v",
        ]
        params: dict = {
            "q": q.tobytes(),
            "k": max(limit, 0),
        }
        join_chunks = bool(
            filters is not None
            and (
                filters.release is not None
                or filters.sections is not None
                or filters.tables is not None
            )
        )
        if join_chunks:
            sql.append(
                "  JOIN spec_doc_chunks c ON c.chunk_id = v.chunk_id"
            )
        clauses: list[str] = []
        if filters is not None:
            if filters.spec_id is not None:
                clauses.append("v.spec_id = :spec_id")
                params["spec_id"] = filters.spec_id
            if filters.version is not None:
                clauses.append("v.version = :version")
                params["version"] = filters.version
            if filters.release is not None:
                clauses.append("c.release = :release")
                params["release"] = filters.release
            for value, column, param in (
                (filters.sections, "c.sections", "sections"),
                (filters.tables, "c.tables", "tables"),
            ):
                result = build_text_filter_sql(column, value, param=param)
                if result is None:
                    continue
                clause, bound = result
                clauses.append(clause)
                if bound is not None:
                    params[param] = bound
        where_tail = " AND ".join(["embedding MATCH :q", "k = :k", *clauses])
        sql.append(f" WHERE {where_tail}")
        sql.append("  ORDER BY distance IS NULL, distance ASC, v.chunk_id ASC")
        with _VecConnection(self._engine) as conn:
            rows = conn.execute(text("\n".join(sql)), params).all()
        return [
            (r[0], float(r[1]) if r[1] is not None else float("inf"))
            for r in rows
        ]


def _load_vec_on_connect(dbapi_connection, _connection_record) -> None:
    """Engine ``connect`` listener: sqlite-vec on every checkout.

    Registered once per specdata engine by
    :class:`SQLAlchemySpecDocVectorRepository.__init__` (guarded by
    ``event.contains``). Mirrors the best-effort load in
    :func:`doc3gpp.storage.db.migrate._create_specdata_vector_schema`.
    """
    _ensure_vec_loaded(dbapi_connection)


def _parse_iso(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
