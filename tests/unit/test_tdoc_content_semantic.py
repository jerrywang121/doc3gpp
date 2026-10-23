"""Unit tests for semantic content chunks (CR annexes and family documents)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from pydantic import ValidationError

from doc3gpp.models.search import SearchFilters
from doc3gpp.models.semantic_search import SemanticDocumentHit
from doc3gpp.services.semantic_search_service import SemanticSearchService
from doc3gpp.settings.schema import SemanticSearchSettings
from doc3gpp.storage.db.migrate import create_schema

NEEDLE = "zyxqneedle"
DIM = 384


class _Embedder:
    dim = DIM

    def encode(self, texts):
        vectors = np.zeros((len(texts), DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            vectors[row, 1 if NEEDLE in text else 0] = 1.0
        return vectors


def _settings(*, chunk_size: int = 5, chunk_overlap: int = 1, max_content: int = 16):
    return SimpleNamespace(
        semantic_search=SimpleNamespace(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            max_chunks_per_tdoc=8,
            max_content_chunks_per_tdoc=max_content,
            fanout_multiplier=2,
            rrf_k=60,
        ),
    )


def _index(documents, *, embed_text: str = "meta title", settings=None):
    vec = MagicMock()
    vec.embed_text_for_tdoc.return_value = embed_text
    vec.content_documents_for_tdoc.return_value = list(documents)
    service = SemanticSearchService(MagicMock(), _Embedder(), vec, settings or _settings())
    service.index_for_tdoc("R5-1")
    vec.upsert_chunks.assert_called_once()
    return vec.upsert_chunks.call_args.args[1]


def _content_ids(chunks) -> list[str]:
    return [c.chunk_id for c in chunks if c.chunk_id.count("#") == 3]


def test_content_chunk_ids_embed_document_id_and_hash():
    chunks = _index(
        [("doc-annex", "hash-annex", "one two three four five six")],
        embed_text="",
    )

    assert [(c.chunk_id, c.chunk_index) for c in chunks] == [
        ("R5-1#doc-annex#hash-annex#0", 0),
        ("R5-1#doc-annex#hash-annex#1", 1),
    ]


def test_metadata_chunk_ids_unchanged():
    chunks = _index(
        [], embed_text="one two three four five six",
    )

    assert [c.chunk_id for c in chunks] == ["R5-1#0", "R5-1#1"]


def test_content_chunks_capped():
    long_text = " ".join(f"w{i}" for i in range(30))
    documents = [(f"doc-{n}", f"hash-{n}", long_text) for n in (1, 2, 3)]

    chunks = _index(
        documents, embed_text="", settings=_settings(max_content=16),
    )

    content = _content_ids(chunks)
    assert len(content) == 16
    assert {cid.split("#")[1] for cid in content} == {"doc-1", "doc-2"}


def test_zero_text_document_adds_no_content_chunks():
    chunks = _index(
        [("doc-blank", "hash-blank", " \n\t "), ("doc-annex", "hash-annex", "annex text")],
        embed_text="",
    )

    assert [c.chunk_id for c in chunks] == ["R5-1#doc-annex#hash-annex#0"]


def _knn_rows() -> list[tuple[str, str, int, float]]:
    return [
        ("R5-1", "R5-1#doc-a#hash-a#0", 0, 0.1),
        ("R5-1", "R5-1#0", 1, 0.2),
        ("R5-1", "R5-1#doc-a#hash-a#1", 2, 0.3),
        ("R5-2", "R5-2#0", 3, 0.4),
    ]


def test_semantic_only_returns_one_hit_per_tdoc():
    vec = MagicMock()
    vec.knn.return_value = _knn_rows()
    vec.matching_tdoc_ids.side_effect = lambda tdoc_ids, filters: frozenset(tdoc_ids)
    vec.get_tdocs_metadata.return_value = {}
    service = SemanticSearchService(MagicMock(), _Embedder(), vec, _settings())

    out = service.search(
        NEEDLE, fts5_query=None, filters=SearchFilters(), limit=10, fts5_weight=0.5,
    )

    assert [h.tdoc_id for h in out] == ["R5-1", "R5-2"]
    assert out[0].best_chunk_id == "R5-1#doc-a#hash-a#0"
    assert out[0].min_chunk_distance == 0.1
    assert out[0].document_hits == (
        SemanticDocumentHit(
            document_id="doc-a", chunk_id="R5-1#doc-a#hash-a#0", distance=0.1,
        ),
    )
    assert out[1].document_hits == ()


def test_hybrid_carries_document_hits():
    vec = MagicMock()
    vec.knn.return_value = _knn_rows()
    vec.get_tdocs_metadata.return_value = {}
    fts5 = MagicMock()
    fts5.search.return_value = []
    service = SemanticSearchService(fts5, _Embedder(), vec, _settings())

    out = service.search(
        NEEDLE, fts5_query="valid", filters=SearchFilters(), limit=10, fts5_weight=0.5,
    )

    by_id = {h.tdoc_id: h for h in out}
    assert by_id["R5-1"].best_chunk_id == "R5-1#doc-a#hash-a#0"
    assert by_id["R5-1"].document_hits == (
        SemanticDocumentHit(
            document_id="doc-a", chunk_id="R5-1#doc-a#hash-a#0", distance=0.1,
        ),
    )
    assert by_id["R5-2"].document_hits == ()


def test_max_content_chunks_per_tdoc_defaults_to_16_and_rejects_zero():
    assert SemanticSearchSettings().max_content_chunks_per_tdoc == 16
    with pytest.raises(ValidationError):
        SemanticSearchSettings(max_content_chunks_per_tdoc=0)


class _RecordingVec:
    def __init__(self, embed_text, documents) -> None:
        self._embed_text = embed_text
        self._documents = documents
        self.calls: list[tuple[str, str]] = []
        self.upserted: list = []

    def embed_text_for_tdoc(self, tdoc_id):
        self.calls.append(("embed_text_for_tdoc", tdoc_id))
        return self._embed_text

    def content_documents_for_tdoc(self, tdoc_id):
        self.calls.append(("content_documents_for_tdoc", tdoc_id))
        return list(self._documents)

    def upsert_chunks(self, tdoc_id, chunks):
        self.calls.append(("upsert_chunks", tdoc_id))
        self.upserted = list(chunks)

    def remove_for_tdoc(self, tdoc_id):
        self.calls.append(("remove_for_tdoc", tdoc_id))


def test_index_reads_embed_text_and_content_through_vector_repository(sqlite_env):
    create_schema()
    vec = _RecordingVec("meta title", [("doc-annex", "hash-annex", "annex text")])
    service = SemanticSearchService(MagicMock(), _Embedder(), vec, _settings())

    service.index_for_tdoc("R5-1")

    assert vec.calls == [
        ("embed_text_for_tdoc", "R5-1"),
        ("content_documents_for_tdoc", "R5-1"),
        ("upsert_chunks", "R5-1"),
    ]
    assert _content_ids(vec.upserted) == ["R5-1#doc-annex#hash-annex#0"]
