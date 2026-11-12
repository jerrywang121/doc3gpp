"""SQLite sqlite-vec repository for ETSI LISTSERV archives on the main DB."""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Sequence
from datetime import datetime, timezone

import numpy as np
from sqlalchemy import bindparam, event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError

from doc3gpp.models.listserv import ListservFilters
from doc3gpp.models.search import MatchedChunk
from doc3gpp.models.semantic_chunking import chunking_fingerprint
from doc3gpp.models.semantic_search import (
    EmbeddedChunk,
    VectorCandidate,
    VectorIndexUnavailableError,
)
from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import SemanticSearchSettings
from doc3gpp.storage.db.session import ensure_sqlite_read_transaction, get_engine
from doc3gpp.storage.repositories.listserv_search_sql import _build_filter_clauses

DEFAULT_DIM = 384
_BATCH_SIZE = 250


def _check_sqlite_vec(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        raise VectorIndexUnavailableError(
            "LISTSERV vector index requires sqlite; current dialect is "
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

    if not event.contains(engine, "checkout", _load_vec_on_checkout):
        event.listen(engine, "checkout", _load_vec_on_checkout)


def _load_vec_on_checkout(dbapi_connection, _record, _proxy) -> None:
    from doc3gpp.storage.backends.sqlite import load_sqlite_vec

    try:
        dbapi_connection.execute("SELECT vec_version()").fetchone()
    except sqlite3.OperationalError:
        load_sqlite_vec(dbapi_connection)


def _ensure_read_transaction(conn: Connection) -> None:
    ensure_sqlite_read_transaction(conn)


class SQLAlchemyListservVectorRepository:
    """sqlite-vec index and search repository for ETSI LISTSERV messages on the main DB."""

    def __init__(
        self,
        expected_model: str | None = None,
        expected_dim: int | None = None,
        chunk_settings: SemanticSearchSettings | None = None,
        *,
        engine: Engine | None = None,
        initialize_metadata: bool = True,
    ) -> None:
        self._engine = engine or get_engine()
        _check_sqlite_vec(self._engine)
        self._initialize_metadata = initialize_metadata
        self._dim, self._stored_model = self._read_or_init_dim_and_model()
        self._expected_model = expected_model
        self._expected_dim = expected_dim
        self._chunk_settings = chunk_settings or get_settings().semantic_search

    def _read_or_init_dim_and_model(self) -> tuple[int, str | None]:
        with self._engine.begin() as conn:
            try:
                vec_exists = conn.execute(
                    text(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'vec_listserv_embeddings'"
                    )
                ).first()
                if vec_exists is None:
                    raise VectorIndexUnavailableError(
                        "LISTSERV vector schema is not initialized; run `doc3gpp db init` "
                        "or `doc3gpp db reset` first"
                    )
                dim_row = conn.execute(
                    text(
                        "SELECT value FROM vec_listserv_meta "
                        "WHERE key = 'embedding_dim'"
                    )
                ).scalar()
                model_row = conn.execute(
                    text(
                        "SELECT value FROM vec_listserv_meta "
                        "WHERE key = 'embedding_model'"
                    )
                ).scalar()
                if dim_row is None:
                    if self._initialize_metadata:
                        conn.execute(
                            text(
                                "INSERT INTO vec_listserv_meta (key, value) "
                                "VALUES ('embedding_dim', :d)"
                            ),
                            {"d": str(DEFAULT_DIM)},
                        )
                    return DEFAULT_DIM, model_row
                return int(dim_row), model_row
            except OperationalError as exc:
                raise VectorIndexUnavailableError(
                    "LISTSERV vector schema is not initialized; run `doc3gpp db init` "
                    f"or `doc3gpp db reset` first: {exc}"
                ) from exc

    def _check_compatible(self, dim: int, *, what: str) -> None:
        if self._expected_dim is not None and self._expected_dim != self._dim:
            raise VectorIndexUnavailableError(
                f"vector dim mismatch: stored={self._dim} "
                f"expected={self._expected_dim}; run "
                "`doc3gpp listserv index --rebuild-embeddings`"
            )
        if dim != self._dim:
            prefix = "query" if what == "query" else "vector"
            raise VectorIndexUnavailableError(
                f"{prefix} dim mismatch: stored={self._dim} "
                f"requested={dim}; run "
                "`doc3gpp listserv index --rebuild-embeddings`"
            )
        if (
            self._expected_model is not None
            and self._stored_model is not None
            and self._stored_model != self._expected_model
        ):
            raise VectorIndexUnavailableError(
                f"vector model mismatch: stored={self._stored_model!r} "
                f"expected={self._expected_model!r}; run "
                "`doc3gpp listserv index --rebuild-embeddings`"
            )

    def verify_compatible(self, dim: int, model: str | None) -> None:
        """Raise on live-vs-stored dim/model mismatch without writing."""
        if dim != self._dim:
            raise VectorIndexUnavailableError(
                f"vector dim mismatch: stored={self._dim} "
                f"requested={dim}; run "
                "`doc3gpp listserv index --rebuild-embeddings`"
            )
        if (
            model is not None
            and self._stored_model is not None
            and self._stored_model != model
        ):
            raise VectorIndexUnavailableError(
                f"vector model mismatch: stored={self._stored_model!r} "
                f"expected={model!r}; run "
                "`doc3gpp listserv index --rebuild-embeddings`"
            )

    def reset_for_rebuild(self, dim: int, model: str | None) -> None:
        """Drop + recreate ``vec_listserv_embeddings`` at ``dim`` and stamp ``vec_listserv_meta``."""
        width = int(dim)
        with self._engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS vec_listserv_embeddings"))
            conn.execute(text("DELETE FROM vec_listserv_chunk_text"))
            conn.execute(
                text(
                    "DELETE FROM vec_listserv_meta "
                    "WHERE key IN ('last_rebuild_at', 'last_indexed_at')"
                )
            )
            conn.execute(
                text(
                    "CREATE VIRTUAL TABLE vec_listserv_embeddings USING vec0(\n"
                    "    chunk_id TEXT PRIMARY KEY,\n"
                    "    url TEXT,\n"
                    "    chunk_index INTEGER,\n"
                    "    source_revision INTEGER,\n"
                    f"    embedding FLOAT[{width}] distance_metric=cosine\n"
                    ")"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO vec_listserv_meta (key, value) "
                    "VALUES ('embedding_dim', :v) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                ),
                {"v": str(width)},
            )
            if model is None:
                conn.execute(
                    text("DELETE FROM vec_listserv_meta WHERE key = 'embedding_model'")
                )
            else:
                conn.execute(
                    text(
                        "INSERT INTO vec_listserv_meta (key, value) "
                        "VALUES ('embedding_model', :v) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                    ),
                    {"v": model},
                )
        self._dim = width
        self._stored_model = model

    def upsert_chunks(
        self,
        url: str,
        chunks: Sequence[EmbeddedChunk],
        *,
        source_revision: int = 1,
        stamp: bool = True,
    ) -> int:
        """Replace all chunks for ``url`` in one transaction and return replaced chunk count."""
        return self.upsert_chunks_many(
            [(url, list(chunks), source_revision)], stamp=stamp
        )

    def upsert_chunks_many(
        self,
        items: Sequence[tuple[str, Sequence[EmbeddedChunk], int]],
        *,
        stamp: bool = True,
    ) -> int:
        """Atomically replace chunks for one or more canonical message URLs."""
        if not items:
            return 0
        for _url, chunks, _rev in items:
            for chunk in chunks:
                self._check_compatible(int(chunk.embedding.shape[-1]), what="upsert")

        urls = [url for url, _chunks, _rev in items]
        settings = self._chunk_settings
        default_fp = chunking_fingerprint(settings.chunk_size, settings.chunk_overlap)
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        vec_params: list[dict[str, object]] = []
        txt_params: list[dict[str, object]] = []
        for url, chunks, source_revision in items:
            rev = int(source_revision)
            for chunk in chunks:
                vec_params.append(
                    {
                        "cid": chunk.chunk_id,
                        "url": url,
                        "ci": int(chunk.chunk_index),
                        "rev": rev,
                        "emb": np.asarray(chunk.embedding, dtype=np.float32).tobytes(),
                    }
                )
                txt_params.append(
                    {
                        "cid": chunk.chunk_id,
                        "url": url,
                        "ci": int(chunk.chunk_index),
                        "rev": rev,
                        "fp": chunk.chunking_fingerprint or default_fp,
                        "body": chunk.text or "",
                    }
                )

        with self._engine.begin() as conn:
            for start in range(0, len(urls), _BATCH_SIZE):
                batch_urls = urls[start : start + _BATCH_SIZE]
                placeholders = ", ".join(f":u_{i}" for i in range(len(batch_urls)))
                bound = {f"u_{i}": u for i, u in enumerate(batch_urls)}
                conn.execute(
                    text(
                        f"DELETE FROM vec_listserv_embeddings WHERE url IN ({placeholders})"
                    ),
                    bound,
                )
                conn.execute(
                    text(
                        f"DELETE FROM vec_listserv_chunk_text WHERE url IN ({placeholders})"
                    ),
                    bound,
                )
            if vec_params:
                conn.execute(
                    text(
                        "INSERT INTO vec_listserv_embeddings "
                        "(chunk_id, url, chunk_index, source_revision, embedding) "
                        "VALUES (:cid, :url, :ci, :rev, :emb)"
                    ),
                    vec_params,
                )
            if txt_params:
                conn.execute(
                    text(
                        "INSERT INTO vec_listserv_chunk_text "
                        "(chunk_id, url, chunk_index, source_revision, fingerprint, text) "
                        "VALUES (:cid, :url, :ci, :rev, :fp, :body)"
                    ),
                    txt_params,
                )
            if self._expected_model is not None and self._stored_model is None:
                conn.execute(
                    text(
                        "INSERT INTO vec_listserv_meta (key, value) "
                        "VALUES ('embedding_model', :m) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                    ),
                    {"m": self._expected_model},
                )
                self._stored_model = self._expected_model
            if stamp:
                conn.execute(
                    text(
                        "INSERT INTO vec_listserv_meta (key, value) "
                        "VALUES ('last_indexed_at', :ts) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                    ),
                    {"ts": now_iso},
                )
        return len(vec_params)

    def indexed_revisions(self) -> dict[str, tuple[int, str]]:
        """Return ``{url: (source_revision, chunking_fingerprint)}`` for indexed emails."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT url, MIN(source_revision), MIN(fingerprint) "
                    "FROM vec_listserv_chunk_text GROUP BY url"
                )
            ).all()
        return {str(u): (int(r), str(f)) for u, r, f in rows}

    def chunk_count(self) -> int:
        """Return the number of stored vector chunks."""
        with self._engine.connect() as conn:
            return int(
                conn.execute(
                    text("SELECT COUNT(*) FROM vec_listserv_chunk_text")
                ).scalar()
                or 0
            )

    def meta(self) -> dict[str, str]:
        """Return the stored vector metadata sidecar."""
        with self._engine.connect() as conn:
            return {
                str(k): str(v)
                for k, v in conn.execute(
                    text("SELECT key, value FROM vec_listserv_meta")
                ).all()
            }

    @property
    def stored_model(self) -> str | None:
        return self._stored_model

    @property
    def stored_dim(self) -> int:
        return self._dim

    def mark_rebuilt(self) -> None:
        """Stamp rebuild completion timestamps after a successful rebuild."""
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._engine.begin() as conn:
            for key in ("last_rebuild_at", "last_indexed_at"):
                conn.execute(
                    text(
                        "INSERT INTO vec_listserv_meta (key, value) "
                        "VALUES (:k, :ts) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                    ),
                    {"k": key, "ts": now_iso},
                )

    def remove(self, url: str) -> None:
        """Delete all vector and chunk-text rows for canonical message ``url``."""
        with self._engine.begin() as conn:
            conn.execute(
                text("DELETE FROM vec_listserv_embeddings WHERE url = :u"),
                {"u": url},
            )
            conn.execute(
                text("DELETE FROM vec_listserv_chunk_text WHERE url = :u"),
                {"u": url},
            )

    def matched_chunk_texts(
        self, chunk_ids: Collection[str]
    ) -> dict[str, MatchedChunk]:
        ids = list(dict.fromkeys(chunk_ids))
        if not ids:
            return {}
        with self._engine.begin() as conn:
            _ensure_read_transaction(conn)
            return self._matched_chunk_texts(conn, ids)

    def _matched_chunk_texts(
        self,
        conn: Connection,
        ids: list[str],
    ) -> dict[str, MatchedChunk]:
        if not ids:
            return {}
        if not conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'vec_listserv_chunk_text'"
            )
        ).first():
            return {}
        settings = self._chunk_settings
        expected_fp = chunking_fingerprint(
            settings.chunk_size, settings.chunk_overlap
        )
        result: dict[str, MatchedChunk] = {}
        for start in range(0, len(ids), _BATCH_SIZE):
            batch = ids[start : start + _BATCH_SIZE]
            placeholders = ", ".join(f":id_{i}" for i in range(len(batch)))
            rows = conn.execute(
                text(
                    "SELECT s.chunk_id, s.url, s.chunk_index, s.fingerprint, s.text "
                    "FROM vec_listserv_chunk_text s "
                    "JOIN vec_listserv_embeddings v "
                    "  ON v.chunk_id = s.chunk_id "
                    " AND v.url = s.url "
                    " AND v.chunk_index = s.chunk_index "
                    " AND v.source_revision = s.source_revision "
                    "JOIN listserv_messages m "
                    "  ON m.url = s.url "
                    " AND m.source_revision = s.source_revision "
                    f"WHERE s.chunk_id IN ({placeholders})"
                ),
                {f"id_{i}": val for i, val in enumerate(batch)},
            ).all()
            for cid, url, index, fp, body in rows:
                if fp == expected_fp and cid == f"{url}#{index}":
                    result[str(cid)] = MatchedChunk(str(cid), int(index), str(body))
        return result

    def knn(
        self,
        query_vec: np.ndarray,
        limit: int,
    ) -> list[VectorCandidate]:
        """Run KNN over ``vec_listserv_embeddings`` and capture matched chunk texts in the same snapshot."""
        q = np.asarray(query_vec, dtype=np.float32)
        self._check_compatible(int(q.shape[-1]), what="query")
        k = max(int(limit), 0)
        if k == 0:
            return []
        sql = (
            "SELECT chunk_id, url, chunk_index, distance\n"
            "  FROM vec_listserv_embeddings\n"
            " WHERE embedding MATCH :q AND k = :k\n"
            " ORDER BY distance IS NULL, distance ASC, chunk_id ASC"
        )
        params = {
            "q": q.tobytes(),
            "k": k,
        }
        with self._engine.begin() as conn:
            _ensure_read_transaction(conn)
            rows = conn.execute(text(sql), params).all()
            chunks = self._matched_chunk_texts(conn, [str(r[0]) for r in rows])
        return [
            VectorCandidate(
                str(r[1]),
                str(r[0]),
                int(r[2]),
                float(r[3]) if r[3] is not None else float("inf"),
                chunks.get(str(r[0])),
            )
            for r in rows
        ]

    def exact_distinct(
        self,
        query_vec: np.ndarray,
        *,
        target_distinct: int,
        filters: ListservFilters,
    ) -> list[VectorCandidate]:
        """Return best chunks for filtered messages through the target distance tie.

        Unlike vec0 KNN, scalar scoring has no candidate-count ceiling. Filtering,
        vector scoring, and matched-text validation share one read snapshot. All
        messages tied at the target boundary are returned for deterministic
        message-level tie-breaking by the semantic search service.
        """
        q = np.asarray(query_vec, dtype=np.float32)
        self._check_compatible(int(q.shape[-1]), what="query")
        target = max(int(target_distinct), 0)
        if target == 0:
            return []

        clauses, filter_params = _build_filter_clauses(filters)
        sql = [
            "WITH scored AS (",
            "    SELECT v.chunk_id, v.url, v.chunk_index,",
            "           vec_distance_cosine(v.embedding, :q) AS distance",
            "      FROM vec_listserv_embeddings v",
            "      JOIN listserv_messages m ON m.url = v.url",
            "      LEFT JOIN listserv_details d ON d.url = m.url",
            "     WHERE 1 = 1",
        ]
        sql.extend(f"       AND {clause}" for clause in clauses)
        sql.extend(
            [
                ")",
                "SELECT chunk_id, url, chunk_index, distance",
                "  FROM scored",
                " ORDER BY distance IS NULL, distance ASC, chunk_id ASC",
            ]
        )
        params = {"q": q.tobytes(), **filter_params}
        selected: list[tuple[str, str, int, float]] = []
        seen_urls: set[str] = set()
        boundary_distance: float | None = None
        with self._engine.begin() as conn:
            _ensure_read_transaction(conn)
            rows = conn.execute(text("\n".join(sql)), params)
            for row in rows:
                raw_distance = row[3]
                if len(selected) >= target and raw_distance != boundary_distance:
                    break
                chunk_id = str(row[0])
                url = str(row[1])
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                distance = (
                    float(raw_distance) if raw_distance is not None else float("inf")
                )
                selected.append((chunk_id, url, int(row[2]), distance))
                if len(selected) == target:
                    boundary_distance = raw_distance
            chunks = self._matched_chunk_texts(
                conn, [chunk_id for chunk_id, _url, _index, _distance in selected]
            )

        return [
            VectorCandidate(url, chunk_id, index, distance, chunks.get(chunk_id))
            for chunk_id, url, index, distance in selected
        ]

    def matching_urls(
        self,
        urls: Collection[str],
        filters: ListservFilters,
    ) -> frozenset[str]:
        """Return the subset of ``urls`` that exist in ``listserv_messages`` and satisfy ``filters``."""
        url_list = list(dict.fromkeys(urls))
        if not url_list:
            return frozenset()
        clauses, params = _build_filter_clauses(filters)
        sql = [
            "SELECT m.url",
            "  FROM listserv_messages m",
            "  LEFT JOIN listserv_details d ON d.url = m.url",
            " WHERE m.url IN :urls",
        ]
        sql.extend(f"   AND {clause}" for clause in clauses)
        stmt = text("\n".join(sql)).bindparams(bindparam("urls", expanding=True))
        matched: set[str] = set()
        with self._engine.begin() as conn:
            for start in range(0, len(url_list), _BATCH_SIZE):
                batch = url_list[start : start + _BATCH_SIZE]
                rows = conn.execute(stmt, {**params, "urls": batch}).all()
                matched.update(str(r[0]) for r in rows)
        return frozenset(matched)


__all__ = [
    "DEFAULT_DIM",
    "SQLAlchemyListservVectorRepository",
]
