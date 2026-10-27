from __future__ import annotations

import gzip
import hashlib
import io
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import delete

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_content import TDocContent, TDocContentDocument
from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
from doc3gpp.models.tdoc_document import TDocDocument, markdown_member_name
from doc3gpp.parsers.normalizers import normalize_ftp_path
from doc3gpp.parsers.tdoc_zip import build_markdown_archive, document_id_for
from doc3gpp.services.tdoc_content_service import (
    TDocContentIncompleteError,
    TDocContentNotFoundError,
    TDocContentNotFoundReason,
    TDocContentService,
    render_tdoc_documents_markdown,
    tdoc_content_to_dict,
)
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import TDocCrAnnexOrm
from doc3gpp.storage.db.session import get_session_factory
from doc3gpp.storage.repositories.tdoc_content_sql import (
    SQLAlchemyTDocContentRepository,
)
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

MAIN_SOURCE = "CR/main.docx"
ANNEX_SOURCE = "Annex/support.docx"
MAIN_MARKDOWN = "main archive Markdown" + chr(10)
ANNEX_MARKDOWN = "support SQL Markdown" + chr(10)


def _make_document(
    ftp_url: str,
    source_file: str,
    file_order: int,
    markdown: str,
) -> TDocDocument:
    document_id = document_id_for(ftp_url, source_file)
    content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    return TDocDocument(
        document_id=document_id,
        source_file=source_file,
        markdown_file=markdown_member_name(document_id, source_file),
        file_order=file_order,
        markdown_text=markdown,
        content_hash=content_hash,
    )


def _seed_sql_bundle(
    tdoc_id: str,
    ftp_url: str,
    cache_root: Path,
    *,
    cache_file: str = "bundle.zip",
) -> tuple[TDocExtractMeta, tuple[TDocDocument, ...]]:
    """Seed a CR parent with one SQL annex and its archive holding the main."""
    normalized_url = normalize_ftp_path(ftp_url)
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=ftp_url)
    )
    main = _make_document(normalized_url, MAIN_SOURCE, 0, MAIN_MARKDOWN)
    annex = _make_document(normalized_url, ANNEX_SOURCE, 1, ANNEX_MARKDOWN)
    meta = TDocExtractMeta(
        ftp_url=ftp_url,
        tdoc_id=tdoc_id,
        cache_file=cache_file,
        doc_filename=MAIN_SOURCE,
        document_count=1,
    )
    SQLAlchemyTDocContentRepository().replace_cr_bundle(
        TDocCRDetails(tdoc_id=tdoc_id, ftp_url=ftp_url, title="Stored CR"),
        None,
        None,
        meta,
        (annex,),
    )
    payload = build_markdown_archive(
        (main, annex),
        ftp_url=normalized_url,
        main_source_file=MAIN_SOURCE,
    )
    _write_legacy_cache(cache_root, cache_file, payload)
    return meta, (main, annex)


def _make_service(cache_root: Path):
    return TDocContentService(
        tdoc_repository=SQLAlchemyTDocRepository(),
        cr_repository=SQLAlchemyTDocCrRepository(),
        content_repository=SQLAlchemyTDocContentRepository(),
        cache_root=cache_root,
    )


def _write_legacy_cache(cache_root: Path, cache_file: str, payload: bytes) -> None:
    markdown_dir = cache_root / "markdown"
    markdown_dir.mkdir(parents=True, exist_ok=True)
    (markdown_dir / cache_file).write_bytes(payload)


def _legacy_single_markdown_zip(markdown: str) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("content.md", markdown)
    return payload.getvalue()


def test_service_requires_exactly_one_selector(sqlite_env, tmp_path) -> None:
    create_schema("main")
    service = _make_service(tmp_path / "cache")

    with pytest.raises(ValueError, match="exactly one"):
        service.get()
    with pytest.raises(ValueError, match="exactly one"):
        service.get(tdoc_id="R5-CONTENT", ftp_url="archive/R5-CONTENT.zip")


def test_tdoc_and_exact_url_selectors_return_all_documents_in_order(
    sqlite_env,
    tmp_path,
) -> None:
    create_schema("main")
    tdoc_id = "R5-CONTENT-1"
    ftp_url = "TSG_RAN/WG5/R5-CONTENT-1.zip"
    meta, stored_documents = _seed_sql_bundle(tdoc_id, ftp_url, tmp_path / "cache")
    service = _make_service(tmp_path / "cache")

    by_id = service.get(tdoc_id=tdoc_id)
    by_url = service.get(
        ftp_url="https://www.3gpp.org/ftp/tsg_ran/wg5/r5-content-1.zip"
    )

    for content in (by_id, by_url):
        assert content.tdoc_id == tdoc_id
        assert content.ftp_url == normalize_ftp_path(ftp_url)
        assert content.extraction_version == 2
        assert content.complete is True
        assert content.skipped_sources == ()
        assert [document.source_file for document in content.documents] == [
            "CR/main.docx",
            "Annex/support.docx",
        ]
        assert [document.markdown for document in content.documents] == [
            MAIN_MARKDOWN,
            ANNEX_MARKDOWN,
        ]
        assert [document.is_main for document in content.documents] == [True, False]
        assert [document.document_id for document in content.documents] == [
            document.document_id for document in stored_documents
        ]
    assert meta.cache_file == "bundle.zip"


def test_exact_url_selector_reads_historical_bundle_and_exact_source(
    sqlite_env,
    tmp_path,
) -> None:
    create_schema("main")
    tdoc_id = "R5-CONTENT-HISTORICAL"
    historical_url = "archive/historical.zip"
    current_url = "archive/current.zip"
    cache_root = tmp_path / "cache"
    _seed_sql_bundle(tdoc_id, historical_url, cache_root, cache_file="historical.zip")
    _seed_sql_bundle(tdoc_id, current_url, cache_root, cache_file="current.zip")

    content = _make_service(cache_root).get(
        ftp_url=historical_url,
        source_file="Annex/support.docx",
    )

    assert content.tdoc_id == tdoc_id
    assert content.ftp_url == normalize_ftp_path(historical_url)
    assert content.complete is True
    assert [document.source_file for document in content.documents] == [
        "Annex/support.docx"
    ]
    assert content.documents[0].markdown == ANNEX_MARKDOWN


def test_source_selection_is_exact_and_unknown_source_never_falls_back(
    sqlite_env,
    tmp_path,
) -> None:
    create_schema("main")
    tdoc_id = "R5-CONTENT-2"
    ftp_url = "archive/R5-CONTENT-2.zip"
    cache_root = tmp_path / "cache"
    meta, _ = _seed_sql_bundle(tdoc_id, ftp_url, cache_root)
    _write_legacy_cache(cache_root, meta.cache_file, b"stale cached main body")
    service = _make_service(cache_root)

    selected = service.get(tdoc_id=tdoc_id, source_file="Annex/support.docx")

    assert len(selected.documents) == 1
    assert selected.documents[0].source_file == "Annex/support.docx"
    assert selected.documents[0].markdown == ANNEX_MARKDOWN
    assert selected.complete is True
    with pytest.raises(TDocContentNotFoundError, match="source"):
        service.get(tdoc_id=tdoc_id, source_file="../../outside.docx")
    with pytest.raises(TDocContentNotFoundError) as exc_info:
        service.get(tdoc_id=tdoc_id)
    assert exc_info.value.reason is TDocContentNotFoundReason.CACHE_UNAVAILABLE


def test_missing_archive_fails_full_content_but_annex_stays_in_sql(
    sqlite_env,
    tmp_path,
) -> None:
    create_schema("main")
    tdoc_id = "R5-CONTENT-3"
    ftp_url = "archive/R5-CONTENT-3.zip"
    cache_root = tmp_path / "cache"
    meta, _ = _seed_sql_bundle(tdoc_id, ftp_url, cache_root)
    (cache_root / "markdown" / meta.cache_file).unlink()
    service = _make_service(cache_root)

    with pytest.raises(TDocContentNotFoundError) as exc_info:
        service.get(tdoc_id=tdoc_id)
    assert exc_info.value.reason is TDocContentNotFoundReason.CACHE_UNAVAILABLE

    annex = service.get(tdoc_id=tdoc_id, source_file="Annex/support.docx")
    assert [document.markdown for document in annex.documents] == [ANNEX_MARKDOWN]
    assert annex.complete is True


@pytest.mark.parametrize(
    "payload_kind",
    ("plain", "gzip", "single-member-zip"),
)
def test_version_one_legacy_cache_is_incomplete_and_uses_only_known_source(
    sqlite_env,
    tmp_path,
    payload_kind: str,
) -> None:
    create_schema("main")
    tdoc_id = f"R5-LEGACY-{payload_kind}"
    ftp_url = f"archive/{tdoc_id}.zip"
    source_file = "CR/known-main.docx"
    cache_file = f"{tdoc_id}.zip"
    markdown = "legacy Markdown body" + chr(10)
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=ftp_url)
    )
    SQLAlchemyTDocCrRepository().upsert_extract_meta(
        TDocExtractMeta(
            ftp_url=ftp_url,
            tdoc_id=tdoc_id,
            cache_file=cache_file,
            doc_filename=source_file,
        )
    )
    if payload_kind == "plain":
        payload = markdown.encode("utf-8")
    elif payload_kind == "gzip":
        payload = gzip.compress(markdown.encode("utf-8"))
    else:
        payload = _legacy_single_markdown_zip(markdown)
    cache_root = tmp_path / "cache"
    _write_legacy_cache(cache_root, cache_file, payload)

    service = _make_service(cache_root)
    content = service.get(tdoc_id=tdoc_id, source_file=source_file)

    assert content.extraction_version == 1
    assert content.complete is False
    assert len(content.documents) == 1
    assert content.documents[0].source_file == source_file
    assert content.documents[0].markdown == markdown
    with pytest.raises(TDocContentNotFoundError, match="source"):
        service.get(tdoc_id=tdoc_id, source_file="CR/not-the-known-source.docx")



def test_version_one_manifest_cache_url_mismatch_fails_closed(
    sqlite_env,
    tmp_path,
) -> None:
    create_schema("main")
    tdoc_id = "R5-LEGACY-MISKEYED"
    requested_url = f"archive/{tdoc_id}.zip"
    manifest_url = "archive/another-document.zip"
    source_file = "CR/known-main.docx"
    cache_file = f"{tdoc_id}.zip"
    markdown = "content belongs to another URL"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=requested_url)
    )
    SQLAlchemyTDocCrRepository().upsert_extract_meta(
        TDocExtractMeta(
            ftp_url=requested_url,
            tdoc_id=tdoc_id,
            cache_file=cache_file,
            doc_filename=source_file,
        )
    )
    document_id = document_id_for(manifest_url, source_file)
    document = TDocDocument(
        document_id=document_id,
        source_file=source_file,
        markdown_file=f"documents/{document_id}/known-main.md",
        file_order=0,
        markdown_text=markdown,
        content_hash=hashlib.sha256(markdown.encode()).hexdigest(),
    )
    payload = build_markdown_archive(
        (document,),
        ftp_url=manifest_url,
        main_source_file=source_file,
    )
    cache_root = tmp_path / "cache"
    _write_legacy_cache(cache_root, cache_file, payload)

    with pytest.raises(TDocContentNotFoundError, match="manifest URL"):
        _make_service(cache_root).get(tdoc_id=tdoc_id)


def test_inconsistent_version_two_sql_is_not_hidden_by_stale_cache(
    sqlite_env,
    tmp_path,
) -> None:
    create_schema("main")
    tdoc_id = "R5-CONTENT-INCOMPLETE"
    ftp_url = "archive/R5-CONTENT-INCOMPLETE.zip"
    cache_root = tmp_path / "cache"
    meta, _ = _seed_sql_bundle(tdoc_id, ftp_url, cache_root)
    with get_session_factory()() as session:
        session.execute(
            delete(TDocCrAnnexOrm).where(
                TDocCrAnnexOrm.ftp_url == normalize_ftp_path(ftp_url),
                TDocCrAnnexOrm.doc_filename == ANNEX_SOURCE,
            )
        )
        session.commit()
    _write_legacy_cache(cache_root, meta.cache_file, b"stale legacy cache")

    with pytest.raises(
        TDocContentIncompleteError,
        match="re-parse",
    ) as exc_info:
        _make_service(cache_root).get(tdoc_id=tdoc_id)

    assert f"doc3gpp tdoc parse --tdoc {tdoc_id} --force" in str(exc_info.value)


def test_corrupt_sql_document_hash_raises_incomplete_reparse_error(
    sqlite_env,
    tmp_path,
) -> None:
    from sqlalchemy import update

    create_schema("main")
    tdoc_id = "R5-CONTENT-CORRUPT"
    ftp_url = "archive/R5-CONTENT-CORRUPT.zip"
    cache_root = tmp_path / "cache"
    _seed_sql_bundle(tdoc_id, ftp_url, cache_root)
    with get_session_factory()() as session:
        session.execute(
            update(TDocCrAnnexOrm)
            .where(
                TDocCrAnnexOrm.ftp_url == normalize_ftp_path(ftp_url),
                TDocCrAnnexOrm.doc_filename == ANNEX_SOURCE,
            )
            .values(content_hash="not-a-sha256")
        )
        session.commit()

    with pytest.raises(TDocContentIncompleteError, match="re-parse") as exc_info:
        _make_service(cache_root).get(tdoc_id=tdoc_id)

    assert f"doc3gpp tdoc parse --tdoc {tdoc_id} --force" in str(exc_info.value)


def test_document_model_type_error_raises_incomplete_reparse_error(
    sqlite_env,
    tmp_path,
    monkeypatch,
) -> None:
    create_schema("main")
    tdoc_id = "R5-CONTENT-BAD-MODEL"
    ftp_url = "archive/R5-CONTENT-BAD-MODEL.zip"
    _seed_sql_bundle(tdoc_id, ftp_url, tmp_path / "cache")
    service = _make_service(tmp_path / "cache")

    def invalid_row(*_args: object):
        raise TypeError("file_order must be an integer")

    monkeypatch.setattr(
        service._content_repository, "list_documents", invalid_row
    )
    with pytest.raises(TDocContentIncompleteError, match="re-parse") as exc_info:
        service.get(tdoc_id=tdoc_id)

    assert f"doc3gpp tdoc parse --tdoc {tdoc_id} --force" in str(exc_info.value)



def test_metadata_identity_mismatch_repair_hint_includes_force(
    sqlite_env,
    tmp_path,
) -> None:
    create_schema("main")
    tdoc_id = "R5-CONTENT-META-MISMATCH"
    other_id = "R5-CONTENT-META-OTHER"
    ftp_url = "archive/R5-CONTENT-META-MISMATCH.zip"
    tdoc_repository = SQLAlchemyTDocRepository()
    tdoc_repository.upsert(TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=ftp_url))
    tdoc_repository.upsert(
        TDoc(tdoc_id=other_id, type="CR", ftp_url="archive/other.zip")
    )
    SQLAlchemyTDocCrRepository().upsert_extract_meta(
        TDocExtractMeta(
            ftp_url=ftp_url,
            tdoc_id=other_id,
            cache_file=f"{tdoc_id}.zip",
            doc_filename="CR/main.docx",
        )
    )

    with pytest.raises(TDocContentIncompleteError) as exc_info:
        _make_service(tmp_path / "cache").get(tdoc_id=tdoc_id)

    assert f"doc3gpp tdoc parse --tdoc {tdoc_id} --force" in str(exc_info.value)


def test_tdoc_content_json_shape_is_shared_and_stable() -> None:
    document = TDocContentDocument(
        document_id="a" * 64,
        source_file="CR/main.docx",
        is_main=True,
        markdown="# Main" + chr(10),
    )
    content = TDocContent(
        tdoc_id="R5-CONTENT-JSON",
        ftp_url="archive/R5-CONTENT-JSON.zip",
        extraction_version=2,
        complete=True,
        documents=(document,),
    )

    assert tdoc_content_to_dict(content) == {
        "tdoc_id": "R5-CONTENT-JSON",
        "ftp_url": "archive/R5-CONTENT-JSON.zip",
        "extraction_version": 2,
        "complete": True,
        "skipped_sources": [],
        "documents": [
            {
                "document_id": "a" * 64,
                "source_file": "CR/main.docx",
                "is_main": True,
                "markdown": "# Main" + chr(10),
            }
        ],
    }


def test_shared_markdown_renderer_keeps_one_body_and_safely_labels_many() -> None:
    main = TDocContentDocument(
        document_id="a" * 64,
        source_file="z/main.docx",
        is_main=True,
        markdown="main body" + chr(10),
    )
    support = TDocContentDocument(
        document_id="b" * 64,
        source_file="annex/comment --> β.docx",
        is_main=False,
        markdown="support body",
    )

    assert render_tdoc_documents_markdown(
        (main,), include_source_headers=True
    ) == "main body" + chr(10)
    assert render_tdoc_documents_markdown(
        (main,), include_source_headers=False
    ) == "main body" + chr(10)
    assert render_tdoc_documents_markdown(
        (support, main), include_source_headers=True
    ) == (
        '<!-- doc3gpp-source: "z/main.docx" -->' + chr(10)
        + "main body" + chr(10) + chr(10)
        + '<!-- doc3gpp-source: "annex/comment --' + chr(92) + 'u003e β.docx" -->'
        + chr(10)
        + "support body"
    )
