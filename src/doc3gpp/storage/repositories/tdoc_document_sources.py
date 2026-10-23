"""SQL view of every stored TDoc content document, keyed by ZIP URL."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import text

from doc3gpp.models.tdoc_content_families import TDOC_CONTENT_STORES
from doc3gpp.storage.db.session import get_engine

_LOOKUP_CHUNK_SIZE = 500


def tdoc_document_sources_sql() -> str:
    """Return a ``UNION ALL`` with one row per stored content document.

    Every branch projects ``ftp_url, document_id, source_file, content_hash``
    so callers can join the result to ``tdoc_extracts`` on ``ftp_url``. The
    CR main document has no content row, so it is absent by construction.
    """
    return "\nUNION ALL\n".join(
        "SELECT ftp_url, document_id, doc_filename AS source_file, content_hash "
        f"FROM {store.table_name}"
        for store in TDOC_CONTENT_STORES
    )


def document_sources_by_id(document_ids: Iterable[str]) -> dict[str, tuple[str, str]]:
    """Map each stored ``document_id`` to its ``(ftp_url, source_file)``.

    Unknown IDs are omitted. The lookup runs in bounded batches so a large
    result set stays under SQLite's bound-parameter limit.
    """
    ids = sorted(set(document_ids))
    sources: dict[str, tuple[str, str]] = {}
    if not ids:
        return sources
    sources_sql = tdoc_document_sources_sql()
    with get_engine().begin() as conn:
        for start in range(0, len(ids), _LOOKUP_CHUNK_SIZE):
            batch = ids[start:start + _LOOKUP_CHUNK_SIZE]
            placeholders = ", ".join(f":id_{i}" for i in range(len(batch)))
            rows = conn.execute(
                text(
                    "SELECT document_id, ftp_url, source_file "
                    f"FROM ({sources_sql}) "
                    f"WHERE document_id IN ({placeholders})"
                ),
                {f"id_{i}": value for i, value in enumerate(batch)},
            ).all()
            for document_id, ftp_url, source_file in rows:
                sources[document_id] = (ftp_url, source_file)
    return sources
