from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

import pytest

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_content import TDocContent, TDocContentDocument
from doc3gpp.models.tdoc_content_families import TDOC_CONTENT_STORES
from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
from doc3gpp.models.tdoc_document import TDocDocument, markdown_member_name
from doc3gpp.models.tdoc_identity import document_id_for
from doc3gpp.parsers.tdoc_zip import build_markdown_archive
from doc3gpp.services.tdoc_content_service import (
    TDocContentIneligibleError,
    TDocContentNotFoundError,
    TDocContentNotFoundReason,
    TDocContentService,
    tdoc_content_to_dict,
)
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.tdoc_content_sql import (
    SQLAlchemyTDocContentRepository,
)
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

STORES = {store.key: store for store in TDOC_CONTENT_STORES}
TDOC_ID = "R5-FAMILY-1"
URL = "r5/docs/r5-family-1.zip"
MAIN = "CRs/main.docx"


def _document(
    ftp_url: str,
    source_file: str,
    markdown: str,
    *,
    file_order: int = 0,
) -> TDocDocument:
    document_id = document_id_for(ftp_url, source_file)
    return TDocDocument(
        document_id=document_id,
        source_file=source_file,
        markdown_file=markdown_member_name(document_id, source_file),
        file_order=file_order,
        markdown_text=markdown,
        content_hash=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
    )


def _meta(
    tdoc_id: str,
    ftp_url: str,
    *,
    doc_filename: str,
    document_count: int,
    extraction_version: int = 2,
    skipped_sources: tuple[str, ...] = (),
) -> TDocExtractMeta:
    return TDocExtractMeta(
        ftp_url=ftp_url,
        tdoc_id=tdoc_id,
        cache_file=f"{tdoc_id}.zip",
        doc_filename=doc_filename,
        extraction_version=extraction_version,
        document_count=document_count,
        skipped_sources=skipped_sources,
    )


def _seed_family(
    store_key: str,
    tdoc_id: str,
    tdoc_type: str,
    documents: Sequence[tuple[str, str]],
    *,
    ftp_url: str = URL,
    skipped_sources: tuple[str, ...] = (),
) -> TDocExtractMeta:
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id=tdoc_id, type=tdoc_type, ftp_url=ftp_url)
    )
    rows = tuple(
        _document(ftp_url, source, markdown)
        for source, markdown in documents
    )
    meta = _meta(
        tdoc_id,
        ftp_url,
        doc_filename=documents[0][0],
        document_count=len(rows),
        skipped_sources=skipped_sources,
    )
    SQLAlchemyTDocContentRepository().replace_family_bundle(
        STORES[store_key],
        meta,
        rows,
    )
    return meta


def _seed_cr(
    tdoc_id: str,
    annexes: Sequence[tuple[str, str]],
    *,
    ftp_url: str = URL,
    main: str = MAIN,
) -> TDocExtractMeta:
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=ftp_url)
    )
    rows = tuple(_document(ftp_url, source, markdown) for source, markdown in annexes)
    meta = _meta(tdoc_id, ftp_url, doc_filename=main, document_count=len(rows))
    SQLAlchemyTDocContentRepository().replace_cr_bundle(
        TDocCRDetails(tdoc_id=tdoc_id, ftp_url=ftp_url, title="CR"),
        None,
        None,
        meta,
        rows,
    )
    return meta


def _write_archive(
    cache_root: Path,
    meta: TDocExtractMeta,
    documents: Sequence[tuple[str, str]],
) -> None:
    rows = tuple(
        _document(meta.ftp_url, source, markdown, file_order=order)
        for order, (source, markdown) in enumerate(documents)
    )
    payload = build_markdown_archive(
        rows,
        ftp_url=meta.ftp_url,
        main_source_file=meta.doc_filename,
    )
    markdown_dir = cache_root / "markdown"
    markdown_dir.mkdir(parents=True, exist_ok=True)
    (markdown_dir / meta.cache_file).write_bytes(payload)


def _make_service(cache_root: Path) -> TDocContentService:
    return TDocContentService(
        tdoc_repository=SQLAlchemyTDocRepository(),
        cr_repository=SQLAlchemyTDocCrRepository(),
        content_repository=SQLAlchemyTDocContentRepository(),
        cache_root=cache_root,
    )


def test_family_content_main_first_with_is_main(sqlite_env, tmp_path) -> None:
    create_schema("main")
    _seed_family(
        "wid",
        TDOC_ID,
        "WID new",
        (
            (MAIN, "main body"),
            ("b/Second.docx", "second body"),
            ("A/first.docx", "first body"),
        ),
    )

    content = _make_service(tmp_path / "cache").get(tdoc_id=TDOC_ID)

    assert content.complete is True
    assert content.extraction_version == 2
    assert content.skipped_sources == ()
    assert [document.source_file for document in content.documents] == [
        MAIN,
        "A/first.docx",
        "b/Second.docx",
    ]
    assert [document.is_main for document in content.documents] == [
        True,
        False,
        False,
    ]
    assert [document.markdown for document in content.documents] == [
        "main body",
        "first body",
        "second body",
    ]


def test_source_file_selects_one_member_and_unknown_is_not_found(
    sqlite_env,
    tmp_path,
) -> None:
    create_schema("main")
    _seed_family(
        "wid",
        TDOC_ID,
        "WID new",
        (
            (MAIN, "main body"),
            ("annex/A.docx", "annex A body"),
            ("annex/b.docx", "annex b body"),
        ),
    )
    service = _make_service(tmp_path / "cache")

    selected = service.get(tdoc_id=TDOC_ID, source_file="annex/b.docx")

    assert [document.source_file for document in selected.documents] == [
        "annex/b.docx"
    ]
    assert selected.documents[0].markdown == "annex b body"
    assert selected.documents[0].is_main is False
    assert selected.complete is True
    for unknown in ("annex/B.docx", "missing.docx"):
        with pytest.raises(TDocContentNotFoundError) as exc_info:
            service.get(tdoc_id=TDOC_ID, source_file=unknown)
        assert exc_info.value.reason is TDocContentNotFoundReason.SOURCE_NOT_FOUND


def test_skipped_sources_makes_content_incomplete(sqlite_env, tmp_path) -> None:
    create_schema("main")
    _seed_family(
        "ls",
        TDOC_ID,
        "LS out",
        ((MAIN, "main body"), ("annex/a.docx", "annex body")),
        skipped_sources=("annex/broken.docx",),
    )
    service = _make_service(tmp_path / "cache")

    content = service.get(tdoc_id=TDOC_ID)

    assert content.complete is False
    assert content.skipped_sources == ("annex/broken.docx",)
    assert [document.source_file for document in content.documents] == [
        MAIN,
        "annex/a.docx",
    ]
    main_only = service.get(tdoc_id=TDOC_ID, source_file=MAIN)
    assert main_only.complete is False
    assert main_only.skipped_sources == ("annex/broken.docx",)
    with pytest.raises(TDocContentNotFoundError):
        service.get(tdoc_id=TDOC_ID, source_file="annex/broken.docx")


def test_ineligible_type_raises_named_error(sqlite_env, tmp_path) -> None:
    create_schema("main")
    SQLAlchemyTDocRepository().upsert(
        TDoc(
            tdoc_id="R5-OTHER-1",
            type="other",
            ftp_url="r5/docs/r5-other-1.zip",
        )
    )

    with pytest.raises(TDocContentIneligibleError) as exc_info:
        _make_service(tmp_path / "cache").get(tdoc_id="R5-OTHER-1")

    message = str(exc_info.value)
    assert "'other'" in message
    assert "WID, LS, and discussion" in message
    assert exc_info.value.tdoc_id == "R5-OTHER-1"
    assert exc_info.value.observed_type == "other"


def test_tdoc_content_to_dict_has_skipped_sources_key() -> None:
    content = TDocContent(
        tdoc_id=TDOC_ID,
        ftp_url=URL,
        extraction_version=2,
        complete=False,
        documents=(
            TDocContentDocument(
                document_id="a" * 64,
                source_file=MAIN,
                is_main=True,
                markdown="main body",
            ),
        ),
        skipped_sources=("annex/broken.docx",),
    )

    payload = tdoc_content_to_dict(content)

    assert set(payload) == {
        "tdoc_id",
        "ftp_url",
        "extraction_version",
        "complete",
        "skipped_sources",
        "documents",
    }
    assert payload["skipped_sources"] == ["annex/broken.docx"]
    assert payload["complete"] is False


def test_cr_content_is_archive_main_plus_annexes(sqlite_env, tmp_path) -> None:
    create_schema("main")
    meta = _seed_cr(
        TDOC_ID,
        (("annex/b.docx", "second annex"), ("annex/a.docx", "first annex")),
    )
    cache_root = tmp_path / "cache"
    _write_archive(
        cache_root,
        meta,
        (
            (MAIN, "archive main body"),
            ("annex/a.docx", "first annex"),
            ("annex/b.docx", "second annex"),
        ),
    )

    content = _make_service(cache_root).get(tdoc_id=TDOC_ID)

    assert content.complete is True
    assert content.extraction_version == 2
    assert [document.source_file for document in content.documents] == [
        MAIN,
        "annex/a.docx",
        "annex/b.docx",
    ]
    assert [document.is_main for document in content.documents] == [True, False, False]
    assert content.documents[0].markdown == "archive main body"
    assert [document.markdown for document in content.documents[1:]] == [
        "first annex",
        "second annex",
    ]
    stored_sources = [
        document.source_file
        for document in SQLAlchemyTDocContentRepository().list_documents(
            STORES["cr"],
            URL,
        )
    ]
    assert stored_sources == ["annex/a.docx", "annex/b.docx"]


def test_cr_zero_annexes_with_archive_is_complete(sqlite_env, tmp_path) -> None:
    create_schema("main")
    ftp_url = "r5/docs/r5-cr-zero.zip"
    meta = _seed_cr("R5-CR-ZERO", (), ftp_url=ftp_url)
    cache_root = tmp_path / "cache"
    _write_archive(cache_root, meta, ((MAIN, "main only body"),))

    content = _make_service(cache_root).get(tdoc_id="R5-CR-ZERO")

    assert content.complete is True
    assert [document.source_file for document in content.documents] == [MAIN]
    assert content.documents[0].is_main is True


def test_cr_raw_without_archive_is_cache_miss(sqlite_env, tmp_path) -> None:
    create_schema("main")
    _seed_cr(TDOC_ID, (("annex/a.docx", "first annex"),))
    service = _make_service(tmp_path / "cache")

    with pytest.raises(TDocContentNotFoundError) as exc_info:
        service.get(tdoc_id=TDOC_ID)

    assert exc_info.value.reason is TDocContentNotFoundReason.CACHE_UNAVAILABLE
    assert f"doc3gpp tdoc parse --tdoc {TDOC_ID} --force" in str(exc_info.value)


def test_cr_annex_source_is_served_from_sql_without_archive(
    sqlite_env,
    tmp_path,
) -> None:
    create_schema("main")
    _seed_cr(TDOC_ID, (("annex/a.docx", "first annex"),))

    content = _make_service(tmp_path / "cache").get(
        tdoc_id=TDOC_ID,
        source_file="annex/a.docx",
    )

    assert [document.source_file for document in content.documents] == ["annex/a.docx"]
    assert content.documents[0].markdown == "first annex"
    assert content.documents[0].is_main is False
    assert content.complete is True


def test_family_content_never_reads_cache(
    sqlite_env,
    tmp_path,
    monkeypatch,
) -> None:
    create_schema("main")
    meta = _seed_family(
        "wid",
        TDOC_ID,
        "WID new",
        ((MAIN, "main body"), ("annex/a.docx", "annex body")),
    )
    cache_root = tmp_path / "cache"
    markdown_dir = cache_root / "markdown"
    markdown_dir.mkdir(parents=True)
    (markdown_dir / meta.cache_file).write_bytes(b"not a readable archive")

    def forbidden_cache_read(*_args, **_kwargs):
        raise AssertionError("family content must not read the Markdown cache")

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_content_service.read_markdown_archive",
        forbidden_cache_read,
    )

    content = _make_service(cache_root).get(tdoc_id=TDOC_ID)

    assert content.complete is True
    assert [document.markdown for document in content.documents] == [
        "main body",
        "annex body",
    ]


def test_family_without_rows_is_incomplete_and_empty(sqlite_env, tmp_path) -> None:
    create_schema("main")
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id=TDOC_ID, type="LS in", ftp_url=URL)
    )
    SQLAlchemyTDocCrRepository().upsert_extract_meta(
        _meta(TDOC_ID, URL, doc_filename=MAIN, document_count=0)
    )

    content = _make_service(tmp_path / "cache").get(tdoc_id=TDOC_ID)

    assert content.complete is False
    assert content.documents == ()


def test_zero_text_documents_do_not_break_envelope(sqlite_env, tmp_path) -> None:
    create_schema("main")
    _seed_family(
        "discussion",
        TDOC_ID,
        "discussion",
        ((MAIN, ""), ("annex/empty.docx", "")),
    )

    content = _make_service(tmp_path / "cache").get(tdoc_id=TDOC_ID)

    assert content.complete is True
    assert [document.source_file for document in content.documents] == [
        MAIN,
        "annex/empty.docx",
    ]
    assert [document.markdown for document in content.documents] == ["", ""]
    payload = tdoc_content_to_dict(content)
    assert [document["markdown"] for document in payload["documents"]] == ["", ""]


def test_v1_cr_uses_legacy_path_and_is_incomplete(sqlite_env, tmp_path) -> None:
    create_schema("main")
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id=TDOC_ID, type="CR", ftp_url=URL)
    )
    SQLAlchemyTDocCrRepository().upsert_extract_meta(
        _meta(
            TDOC_ID,
            URL,
            doc_filename=MAIN,
            document_count=0,
            extraction_version=1,
        )
    )
    cache_root = tmp_path / "cache"
    markdown_dir = cache_root / "markdown"
    markdown_dir.mkdir(parents=True)
    (markdown_dir / f"{TDOC_ID}.zip").write_bytes(b"legacy main body")

    content = _make_service(cache_root).get(tdoc_id=TDOC_ID)

    assert content.extraction_version == 1
    assert content.complete is False
    assert [(document.source_file, document.is_main) for document in content.documents] == [
        (MAIN, True)
    ]
    assert content.documents[0].markdown == "legacy main body"
