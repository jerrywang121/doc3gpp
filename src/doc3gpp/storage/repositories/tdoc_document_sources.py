"""SQL view of every stored TDoc content document, keyed by ZIP URL."""

from __future__ import annotations

from doc3gpp.models.tdoc_content_families import TDOC_CONTENT_STORES


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
