"""Pure-Python chunker for the semantic-search embed text.

Splits a long string into chunks of ~``size`` whitespace tokens with
``overlap`` trailing tokens repeated at the start of the next chunk.
The boundary is on whitespace (not model word-piece) to keep the
function pure-Python and fast; the embedder's own tokenizer will
further subdivide each chunk.

``size`` and ``overlap`` are in whitespace tokens, NOT model tokens.
"""

from __future__ import annotations

CHUNK_SIZE_DEFAULT = 200
CHUNK_OVERLAP_DEFAULT = 20


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
