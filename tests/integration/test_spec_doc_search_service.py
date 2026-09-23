# tests/integration/test_spec_doc_search_service.py
"""Integration tests for the spec-doc search services (Task 10)."""
import numpy as np
import pytest

from doc3gpp.models.spec_doc import (
    ChunkDraft,
    SpecDocHit,
    SpecDocSearchFilters,
    SpecDocSemanticHit,
)
from doc3gpp.services.spec_doc_search_service import SpecDocSearchService
from doc3gpp.services.spec_doc_semantic_service import (
    SpecDocSemanticService,
    rrf_merge,
)
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.spec_doc_sql import SQLAlchemySpecDocRepository


class FakeEmbedder:
    """Deterministic embedder: 'handover' texts -> e0, everything else -> e1."""

    def __init__(self, dim: int = 4, model_name: str = "fake-model") -> None:
        self._dim = dim
        self.model_name = model_name

    @property
    def dim(self) -> int:
        return self._dim

    def encode(self, texts: list[str]) -> np.ndarray:
        vecs = []
        for text in texts:
            vec = np.zeros(self._dim, dtype=np.float32)
            vec[0 if "handover" in text.lower() else 1] = 1.0
            vecs.append(vec)
        return np.stack(vecs)


def _seed():
    repo = SQLAlchemySpecDocRepository()
    repo.record_download(
        "38.331", "18.5.0", release="Rel-18", ftp_url="https://x", docx_count=1
    )
    repo.replace_chunks(
        "38.331",
        "18.5.0",
        release="Rel-18",
        drafts=[
            ChunkDraft(
                file_order=0,
                source_file="a.docx",
                section_no="5.1",
                section_title="Handover",
                text="handover procedure signalling",
            )
        ],
    )


def _seed_two():
    repo = SQLAlchemySpecDocRepository()
    repo.record_download(
        "38.331", "18.5.0", release="Rel-18", ftp_url="https://x", docx_count=1
    )
    repo.replace_chunks(
        "38.331",
        "18.5.0",
        release="Rel-18",
        drafts=[
            ChunkDraft(
                file_order=0,
                source_file="a.docx",
                section_no="5.1",
                section_title="Handover",
                text="handover procedure signalling",
            ),
            ChunkDraft(
                file_order=0,
                source_file="a.docx",
                section_no="5.2",
                section_title="Measurements",
                text="measurement configuration report",
            ),
        ],
    )


def _vec_repo(dim: int = 4):
    pytest.importorskip("sqlite_vec")
    from doc3gpp.storage.repositories.spec_doc_vector_sql import (
        SQLAlchemySpecDocVectorRepository,
    )

    vec = SQLAlchemySpecDocVectorRepository()
    vec.reset_for_rebuild(dim, "fake-model")
    return vec


def _hit(chunk_id: str) -> SpecDocHit:
    return SpecDocHit(
        chunk_id=chunk_id,
        spec_id="38.331",
        version="18.5.0",
        release="Rel-18",
        section_no=None,
        section_title=None,
        table_no=None,
        table_title=None,
        chunk_index=0,
        text="t",
        score=-1.0,
        previews={},
    )


def test_search_and_rebuild(sqlite_env):
    create_schema("all")
    _seed()
    svc = SpecDocSearchService()
    svc.upsert_for_version("38.331", "18.5.0")
    hits = svc.search("handover", SpecDocSearchFilters(spec_id="38.331"))
    assert hits and hits[0].spec_id == "38.331"
    seen = list(svc.rebuild(batch_size=10, resume=False, stale_only=False, quiet=True))
    assert seen  # yields RebuildProgress


def test_status_and_remove(sqlite_env):
    create_schema("all")
    _seed()
    svc = SpecDocSearchService()
    svc.upsert_for_version("38.331", "18.5.0")
    assert svc.status().row_count == 1
    svc.remove_for_version("38.331", "18.5.0")
    assert svc.search("handover", SpecDocSearchFilters()) == []


def test_rebuild_resume_and_stale_only(sqlite_env):
    create_schema("all")
    _seed()
    svc = SpecDocSearchService()
    first = list(svc.rebuild(batch_size=10, resume=False, stale_only=False, quiet=True))
    assert first and first[0].total == 1
    resumed = list(svc.rebuild(batch_size=10, resume=True, stale_only=False, quiet=True))
    assert resumed == []  # cursor sits at the last pair; nothing follows
    stale = list(svc.rebuild(batch_size=10, resume=False, stale_only=True, quiet=True))
    assert stale == []  # upsert stamped the watermark; nothing is newer


def test_rrf_merge_chunk_level_ordering():
    fts = [_hit("a"), _hit("b")]
    vec = [("b", 0.2), ("c", 0.1)]
    merged = rrf_merge(fts, vec, k=60, vector_weight=0.5, limit=10)
    assert all(isinstance(h, SpecDocSemanticHit) for h in merged)
    # b: both sides; a: FTS-only; c: vector-only (hit=None).
    assert [h.chunk_id for h in merged] == ["b", "a", "c"]
    assert merged[0].rank_fts5 == 1 and merged[0].rank_vec == 0
    assert merged[0].hit is not None
    assert merged[2].hit is None and merged[2].rank_fts5 is None
    assert merged[:2] == rrf_merge(fts, vec, k=60, vector_weight=0.5, limit=2)


def _semantic_service():
    fts = SpecDocSearchService()
    fts.upsert_for_version("38.331", "18.5.0")
    vec = _vec_repo()
    sem = SpecDocSemanticService(
        fts5_service=fts,
        embedder=FakeEmbedder(),
        vector_repo=vec,
        settings=get_settings(),
    )
    sem.index_for_version("38.331", "18.5.0")
    return sem


def test_semantic_index_and_pure_vector_search(sqlite_env):
    create_schema("all")
    _seed_two()
    sem = _semantic_service()
    hits = sem.search(
        "handover",
        fts5_query=None,
        filters=SpecDocSearchFilters(),
        limit=10,
        fts5_weight=0.5,
    )
    assert [h.chunk_id for h in hits] == ["38.331@18.5.0#0", "38.331@18.5.0#1"]
    assert all(h.rank_fts5 is None and h.hit is None for h in hits)


def test_semantic_hybrid_search(sqlite_env):
    create_schema("all")
    _seed_two()
    sem = _semantic_service()
    hits = sem.search(
        "handover procedure",
        fts5_query="handover",
        filters=SpecDocSearchFilters(),
        limit=10,
        fts5_weight=0.5,
    )
    assert hits and hits[0].chunk_id == "38.331@18.5.0#0"
    assert hits[0].hit is not None
    assert hits[0].rank_fts5 == 0 and hits[0].rank_vec == 0


def test_semantic_index_empty_version_removes(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository()
    repo.record_download(
        "38.331", "18.5.0", release="Rel-18", ftp_url="https://x", docx_count=0
    )
    vec = _vec_repo()
    sem = SpecDocSemanticService(
        fts5_service=SpecDocSearchService(),
        embedder=FakeEmbedder(),
        vector_repo=vec,
        settings=get_settings(),
    )
    sem.index_for_version("38.331", "18.5.0")  # no chunks: must not raise
    hits = sem.search(
        "handover",
        fts5_query=None,
        filters=SpecDocSearchFilters(),
        limit=10,
        fts5_weight=0.5,
    )
    assert hits == []


def test_factory_builders_succeed(sqlite_env):
    create_schema("all")
    from doc3gpp.services.factory import (
        build_spec_doc_search_service,
        build_spec_doc_semantic_service,
    )

    svc = build_spec_doc_search_service()
    assert isinstance(svc, SpecDocSearchService)
    sem = build_spec_doc_semantic_service(embedder=FakeEmbedder())
    assert isinstance(sem, SpecDocSemanticService)
