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
