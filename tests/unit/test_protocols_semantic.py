from __future__ import annotations

from collections.abc import Iterable

import numpy as np

from doc3gpp.models.search import SearchFilters, SearchIndexStatus
from doc3gpp.repository.protocols import Embedder, VectorIndexRepository


def test_embedder_protocol_signature():
    class Impl:
        def encode(self, texts: list[str]) -> np.ndarray:
            return np.zeros((len(texts), 4), dtype=np.float32)
        @property
        def dim(self) -> int:
            return 4
    e: Embedder = Impl()
    assert e.dim == 4
    assert e.encode(["a"]).shape == (1, 4)


def test_vector_index_repository_protocol_signature():
    class Impl:
        def upsert_chunks(self, tdoc_id: str, embeddings: list[np.ndarray]) -> None: ...
        def remove_for_tdoc(self, tdoc_id: str) -> None: ...
        def knn(self, query_vec, limit, filters=None): return []
        def rebuild_batch(self, batch_size, after_id, stale_only) -> Iterable[list[str]]: return iter([])
        def count_tdocs_to_index(self, stale_only) -> int: return 0
        def get_resume_cursor(self) -> str | None: return None
        def set_resume_cursor(self, tdoc_id: str) -> None: ...
        def status(self) -> SearchIndexStatus: ...
    r: VectorIndexRepository = Impl()
    assert callable(r.upsert_chunks)
