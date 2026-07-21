"""Shared gzip JSON helpers for binary detail columns.

Used by :class:`~doc3gpp.storage.repositories.tdoc_cr_sql.SQLAlchemyTDocCrRepository`
and :class:`~doc3gpp.storage.repositories.tdoc_cr_ttcn_sql.SQLAlchemyTDocCrTtcnRepository`
to compress and decompress JSON payloads stored in ``LargeBinary`` columns.
"""

from __future__ import annotations

import gzip
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_GZIP_MAGIC = b"\x1f\x8b"


def compress_json(payload: Any) -> bytes:
    """Gzip-compress ``payload`` as UTF-8 JSON at maximum compression."""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return gzip.compress(raw, compresslevel=9)


def decompress_json(blob: bytes | None) -> Any:
    """Decode a gzip-compressed (or legacy plain) JSON blob.

    Tolerant by design: ``None`` / empty bytes, gzip decompression errors,
    JSON decode errors, and Unicode decode errors all return ``None`` so a
    corrupt row never breaks the read path. Legacy uncompressed payloads
    (no gzip magic bytes) are still parsed transparently.

    Returns:
        The decoded JSON value, or ``None`` when the blob is missing or
        cannot be decoded.
    """
    if not blob:
        return None
    try:
        raw = gzip.decompress(blob) if blob[:2] == _GZIP_MAGIC else blob
        decoded = json.loads(raw.decode("utf-8"))
    except (gzip.BadGzipFile, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning(
            "Could not decompress JSON blob (length=%d); returning None",
            len(blob),
        )
        return None
    return decoded
