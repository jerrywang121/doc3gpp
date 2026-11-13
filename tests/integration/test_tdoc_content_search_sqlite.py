"""SQLite coverage for FTS5 rows built from the per-type TDoc content tables."""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from datetime import date
from pathlib import Path

from sqlalchemy import select, text

from doc3gpp.models.search import SearchFilters
from doc3gpp.models.tdoc_content_families import TDOC_CONTENT_STORES, TDocContentStore
from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
from doc3gpp.models.tdoc_document import TDocDocument, markdown_member_name
from doc3gpp.models.tdoc_identity import document_id_for
from doc3gpp.parsers.tdoc_zip import build_markdown_archive
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.compression import decompress_json
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import CONTENT_ORM_BY_STORE_KEY, TDocORM
from doc3gpp.storage.db.session import get_engine, get_session_factory
from doc3gpp.storage.repositories.search_sql import SQLAlchemySearchIndexRepository
from doc3gpp.storage.repositories.tdoc_content_sql import (
    SQLAlchemyTDocContentRepository,
)

STORES = {store.key: store for store in TDOC_CONTENT_STORES}
WID = STORES["wid"]
LS = STORES["ls"]

_MEETING_ID = 100
_ANNEX_TERM = "zyxqvunique"
_MAIN_TERM = "zyxqmainterm"
_TAIL_TERM = "zyxqtailterm"

_WID_ID = "R5-2600101"
_WID_URL = "r5/docs/r5-2600101.zip"
_WID_MAIN = "WID/main.docx"
_WID_ANNEX = "WID/annex/support.docx"

_CR_ID = "R5-2600102"
_CR_URL = "r5/docs/r5-2600102.zip"
_CR_MAIN = "CRs/main.docx"
_CR_ANNEX = "CRs/annex/support.docx"


def _seed_meeting() -> None:
    create_schema()
    with get_engine().begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tsgs (tsg_name, short_name, description) "
                "VALUES ('RAN WG1 test fixture', 'RAN1', 'TSG RAN WG1')"
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


def _document(ftp_url: str, source_file: str, markdown: str, file_order: int) -> TDocDocument:
    document_id = document_id_for(ftp_url, source_file)
    return TDocDocument(
        document_id=document_id,
        source_file=source_file,
        markdown_file=markdown_member_name(document_id, source_file),
        file_order=file_order,
        markdown_text=markdown,
        content_hash=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
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
    store: TDocContentStore,
    tdoc_id: str,
    ftp_url: str,
    documents: tuple[tuple[str, str], ...],
) -> None:
    """Write a family bundle whose first ``(source, markdown)`` pair is the main."""
    bundle = tuple(
        _document(ftp_url, source, markdown, order)
        for order, (source, markdown) in enumerate(documents)
    )
    meta = _meta(tdoc_id, ftp_url, documents[0][0], len(bundle))
    SQLAlchemyTDocContentRepository().replace_family_bundle(store, meta, bundle)


def _write_cr(
    tdoc_id: str,
    ftp_url: str,
    main_filename: str,
    annexes: tuple[tuple[str, str], ...],
    cover_title: str,
) -> None:
    bundle = tuple(
        _document(ftp_url, source, markdown, order)
        for order, (source, markdown) in enumerate(annexes)
    )
    details = TDocCRDetails(tdoc_id=tdoc_id, ftp_url=ftp_url, title=cover_title)
    SQLAlchemyTDocContentRepository().replace_cr_bundle(
        details,
        None,
        None,
        _meta(tdoc_id, ftp_url, main_filename, len(bundle)),
        bundle,
    )


def _write_cr_archive(
    cache_dir: Path,
    tdoc_id: str,
    ftp_url: str,
    main_filename: str,
    main_markdown: str,
    annexes: tuple[tuple[str, str], ...],
) -> Path:
    """Write the derived Markdown archive, the only copy of the CR main body."""
    documents = (
        _document(ftp_url, main_filename, main_markdown, 0),
        *(
            _document(ftp_url, source, markdown, order)
            for order, (source, markdown) in enumerate(annexes, start=1)
        ),
    )
    archive = build_markdown_archive(
        documents,
        ftp_url=ftp_url,
        main_source_file=main_filename,
    )
    path = cache_dir / "markdown" / f"{tdoc_id.lower()}.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(archive)
    return path


def _stored_content_texts() -> list[str]:
    texts: list[str] = []
    with get_session_factory()() as session:
        for store in TDOC_CONTENT_STORES:
            orm = CONTENT_ORM_BY_STORE_KEY[store.key]
            for blob in session.scalars(select(orm.content)).all():
                texts.append(decompress_json(blob))
    return texts


def _document_sources(tdoc_id: str) -> list[str]:
    with get_engine().begin() as connection:
        rows = connection.execute(
            text(
                "SELECT source_file FROM tdoc_search "
                "WHERE tdoc_id = :tdoc_id AND row_kind = 'document' "
                "ORDER BY source_file"
            ),
            {"tdoc_id": tdoc_id},
        ).all()
    return [row[0] for row in rows]


def test_annex_only_term_finds_parent_with_matched_document(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Metadata title for WID", "WID new")
    _write_family(
        WID,
        _WID_ID,
        _WID_URL,
        (
            (_WID_MAIN, "# Main body\n\nplain main text"),
            (_WID_ANNEX, f"# Annex\n\n{_ANNEX_TERM} appears only here"),
        ),
    )
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)

    hits = repo.search(_ANNEX_TERM, SearchFilters(limit=10))

    assert [hit.tdoc_id for hit in hits] == [_WID_ID]
    assert [matched.source_file for matched in hits[0].matched_documents] == [
        _WID_ANNEX
    ]


def test_main_term_reports_main_document(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Metadata title for WID", "WID new")
    _write_family(
        WID,
        _WID_ID,
        _WID_URL,
        (
            (_WID_MAIN, f"# Main body\n\n{_MAIN_TERM} lives in the main"),
            (_WID_ANNEX, "# Annex\n\nunrelated annex text"),
        ),
    )
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)

    hits = repo.search(_MAIN_TERM, SearchFilters(limit=10))

    assert [hit.tdoc_id for hit in hits] == [_WID_ID]
    assert [matched.source_file for matched in hits[0].matched_documents] == [
        _WID_MAIN
    ]


def test_metadata_only_tdoc_remains_searchable_by_title(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Zyxqmetadataonly title", "discussion")
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)

    hits = repo.search("zyxqmetadataonly", SearchFilters(limit=10))

    assert [hit.tdoc_id for hit in hits] == [_WID_ID]
    assert hits[0].matched_documents == ()


def test_zero_text_document_indexes_without_error(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Zerotext title marker", "LS in")
    _write_family(
        LS,
        _WID_ID,
        _WID_URL,
        ((_WID_MAIN, ""), (_WID_ANNEX, "")),
    )
    repo = SQLAlchemySearchIndexRepository()

    repo.upsert(_WID_ID)

    hits = repo.search("zerotext", SearchFilters(limit=10))
    assert [hit.tdoc_id for hit in hits] == [_WID_ID]
    assert _document_sources(_WID_ID) == [_WID_ANNEX, _WID_MAIN]
    assert repo.status().is_stale is False


def test_fts_finds_text_beyond_semantic_cap(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Long family document", "WID revised")
    long_body = " ".join(["filler"] * 5000) + f" {_TAIL_TERM}"
    _write_family(WID, _WID_ID, _WID_URL, ((_WID_MAIN, long_body),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)

    hits = repo.search(_TAIL_TERM, SearchFilters(limit=10))

    assert [hit.tdoc_id for hit in hits] == [_WID_ID]
    assert [matched.source_file for matched in hits[0].matched_documents] == [
        _WID_MAIN
    ]


def test_cr_annex_term_is_indexed(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_CR_ID, "CR parent title", "CR")
    _write_cr(
        _CR_ID,
        _CR_URL,
        _CR_MAIN,
        ((_CR_ANNEX, f"# Annex\n\n{_ANNEX_TERM} in the annex"),),
        cover_title="Cover title",
    )
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_CR_ID)

    hits = repo.search(_ANNEX_TERM, SearchFilters(limit=10))

    assert [hit.tdoc_id for hit in hits] == [_CR_ID]
    assert [matched.source_file for matched in hits[0].matched_documents] == [
        _CR_ANNEX
    ]


def test_cr_main_only_term_not_indexed(sqlite_env, tmp_path, monkeypatch) -> None:
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(cache_dir))
    get_settings.cache_clear()
    _seed_meeting()
    _seed_tdoc(_CR_ID, "CR parent title", "CR")
    annexes = ((_CR_ANNEX, "# Annex\n\nannex text only"),)
    _write_cr(
        _CR_ID,
        _CR_URL,
        _CR_MAIN,
        annexes,
        cover_title="Coverfieldzyxq reason for change",
    )
    archive_path = _write_cr_archive(
        cache_dir,
        _CR_ID,
        _CR_URL,
        _CR_MAIN,
        f"# Main body\n\n{_MAIN_TERM} appears only in the CR main body",
        annexes,
    )
    main_member = markdown_member_name(document_id_for(_CR_URL, _CR_MAIN), _CR_MAIN)
    with zipfile.ZipFile(archive_path) as archive:
        assert _MAIN_TERM in archive.read(main_member).decode("utf-8")
    assert not any(_MAIN_TERM in text for text in _stored_content_texts())

    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_CR_ID)

    assert repo.search(_MAIN_TERM, SearchFilters(limit=10)) == []
    cover_hits = repo.search("coverfieldzyxq", SearchFilters(limit=10))
    assert [hit.tdoc_id for hit in cover_hits] == [_CR_ID]
    assert _document_sources(_CR_ID) == [_CR_ANNEX]


def test_changed_content_hash_reports_stale(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Hash change title", "WID new")
    _write_family(
        WID,
        _WID_ID,
        _WID_URL,
        ((_WID_MAIN, "# Main"), (_WID_ANNEX, "original annex text")),
    )
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    assert repo.count_tdocs_to_index(stale_only=True) == 0

    _write_family(
        WID,
        _WID_ID,
        _WID_URL,
        ((_WID_MAIN, "# Main"), (_WID_ANNEX, "revised annex text")),
    )
    assert repo.count_tdocs_to_index(stale_only=True) == 1

    repo.upsert(_WID_ID)
    assert repo.count_tdocs_to_index(stale_only=True) == 0


def test_removed_member_reports_stale(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Removed member title", "WID new")
    _write_family(
        WID,
        _WID_ID,
        _WID_URL,
        ((_WID_MAIN, "# Main"), (_WID_ANNEX, "annex text to remove")),
    )
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    assert repo.count_tdocs_to_index(stale_only=True) == 0

    _write_family(WID, _WID_ID, _WID_URL, ((_WID_MAIN, "# Main"),))
    assert repo.count_tdocs_to_index(stale_only=True) == 1

    repo.upsert(_WID_ID)

    assert repo.count_tdocs_to_index(stale_only=True) == 0
    assert _document_sources(_WID_ID) == [_WID_MAIN]


def test_content_text_column_and_preview_key(sqlite_env) -> None:
    _seed_meeting()
    with get_engine().begin() as connection:
        columns = [
            row[1]
            for row in connection.execute(text("PRAGMA table_info(tdoc_search)")).all()
        ]
    assert "content_text" in columns
    assert "document_text" not in columns

    _seed_tdoc(_CR_ID, "CR parent title", "CR")
    _write_cr(
        _CR_ID,
        _CR_URL,
        _CR_MAIN,
        ((_CR_ANNEX, f"annex {_ANNEX_TERM} marker"),),
        cover_title="Cover title",
    )
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_CR_ID)

    hits = repo.search(_ANNEX_TERM, SearchFilters(limit=10))

    previews = hits[0].matched_documents[0].previews
    assert set(previews) == {"content_text"}
    assert f"<<{_ANNEX_TERM}>>" in previews["content_text"]


def test_rebuild_after_cache_purge_uses_sql_only(sqlite_env, tmp_path, monkeypatch) -> None:
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(cache_dir))
    get_settings.cache_clear()
    markdown_dir = cache_dir / "markdown"
    markdown_dir.mkdir(parents=True)
    (markdown_dir / "r5-2600101.zip").write_bytes(b"derived archive")

    _seed_meeting()
    _seed_tdoc(_WID_ID, "Purged cache title", "WID new")
    _write_family(
        WID,
        _WID_ID,
        _WID_URL,
        ((_WID_MAIN, "# Main"), (_WID_ANNEX, f"{_ANNEX_TERM} from SQL")),
    )
    shutil.rmtree(cache_dir)

    repo = SQLAlchemySearchIndexRepository()
    for batch in repo.rebuild_batch(batch_size=10, after_id=None, stale_only=False):
        for tdoc_id in batch:
            repo.upsert(tdoc_id)

    hits = repo.search(_ANNEX_TERM, SearchFilters(limit=10))
    assert [hit.tdoc_id for hit in hits] == [_WID_ID]
    assert repo.count_tdocs_to_index(stale_only=True) == 0
    assert not cache_dir.exists()


def test_presentation_fts_finds_late_chunk(sqlite_env, monkeypatch) -> None:
    settings = get_settings().semantic_search
    monkeypatch.setattr(settings, "chunk_size", 4)
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Presentation parent", "WID new")
    body = " ".join(["filler"] * 68 + ["final", "distinctive", "ending", "tailmarker"])
    _write_family(WID, _WID_ID, _WID_URL, (
        ("Annexes/Radio.PPTX", body), ("legacy/Deck.ppt", body),
    ))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    hits = repo.search("tailmarker", SearchFilters(limit=10))
    assert [doc.source_file for doc in hits[0].matched_documents] == [
        "Annexes/Radio.PPTX", "legacy/Deck.ppt",
    ]
    for doc in hits[0].matched_documents:
        assert doc.matched_chunk.chunk_index == 17
        assert doc.matched_chunk.text == "final distinctive ending tailmarker"
        assert doc.matched_chunk.chunk_id == f"{_WID_ID}#{doc.document_id}#{hashlib.sha256(body.encode()).hexdigest()}#17"


def test_presentation_fts_preserves_spec_id_text(sqlite_env) -> None:
    from doc3gpp.cli_filters import SearchQueryBuilder

    _seed_meeting()
    _seed_tdoc(_WID_ID, "Presentation parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "Specification 38.331 here"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    query = SearchQueryBuilder("38.331").build()
    chunk = repo.search(query, SearchFilters(limit=10))[0].matched_documents[0].matched_chunk
    assert "38.331" in chunk.text
    assert "38_331" not in chunk.text
    assert not repo.status().is_stale


def test_presentation_fts_cross_chunk_and_keeps_parent(sqlite_env, monkeypatch) -> None:
    settings = get_settings().semantic_search
    monkeypatch.setattr(settings, "chunk_size", 4)
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Metadataonly", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha one two three omega four five six"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    hits = repo.search("alpha AND omega", SearchFilters(limit=1))
    assert len(hits) == 1
    assert hits[0].matched_documents[0].matched_chunk is None
    assert repo.search("Metadataonly", SearchFilters(limit=1))[0].matched_documents == ()


def test_presentation_fts_empty_is_current(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Empty", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "  "),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    with get_engine().begin() as conn:
        assert conn.execute(text("SELECT chunk_count FROM tdoc_presentation_search_ledger")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM tdoc_presentation_search")).scalar_one() == 0
    assert not repo.status().is_stale
    assert repo.count_tdocs_to_index(stale_only=True) == 0


def test_presentation_fts_detects_chunk_setting_change(sqlite_env, monkeypatch) -> None:
    settings = get_settings().semantic_search
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    monkeypatch.setattr(settings, "chunk_size", 4)
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha one two three omega"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    assert not repo.status().is_stale
    monkeypatch.setattr(settings, "chunk_size", 5)
    repo = SQLAlchemySearchIndexRepository()
    assert repo.status().is_stale
    assert repo.count_tdocs_to_index(stale_only=True) == 1
    assert list(repo.rebuild_batch(10, None, True)) == [[_WID_ID]]
    assert repo.search("omega", SearchFilters(limit=10))[0].matched_documents[0].matched_chunk is None
    repo.upsert(_WID_ID)
    assert not repo.status().is_stale
    assert repo.search("omega", SearchFilters(limit=10))[0].matched_documents[0].matched_chunk.text == "alpha one two three omega"


def test_presentation_fts_stale_entry_returns_null(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha original"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha changed"),))
    assert repo.search("alpha", SearchFilters(limit=10))[0].matched_documents[0].matched_chunk is None
    assert repo.status().is_stale
    assert list(repo.rebuild_batch(10, None, True)) == [[_WID_ID]]
    repo.upsert(_WID_ID)
    assert repo.search("alpha", SearchFilters(limit=10))[0].matched_documents[0].matched_chunk.text == "alpha changed"


def test_presentation_fts_remove_and_stale_rebuild(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.ppt", "alpha original"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    repo.remove(_WID_ID)
    with get_engine().begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM tdoc_search")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM tdoc_presentation_search")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM tdoc_presentation_search_ledger")).scalar_one() == 0
    assert list(repo.rebuild_batch(10, None, True)) == [[_WID_ID]]
    repo.upsert(_WID_ID)
    assert repo.search("alpha", SearchFilters(limit=10))[0].matched_documents[0].matched_chunk.text == "alpha original"


def test_presentation_fts_replace_rolls_back_both_indexes(sqlite_env) -> None:
    import pytest
    from sqlalchemy.exc import IntegrityError

    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha original"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    with get_engine().begin() as conn:
        before_parent = conn.execute(text("SELECT * FROM tdoc_search")).all()
        before_chunks = conn.execute(text("SELECT * FROM tdoc_presentation_search")).all()
        before_ledger = conn.execute(text("SELECT * FROM tdoc_presentation_search_ledger")).all()
        before_rowids = conn.execute(text("SELECT * FROM tdoc_presentation_search_rowids")).all()
        conn.execute(text("CREATE TRIGGER reject_presentation BEFORE INSERT ON tdoc_presentation_search_ledger BEGIN SELECT RAISE(ABORT, 'injected'); END"))
    with pytest.raises(IntegrityError):
        repo.upsert(_WID_ID)
    with get_engine().begin() as conn:
        assert conn.execute(text("SELECT * FROM tdoc_search")).all() == before_parent
        assert conn.execute(text("SELECT * FROM tdoc_presentation_search")).all() == before_chunks
        assert conn.execute(text("SELECT * FROM tdoc_presentation_search_ledger")).all() == before_ledger
        assert conn.execute(text("SELECT * FROM tdoc_presentation_search_rowids")).all() == before_rowids


def test_presentation_fts_schema_is_additive_and_idempotent(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha original"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    with get_engine().begin() as conn:
        before_parent = conn.execute(text("SELECT * FROM tdoc_search")).all()
        before_chunks = conn.execute(text("SELECT * FROM tdoc_presentation_search")).all()
    create_schema()
    create_schema()
    with get_engine().begin() as conn:
        assert conn.execute(text("SELECT * FROM tdoc_search")).all() == before_parent
        assert conn.execute(text("SELECT * FROM tdoc_presentation_search")).all() == before_chunks


def test_presentation_fts_missing_or_incomplete_sidecar_keeps_parent(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha original"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM tdoc_presentation_search"))
    assert repo.search("alpha", SearchFilters(limit=10))[0].matched_documents[0].matched_chunk is None
    assert repo.status().is_stale
    assert list(repo.rebuild_batch(10, None, True)) == [[_WID_ID]]
    repo.upsert(_WID_ID)
    with get_engine().begin() as conn:
        conn.execute(text("DROP TABLE tdoc_presentation_search"))
    assert repo.search("alpha", SearchFilters(limit=10))[0].matched_documents[0].matched_chunk is None
    assert repo.status().is_stale
    assert list(repo.rebuild_batch(10, None, True)) == [[_WID_ID]]
    create_schema()
    repo.upsert(_WID_ID)
    assert repo.search("alpha", SearchFilters(limit=10))[0].matched_documents[0].matched_chunk.text == "alpha original"


def test_presentation_fts_does_not_change_parent_ranking_or_limit(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent one", "WID new")
    _seed_tdoc(_CR_ID, "Parent two", "CR")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha alpha one"),))
    _write_cr(_CR_ID, _CR_URL, _CR_MAIN, (("Annex.ppt", "alpha two"),), "Cover")
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert_many([_WID_ID, _CR_ID])
    indexed = repo.search("alpha", SearchFilters(limit=1))
    with get_engine().begin() as conn:
        conn.execute(text("DROP TABLE tdoc_presentation_search"))
    without_sidecar = repo.search("alpha", SearchFilters(limit=1))
    assert len(indexed) == len(without_sidecar) == 1
    assert [(h.tdoc_id, h.score) for h in indexed] == [
        (h.tdoc_id, h.score) for h in without_sidecar
    ]
    assert indexed[0].matched_documents[0].matched_chunk is not None
    assert without_sidecar[0].matched_documents[0].matched_chunk is None


def test_presentation_fts_corruption_is_not_silenced(sqlite_env) -> None:
    import pytest

    from doc3gpp.models.search import SearchIndexCorruptError

    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha original"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    with get_engine().begin() as conn:
        conn.execute(text("DROP TABLE tdoc_presentation_search_data"))
    with pytest.raises(SearchIndexCorruptError):
        repo.search("alpha", SearchFilters(limit=10))


def test_presentation_fts_stale_rebuild_ignores_deleted_parents(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha original"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM tdocs WHERE tdoc_id = :id"), {"id": _WID_ID})
    assert repo.count_tdocs_to_index(stale_only=True) == 0
    assert list(repo.rebuild_batch(10, None, True)) == []


def test_presentation_fts_parent_column_qualifiers_remain_valid(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha original"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    for column, term in (("row_kind", "document"), ("extraction_version", "2")):
        hits = repo.search(f"{column}:{term} OR content_text:alpha", SearchFilters(limit=1))
        assert [hit.tdoc_id for hit in hits] == [_WID_ID]
        assert hits[0].matched_documents[0].matched_chunk.text == "alpha original"


def test_presentation_fts_legacy_sidecar_upgrade_preserves_parent(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha original"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    with get_engine().begin() as conn:
        conn.execute(text("""CREATE TEMP TABLE saved_presentation AS
            SELECT tdoc_id, document_id, source_file, content_hash, chunk_id,
                   chunk_index, fingerprint, original_text, content_text
            FROM tdoc_presentation_search"""))
        conn.execute(text("DROP TABLE tdoc_presentation_search"))
        conn.execute(text("""CREATE VIRTUAL TABLE tdoc_presentation_search USING fts5(
            tdoc_id UNINDEXED, title, ftp_url, meeting_title, meeting_location,
            wis, cover_text, change_text, ttcn_text, content_text,
            document_id UNINDEXED, source_file UNINDEXED, content_hash UNINDEXED,
            chunk_id UNINDEXED, chunk_index UNINDEXED, fingerprint UNINDEXED,
            original_text UNINDEXED)"""))
        conn.execute(text("""INSERT INTO tdoc_presentation_search
            (tdoc_id, document_id, source_file, content_hash, chunk_id,
             chunk_index, fingerprint, original_text, content_text)
            SELECT * FROM temp.saved_presentation"""))
        conn.execute(text("DROP TABLE temp.saved_presentation"))
    legacy_hits = repo.search("row_kind:document OR content_text:alpha", SearchFilters(limit=1))
    assert legacy_hits[0].matched_documents[0].matched_chunk is None
    assert repo.status().is_stale
    create_schema()
    assert repo.search("alpha", SearchFilters(limit=1))[0].matched_documents[0].matched_chunk is None
    assert repo.status().is_stale
    repo.upsert(_WID_ID)
    assert repo.search("row_kind:document OR content_text:alpha", SearchFilters(limit=1))[0].matched_documents[0].matched_chunk.text == "alpha original"


def test_presentation_fts_tampered_indexed_text_never_fabricates_chunk(sqlite_env, monkeypatch) -> None:
    settings = get_settings().semantic_search
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    monkeypatch.setattr(settings, "chunk_size", 4)
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha one two three omega four five six"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE tdoc_presentation_search SET content_text = 'alpha omega' WHERE chunk_index = 0"))
    hits = repo.search("alpha AND omega", SearchFilters(limit=1))
    assert [hit.tdoc_id for hit in hits] == [_WID_ID]
    assert hits[0].matched_documents[0].matched_chunk is None
    assert repo.status().is_stale
    assert repo.count_tdocs_to_index(stale_only=True) == 1


def test_presentation_fts_batch_replacement_deletes_once(sqlite_env) -> None:
    from sqlalchemy import event

    _seed_meeting()
    ids = [_WID_ID, "R5-2600103", "R5-2600104", "R5-2600105"]
    for index, tdoc_id in enumerate(ids):
        _seed_tdoc(tdoc_id, "Parent", "WID new")
        if index < 2:
            _write_family(WID, tdoc_id, f"r5/docs/{tdoc_id}.zip", (("Deck.pptx", "alpha"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert_many(ids)
    deletes = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        if statement.lower().startswith("delete from tdoc_presentation_search "):
            deletes.append(statement)

    event.listen(get_engine(), "before_cursor_execute", capture)
    try:
        repo.upsert_many(ids)
    finally:
        event.remove(get_engine(), "before_cursor_execute", capture)
    assert len(deletes) == 1
    assert not repo.status().is_stale


def test_presentation_fts_status_uses_metadata_without_decoding(sqlite_env, monkeypatch) -> None:
    import re

    from sqlalchemy import event

    from doc3gpp.storage.repositories import tdoc_presentation_search_sql as sidecar

    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (
        ("Deck.pptx", "alpha beta"), ("empty.PPT", "  "),
        ("not-presentation.docx", "ordinary content"),
    ))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.lower())

    def no_decode(_blob):
        raise AssertionError("status must not decode presentation bodies")

    monkeypatch.setattr(sidecar, "decompress_json", no_decode)
    event.listen(get_engine(), "before_cursor_execute", capture)
    try:
        assert not repo.status().is_stale
        assert repo.count_tdocs_to_index(stale_only=True) == 0
        assert list(repo.rebuild_batch(10, None, True)) == []
    finally:
        event.remove(get_engine(), "before_cursor_execute", capture)
    assert not any(re.search(r"\btdoc_\w+\.content\b", statement) for statement in statements)
    assert not any("select * from tdoc_presentation_search_ledger" in s for s in statements)


def test_presentation_fts_unnormalized_index_is_stale_and_repaired(sqlite_env) -> None:
    from doc3gpp.cli_filters import SearchQueryBuilder

    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "Specification 38.331"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE tdoc_presentation_search SET content_text = original_text"))
    assert repo.status().is_stale
    assert repo.count_tdocs_to_index(stale_only=True) == 1
    batches = list(repo.rebuild_batch(1, None, True))
    assert batches == [[_WID_ID]]
    for batch in batches:
        repo.upsert_many(batch)
    assert not repo.status().is_stale
    query = SearchQueryBuilder("38.331").build()
    assert repo.search(query, SearchFilters(limit=1))[0].matched_documents[0].matched_chunk.text == "Specification 38.331"


def test_presentation_fts_batch_delete_uses_indexed_rowids(sqlite_env) -> None:
    from sqlalchemy import event

    _seed_meeting()
    ids = [_WID_ID, "R5-2600103"]
    for tdoc_id in ids:
        _seed_tdoc(tdoc_id, "Parent", "WID new")
        _write_family(WID, tdoc_id, f"r5/docs/{tdoc_id}.zip", (("Deck.pptx", "alpha beta"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert_many(ids)
    deletions = []
    owner_checks = []

    def capture(_conn, _cursor, statement, params, _context, _many):
        if statement.lower().startswith("delete from tdoc_presentation_search "):
            deletions.append((statement, params))
        if statement.lower().startswith("select 1 from tdoc_presentation_search_rowids r "):
            owner_checks.append((statement, params))

    event.listen(get_engine(), "before_cursor_execute", capture)
    try:
        repo.upsert_many([ids[0]])
        repo.upsert_many([ids[1]])
    finally:
        event.remove(get_engine(), "before_cursor_execute", capture)
    assert len(deletions) == 2
    assert len(owner_checks) == 2
    with get_engine().begin() as conn:
        for statement, params in deletions + owner_checks:
            plan = conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, params).all()
            details = [row[3].upper() for row in plan]
            assert any("VIRTUAL TABLE INDEX 0:=" in detail for detail in details), details
            assert any(
                "SEARCH" in detail and ("TDOC_PRESENTATION" in detail or "IDX_PRESENTATION_ROWIDS_TDOC" in detail)
                for detail in details
            ), details
    assert repo.count_tdocs_to_index(stale_only=True) == 0
    assert repo.search("alpha", SearchFilters(limit=10))[0].matched_documents[0].matched_chunk.text == "alpha beta"


def test_presentation_fts_rowid_map_additive_upgrade(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha beta"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    with get_engine().begin() as conn:
        before = conn.execute(text("SELECT rowid, * FROM tdoc_presentation_search")).all()
        conn.execute(text("DROP TABLE IF EXISTS tdoc_presentation_search_rowids"))
    assert repo.status().is_stale
    create_schema()
    create_schema()
    with get_engine().begin() as conn:
        assert conn.execute(text("SELECT rowid, * FROM tdoc_presentation_search")).all() == before
        assert conn.execute(text("SELECT tdoc_id, fts_rowid FROM tdoc_presentation_search_rowids")).all() == [(_WID_ID, before[0][0])]
    assert not repo.status().is_stale
    repo.upsert(_WID_ID)
    assert repo.search("alpha", SearchFilters(limit=1))[0].matched_documents[0].matched_chunk.text == "alpha beta"


def test_presentation_fts_recreated_table_clears_obsolete_rowids(sqlite_env) -> None:
    _seed_meeting()
    ids = [_WID_ID, "R5-2600103"]
    for tdoc_id in ids:
        _seed_tdoc(tdoc_id, "Parent", "WID new")
        _write_family(WID, tdoc_id, f"r5/docs/{tdoc_id}.zip", (("Deck.pptx", "alpha beta"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert_many(ids)
    with get_engine().begin() as conn:
        conn.execute(text("DROP TABLE tdoc_presentation_search"))
    create_schema()
    with get_engine().begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM tdoc_presentation_search_rowids")).scalar_one() == 0
    assert list(repo.rebuild_batch(1, None, True)) == [[ids[0]], [ids[1]]]
    for tdoc_id in ids:
        repo.upsert(tdoc_id)
    assert not repo.status().is_stale
    assert len(repo.search("alpha", SearchFilters(limit=10))) == 2


def test_presentation_fts_retries_interrupted_migration_backfill(sqlite_env) -> None:
    import pytest
    from sqlalchemy import event

    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha beta"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    engine = get_engine()
    with engine.begin() as conn:
        before = conn.execute(text("SELECT rowid, * FROM tdoc_presentation_search")).all()
        conn.execute(text("DROP TABLE tdoc_presentation_search_rowids"))

    def interrupt(_conn, _cursor, statement, _params, _context, _many):
        if statement.lower().startswith("insert or ignore into tdoc_presentation_search_rowids"):
            raise RuntimeError("interrupted before backfill")

    event.listen(engine, "before_cursor_execute", interrupt)
    try:
        with pytest.raises(RuntimeError, match="interrupted before backfill"):
            create_schema()
    finally:
        event.remove(engine, "before_cursor_execute", interrupt)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT rowid, * FROM tdoc_presentation_search")).all() == before
    create_schema()
    with engine.begin() as conn:
        assert conn.execute(text("SELECT tdoc_id, fts_rowid FROM tdoc_presentation_search_rowids")).all() == [(_WID_ID, before[0][0])]
    repo.upsert(_WID_ID)
    assert not repo.status().is_stale
    assert repo.search("alpha", SearchFilters(limit=1))[0].matched_documents[0].matched_chunk.text == "alpha beta"


def test_presentation_fts_interrupted_rowid_upgrade_recovers(sqlite_env) -> None:
    import pytest

    from doc3gpp.models.search import SearchIndexCorruptError

    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha beta"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    with get_engine().begin() as conn:
        before = conn.execute(text("SELECT rowid, * FROM tdoc_presentation_search")).all()
        before_parent = conn.execute(text("SELECT * FROM tdoc_search")).all()
        conn.execute(text("DROP TABLE tdoc_presentation_search_rowids"))
        conn.execute(text("CREATE TABLE tdoc_presentation_search_rowids (fts_rowid INTEGER PRIMARY KEY, tdoc_id TEXT NOT NULL)"))
        conn.execute(text("CREATE INDEX idx_presentation_rowids_tdoc ON tdoc_presentation_search_rowids (tdoc_id, fts_rowid)"))
    assert repo.status().is_stale
    assert list(repo.rebuild_batch(1, None, True)) == [[_WID_ID]]
    assert repo.search("alpha", SearchFilters(limit=1))[0].matched_documents[0].matched_chunk is None
    with pytest.raises(SearchIndexCorruptError, match="db init"):
        repo.upsert(_WID_ID)
    with get_engine().begin() as conn:
        assert conn.execute(text("SELECT * FROM tdoc_search")).all() == before_parent
        assert conn.execute(text("SELECT rowid, * FROM tdoc_presentation_search")).all() == before
    create_schema()
    create_schema()
    with get_engine().begin() as conn:
        assert conn.execute(text("SELECT rowid, * FROM tdoc_presentation_search")).all() == before
        assert conn.execute(text("SELECT tdoc_id, fts_rowid FROM tdoc_presentation_search_rowids")).all() == [(_WID_ID, before[0][0])]
    repo.upsert(_WID_ID)
    with get_engine().begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM tdoc_presentation_search")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM tdoc_presentation_search_rowids")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM tdoc_presentation_search c LEFT JOIN tdoc_presentation_search_rowids r ON r.fts_rowid = c.rowid AND r.tdoc_id = c.tdoc_id WHERE r.fts_rowid IS NULL")).scalar_one() == 0
    assert not repo.status().is_stale
    assert repo.search("alpha", SearchFilters(limit=1))[0].matched_documents[0].matched_chunk.text == "alpha beta"


def test_presentation_fts_partial_rowid_map_is_stale_and_repairable(sqlite_env, monkeypatch) -> None:
    settings = get_settings().semantic_search
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    monkeypatch.setattr(settings, "chunk_size", 4)
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha one two three omega four five six"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM tdoc_presentation_search_rowids WHERE fts_rowid = (SELECT min(fts_rowid) FROM tdoc_presentation_search_rowids)"))
    assert repo.status().is_stale
    assert repo.count_tdocs_to_index(stale_only=True) == 1
    assert repo.search("alpha", SearchFilters(limit=1))[0].matched_documents[0].matched_chunk is None
    create_schema()
    assert not repo.status().is_stale
    assert repo.search("alpha", SearchFilters(limit=1))[0].matched_documents[0].matched_chunk.text == "alpha one two three"


def test_presentation_fts_swapped_rowid_owners_block_replacement(sqlite_env) -> None:
    import pytest

    from doc3gpp.models.search import SearchIndexCorruptError

    _seed_meeting()
    ids = [_WID_ID, "R5-2600103"]
    for tdoc_id in ids:
        _seed_tdoc(tdoc_id, "Parent", "WID new")
        _write_family(WID, tdoc_id, f"r5/docs/{tdoc_id}.zip", (("Deck.pptx", "alpha beta"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert_many(ids)
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE tdoc_presentation_search_rowids SET tdoc_id = CASE WHEN tdoc_id = :a THEN :b ELSE :a END"), {"a": ids[0], "b": ids[1]})
    assert repo.status().is_stale
    assert repo.count_tdocs_to_index(stale_only=True) == 2
    assert all(hit.matched_documents[0].matched_chunk is None for hit in repo.search("alpha", SearchFilters(limit=10)))
    with pytest.raises(SearchIndexCorruptError, match="db init"):
        repo.upsert(ids[0])
    create_schema()
    assert not repo.status().is_stale
    repo.upsert_many(ids)
    assert not repo.status().is_stale
    assert len(repo.search("alpha", SearchFilters(limit=10))) == 2


def test_presentation_fts_init_then_repairs_missing_fts_rows(sqlite_env, monkeypatch) -> None:
    settings = get_settings().semantic_search
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    monkeypatch.setattr(settings, "chunk_size", 4)
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    other_id = "R5-2600103"
    _seed_tdoc(other_id, "Other parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha one two three omega four five six"),))
    _write_family(WID, other_id, f"r5/docs/{other_id}.zip", (("Other.pptx", "unrelated"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert_many([_WID_ID, other_id])
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM tdoc_presentation_search WHERE tdoc_id = :id AND chunk_index = 0"), {"id": _WID_ID})
    create_schema()
    assert repo.status().is_stale
    assert list(repo.rebuild_batch(1, None, True)) == [[_WID_ID]]
    repo.upsert(_WID_ID)
    assert not repo.status().is_stale
    assert repo.search("alpha", SearchFilters(limit=1))[0].matched_documents[0].matched_chunk.text == "alpha one two three"
    assert repo.search("unrelated", SearchFilters(limit=1))[0].matched_documents[0].matched_chunk.text == "unrelated"


def test_presentation_fts_null_token_count_and_wrong_chunk_id_are_stale(sqlite_env) -> None:
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Parent", "WID new")
    _write_family(WID, _WID_ID, _WID_URL, (("Deck.pptx", "alpha beta"),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE tdoc_wid_contents SET token_count = NULL"))
    assert repo.status().is_stale
    assert list(repo.rebuild_batch(10, None, True)) == [[_WID_ID]]
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE tdoc_wid_contents SET token_count = 2"))
        conn.execute(text("UPDATE tdoc_presentation_search SET chunk_id = 'wrong'"))
    assert repo.status().is_stale
    assert repo.search("alpha", SearchFilters(limit=1))[0].matched_documents[0].matched_chunk is None


def test_presentation_fts_stale_rebuild_writes_between_streamed_batches(sqlite_env) -> None:
    _seed_meeting()
    ids = [_WID_ID, "R5-2600103", "R5-2600104"]
    for tdoc_id in ids:
        _seed_tdoc(tdoc_id, "Parent", "WID new")
        _write_family(WID, tdoc_id, f"r5/docs/{tdoc_id}.zip", (("Deck.pptx", "alpha"),))
    repo = SQLAlchemySearchIndexRepository()
    seen = []
    for batch in repo.rebuild_batch(1, None, True):
        seen.extend(batch)
        repo.upsert_many(batch)
    assert seen == ids
    assert repo.count_tdocs_to_index(stale_only=True) == 0



def test_search_factory_uses_injected_semantic_chunk_settings(sqlite_env):
    import numpy as np
    from sqlalchemy import text

    from doc3gpp.models.semantic_chunking import chunking_fingerprint
    from doc3gpp.models.semantic_search import content_chunking_fingerprint
    from doc3gpp.services.factory import (
        build_search_service,
        build_semantic_search_service,
    )
    from doc3gpp.services.search_service import PassthroughReranker
    from doc3gpp.settings.loader import get_settings
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.search_sql import SQLAlchemySearchIndexRepository

    global_settings = get_settings()
    global_chunking = (
        global_settings.semantic_search.chunk_size,
        global_settings.semantic_search.chunk_overlap,
    )
    settings = global_settings.model_copy(deep=True)
    injected_chunking = (3, 0) if global_chunking != (3, 0) else (4, 0)
    settings.search.enabled = True
    settings.semantic_search.enabled = True
    settings.semantic_search.chunk_size, settings.semantic_search.chunk_overlap = (
        injected_chunking
    )

    _seed_meeting()
    _seed_tdoc(_WID_ID, "Injected chunk settings", "WID new")
    _write_family(
        WID,
        _WID_ID,
        _WID_URL,
        (("slides/Deck.PPTX", "one two three four five six seven eight"),),
    )

    direct_repo = SQLAlchemySearchIndexRepository()
    assert (direct_repo._chunk_size, direct_repo._chunk_overlap) == global_chunking

    fts_service = build_search_service(
        settings,
        reranker=PassthroughReranker(),
    )
    assert fts_service is not None
    fts_service.upsert_for_tdoc(_WID_ID)

    class DeterministicEmbedder:
        dim = 384
        model_name = None

        def encode(self, texts):
            vector = np.ones(self.dim, dtype=np.float32)
            return np.tile(vector, (len(texts), 1))

    semantic_service = build_semantic_search_service(
        settings,
        fts5_service=fts_service,
        embedder=DeterministicEmbedder(),
    )
    assert semantic_service is not None
    semantic_service.index_for_tdoc(_WID_ID)

    with get_engine().begin() as conn:
        fts_rows = conn.execute(
            text(
                "SELECT chunk_index, original_text, fingerprint "
                "FROM tdoc_presentation_search "
                "WHERE tdoc_id = :tdoc_id ORDER BY CAST(chunk_index AS INTEGER)"
            ),
            {"tdoc_id": _WID_ID},
        ).all()
        vector_rows = conn.execute(
            text(
                "SELECT chunk_index, text, fingerprint FROM vec_tdoc_chunk_text "
                "WHERE tdoc_id = :tdoc_id ORDER BY chunk_index"
            ),
            {"tdoc_id": _WID_ID},
        ).all()

    expected_chunks = [
        "one two three" if injected_chunking[0] == 3 else "one two three four",
        "four five six" if injected_chunking[0] == 3 else "five six seven eight",
    ]
    if injected_chunking[0] == 3:
        expected_chunks.append("seven eight")
    assert [row[1] for row in fts_rows] == expected_chunks
    assert [row[1] for row in vector_rows] == expected_chunks
    assert {row[2] for row in fts_rows} == {
        chunking_fingerprint(*injected_chunking)
    }
    assert {row[2] for row in vector_rows} == {
        content_chunking_fingerprint(
            *injected_chunking,
            settings.semantic_search.max_content_chunks_per_tdoc,
        )
    }



def test_metadata_only_search_does_not_decompress_unmatched_presentations(
    sqlite_env, monkeypatch
):
    from doc3gpp.models.search import SearchFilters
    from doc3gpp.storage.repositories import tdoc_presentation_search_sql as presentation_search
    from doc3gpp.storage.repositories.search_sql import SQLAlchemySearchIndexRepository

    _seed_meeting()
    _seed_tdoc(_WID_ID, "metadataonlymarker", "WID new")
    _write_family(
        WID,
        _WID_ID,
        _WID_URL,
        (
            ("docs/Main.docx", "wordonlymarker"),
            ("slides/Unmatched.PPTX", "presentationonlymarker"),
        ),
    )
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)

    def fail_if_decoded(_content):
        raise AssertionError("metadata-only search decoded an unrelated presentation")

    monkeypatch.setattr(presentation_search, "decompress_json", fail_if_decoded)
    hits = repo.search("metadataonlymarker", SearchFilters(limit=5))

    assert [hit.tdoc_id for hit in hits] == [_WID_ID]
    assert hits[0].matched_documents == ()


def test_word_only_search_does_not_decompress_unmatched_presentations(
    sqlite_env, monkeypatch
):
    from doc3gpp.models.search import SearchFilters
    from doc3gpp.storage.repositories import tdoc_presentation_search_sql as presentation_search
    from doc3gpp.storage.repositories.search_sql import SQLAlchemySearchIndexRepository

    _seed_meeting()
    _seed_tdoc(_WID_ID, "Word-only parent", "WID new")
    _write_family(
        WID,
        _WID_ID,
        _WID_URL,
        (
            ("docs/Main.docx", "wordonlymarker"),
            ("slides/Unmatched.PPTX", "presentationonlymarker"),
        ),
    )
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)

    def fail_if_decoded(_content):
        raise AssertionError("Word-only search decoded an unrelated presentation")

    monkeypatch.setattr(presentation_search, "decompress_json", fail_if_decoded)
    hits = repo.search("wordonlymarker", SearchFilters(limit=5))

    assert [hit.tdoc_id for hit in hits] == [_WID_ID]
    assert [document.source_file for document in hits[0].matched_documents] == [
        "docs/Main.docx"
    ]
    assert hits[0].matched_documents[0].matched_chunk is None



def test_fts_search_keeps_parent_and_chunk_reads_in_one_wal_snapshot(
    sqlite_env, monkeypatch
):
    from sqlalchemy import event

    from doc3gpp.models.search import SearchFilters
    from doc3gpp.settings.loader import get_settings
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.search_sql import SQLAlchemySearchIndexRepository

    semantic_settings = get_settings().semantic_search
    monkeypatch.setattr(semantic_settings, "chunk_size", 4)
    monkeypatch.setattr(semantic_settings, "chunk_overlap", 0)
    _seed_meeting()
    _seed_tdoc(_WID_ID, "Snapshot generation parent", "WID new")
    source_file = "slides/Snapshot.PPTX"
    old_text = " ".join(["oldgeneration rankneedle rankneedle rankneedle"] * 8)
    new_text = "newgeneration rankneedle filler filler"
    _write_family(WID, _WID_ID, _WID_URL, ((source_file, old_text),))
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert(_WID_ID)

    with get_engine().connect() as conn:
        journal_mode = conn.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one()
    assert journal_mode.lower() == "wal"

    filters = SearchFilters(limit=5)
    old_hit = repo.search("rankneedle", filters)[0]
    old_document = old_hit.matched_documents[0]
    assert old_document.matched_chunk is not None
    old_generation = (
        old_hit.score,
        old_document.score,
        old_document.matched_chunk.text,
    )

    engine = get_engine()
    replaced = False

    def replace_after_rank_query(conn, _cursor, statement, _parameters, _context, _many):
        nonlocal replaced
        sql = statement.lower()
        if replaced or "from tdoc_search" not in sql or "bm25(tdoc_search" not in sql:
            return
        if "tdoc_search.row_kind" in sql:
            return
        replaced = True
        _write_family(WID, _WID_ID, _WID_URL, ((source_file, new_text),))
        repo.upsert(_WID_ID)

    event.listen(engine, "after_cursor_execute", replace_after_rank_query)
    try:
        concurrent_hit = repo.search("rankneedle", filters)[0]
    finally:
        event.remove(engine, "after_cursor_execute", replace_after_rank_query)

    new_hit = repo.search("rankneedle", filters)[0]
    new_document = new_hit.matched_documents[0]
    assert replaced
    assert new_document.matched_chunk is not None
    new_generation = (
        new_hit.score,
        new_document.score,
        new_document.matched_chunk.text,
    )
    assert old_generation[:2] != new_generation[:2]

    concurrent_document = concurrent_hit.matched_documents[0]
    assert concurrent_document.document_id == old_document.document_id
    assert concurrent_document.source_file == source_file
    if concurrent_document.matched_chunk is None:
        assert concurrent_hit.score in {old_generation[0], new_generation[0]}
    else:
        assert (
            concurrent_hit.score,
            concurrent_document.score,
            concurrent_document.matched_chunk.text,
        ) in {old_generation, new_generation}
