"""Sentence-transformers backed embedder for the semantic search subsystem.

The model is loaded lazily on the first ``.encode()`` call so a
process that never reranks never pays the load cost. Model load
failure (network, OOM, missing repo id) is wrapped as
:class:`EmbedderUnavailableError`.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import numpy as np

from doc3gpp.models.semantic_search import EmbedderUnavailableError

logger = logging.getLogger(__name__)

# Re-export the Embedder Protocol for typing convenience.  Task 5 will
# define the canonical Protocol in ``repository.protocols.py``.  Until
# then, fall back to a local placeholder so this module imports cleanly.
try:
    from doc3gpp.repository.protocols import Embedder
except ImportError:  # pragma: no cover - Task 5 has not landed yet

    @runtime_checkable
    class Embedder(Protocol):  # type: ignore[no-redef]
        """Placeholder Protocol for embedders (Task 5 will replace)."""

        def encode(self, texts: list[str]) -> np.ndarray:
            ...

        @property
        def dim(self) -> int:
            ...


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model = None

    def _load_model(self):
        from sentence_transformers import SentenceTransformer

        return SentenceTransformer(self._model_name)

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        if self._model is None:
            try:
                self._model = self._load_model()
            except (OSError, Exception) as exc:
                raise EmbedderUnavailableError(
                    f"failed to load embedding model {self._model_name!r}: {exc}"
                ) from exc
        vec = self._model.encode(texts, convert_to_numpy=True)
        return vec.astype(np.float32, copy=False)

    @property
    def dim(self) -> int:
        if self._model is None:
            try:
                self._model = self._load_model()
            except (OSError, Exception) as exc:
                raise EmbedderUnavailableError(
                    f"failed to load embedding model {self._model_name!r}: {exc}"
                ) from exc
        return int(self._model.get_sentence_embedding_dimension())
