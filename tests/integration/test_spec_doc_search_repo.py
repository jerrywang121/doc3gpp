# tests/integration/test_spec_doc_search_repo.py
"""Integration tests for the specdata FTS5 + vector repos (Task 8)."""
import pytest

from doc3gpp.models.search import SearchQueryError
from doc3gpp.models.spec_doc import ChunkDraft, SpecDocSearchFilters
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.spec_doc_search_sql import (
    SQLAlchemySpecDocSearchRepository,
)
from doc3gpp.storage.repositories.spec_doc_sql import SQLAlchemySpecDocRepository


def _seed(repo):
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


def test_fts5_search(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository()
    _seed(repo)
    fts = SQLAlchemySpecDocSearchRepository()
    fts.upsert_for_version("38.331", "18.5.0")
    hits = fts.search("handover", SpecDocSearchFilters(spec_id="38.331"))
    assert len(hits) == 1 and hits[0].chunk_id == "38.331@18.5.0#0"
    assert "text" in hits[0].previews


def test_stopwords_only_raises(sqlite_env):
    create_schema("all")
    fts = SQLAlchemySpecDocSearchRepository()
    with pytest.raises(SearchQueryError):
        fts.search("the and of", SpecDocSearchFilters())


def test_section_filter_hits_combined_column(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository()
    _seed(repo)
    fts = SQLAlchemySpecDocSearchRepository()
    fts.upsert_for_version("38.331", "18.5.0")
    hits = fts.search("handover", SpecDocSearchFilters(section="%5.1%"))
    assert len(hits) == 1 and hits[0].chunk_id == "38.331@18.5.0#0"


def test_remove_for_version(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository()
    _seed(repo)
    fts = SQLAlchemySpecDocSearchRepository()
    fts.upsert_for_version("38.331", "18.5.0")
    fts.remove_for_version("38.331", "18.5.0")
    hits = fts.search("handover", SpecDocSearchFilters())
    assert hits == []


def test_rebuild_batch_and_cursor_round_trip(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository()
    _seed(repo)
    fts = SQLAlchemySpecDocSearchRepository()
    assert fts.count_versions_to_index(stale_only=False) == 1
    batches = list(
        fts.rebuild_batch(batch_size=10, after_id=None, stale_only=False)
    )
    assert batches == [[("38.331", "18.5.0")]]
    assert fts.get_resume_cursor() is None
    fts.set_resume_cursor("38.331@18.5.0")
    assert fts.get_resume_cursor() == "38.331@18.5.0"
    fts.clear_resume_cursor()
    assert fts.get_resume_cursor() is None
    assert fts.status().row_count == 0


def _vec_repo(sqlite_env):
    pytest.importorskip("sqlite_vec")
    from doc3gpp.storage.repositories.spec_doc_vector_sql import (
        SQLAlchemySpecDocVectorRepository,
    )

    return SQLAlchemySpecDocVectorRepository()


def test_vector_upsert_knn_and_remove(sqlite_env):
    import numpy as np

    create_schema("all")
    repo = SQLAlchemySpecDocRepository()
    repo.record_download(
        "38.331", "18.5.0", release="Rel-18", ftp_url="https://x", docx_count=1
    )
    repo.replace_chunks(
        "38.331",
        "18.5.0",
        release="Rel-18",
        drafts=[
            ChunkDraft(file_order=0, source_file="a.docx", text="hello"),
            ChunkDraft(file_order=0, source_file="a.docx", text="world"),
        ],
    )
    vec = _vec_repo(sqlite_env)
    dim = vec._dim
    a = np.zeros(dim, dtype=np.float32)
    a[0] = 1.0
    b = np.zeros(dim, dtype=np.float32)
    vec.upsert_for_version("38.331", "18.5.0", [a, b])
    q = np.zeros(dim, dtype=np.float32)
    q[0] = 1.0
    hits = vec.knn(q, limit=10)
    assert hits and hits[0][0] == "38.331@18.5.0#0"
    assert all(isinstance(cid, str) and isinstance(d, float) for cid, d in hits)
    vec.remove_for_version("38.331", "18.5.0")
    assert vec.knn(q, limit=10) == []
