from types import SimpleNamespace
from unittest.mock import MagicMock

from doc3gpp.models.spec_doc import SpecDocSearchFilters
from doc3gpp.services.spec_doc_semantic_service import SpecDocSemanticService


def test_hybrid_knn_limit_is_clamped_to_sqlite_vec_k_limit():
    fts5 = MagicMock()
    fts5.search.return_value = []
    vec = MagicMock()
    vec.knn.return_value = []
    service = SpecDocSemanticService(
        fts5_service=fts5,
        embedder=SimpleNamespace(encode=lambda texts: [[0.0] for _ in texts]),
        vector_repo=vec,
        settings=SimpleNamespace(
            semantic_search=SimpleNamespace(fanout_multiplier=100, rrf_k=60)
        ),
    )

    service.search(
        "q",
        fts5_query="q",
        filters=SpecDocSearchFilters(),
        limit=150,
        fts5_weight=0.5,
    )

    assert vec.knn.call_args.kwargs["limit"] == 4096
