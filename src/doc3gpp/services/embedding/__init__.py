"""Public re-exports for the semantic-search embedding helpers."""

from __future__ import annotations

from doc3gpp.services.embedding.embedder import Embedder, SentenceTransformerEmbedder
from doc3gpp.services.embedding.remote_embedder import OpenAICompatibleEmbedder

__all__ = [
    "Embedder",
    "OpenAICompatibleEmbedder",
    "SentenceTransformerEmbedder",
]
