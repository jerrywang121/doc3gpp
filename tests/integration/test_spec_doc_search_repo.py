# tests/integration/test_spec_doc_search_repo.py
"""Integration tests for the specdata FTS5 + vector repos (Task 8)."""
import pytest
from sqlalchemy import text

from doc3gpp.models.search import SearchQueryError
from doc3gpp.models.spec_doc import ChunkDraft, SpecDocSearchFilters
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_specdata_engine
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
                sections="5.1 Handover",
                tables="Table 1 Values",
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
    assert hits[0].sections == "5.1 Handover"
    assert hits[0].tables == "Table 1 Values"
    assert "text" in hits[0].previews


def test_stopwords_only_raises(sqlite_env):
    create_schema("all")
    fts = SQLAlchemySpecDocSearchRepository()
    with pytest.raises(SearchQueryError):
        fts.search("the and of", SpecDocSearchFilters())


def test_sections_filter_hits_combined_column(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository()
    _seed(repo)
    fts = SQLAlchemySpecDocSearchRepository()
    fts.upsert_for_version("38.331", "18.5.0")
    hits = fts.search("handover", SpecDocSearchFilters(sections="%5.1%"))
    assert len(hits) == 1 and hits[0].chunk_id == "38.331@18.5.0#0"


def test_fts_search_returns_and_filters_combined_table_metadata(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository()
    repo.record_download(
        "38.331", "19.0.0", release="Rel-19", ftp_url="https://x", docx_count=1
    )
    repo.replace_chunks(
        "38.331",
        "19.0.0",
        release="Rel-19",
        drafts=[
            ChunkDraft(0, "a.docx", "5 Scope", "Table 1 UE values", "handover UE"),
            ChunkDraft(0, "a.docx", "6 Other", "Table 2 Timers", "handover timers"),
        ],
    )
    fts = SQLAlchemySpecDocSearchRepository()
    fts.upsert_for_version("38.331", "19.0.0")
    hits = fts.search(
        "handover", SpecDocSearchFilters(tables="%UE values%", limit=20)
    )
    assert [hit.tables for hit in hits] == ["Table 1 UE values"]
    assert hits[0].sections == "5 Scope"


def _seed_rich_filter_rows(repo):
    repo.record_download(
        "38.331", "20.0.0", release="Rel-20", ftp_url="https://x", docx_count=1
    )
    repo.replace_chunks(
        "38.331",
        "20.0.0",
        release="Rel-20",
        drafts=[
            ChunkDraft(0, "a.docx", None, None, "handover null"),
            ChunkDraft(0, "a.docx", "5 Scope", None, "handover section"),
            ChunkDraft(0, "a.docx", None, "Table 1 Values", "handover table"),
            ChunkDraft(0, "a.docx", "6 Other", "Table 2 Timers", "handover both"),
            ChunkDraft(0, "a.docx", "5 Scope", "Table 1 Values", "handover both filters"),
        ],
    )


def test_fts_null_and_not_null_filters_preserve_nullable_sections(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository()
    _seed_rich_filter_rows(repo)
    fts = SQLAlchemySpecDocSearchRepository()
    fts.upsert_for_version("38.331", "20.0.0")

    null_hits = fts.search(
        "handover", SpecDocSearchFilters(version="20.0.0", sections="null", limit=20)
    )
    not_null_hits = fts.search(
        "handover", SpecDocSearchFilters(version="20.0.0", sections="not-null", limit=20)
    )

    assert [hit.chunk_index for hit in null_hits] == [0, 2]
    assert [hit.chunk_index for hit in not_null_hits] == [1, 3, 4]


def test_fts_null_and_not_null_filters_preserve_nullable_tables(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository()
    _seed_rich_filter_rows(repo)
    fts = SQLAlchemySpecDocSearchRepository()
    fts.upsert_for_version("38.331", "20.0.0")

    null_hits = fts.search(
        "handover", SpecDocSearchFilters(version="20.0.0", tables="null", limit=20)
    )
    not_null_hits = fts.search(
        "handover", SpecDocSearchFilters(version="20.0.0", tables="not-null", limit=20)
    )

    assert [hit.chunk_index for hit in null_hits] == [0, 1]
    assert [hit.chunk_index for hit in not_null_hits] == [2, 3, 4]


def test_fts_rich_metadata_filters_support_positive_negated_and_and_semantics(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository()
    _seed_rich_filter_rows(repo)
    fts = SQLAlchemySpecDocSearchRepository()
    fts.upsert_for_version("38.331", "20.0.0")

    positive_sections = fts.search(
        "handover", SpecDocSearchFilters(version="20.0.0", sections="%5 Scope%", limit=20)
    )
    positive_tables = fts.search(
        "handover", SpecDocSearchFilters(version="20.0.0", tables="%Table 1%", limit=20)
    )
    negated_tables = fts.search(
        "handover", SpecDocSearchFilters(version="20.0.0", tables="!%Timers%", limit=20)
    )
    both = fts.search(
        "handover",
        SpecDocSearchFilters(
            version="20.0.0",
            sections="%5 Scope%",
            tables="%Table 1%",
            limit=20,
        ),
    )

    assert [hit.chunk_index for hit in positive_sections] == [1, 4]
    assert [hit.chunk_index for hit in positive_tables] == [2, 4]
    assert [hit.chunk_index for hit in negated_tables] == [2, 4]
    assert [hit.chunk_index for hit in both] == [4]


def test_stale_only_rebuild_uses_one_cutoff_for_all_batches(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository()
    for version in ("18.0.0", "19.0.0"):
        repo.record_download(
            "38.331", version, release="Rel-18", ftp_url="https://x", docx_count=1
        )
        repo.replace_chunks(
            "38.331",
            version,
            release="Rel-18",
            drafts=[ChunkDraft(0, "a.docx", "5 Scope", None, "handover")],
        )
        repo.record_parsed("38.331", version, chunk_count=1)

    with get_specdata_engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE spec_doc_sources SET parsed_at = CASE version "
                "WHEN '18.0.0' THEN '2026-01-02 00:00:00.000000' "
                "WHEN '19.0.0' THEN '2026-01-01 00:00:00.000000' END"
            )
        )
        conn.execute(
            text(
                "INSERT INTO spec_doc_search_meta (key, value) "
                "VALUES ('last_indexed_parsed_at', '2025-12-31 00:00:00.000000')"
            )
        )

    from doc3gpp.services.spec_doc_search_service import SpecDocSearchService

    service = SpecDocSearchService()
    list(service.rebuild(batch_size=1, resume=False, stale_only=True, quiet=True))

    with get_specdata_engine().begin() as conn:
        indexed = conn.execute(text("SELECT COUNT(*) FROM spec_doc_search")).scalar_one()
    assert indexed == 2


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


def test_vector_knn_filters_by_combined_table_metadata(sqlite_env):
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
            ChunkDraft(
                file_order=0,
                source_file="a.docx",
                sections="5 Scope",
                tables="Table 1 UE values",
                text="hello",
            ),
            ChunkDraft(
                file_order=0,
                source_file="a.docx",
                sections="6 Other",
                tables="Table 2 Timers",
                text="world",
            ),
        ],
    )
    vec = _vec_repo(sqlite_env)
    dim = vec._dim
    a = np.zeros(dim, dtype=np.float32)
    a[0] = 1.0
    b = np.zeros(dim, dtype=np.float32)
    b[1] = 1.0
    vec.upsert_for_version("38.331", "18.5.0", [a, b])

    hits = vec.knn(
        a,
        limit=10,
        filters=SpecDocSearchFilters(tables="%UE values%"),
    )

    assert [chunk_id for chunk_id, _distance in hits] == [
        "38.331@18.5.0#0"
    ]


def test_vector_knn_release_filter_is_exact(sqlite_env):
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
        drafts=[ChunkDraft(0, "a.docx", None, None, "handover")],
    )
    vec = _vec_repo(sqlite_env)
    vector = np.zeros(vec._dim, dtype=np.float32)
    vector[0] = 1.0
    vec.upsert_for_version("38.331", "18.5.0", [vector])

    exact = vec.knn(
        vector,
        limit=10,
        filters=SpecDocSearchFilters(release="Rel-18"),
    )
    wildcard = vec.knn(
        vector,
        limit=10,
        filters=SpecDocSearchFilters(release="Rel-1%"),
    )

    assert [chunk_id for chunk_id, _distance in exact] == [
        "38.331@18.5.0#0"
    ]
    assert wildcard == []
