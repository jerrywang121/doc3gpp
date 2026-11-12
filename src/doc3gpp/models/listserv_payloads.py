"""Payload serializers and sanitized indexing text helpers for ETSI LISTSERV archives."""

from __future__ import annotations

import re
from urllib.parse import unquote

from doc3gpp.models.listserv import (
    ListservDetailMetadata,
    ListservMessage,
    ListservRecord,
    ListservSearchRecord,
    ListservSearchResult,
    normalize_tdoc_ids,
)

PREVIEW_MAX_CHARS = 200

_TDOC_ID_EXTRACT_RE = re.compile(r"([RSC][1-6P](?:[-sw])\d{6,7})", re.IGNORECASE)

_MD_LINK_URL_RE = re.compile(
    r"\[([^\]]*)\]\(\s*(?:(?:https?|ftp)://|mailto:|/|#)[^)]*\)",
    re.IGNORECASE,
)
_ANGLE_URL_RE = re.compile(
    r"(?:&lt;|<)\s*(?:(?:https?|ftp)://|mailto:)[^\s<>]+?(?:&gt;|>)",
    re.IGNORECASE,
)
_BARE_URL_RE = re.compile(
    r"\b(?:(?:https?|ftp)://|mailto:)\S+|\bwww\.[A-Za-z0-9.-]+\.[A-Za-z]{2,}\S*",
    re.IGNORECASE,
)


def strip_urls(text: str) -> str:
    """Strip URL strings from ``text`` while preserving visible non-URL prose."""
    if not text:
        return ""
    cleaned = text
    # Unwrap Markdown links [label](url) -> label (repeat for nested/adjacent cases)
    for _ in range(2):
        next_cleaned = _MD_LINK_URL_RE.sub(r"\1", cleaned)
        if next_cleaned == cleaned:
            break
        cleaned = next_cleaned
    # Remove <https://...> and HTML-escaped &lt;https://...&gt;
    cleaned = _ANGLE_URL_RE.sub(" ", cleaned)
    # Remove bare scheme URLs and www. hosts
    cleaned = _BARE_URL_RE.sub(" ", cleaned)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in cleaned.splitlines()]
    return "\n".join(line for line in lines if line)


def _extract_attachment_url_tdocs(urls: tuple[str, ...]) -> tuple[str, ...]:
    matches: list[str] = []
    for raw_url in urls:
        if not raw_url:
            continue
        for candidate in (raw_url, unquote(raw_url)):
            for m in _TDOC_ID_EXTRACT_RE.finditer(candidate):
                matches.append(m.group(0))
    return normalize_tdoc_ids(matches)


def effective_record_tdocs(record: ListservRecord) -> tuple[str, ...]:
    """Return current unioned TDoc IDs for ``record`` from current subject and detail."""
    if record.detail is None:
        if record.message.subject_tdocs:
            return normalize_tdoc_ids(record.message.subject_tdocs)
        return normalize_tdoc_ids(record.tdocs)
    att_url_tdocs = _extract_attachment_url_tdocs(
        tuple(att.url for att in record.detail.attachments)
    )
    combined = normalize_tdoc_ids(
        (*record.message.subject_tdocs, *record.detail.tdocs, *att_url_tdocs)
    )
    if combined:
        return combined
    return normalize_tdoc_ids(record.tdocs)


def format_indexable_date(message: ListservMessage) -> str:
    """Format message local calendar date for FTS and semantic indexing."""
    return message.date.date().isoformat()


def indexable_fields(record: ListservRecord) -> dict[str, str]:
    """Return URL-stripped per-field strings used by FTS5 and semantic indexing."""
    tdocs = effective_record_tdocs(record)
    attachments_text = ""
    content_text = ""
    if record.detail is not None:
        attachments_text = " ".join(
             part
            for att in record.detail.attachments
            if (part := strip_urls(att.name))
        )
        content_text = strip_urls(record.detail.content)
    return {
        "list_id": record.message.list_id,
        "subject": strip_urls(record.message.subject),
        "sender": strip_urls(record.message.sender),
        "date": format_indexable_date(record.message),
        "week": record.message.week,
        "tdocs": " ".join(tdocs),
        "attachments": attachments_text,
        "content": content_text,
    }


def indexable_text(record: ListservRecord) -> str:
    """Return sanitized URL-free text for ``record`` shared across FTS and vector indexing."""
    fields = indexable_fields(record)
    ordered_parts = [
        fields["list_id"],
        fields["subject"],
        fields["sender"],
        fields["date"],
        fields["week"],
        fields["tdocs"],
        fields["attachments"],
        fields["content"],
    ]
    return "\n".join(part for part in ordered_parts if part)


def record_to_search_record(record: ListservRecord) -> ListservSearchRecord:
    """Project a full :class:`ListservRecord` to a body-free :class:`ListservSearchRecord`."""
    tdocs = effective_record_tdocs(record)
    if record.detail is None:
        detail_meta = None
    else:
        detail_meta = ListservDetailMetadata(
            attachments=record.detail.attachments,
            content_truncated=bool(record.detail.content_truncated),
            parser_version=record.detail.parser_version,
            parsed_at=record.detail.parsed_at,
        )
    return ListservSearchRecord(
        message=record.message,
        detail=detail_meta,
        tdocs=tdocs,
        source_revision=record.source_revision,
    )


def record_to_search_result(
    record: ListservRecord,
    *,
    score: float | None = None,
    snippet: str | None = None,
    matched_source: str | None = None,
) -> ListservSearchResult:
    """Convert a :class:`ListservRecord` into a :class:`ListservSearchResult` with 200-char preview."""
    search_record = record_to_search_record(record)
    if record.detail is None:
        content_preview = None
        preview_truncated = None
    else:
        content = record.detail.content
        content_preview = content[:PREVIEW_MAX_CHARS]
        preview_truncated = len(content) > PREVIEW_MAX_CHARS
    return ListservSearchResult(
        record=search_record,
        score=score,
        snippet=snippet,
        matched_source=matched_source,
        content_preview=content_preview,
        preview_truncated=preview_truncated,
    )


def listserv_record_payload(record: ListservRecord) -> dict[str, object]:
    """Serialize a :class:`ListservRecord` for ``show`` responses (includes full stored content)."""
    tdocs = effective_record_tdocs(record)
    detail = record.detail
    return {
        "url": record.message.url,
        "list_id": record.message.list_id,
        "subject": record.message.subject,
        "sender": record.message.sender,
        "date": record.message.date.isoformat(),
        "date_precision": record.message.date_precision,
        "week": record.message.week,
        "tdocs": list(tdocs),
        "attachments": (
            [{"name": att.name, "url": att.url} for att in detail.attachments]
            if detail is not None
            else None
        ),
        "content": detail.content if detail is not None else None,
        "content_truncated": (
            bool(detail.content_truncated) if detail is not None else None
        ),
        "parser_version": detail.parser_version if detail is not None else None,
        "parsed_at": (
            detail.parsed_at.isoformat() if detail is not None else None
        ),
    }


def listserv_search_payload(result: ListservSearchResult) -> dict[str, object]:
    """Serialize a :class:`ListservSearchResult` for ``search`` responses (200-char preview only)."""
    rec = result.record
    detail = rec.detail
    return {
        "url": rec.message.url,
        "list_id": rec.message.list_id,
        "subject": rec.message.subject,
        "sender": rec.message.sender,
        "date": rec.message.date.isoformat(),
        "date_precision": rec.message.date_precision,
        "week": rec.message.week,
        "tdocs": list(rec.tdocs),
        "attachments": (
            [{"name": att.name, "url": att.url} for att in detail.attachments]
            if detail is not None
            else None
        ),
        "content_truncated": (
            bool(detail.content_truncated) if detail is not None else None
        ),
        "parser_version": detail.parser_version if detail is not None else None,
        "parsed_at": (
            detail.parsed_at.isoformat() if detail is not None else None
        ),
        "score": result.score,
        "snippet": result.snippet,
        "matched_source": result.matched_source,
        "content_preview": result.content_preview,
        "preview_truncated": result.preview_truncated,
    }


__all__ = [
    "PREVIEW_MAX_CHARS",
    "effective_record_tdocs",
    "format_indexable_date",
    "indexable_fields",
    "indexable_text",
    "listserv_record_payload",
    "listserv_search_payload",
    "record_to_search_record",
    "record_to_search_result",
    "strip_urls",
]
