"""Transactional FTS chunk sidecar for SQL-stored presentation documents."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from sqlalchemy import bindparam, select, text
from sqlalchemy.engine import Connection

from doc3gpp.models.search import MatchedChunk, SearchIndexCorruptError
from doc3gpp.models.semantic_chunking import _chunks, chunking_fingerprint
from doc3gpp.models.tdoc_content_families import TDOC_CONTENT_STORES
from doc3gpp.storage.compression import decompress_json
from doc3gpp.storage.db.fts5_query import normalize_query
from doc3gpp.storage.db.models import CONTENT_ORM_BY_STORE_KEY, TDocExtractOrm


def _available(conn: Connection) -> bool:
    tables_exist = conn.execute(text(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN "
        "('tdoc_presentation_search', 'tdoc_presentation_search_ledger', "
        "'tdoc_presentation_search_rowids')"
    )).scalar_one() == 3
    if not tables_exist:
        return False
    columns = {row[1] for row in conn.execute(text("PRAGMA table_info(tdoc_presentation_search)"))}
    return {"row_kind", "extraction_version"} <= columns


def _sources(
    conn: Connection,
    ids: list[str] | None = None,
    *,
    selected: bool = False,
    matched_candidates: bool = False,
):
    sources = []
    for store in TDOC_CONTENT_STORES:
        orm = CONTENT_ORM_BY_STORE_KEY[store.key]
        query = (
            select(
                TDocExtractOrm.tdoc_id,
                orm.document_id,
                orm.doc_filename,
                orm.content_hash,
                orm.content,
            )
            .join(TDocExtractOrm, TDocExtractOrm.ftp_url == orm.ftp_url)
            .where(
                orm.doc_filename.ilike("%.ppt")
                | orm.doc_filename.ilike("%.pptx")
            )
        )
        if matched_candidates:
            query = query.where(
                text(
                    "EXISTS (SELECT 1 FROM temp._doc3gpp_search_matched_documents "
                    "candidate WHERE candidate.tdoc_id = tdoc_extracts.tdoc_id "
                    f"AND candidate.document_id = {orm.__tablename__}.document_id)"
                )
            )
        elif selected:
            query = query.where(
                TDocExtractOrm.tdoc_id.in_(
                    text("SELECT tdoc_id FROM temp._doc3gpp_search_selected_tdocs")
                )
            )
        elif ids is not None:
            query = query.where(TDocExtractOrm.tdoc_id.in_(ids))
        sources.extend(conn.execute(query))
    return sources


def freshness_ctes(conn: Connection, size: int, overlap: int) -> tuple[str, dict]:
    """SQL freshness plan; never load source blobs or chunk rows into Python."""
    source_sql = " UNION ALL ".join(
        "SELECT e.tdoc_id, d.document_id, d.doc_filename AS source_file, "
        "d.content_hash, d.token_count FROM " + store.table_name + " d "
        "JOIN tdoc_extracts e ON e.ftp_url = d.ftp_url "
        "WHERE lower(substr(d.doc_filename, -4)) = '.ppt' "
        "OR lower(substr(d.doc_filename, -5)) = '.pptx'"
        for store in TDOC_CONTENT_STORES
    )
    sources = f", presentation_sources AS ({source_sql})"
    if not _available(conn):
        return sources + ", presentation_stale AS (SELECT DISTINCT tdoc_id FROM presentation_sources)", {}
    conn.connection.driver_connection.create_function(
        "doc3gpp_fts_normalize", 1,
        lambda value: normalize_query(value) if value is not None else None,
        deterministic=True,
    )
    sql = sources + """
    , presentation_stats AS (
        SELECT c.tdoc_id, c.document_id, COUNT(*) AS n,
               COUNT(DISTINCT CAST(c.chunk_index AS INTEGER)) AS distinct_indices,
               MIN(CAST(c.chunk_index AS INTEGER)) AS min_index,
               MAX(CAST(c.chunk_index AS INTEGER)) AS max_index,
               SUM(CASE WHEN l.document_id IS NULL
                    OR r.fts_rowid IS NULL OR r.tdoc_id IS NOT c.tdoc_id
                    OR c.source_file IS NOT l.source_file
                    OR c.content_hash IS NOT l.content_hash
                    OR c.fingerprint IS NOT l.fingerprint
                    OR c.chunk_index IS NULL
                    OR CAST(CAST(c.chunk_index AS INTEGER) AS TEXT) != CAST(c.chunk_index AS TEXT)
                    OR c.chunk_id IS NOT (c.tdoc_id || '#' || c.document_id || '#' ||
                                         c.content_hash || '#' || CAST(c.chunk_index AS TEXT))
                    OR c.original_text IS NULL
                    OR c.content_text IS NOT doc3gpp_fts_normalize(c.original_text)
                    THEN 1 ELSE 0 END) AS invalid
        FROM tdoc_presentation_search c
        LEFT JOIN tdoc_presentation_search_ledger l
          ON l.tdoc_id = c.tdoc_id AND l.document_id = c.document_id
        LEFT JOIN tdoc_presentation_search_rowids r ON r.fts_rowid = c.rowid
        GROUP BY c.tdoc_id, c.document_id
    ), presentation_expected AS (
        SELECT *, CASE WHEN token_count <= 0 THEN 0
                       WHEN token_count <= :presentation_size THEN 1
                       ELSE 1 + (token_count - :presentation_size + :presentation_step - 1)
                                / :presentation_step END AS expected_count
        FROM presentation_sources
    ), presentation_stale AS (
        SELECT s.tdoc_id FROM presentation_expected s
        LEFT JOIN tdoc_presentation_search_ledger l
          ON l.tdoc_id = s.tdoc_id AND l.document_id = s.document_id
        LEFT JOIN presentation_stats c
          ON c.tdoc_id = s.tdoc_id AND c.document_id = s.document_id
        WHERE s.token_count IS NULL OR s.token_count < 0 OR l.document_id IS NULL
           OR l.source_file IS NOT s.source_file OR l.content_hash IS NOT s.content_hash
           OR l.fingerprint IS NOT :presentation_fingerprint
           OR l.chunk_count IS NOT s.expected_count
           OR COALESCE(c.n, 0) != s.expected_count
           OR COALESCE(c.distinct_indices, 0) != s.expected_count
           OR COALESCE(c.invalid, 0) != 0
           OR (s.expected_count > 0 AND (c.min_index != 0 OR c.max_index != s.expected_count - 1))
        UNION
        SELECT l.tdoc_id FROM tdoc_presentation_search_ledger l
        LEFT JOIN presentation_sources s
          ON s.tdoc_id = l.tdoc_id AND s.document_id = l.document_id
        WHERE s.document_id IS NULL
        UNION
        SELECT c.tdoc_id FROM presentation_stats c
        LEFT JOIN presentation_sources s
          ON s.tdoc_id = c.tdoc_id AND s.document_id = c.document_id
        WHERE s.document_id IS NULL
        UNION
        SELECT r.tdoc_id FROM tdoc_presentation_search_rowids r
        LEFT JOIN tdoc_presentation_search c ON c.rowid = r.fts_rowid
        WHERE c.rowid IS NULL OR c.tdoc_id IS NOT r.tdoc_id
    )
    """
    return sql, {"presentation_size": size, "presentation_step": size - overlap,
                 "presentation_fingerprint": chunking_fingerprint(size, overlap)}


def remove(conn: Connection, tdoc_id: str) -> None:
    remove_many(conn, [tdoc_id])


def remove_many(conn: Connection, tdoc_ids: Sequence[str]) -> None:
    if not tdoc_ids:
        return
    if not _available(conn):
        return
    for start in range(0, len(tdoc_ids), 500):
        ids = tdoc_ids[start:start + 500]
        ledger_counts = dict(conn.execute(text(
            "SELECT tdoc_id, SUM(chunk_count) FROM tdoc_presentation_search_ledger "
            "WHERE tdoc_id IN :ids GROUP BY tdoc_id"
        ).bindparams(bindparam("ids", expanding=True)), {"ids": ids}).all())
        map_counts = dict(conn.execute(text(
            "SELECT tdoc_id, COUNT(*) FROM tdoc_presentation_search_rowids "
            "WHERE tdoc_id IN :ids GROUP BY tdoc_id"
        ).bindparams(bindparam("ids", expanding=True)), {"ids": ids}).all())
        wrong_owner = conn.execute(text(
            "SELECT 1 FROM tdoc_presentation_search_rowids r "
            "CROSS JOIN tdoc_presentation_search c "
            "WHERE r.tdoc_id IN :ids AND c.rowid = r.fts_rowid "
            "AND c.tdoc_id IS NOT r.tdoc_id LIMIT 1"
        ).bindparams(bindparam("ids", expanding=True)), {"ids": ids}).first()
        if wrong_owner or any(
            ledger_counts.get(tdoc_id, 0) > map_counts.get(tdoc_id, 0) for tdoc_id in ids
        ):
            raise SearchIndexCorruptError(
                "presentation search rowid map is incomplete; run 'doc3gpp db init --scope main'"
            )
        delete_fts = text(
            "DELETE FROM tdoc_presentation_search WHERE rowid IN "
            "(SELECT fts_rowid FROM tdoc_presentation_search_rowids WHERE tdoc_id IN :ids) "
            "AND tdoc_id = (SELECT tdoc_id FROM tdoc_presentation_search_rowids "
            "WHERE fts_rowid = tdoc_presentation_search.rowid)"
        ).bindparams(bindparam("ids", expanding=True))
        conn.execute(delete_fts, {"ids": ids})
        for table in ("tdoc_presentation_search_rowids", "tdoc_presentation_search_ledger"):
            sql = text(f"DELETE FROM {table} WHERE tdoc_id IN :ids")
            conn.execute(sql.bindparams(bindparam("ids", expanding=True)), {"ids": ids})


def replace(conn: Connection, tdoc_id: str, size: int, overlap: int) -> None:
    if not _available(conn):
        return
    fingerprint = chunking_fingerprint(size, overlap)
    for source in _sources(conn, [tdoc_id]):
        markdown = decompress_json(source.content)
        if markdown is None:
            raise ValueError(f"stored content for {source.doc_filename!r} cannot be decoded")
        chunks = _chunks(markdown, size, overlap)
        values = {"tdoc_id": tdoc_id, "document_id": source.document_id,
                  "source_file": source.doc_filename, "content_hash": source.content_hash,
                  "fingerprint": fingerprint, "chunk_count": len(chunks)}
        conn.execute(text("""INSERT INTO tdoc_presentation_search_ledger
            (tdoc_id, document_id, source_file, content_hash, fingerprint, chunk_count)
            VALUES (:tdoc_id, :document_id, :source_file, :content_hash, :fingerprint, :chunk_count)
        """), values)
        for index, chunk in enumerate(chunks):
            result = conn.execute(text("""INSERT INTO tdoc_presentation_search
                (tdoc_id, document_id, source_file, content_hash, fingerprint,
                 chunk_id, chunk_index, original_text, content_text)
                VALUES (:tdoc_id, :document_id, :source_file, :content_hash, :fingerprint,
                        :chunk_id, :chunk_index, :original_text, :content_text)
            """), {**values, "chunk_index": index,
                  "chunk_id": f"{tdoc_id}#{source.document_id}#{source.content_hash}#{index}",
                  "original_text": chunk, "content_text": normalize_query(chunk)})
            conn.execute(text(
                "INSERT INTO tdoc_presentation_search_rowids (fts_rowid, tdoc_id) "
                "VALUES (:rowid, :tdoc_id)"
            ), {"rowid": result.lastrowid, "tdoc_id": tdoc_id})


def current_documents(
    conn: Connection,
    size: int,
    overlap: int,
) -> tuple[set[tuple[str, str]], set[str]]:
    """Return verified candidate documents and stale parent IDs."""
    sources = _sources(conn, matched_candidates=True)
    if not _available(conn):
        return set(), {row.tdoc_id for row in sources}
    fingerprint = chunking_fingerprint(size, overlap)
    candidate_join = (
        "EXISTS (SELECT 1 FROM temp._doc3gpp_search_matched_documents candidate "
        "WHERE candidate.tdoc_id = {alias}.tdoc_id "
        "AND candidate.document_id = {alias}.document_id)"
    )
    ledger_sql = text(
        "SELECT l.* FROM tdoc_presentation_search_ledger l WHERE "
        + candidate_join.format(alias="l")
    )
    chunk_sql = text(
        "SELECT c.tdoc_id, c.document_id, c.source_file, c.content_hash, c.fingerprint, "
        "c.chunk_id, c.chunk_index, c.original_text, c.content_text, "
        "r.tdoc_id AS mapped_tdoc_id "
        "FROM tdoc_presentation_search c "
        "LEFT JOIN tdoc_presentation_search_rowids r ON r.fts_rowid = c.rowid "
        "WHERE " + candidate_join.format(alias="c")
    )
    ledgers = conn.execute(ledger_sql).all()
    chunk_rows = conn.execute(chunk_sql).all()
    ledger_by_key = {(row.tdoc_id, row.document_id): row for row in ledgers}
    chunks_by_key: dict[tuple[str, str], list] = {}
    for row in chunk_rows:
        chunks_by_key.setdefault((row.tdoc_id, row.document_id), []).append(row)

    current: set[tuple[str, str]] = set()
    stale: set[str] = set()
    expected_keys = set()
    for source in sources:
        key = (source.tdoc_id, source.document_id)
        expected_keys.add(key)
        markdown = decompress_json(source.content)
        if markdown is None:
            stale.add(source.tdoc_id)
            continue
        chunks = _chunks(markdown, size, overlap)
        ledger = ledger_by_key.get(key)
        actual = chunks_by_key.get(key, [])
        expected = {
            (
                f"{source.tdoc_id}#{source.document_id}#{source.content_hash}#{index}",
                str(index),
                chunk,
            )
            for index, chunk in enumerate(chunks)
        }
        actual_set = {
            (row.chunk_id, str(row.chunk_index), row.original_text)
            for row in actual
        }
        if (
            ledger is None
            or ledger.source_file != source.doc_filename
            or ledger.content_hash != source.content_hash
            or ledger.fingerprint != fingerprint
            or ledger.chunk_count != len(chunks)
            or len(actual) != len(chunks)
            or actual_set != expected
            or any(
                row.source_file != source.doc_filename
                or row.content_hash != source.content_hash
                or row.fingerprint != fingerprint
                or row.mapped_tdoc_id != source.tdoc_id
                or row.content_text != normalize_query(row.original_text)
                for row in actual
            )
        ):
            stale.add(source.tdoc_id)
        else:
            current.add(key)
    for key in set(ledger_by_key) | set(chunks_by_key):
        if key not in expected_keys:
            stale.add(key[0])
    return current, stale


def matched_chunks(
    conn: Connection,
    query: str,
    candidates: Collection[tuple[str, str]],
    size: int,
    overlap: int,
) -> dict[tuple[str, str], MatchedChunk]:
    candidate_ids = sorted(set(candidates))
    if not candidate_ids or not _available(conn):
        return {}

    conn.execute(
        text("DROP TABLE IF EXISTS temp._doc3gpp_search_matched_documents")
    )
    conn.execute(
        text(
            "CREATE TEMP TABLE _doc3gpp_search_matched_documents ("
            "tdoc_id TEXT NOT NULL, document_id TEXT NOT NULL, "
            "PRIMARY KEY (tdoc_id, document_id))"
        )
    )
    try:
        conn.execute(
            text(
                "INSERT INTO temp._doc3gpp_search_matched_documents "
                "(tdoc_id, document_id) VALUES (:tdoc_id, :document_id)"
            ),
            [
                {"tdoc_id": tdoc_id, "document_id": document_id}
                for tdoc_id, document_id in candidate_ids
            ],
        )
        current, _ = current_documents(conn, size, overlap)
        if not current:
            return {}
        rows = conn.execute(
            text(
                "SELECT tdoc_presentation_search.tdoc_id, "
                "tdoc_presentation_search.document_id, "
                "tdoc_presentation_search.chunk_id, "
                "tdoc_presentation_search.chunk_index, "
                "tdoc_presentation_search.original_text, "
                "bm25(tdoc_presentation_search) AS score "
                "FROM tdoc_presentation_search "
                "JOIN temp._doc3gpp_search_matched_documents candidate "
                "ON candidate.tdoc_id = tdoc_presentation_search.tdoc_id "
                "AND candidate.document_id = tdoc_presentation_search.document_id "
                "WHERE tdoc_presentation_search MATCH :query "
                "ORDER BY score, "
                "CAST(tdoc_presentation_search.chunk_index AS INTEGER), "
                "tdoc_presentation_search.chunk_id"
            ),
            {"query": query},
        )
        result: dict[tuple[str, str], MatchedChunk] = {}
        for row in rows:
            key = (row.tdoc_id, row.document_id)
            if key in current and key not in result:
                result[key] = MatchedChunk(
                    row.chunk_id,
                    int(row.chunk_index),
                    row.original_text,
                )
        return result
    finally:
        conn.execute(
            text("DROP TABLE IF EXISTS temp._doc3gpp_search_matched_documents")
        )
