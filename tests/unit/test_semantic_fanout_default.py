from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np

from doc3gpp.models.search import SearchFilters
from doc3gpp.models.spec_doc import SpecDocSearchFilters
from doc3gpp.services.semantic_search_service import SemanticSearchService
from doc3gpp.services.spec_doc_semantic_service import SpecDocSemanticService
from doc3gpp.settings.schema import SemanticSearchSettings


def test_default_fanout_multiplier_is_8():
    assert SemanticSearchSettings().fanout_multiplier == 8


def test_tdoc_hybrid_default_fanout_requests_1200_knn_candidates():
    fts5 = MagicMock()
    fts5.search.return_value = []
    vec = MagicMock()
    vec.knn.return_value = []
    embedder = MagicMock()
    embedder.encode.return_value = np.zeros((1, 384), dtype=np.float32)
    service = SemanticSearchService(
        fts5,
        embedder,
        vec,
        SimpleNamespace(semantic_search=SemanticSearchSettings()),
    )

    service.search(
        "q",
        fts5_query="q",
        filters=SearchFilters(),
        limit=150,
        fts5_weight=0.5,
    )

    assert vec.knn.call_args.kwargs["limit"] == 1200


def test_spec_doc_hybrid_default_fanout_requests_1200_knn_candidates():
    fts5 = MagicMock()
    fts5.search.return_value = []
    vec = MagicMock()
    vec.knn.return_value = []
    service = SpecDocSemanticService(
        fts5_service=fts5,
        embedder=SimpleNamespace(encode=lambda texts: [[0.0] for _ in texts]),
        vector_repo=vec,
        settings=SimpleNamespace(semantic_search=SemanticSearchSettings()),
    )

    service.search(
        "q",
        fts5_query="q",
        filters=SpecDocSearchFilters(),
        limit=150,
        fts5_weight=0.5,
    )

    assert vec.knn.call_args.kwargs["limit"] == 1200
