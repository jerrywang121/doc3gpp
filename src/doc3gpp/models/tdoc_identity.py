"""Canonical TDoc URL and document identities shared across package layers."""

from __future__ import annotations

import hashlib
import json
import re

from doc3gpp.models.tdoc_document import _normalize_safe_relative_path

_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")


def normalize_ftp_path(path: str) -> str:
    """Normalize a 3GPP FTP URL/path to its canonical relative form.

    Strips FTP and 3GPP HTTPS roots, leading slashes, and repeated separators,
    converts backslashes, and lowercases the result to match stored ``ftp_url``
    identities.
    """
    normalized = path.strip().replace("\\", "/")
    normalized = re.sub(
        r"^https?://www\.3gpp\.org/ftp/",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"^ftp:/+", "", normalized, flags=re.IGNORECASE)
    normalized = normalized.lstrip("/")
    normalized = re.sub(r"/{2,}", "/", normalized)
    return normalized.lower()


def _sha256_json(values: list[str]) -> str:
    serialized = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def document_id_for(ftp_url: str, source_file: str) -> str:
    """Return the stable SHA-256 ID for a normalized ZIP URL and member path."""
    normalized_url = normalize_ftp_path(ftp_url)
    if not normalized_url:
        raise ValueError("ftp_url must be a non-empty ZIP URL")
    normalized_source = _normalize_safe_relative_path(
        source_file,
        field_name="ZIP member name",
    )
    return _sha256_json([normalized_url, normalized_source])
