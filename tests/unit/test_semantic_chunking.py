"""Pure chunk-planning helpers: ``chunk_count`` and ``planned_document_keys``.

They must agree with ``_chunks`` and ``plan_content_chunks`` so vector status can
derive the expected content signatures from stored token counts alone.
"""

from __future__ import annotations

import pytest

from doc3gpp.models.semantic_chunking import (
    _chunks,
    chunk_count,
    plan_content_chunks,
    planned_document_keys,
)


def _text(token_count: int) -> str:
    return " ".join(f"w{i}" for i in range(token_count))


def test_chunk_count_matches_chunks_for_many_texts() -> None:
    for size in (1, 2, 5, 512):
        for overlap in sorted({overlap for overlap in (0, 1, size - 1, 24) if overlap < size}):
            for token_count in range(3 * size + 8):
                text = _text(token_count)
                assert chunk_count(token_count, size, overlap) == len(
                    _chunks(text, size, overlap)
                ), (size, overlap, token_count)


def test_chunk_count_rejects_invalid_settings() -> None:
    with pytest.raises(ValueError):
        chunk_count(10, 0, 0)
    with pytest.raises(ValueError):
        chunk_count(10, 4, 4)


def test_planned_document_keys_matches_plan_content_chunks() -> None:
    documents = [
        ("doc-blank", "h-blank", "   "),
        ("doc-short", "h-short", "alpha beta"),
        ("doc-long", "h-long", _text(40)),
        ("doc-medium", "h-medium", _text(11)),
        ("doc-tail", "h-tail", _text(3)),
    ]
    with_tokens = [
        (document_id, content_hash, len(markdown.split()))
        for document_id, content_hash, markdown in documents
    ]
    for max_chunks in (0, 1, 3, 16, 1000):
        expected = [
            (document_id, content_hash)
            for document_id, content_hash, _chunk_texts in plan_content_chunks(
                documents, 5, 2, max_chunks
            )
        ]

        assert planned_document_keys(with_tokens, 5, 2, max_chunks) == expected, max_chunks
