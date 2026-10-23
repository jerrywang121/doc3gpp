"""Pure chunking helpers for the semantic index (stdlib only, no I/O).

Splits a long string into chunks of ~``size`` whitespace tokens with
``overlap`` trailing tokens repeated at the start of the next chunk.
The boundary is on whitespace (not model word-piece) to keep the
function pure-Python and fast; the embedder's own tokenizer will
further subdivide each chunk.

``size`` and ``overlap`` are in whitespace tokens, NOT model tokens.
Lives in ``models/`` so storage can plan content chunks without
importing the services layer.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

CHUNK_SIZE_DEFAULT = 512
CHUNK_OVERLAP_DEFAULT = 24


def _chunks(text: str, size: int, overlap: int) -> list[str]:
    """Split ``text`` into chunks of ~``size`` whitespace tokens with ``overlap``.

    Boundary cases:
    * empty / whitespace-only input → ``[]``.
    * text shorter than ``size`` → ``[text.strip()]``.
    * trailing whitespace stripped from every chunk.

    Raises:
        ValueError: if ``size <= 0`` or ``overlap >= size``.
    """
    if size <= 0:
        raise ValueError(f"size must be > 0, got {size}")
    if overlap >= size:
        raise ValueError(f"overlap must be < size, got overlap={overlap} size={size}")
    tokens = text.split()
    if not tokens:
        return []
    chunks: list[str] = []
    start = 0
    n = len(tokens)
    while start < n:
        end = min(start + size, n)
        chunk = " ".join(tokens[start:end]).strip()
        if chunk:
            chunks.append(chunk)
        if end == n:
            break
        start = end - overlap
    return chunks


def chunk_count(token_count: int, size: int, overlap: int) -> int:
    """Return how many chunks ``_chunks`` yields for ``token_count`` whitespace tokens.

    Computed without splitting text, so vector status can plan content chunks from
    a stored token count. Validates ``size`` and ``overlap`` like ``_chunks``.

    Raises:
        ValueError: if ``size <= 0`` or ``overlap >= size``.
    """
    if size <= 0:
        raise ValueError(f"size must be > 0, got {size}")
    if overlap >= size:
        raise ValueError(f"overlap must be < size, got overlap={overlap} size={size}")
    if token_count <= 0:
        return 0
    if token_count <= size:
        return 1
    step = size - overlap
    return 1 + (token_count - size + step - 1) // step


def plan_content_chunks(
    documents: Iterable[tuple[str, str, str]],
    size: int,
    overlap: int,
    max_chunks: int,
) -> Iterator[tuple[str, str, list[str]]]:
    """Yield ``(document_id, content_hash, chunks)`` for each document in order.

    ``documents`` holds ``(document_id, content_hash, markdown)`` in indexing
    order. Chunks are taken until ``max_chunks`` is spent, so a document past
    the cap yields nothing. A blank document yields nothing.
    """
    remaining = max_chunks
    for document_id, content_hash, markdown in documents:
        if remaining <= 0:
            return
        chunks = _chunks(markdown, size, overlap)[:remaining]
        if not chunks:
            continue
        remaining -= len(chunks)
        yield document_id, content_hash, chunks


def planned_document_keys(
    documents: Iterable[tuple[str, str, int]],
    size: int,
    overlap: int,
    max_chunks: int,
) -> list[tuple[str, str]]:
    """Return ``(document_id, content_hash)`` for each document ``plan_content_chunks`` yields.

    ``documents`` holds ``(document_id, content_hash, token_count)`` in indexing
    order. Applies the same cap as :func:`plan_content_chunks` without decoding
    the Markdown.
    """
    remaining = max_chunks
    keys: list[tuple[str, str]] = []
    for document_id, content_hash, token_count in documents:
        if remaining <= 0:
            break
        count = min(chunk_count(token_count, size, overlap), remaining)
        if count == 0:
            continue
        remaining -= count
        keys.append((document_id, content_hash))
    return keys
