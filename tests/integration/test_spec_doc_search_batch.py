# tests/integration/test_spec_doc_search_batch.py
"""Batched spec-doc FTS5 upsert: parity with per-version upsert and fallback."""
from unittest.mock import MagicMock, call

from sqlalchemy import text

from doc3gpp.models.spec_doc import ChunkDraft, SpecDocSearchFilters
from doc3gpp.services.spec_doc_search_service import SpecDocSearchService
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_specdata_engine
from doc3gpp.storage.repositories.spec_doc_search_sql import (
    SQLAlchemySpecDocSearchRepository,
)
from doc3gpp.storage.repositories.spec_doc_sql import SQLAlchemySpecDocRepository


def _seed_version(repo, spec_id, version, texts):
    repo.record_download(
        spec_id, version, release="Rel-18", ftp_url="https://x", docx_count=1
    )
    repo.replace_chunks(
        spec_id,
        version,
        release="Rel-18",
        drafts=[
            ChunkDraft(
                file_order=0, source_file="a.docx", sections=f"{index} Sec",
                tables=None, text=body,
            )
            for index, body in enumerate(texts)
        ],
    )
    repo.record_parsed(spec_id, version, chunk_count=len(texts))


def _fts_rows():
    with get_specdata_engine().connect() as conn:
        return conn.execute(
            text(
                "SELECT chunk_id, spec_id, version, text, sections, tables, release "
                "FROM spec_doc_search ORDER BY chunk_id"
            )
        ).all()


def test_upsert_many_versions_matches_per_version_upsert(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository()
    _seed_version(repo, "38.331", "18.5.0", ["handover one", "timers two"])
    _seed_version(repo, "38.331", "19.0.0", ["handover three"])
    _seed_version(repo, "38.300", "18.5.0", ["handover four"])
    pairs = [("38.331", "18.5.0"), ("38.331", "19.0.0"), ("38.300", "18.5.0")]
    fts = SQLAlchemySpecDocSearchRepository()

    fts.upsert_many_versions(pairs)
    batched = _fts_rows()

    with get_specdata_engine().begin() as conn:
        conn.execute(text("DELETE FROM spec_doc_search"))
    for spec_id, version in pairs:
        fts.upsert_for_version(spec_id, version)

    assert len(batched) == 4
    assert _fts_rows() == batched


def test_upsert_many_versions_replaces_stale_rows(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository()
    _seed_version(repo, "38.331", "18.5.0", ["handover old"])
    _seed_version(repo, "38.300", "18.5.0", ["other"])
    fts = SQLAlchemySpecDocSearchRepository()
    fts.upsert_many_versions([("38.331", "18.5.0"), ("38.300", "18.5.0")])

    repo.replace_chunks(
        "38.331",
        "18.5.0",
        release="Rel-18",
        drafts=[ChunkDraft(0, "a.docx", "1 New", None, "timers new")],
    )
    fts.upsert_many_versions([("38.331", "18.5.0"), ("38.300", "18.5.0")])

    assert fts.search("handover", SpecDocSearchFilters(spec_id="38.331")) == []
    hits = fts.search("timers", SpecDocSearchFilters(spec_id="38.331"))
    assert [hit.chunk_id for hit in hits] == ["38.331@18.5.0#0"]
    assert len(_fts_rows()) == 2


def test_upsert_many_versions_stamps_watermark_like_per_version(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository()
    _seed_version(repo, "38.331", "18.5.0", ["handover one"])
    _seed_version(repo, "38.300", "18.5.0", ["handover two"])
    SQLAlchemySpecDocSearchRepository().upsert_many_versions(
        [("38.331", "18.5.0"), ("38.300", "18.5.0")]
    )
    with get_specdata_engine().connect() as conn:
        stored = conn.execute(
            text(
                "SELECT parsed_at FROM spec_doc_sources "
                "WHERE spec_id = '38.300' AND version = '18.5.0'"
            )
        ).scalar()
        mark = conn.execute(
            text(
                "SELECT value FROM spec_doc_search_meta "
                "WHERE key = 'last_indexed_parsed_at'"
            )
        ).scalar()
    assert mark == str(stored)


def test_rebuild_indexes_every_version_in_batches(sqlite_env):
    create_schema("all")
    repo = SQLAlchemySpecDocRepository()
    _seed_version(repo, "38.331", "18.5.0", ["handover one", "timers two"])
    _seed_version(repo, "38.331", "19.0.0", ["handover three"])
    _seed_version(repo, "38.300", "18.5.0", ["handover four"])

    list(SpecDocSearchService(quiet=True).rebuild(
        batch_size=2, resume=False, stale_only=False, quiet=True,
    ))

    assert len(_fts_rows()) == 4


def test_service_batch_failure_falls_back_per_version():
    repo = MagicMock()
    repo.upsert_many_versions.side_effect = RuntimeError("boom")
    svc = SpecDocSearchService(repo=repo, quiet=True)

    svc._upsert_batch([("38.331", "18.5.0"), ("38.300", "18.5.0")])

    assert repo.upsert_for_version.call_args_list == [
        call("38.331", "18.5.0"),
        call("38.300", "18.5.0"),
    ]
