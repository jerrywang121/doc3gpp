"""Public re-exports for the semantic-search embedding helpers."""

from __future__ import annotations

from doc3gpp.repository.protocols import Embedder
from doc3gpp.services.embedding.remote_embedder import OpenAICompatibleEmbedder

__all__ = [
    "Embedder",
    "OpenAICompatibleEmbedder",
]
