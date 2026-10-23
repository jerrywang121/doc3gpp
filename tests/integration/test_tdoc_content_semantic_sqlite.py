"""SQLite coverage for semantic content chunks (CR annexes and family documents)."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from unittest.mock import MagicMock

import numpy as np
import pytest
from sqlalchemy import text

from doc3gpp.models.search import SearchFilters
from doc3gpp.models.tdoc_content_families import TDOC_CONTENT_STORES, TDocContentStore
from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
from doc3gpp.models.tdoc_document import TDocDocument, markdown_member_name
from doc3gpp.models.tdoc_identity import document_id_for
from doc3gpp.services.semantic_search_service import SemanticSearchService
from doc3gpp.services.tdoc_search_facade import TDocSearchFacade
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import TDocORM
from doc3gpp.storage.db.session import get_engine, get_session_factory
from doc3gpp.storage.repositories.tdoc_content_sql import (
    SQLAlchemyTDocContentRepository,
)
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
from doc3gpp.storage.repositories.vector_sql import SQLAlchemyVectorIndexRepository

pytestmark = pytest.mark.semantic

STORES = {store.key: store for store in TDOC_CONTENT_STORES}
WID: TDocContentStore = STORES["wid"]

NEEDLE = "zyxqneedle"
DIM = 384
_MEETING_ID = 100

_WID_ID = "R5-2600101"
_WID_URL = "r5/docs/r5-2600101.zip"
_WID_MAIN = "WID/main.docx"
_WID_ANNEX = "WID/annex/support.docx"

_OTHER_ID = "R5-2600103"
_OTHER_URL = "r5/docs/r5-2600103.zip"

_CR_ID = "R5-2600102"
_CR_URL = "r5/docs/r5-2600102.zip"
_CR_MAIN = "CRs/main.docx"
_CR_ANNEX = "CRs/annex/support.docx"

_MAIN_TEXT = "# Main body\n\nplain main text"


class _Embedder:
    dim = DIM

    def encode(self, texts):
        vectors = np.zeros((len(texts), DIM), dtype=np.float32)
        for row, text_value in enumerate(texts):
            vectors[row, 1 if NEEDLE in text_value else 0] = 1.0
        return vectors


def _service(settings=None) -> SemanticSearchService:
    return SemanticSearchService(
        MagicMock(),
        _Embedder(),
        SQLAlchemyVectorIndexRepository(),
        settings or get_settings(),
    )


def _seed_meeting() -> None:
    create_schema()
    with get_engine().begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tsgs (tsg_name, short_name, description) "
                "VALUES ('RAN WG1', 'RAN1', 'TSG RAN WG1')"
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO meetings (
                    meeting_id, name, title, location, tsg, start_date,
                    end_date, ftp_url, tdoc_list_last_sync
                ) VALUES (
                    100, 'RAN1#100', 'RAN1#100', 'Online', 'RAN1',
                    '2026-01-01', '2026-01-05',
                    'https://www.3gpp.org/ftp/meetings/RAN1_100',
                    '2026-01-05T00:00:00'
                )
                """
            )
        )


def _seed_tdoc(tdoc_id: str, title: str, tdoc_type: str) -> None:
    with get_session_factory()() as session:
        session.add(
            TDocORM(
                tdoc_id=tdoc_id,
                title=title,
                meeting_id=_MEETING_ID,
                type=tdoc_type,
                status="Agreed",
                uploaded_date=date(2026, 1, 2),
                release="Rel-18",
                spec="38.300",
            )
        )
        session.commit()


def _sha(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def _document(ftp_url: str, source_file: str, markdown: str, file_order: int) -> TDocDocument:
    document_id = document_id_for(ftp_url, source_file)
    return TDocDocument(
        document_id=document_id,
        source_file=source_file,
        markdown_file=markdown_member_name(document_id, source_file),
        file_order=file_order,
        markdown_text=markdown,
        content_hash=_sha(markdown),
    )


def _meta(tdoc_id: str, ftp_url: str, doc_filename: str, document_count: int) -> TDocExtractMeta:
    return TDocExtractMeta(
        ftp_url=ftp_url,
        tdoc_id=tdoc_id,
        cache_file=f"{tdoc_id.lower()}.zip",
        doc_filename=doc_filename,
        document_count=document_count,
    )


def _write_family(
    tdoc_id: str, ftp_url: str, documents: tuple[tuple[str, str], ...],
) -> None:
    """Write a WID bundle whose first ``(source, markdown)`` pair is the main."""
    bundle = tuple(
        _document(ftp_url, source, markdown, order)
        for order, (source, markdown) in enumerate(documents)
    )
    meta = _meta(tdoc_id, ftp_url, documents[0][0], len(bundle))
    SQLAlchemyTDocContentRepository().replace_family_bundle(WID, meta, bundle)


def _write_cr(
    tdoc_id: str,
    ftp_url: str,
    main_filename: str,
    annexes: tuple[tuple[str, str], ...],
) -> None:
    bundle = tuple(
        _document(ftp_url, source, markdown, order)
        for order, (source, markdown) in enumerate(annexes)
    )
    details = TDocCRDetails(tdoc_id=tdoc_id, ftp_url=ftp_url, title="CR cover")
    SQLAlchemyTDocContentRepository().replace_cr_bundle(
        details,
        None,
        None,
        _meta(tdoc_id, ftp_url, main_filename, len(bundle)),
        bundle,
    )


def _mark_date_checkpoint() -> None:
    """Record a date checkpoint past every seeded upload, so only content freshness is under test.

    The checkpoint is one day after the newest upload, so the ``>=`` date cutoff selects
    nothing. A completed rebuild stamps the newest upload day instead, and that day is
    re-selected by design; these tests index directly and do not exercise that case.
    """
    with get_engine().begin() as connection:
        latest = connection.execute(text("SELECT MAX(uploaded_date) FROM tdocs")).scalar()
        checkpoint = date.fromisoformat(str(latest)) + timedelta(days=1)
        connection.execute(
            text(
                "INSERT INTO vec_meta (key, value) "
                "VALUES ('last_indexed_uploaded_date', :value) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            ),
            {"value": checkpoint.isoformat()},
        )


def _chunk_ids(tdoc_id: str) -> list[str]:
    with get_engine().begin() as connection:
        rows = connection.execute(
            text("SELECT chunk_id FROM vec_tdoc_embeddings WHERE tdoc_id = :tdoc_id"),
            {"tdoc_id": tdoc_id},
        ).all()
    return [row[0] for row in rows]


def test_content_signatures_list_only_content_documents(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Metadata title for WID", "WID new")
    _write_family(
        _WID_ID,
        _WID_URL,
        ((_WID_MAIN, _MAIN_TEXT), (_WID_ANNEX, f"# Annex\n\n{NEEDLE} text")),
    )
    _service().index_for_tdoc(_WID_ID)

    signatures = SQLAlchemyVectorIndexRepository().content_signatures()

    assert signatures == {
        _WID_ID: frozenset(
            {
                (document_id_for(_WID_URL, _WID_MAIN), _sha(_MAIN_TEXT)),
                (
                    document_id_for(_WID_URL, _WID_ANNEX),
                    _sha(f"# Annex\n\n{NEEDLE} text"),
                ),
            }
        ),
    }


def test_cr_main_gets_no_content_chunks_and_cr_annex_does(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_CR_ID, "CR parent title", "CR")
    _write_cr(
        _CR_ID,
        _CR_URL,
        _CR_MAIN,
        ((_CR_ANNEX, f"# Annex\n\n{NEEDLE} in the annex"),),
    )

    _service().index_for_tdoc(_CR_ID)

    chunk_ids = _chunk_ids(_CR_ID)
    main_id = document_id_for(_CR_URL, _CR_MAIN)
    annex_id = document_id_for(_CR_URL, _CR_ANNEX)
    assert not any(main_id in chunk_id for chunk_id in chunk_ids)
    assert any(f"#{annex_id}#" in chunk_id for chunk_id in chunk_ids)


def test_corrupt_content_marks_only_its_tdoc_stale(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Metadata title for WID", "WID new")
    _seed_tdoc(_OTHER_ID, "Metadata title for other WID", "WID new")
    _write_family(
        _WID_ID,
        _WID_URL,
        ((_WID_MAIN, _MAIN_TEXT), (_WID_ANNEX, "# Annex\n\nannex text")),
    )
    _write_family(_OTHER_ID, _OTHER_URL, ((_WID_MAIN, _MAIN_TEXT),))
    _service().index_for_tdoc(_WID_ID)
    _service().index_for_tdoc(_OTHER_ID)
    _mark_date_checkpoint()
    repo = SQLAlchemyVectorIndexRepository()
    assert repo.count_tdocs_to_index(stale_only=True) == 0

    with get_engine().begin() as connection:
        connection.execute(
            text(
                "UPDATE tdoc_wid_contents SET content = :blob WHERE tdoc_id = :tdoc_id"
            ),
            {"blob": b"not a json payload", "tdoc_id": _WID_ID},
        )

    assert repo.count_tdocs_to_index(stale_only=True) == 1
    stale = [
        tdoc_id
        for batch in repo.rebuild_batch(batch_size=10, after_id=None, stale_only=True)
        for tdoc_id in batch
    ]
    assert stale == [_WID_ID]
    assert repo.status().is_stale is True
    with pytest.raises(ValueError, match="cannot be decoded"):
        _service().index_for_tdoc(_WID_ID)


def test_content_documents_order_main_first_then_path(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Metadata title for WID", "WID new")
    _write_family(
        _WID_ID,
        _WID_URL,
        (
            (_WID_MAIN, _MAIN_TEXT),
            ("WID/annex/zeta.docx", "zeta text"),
            ("WID/annex/alpha.docx", "alpha text"),
        ),
    )
    _seed_tdoc(_CR_ID, "CR parent title", "CR")
    _write_cr(
        _CR_ID,
        _CR_URL,
        _CR_MAIN,
        (("CRs/annex/zeta.docx", "zeta text"), ("CRs/annex/alpha.docx", "alpha text")),
    )

    vec_repo = SQLAlchemyVectorIndexRepository()
    family_order = [
        document_id
        for document_id, _hash, _markdown in vec_repo.content_documents_for_tdoc(_WID_ID)
    ]
    cr_order = [
        document_id
        for document_id, _hash, _markdown in vec_repo.content_documents_for_tdoc(_CR_ID)
    ]

    assert family_order == [
        document_id_for(_WID_URL, _WID_MAIN),
        document_id_for(_WID_URL, "WID/annex/alpha.docx"),
        document_id_for(_WID_URL, "WID/annex/zeta.docx"),
    ]
    assert cr_order == [
        document_id_for(_CR_URL, "CRs/annex/alpha.docx"),
        document_id_for(_CR_URL, "CRs/annex/zeta.docx"),
    ]


def test_blank_annex_is_not_content_stale(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Metadata title for WID", "WID new")
    _write_family(_WID_ID, _WID_URL, ((_WID_MAIN, _MAIN_TEXT), (_WID_ANNEX, "")))
    _mark_date_checkpoint()
    repo = SQLAlchemyVectorIndexRepository()
    assert repo.count_tdocs_to_index(stale_only=True) == 1

    _service().index_for_tdoc(_WID_ID)

    assert repo.status().is_stale is False
    assert repo.count_tdocs_to_index(stale_only=True) == 0
    assert list(repo.rebuild_batch(batch_size=10, after_id=None, stale_only=True)) == []


def test_changed_content_hash_marks_status_stale(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Metadata title for WID", "WID new")
    _write_family(
        _WID_ID, _WID_URL, ((_WID_MAIN, _MAIN_TEXT), (_WID_ANNEX, "# Annex\n\nversion one")),
    )
    _service().index_for_tdoc(_WID_ID)
    _mark_date_checkpoint()
    repo = SQLAlchemyVectorIndexRepository()
    assert repo.status().is_stale is False

    _write_family(
        _WID_ID, _WID_URL, ((_WID_MAIN, _MAIN_TEXT), (_WID_ANNEX, "# Annex\n\nversion two")),
    )

    assert repo.status().is_stale is True
    assert repo.count_tdocs_to_index(stale_only=True) == 1

    _service().index_for_tdoc(_WID_ID)

    assert repo.status().is_stale is False


def test_document_beyond_content_cap_is_not_content_stale(sqlite_env) -> None:
    settings = get_settings()
    settings.semantic_search.chunk_size = 4
    settings.semantic_search.chunk_overlap = 0
    settings.semantic_search.max_content_chunks_per_tdoc = 2
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Metadata title for WID", "WID new")
    _write_family(_WID_ID, _WID_URL, ((_WID_MAIN, "a b c d e f"), (_WID_ANNEX, "g h")))
    _service(settings).index_for_tdoc(_WID_ID)
    _mark_date_checkpoint()
    repo = SQLAlchemyVectorIndexRepository()

    assert repo.content_signatures() == {
        _WID_ID: frozenset({(document_id_for(_WID_URL, _WID_MAIN), _sha("a b c d e f"))}),
    }
    assert repo.status().is_stale is False
    assert repo.count_tdocs_to_index(stale_only=True) == 0


def test_annex_text_found_by_semantic_search(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Metadata title for WID", "WID new")
    annex_markdown = f"# Annex\n\n{NEEDLE} appears only here"
    _write_family(_WID_ID, _WID_URL, ((_WID_MAIN, _MAIN_TEXT), (_WID_ANNEX, annex_markdown)))
    _service().index_for_tdoc(_WID_ID)

    hits = _service().search(
        NEEDLE, fts5_query=None, filters=SearchFilters(), limit=5, fts5_weight=0.5,
    )

    assert [hit.tdoc_id for hit in hits] == [_WID_ID]
    annex_id = document_id_for(_WID_URL, _WID_ANNEX)
    best_document = hits[0].document_hits[0]
    assert best_document.document_id == annex_id
    assert best_document.chunk_id == f"{_WID_ID}#{annex_id}#{_sha(annex_markdown)}#0"
    assert best_document.distance == pytest.approx(0.0, abs=1e-6)
    assert hits[0].best_chunk_id == best_document.chunk_id


def test_chunk_hit_matched_document_resolves_source_and_url(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Metadata title for WID", "WID new")
    _write_family(
        _WID_ID,
        _WID_URL,
        ((_WID_MAIN, _MAIN_TEXT), (_WID_ANNEX, f"# Annex\n\n{NEEDLE} here")),
    )
    _service().index_for_tdoc(_WID_ID)
    facade = TDocSearchFacade(
        fts5_service=None,
        semantic_service=_service(),
        source_repo=SQLAlchemyTDocRepository(),
        settings=get_settings(),
    )

    results = facade.search(text=None, semantic=NEEDLE, filters=SearchFilters(limit=5))

    annex_id = document_id_for(_WID_URL, _WID_ANNEX)
    matched = {doc.document_id: doc for doc in results[0].matched_documents}
    assert matched[annex_id].ftp_url == _WID_URL
    assert matched[annex_id].source_file == _WID_ANNEX


def test_rebuild_embeddings_with_cache_purged_uses_sql(sqlite_env, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Metadata title for WID", "WID new")
    annex_markdown = f"# Annex\n\n{NEEDLE} in the annex"
    _write_family(_WID_ID, _WID_URL, ((_WID_MAIN, _MAIN_TEXT), (_WID_ANNEX, annex_markdown)))

    list(_service().rebuild_embeddings(batch_size=10, stale_only=False, quiet=True))

    annex_id = document_id_for(_WID_URL, _WID_ANNEX)
    assert f"{_WID_ID}#{annex_id}#{_sha(annex_markdown)}#0" in _chunk_ids(_WID_ID)
    assert not (tmp_path / ".cache").exists()
