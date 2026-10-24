"""Unit tests for :mod:`doc3gpp.services.tdoc_cr_service` helpers.

These tests exercise the markdown-cache compression logic and the DB-
cache-hit markdown population in isolation, without touching the SQLite
backend. The repository and scraper boundaries are stubbed with
:class:`unittest.mock.MagicMock`.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import logging
import threading
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_content_families import (
    TDOC_CONTENT_STORES,
    is_complete_cr_extraction,
)
from doc3gpp.models.tdoc_cr import (
    DirectParseResult,
    TDocCRChangeDetails,
    TDocCRDetails,
    TDocCRParseResult,
    TDocCRTTCNDetails,
    TDocExtractMeta,
)
from doc3gpp.models.tdoc_document import TDocDocument
from doc3gpp.parsers.tdoc_zip import (
    build_markdown_archive,
    document_id_for,
    read_markdown_archive,
)
from doc3gpp.scraping.cache import TDocCache
from doc3gpp.services.pipeline_runtime import PipelineRuntime
from doc3gpp.services.tdoc_cr_service import (
    BatchExtractResult,
    ExtractResult,
    PreparedDirectTDoc,
    PreparedTDocExtract,
    TDocCrService,
)
from doc3gpp.services.tdoc_family_service import TDocFamilyService
from doc3gpp.services.tdoc_pipeline import TDocPipeline
from doc3gpp.settings.schema import TDocParseSettings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import TDocCrAnnexOrm, TDocORM
from doc3gpp.storage.db.session import get_session_factory
from doc3gpp.storage.repositories.tdoc_content_sql import (
    SQLAlchemyTDocContentRepository,
)
from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
    SQLAlchemyTDocCrChangeDetailsRepository,
)
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
    SQLAlchemyTDocCrTtcnRepository,
)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tdoc_cr_doc"
CR_STORE = next(store for store in TDOC_CONTENT_STORES if store.key == "cr")


def _docx_available() -> bool:
    try:
        from docx import Document  # noqa: F401
    except ImportError:
        return False
    return True


def _build_service(
    tmp_path: Path,
    *,
    zip_bytes: bytes | None = None,
    family_service: TDocFamilyService | None = None,
) -> tuple[
    TDocCrService, MagicMock, TDocCache, MagicMock, MagicMock, MagicMock,
]:
    """Build a service with a real disk cache and stubbed repos/scraper.

    Returns ``(service, scraper_mock, cache, cr_repo, cr_ttcn_repo,
    tdoc_repo)`` so each test can assert against the three repository
    mocks (cover-page, TTCN sidecar, TDoc lookup) independently.
    """
    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    scraper = MagicMock()
    if zip_bytes is not None:
        scraper.get_bytes.return_value = zip_bytes

    cr_repo = MagicMock()
    cr_repo.get_by_url.return_value = None
    cr_ttcn_repo = MagicMock()
    cr_ttcn_repo.get_by_url.return_value = None
    cr_change_details_repo = MagicMock()
    cr_change_details_repo.get_by_url.return_value = None
    tdoc_repo = MagicMock()
    tdoc_repo.get_by_id.return_value = None
    content_repo = MagicMock()
    content_repo.get_extract_meta.return_value = None
    content_repo.list_documents.return_value = ()

    service = TDocCrService(
        cache=cache,
        scraper_client=scraper,
        cr_repository=cr_repo,
        cr_ttcn_repository=cr_ttcn_repo,
        cr_change_details_repository=cr_change_details_repo,
        tdoc_repository=tdoc_repo,
        content_repo=content_repo,
        family_service=family_service,
    )
    return service, scraper, cache, cr_repo, cr_ttcn_repo, tdoc_repo


def _dummy_details(tdoc_id: str) -> TDocCRDetails:
    return TDocCRDetails(tdoc_id=tdoc_id, spec="38.523-3", cr_num="3790", rev="0")


def _test_document(
    markdown_text: str,
    *,
    ftp_url: str = "stored/test.zip",
    source_file: str = "main.docx",
    file_order: int = 0,
) -> TDocDocument:
    document_id = document_id_for(ftp_url, source_file)
    stem = source_file.rsplit("/", 1)[-1][:-5] or "document"
    return TDocDocument(
        document_id=document_id,
        source_file=source_file,
        markdown_file=f"documents/{document_id}/{stem}.md",
        file_order=file_order,
        markdown_text=markdown_text,
        content_hash=hashlib.sha256(markdown_text.encode("utf-8")).hexdigest(),
    )


def _make_docx(text: str) -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph(text)
    payload = io.BytesIO()
    document.save(payload)
    return payload.getvalue()


def _multifile_zip_with_main(main_zip: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(main_zip)) as source:
        main_docx = source.read(next(
            name for name in source.namelist() if name.lower().endswith(".docx")
        ))
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("a/support-alpha.docx", _make_docx("support alpha text"))
        archive.writestr("b/support-beta.docx", _make_docx("support beta text"))
        archive.writestr("z/R5s260009.docx", main_docx)
    return payload.getvalue()


# ---------------------------------------------------------------------------
# Markdown cache compression / round-trip.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_markdown_cache_writes_real_multi_document_zip_and_manifest(
    tmp_path: Path,
) -> None:
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    zip_bytes = _multifile_zip_with_main(fixture.read_bytes())
    service, _, cache, _, _, tdoc_repo = _build_service(tmp_path, zip_bytes=zip_bytes)
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009", type="CR", ftp_url="stored/r5s260009.zip"
    )

    prepared = service.prepare_extract("R5s260009", force=True)

    assert isinstance(prepared, PreparedTDocExtract)
    assert prepared.main_source_file == "z/R5s260009.docx"
    assert prepared.extract_meta.doc_filename == "z/R5s260009.docx"
    assert prepared.extract_meta.extraction_version == 2
    assert prepared.extract_meta.document_count == 2
    raw = cache.get_bytes(prepared.extract_meta.cache_file, "markdown")
    assert raw is not None and raw.startswith(b"PK\x03\x04")
    archive = read_markdown_archive(raw)
    assert archive.manifest_version == 1
    assert archive.main_source_file == "z/R5s260009.docx"
    assert [document.source_file for document in archive.documents] == [
        "z/R5s260009.docx",
        "a/support-alpha.docx",
        "b/support-beta.docx",
    ]
    assert "support alpha text" in archive.documents[1].markdown_text
    assert "support beta text" in archive.documents[2].markdown_text


def test_markdown_cache_reads_legacy_plain_utf8(tmp_path: Path) -> None:
    """Legacy plain UTF-8 bytes remain isolated from versioned documents."""
    legacy_text = "legacy plain markdown cache content"
    archive = read_markdown_archive(legacy_text.encode("utf-8"))
    assert archive.is_legacy is True
    assert archive.legacy_markdown == legacy_text
    assert archive.documents == ()


def test_markdown_cache_reads_legacy_gzip_blob(tmp_path: Path) -> None:
    """Legacy gzip bytes remain readable without claiming provenance."""
    legacy_text = "legacy gzip-blob markdown cache content"
    archive = read_markdown_archive(gzip.compress(legacy_text.encode("utf-8")))
    assert archive.is_legacy is True
    assert archive.legacy_markdown == legacy_text
    assert archive.documents == ()


def test_markdown_cache_archive_contains_manifest_and_source_documents() -> None:
    ftp_url = "tsg_ran/r5s260009.zip"
    main = _test_document("# main\nbody\n", ftp_url=ftp_url)
    support = _test_document(
        "support body\n",
        ftp_url=ftp_url,
        source_file="annex/info.docx",
        file_order=1,
    )

    payload = build_markdown_archive(
        (main, support),
        ftp_url=ftp_url,
        main_source_file="main.docx",
    )

    assert payload.startswith(b"PK\x03\x04")
    decoded = read_markdown_archive(payload)
    assert decoded.manifest_version == 1
    assert decoded.main_source_file == "main.docx"
    assert [document.markdown_text for document in decoded.documents] == [
        "# main\nbody\n",
        "support body\n",
    ]
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert len(archive.namelist()) == 3
        assert sum(name.endswith(".md") for name in archive.namelist()) == 2


# ---------------------------------------------------------------------------
# DB cache hit populates markdown for --format raw.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_from_url_db_cache_hit_returns_sql_documents(
    tmp_path: Path,
) -> None:
    """A direct URL cache hit returns the archive main plus the SQL annexes."""
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    service, scraper, cache, cr_repo, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=fixture.read_bytes()
    )
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009",
        type="CR",
        ftp_url="tsg_ran/r5s260009.zip",
    )

    url = "https://www.3gpp.org/ftp/tsg_ran/R5s260009.zip"
    stored_ftp_url = "tsg_ran/r5s260009.zip"
    main = _test_document(
        "# Archive main\nStored body.",
        ftp_url=stored_ftp_url,
        source_file="z/R5s260009.docx",
    )
    annex = _test_document(
        "# SQL annex\nStored body.",
        ftp_url=stored_ftp_url,
        source_file="b/support-beta.docx",
        file_order=1,
    )
    meta = TDocExtractMeta(
        ftp_url=stored_ftp_url,
        tdoc_id="R5s260009",
        cache_file="R5s260009-abcdef0123456789.zip",
        doc_filename="z/R5s260009.docx",
        extraction_version=2,
        document_count=1,
    )
    cache.put_bytes(
        meta.cache_file,
        build_markdown_archive(
            (main, annex),
            ftp_url=stored_ftp_url,
            main_source_file=main.source_file,
        ),
        "markdown",
    )
    cr_repo.get_by_url.return_value = _dummy_details("R5s260009")
    service._content_repo.get_extract_meta.return_value = meta
    service._content_repo.list_documents.return_value = (annex,)

    result = service.extract_from_url(url, force=False)

    assert result.from_cache is True
    assert result.persisted is False
    assert result.tdoc_id == "R5s260009"
    assert result.tdoc_id_in_tdocs is True
    assert result.documents == (main, annex)
    assert result.main_source_file == "z/R5s260009.docx"
    assert scraper.get_bytes.call_count == 0
    service._content_repo.list_documents.assert_called_once_with(CR_STORE, stored_ftp_url)


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_incomplete_sql_bundle_is_reparsed_for_direct_url(tmp_path: Path) -> None:
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    service, scraper, _cache, cr_repo, _, tdoc_repo = _build_service(
        tmp_path, zip_bytes=fixture.read_bytes()
    )
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009", type="CR", ftp_url="tsg_ran/r5s260009.zip"
    )
    stored_url = "tsg_ran/r5s260009.zip"
    old_document = _test_document(
        "old body", ftp_url=stored_url, source_file="main.docx"
    )
    cr_repo.get_by_url.return_value = _dummy_details("R5s260009")
    service._content_repo.get_extract_meta.return_value = TDocExtractMeta(
        ftp_url=stored_url,
        tdoc_id="R5s260009",
        cache_file="R5s260009.zip",
        doc_filename="main.docx",
        extraction_version=2,
        document_count=2,
    )
    service._content_repo.list_documents.return_value = (old_document,)  # type: ignore[attr-defined]

    prepared = service.prepare_from_url(
        "https://www.3gpp.org/ftp/tsg_ran/r5s260009.zip",
        force=False,
    )

    assert isinstance(prepared, PreparedDirectTDoc)
    assert len(prepared.documents) == 1
    assert scraper.get_bytes.call_count == 1


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_prepare_extract_reparses_incomplete_sql_document_rows(tmp_path: Path) -> None:
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    service, scraper, _cache, cr_repo, _, tdoc_repo = _build_service(
        tmp_path, zip_bytes=fixture.read_bytes()
    )
    stored_url = "stored/r5s260009.zip"
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009", type="CR", ftp_url=stored_url
    )
    cr_repo.get_by_url.return_value = _dummy_details("R5s260009")
    service._content_repo.get_extract_meta.return_value = TDocExtractMeta(
        ftp_url=stored_url,
        tdoc_id="R5s260009",
        cache_file="R5s260009.zip",
        doc_filename="main.docx",
        extraction_version=2,
        document_count=2,
    )
    service._content_repo.list_documents.return_value = (  # type: ignore[attr-defined]
        _test_document("old body", ftp_url=stored_url, source_file="main.docx"),
    )

    prepared = service.prepare_extract("R5s260009", force=False)

    assert isinstance(prepared, PreparedTDocExtract)
    assert len(prepared.documents) == 1
    assert scraper.get_bytes.call_count == 1


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_prepare_extract_does_not_reuse_complete_fallback_for_current_url(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from doc3gpp.parsers.normalizers import normalize_ftp_path

    tdoc_id = "R5s260009"
    current_path = "stored/r5s260009.zip"
    fallback_path = "template/r5s260009.zip"
    current_url = f"https://www.3gpp.org/ftp/{current_path}"
    fallback_url = f"https://www.3gpp.org/ftp/{fallback_path}"
    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists(), f"fixture missing: {fixture}"

    service, _scraper, _cache, cr_repo, _, tdoc_repo = _build_service(tmp_path)
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id=tdoc_id, type="CR", ftp_url=current_path
    )
    current_key = normalize_ftp_path(current_url)
    fallback_key = normalize_ftp_path(fallback_url)
    details_by_url = {
        current_key: TDocCRDetails(tdoc_id=tdoc_id, ftp_url=current_key),
        fallback_key: TDocCRDetails(tdoc_id=tdoc_id, ftp_url=fallback_key),
    }
    meta_by_url = {
        current_key: TDocExtractMeta(
            ftp_url=current_key,
            tdoc_id=tdoc_id,
            cache_file="R5s260009.zip",
            doc_filename="main.docx",
            extraction_version=2,
            document_count=1,
        ),
        fallback_key: TDocExtractMeta(
            ftp_url=fallback_key,
            tdoc_id=tdoc_id,
            cache_file="R5s260009-fallback.zip",
            doc_filename="main.docx",
            extraction_version=2,
            document_count=1,
        ),
    }
    cr_repo.get_by_url.side_effect = details_by_url.get
    service._content_repo.get_extract_meta.side_effect = meta_by_url.get  # type: ignore[attr-defined]
    fallback_document = _test_document(
        "complete fallback body",
        ftp_url=fallback_key,
        source_file="annex.docx",
        file_order=1,
    )
    service._content_repo.list_documents.side_effect = (  # type: ignore[attr-defined]
        lambda _store, url: (fallback_document,) if url == fallback_key else ()
    )
    monkeypatch.setattr(
        "doc3gpp.services.tdoc_cr_service.resolve_download_url",
        lambda *_args: (current_url, fallback_url),
    )
    monkeypatch.setattr(
        "doc3gpp.services.tdoc_cr_service.download_tdoc_zip",
        lambda *_args, **_kwargs: SimpleNamespace(url=current_url, path=fixture),
    )

    prepared = service.prepare_extract(tdoc_id)

    assert isinstance(prepared, PreparedTDocExtract)
    assert prepared.extract_meta.ftp_url == current_key
    service.persist_prepared_extract(prepared)
    stored_meta = (
        service._content_repo.replace_cr_bundle.call_args.args[3]  # type: ignore[attr-defined]
    )
    assert stored_meta.ftp_url == current_key


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_prepare_from_url_defers_repository_writes(tmp_path: Path) -> None:
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    payload = _multifile_zip_with_main(fixture.read_bytes())
    service, scraper, _cache, cr_repo, cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=payload
    )
    tdoc_repo.get_by_id.return_value = TDoc(tdoc_id="R5s260009", type="CR")

    prepared = service.prepare_from_url(
        "https://www.3gpp.org/ftp/.../R5s260009.zip",
        force=True,
    )

    assert isinstance(prepared, PreparedDirectTDoc)
    assert prepared.tdoc_id == "R5s260009"
    assert prepared.main_source_file == "z/R5s260009.docx"
    assert len(prepared.documents) == 3
    assert not hasattr(prepared, "markdown")
    assert scraper.get_bytes.call_count == 1
    service._content_repo.replace_cr_bundle.assert_not_called()  # type: ignore[attr-defined]
    cr_repo.upsert.assert_not_called()
    cr_repo.upsert_extract_meta.assert_not_called()
    cr_ttcn_repo.upsert.assert_not_called()


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_url_folder_batch_returns_multidocument_bundle(tmp_path: Path) -> None:
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    payload = _multifile_zip_with_main(fixture.read_bytes())
    service, _, _, _, _, tdoc_repo = _build_service(tmp_path, zip_bytes=payload)
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009", type="CR", ftp_url="Docs/R5s260009.zip"
    )
    file_url = "https://www.3gpp.org/ftp/Docs/R5s260009.zip"
    service.collect_3gpp_file_urls = (  # type: ignore[method-assign]
        lambda _url, *, max_depth: [file_url]
    )

    batch = service.extract_from_url_batch(
        "https://www.3gpp.org/ftp/Docs/", max_depth=0,
    )

    assert batch.failures == {}
    assert batch.skipped == {}
    assert len(batch.results) == 1
    result = batch.results[0]
    assert result.main_source_file == "z/R5s260009.docx"
    assert [document.source_file for document in result.documents] == [
        "z/R5s260009.docx",
        "a/support-alpha.docx",
        "b/support-beta.docx",
    ]
    assert result.persisted is True
    service._content_repo.replace_cr_bundle.assert_called_once()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Three-way upsert split: cover + optional TTCN sidecar + extract meta.
# ---------------------------------------------------------------------------


class _FakeParser:
    """A :class:`TDocParser` double that returns a configurable result.

    Mirrors the production parser contract: ``parse(markdown,
    tdoc_id=...)`` returns a :class:`TDocCRParseResult` bundling the
    cover-page fields and the optional TTCN sidecar.
    """

    def __init__(self, result: TDocCRParseResult) -> None:
        self._result = result
        self.calls: list[str] = []

    parser_version = "1.0.0"

    def supports(
        self, tdoc_id: str, *, tdoc_type: str | None = None, spec: str | None = None,
    ) -> bool:
        return True

    def parse(
        self,
        markdown: str,
        *,
        tdoc_id: str,
        max_text_length: int = 0,
    ) -> TDocCRParseResult:
        self.calls.append(tdoc_id)
        return self._result


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_persists_one_atomic_multidocument_bundle(tmp_path: Path) -> None:
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    zip_bytes = _multifile_zip_with_main(fixture.read_bytes())

    cover = TDocCRDetails(
        tdoc_id="R5s260009", spec="38.523-3", cr_num="3790", rev="0"
    )
    ttcn = TDocCRTTCNDetails(
        tdoc_id="R5s260009",
        testcase="7.1.3.5.3",
        required_changes=[{"function_name": "fl_TC_7_1_3_5_3_Body"}],
    )
    changes = TDocCRChangeDetails(clauses=("38.523-3",))
    parser = _FakeParser(TDocCRParseResult(cover=cover, ttcn=ttcn, changes=changes))

    service, _, _, cr_repo, cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=zip_bytes,
    )
    service._parser = parser  # type: ignore[attr-defined]
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009", type="CR", ftp_url="stored/r5s260009.zip"
    )

    prepared = service.prepare_extract("R5s260009")
    assert isinstance(prepared, PreparedTDocExtract)
    assert prepared.main_source_file == "z/R5s260009.docx"
    assert [document.source_file for document in prepared.documents] == [
        "z/R5s260009.docx",
        "a/support-alpha.docx",
        "b/support-beta.docx",
    ]
    assert [document.source_file for document in prepared.annexes] == [
        "a/support-alpha.docx",
        "b/support-beta.docx",
    ]
    assert parser.calls == ["R5s260009"]

    result = service.persist_prepared_extract(prepared)

    service._content_repo.replace_cr_bundle.assert_called_once()  # type: ignore[attr-defined]
    details_arg, ttcn_arg, changes_arg, meta_arg, documents_arg = (
        service._content_repo.replace_cr_bundle.call_args.args  # type: ignore[attr-defined]
    )
    assert details_arg.ftp_url == "stored/r5s260009.zip"
    assert ttcn_arg.testcase == "7.1.3.5.3"
    assert changes_arg.clauses == ("38.523-3",)
    assert meta_arg.ftp_url == details_arg.ftp_url
    assert meta_arg.doc_filename == "z/R5s260009.docx"
    assert meta_arg.extraction_version == 2
    assert meta_arg.document_count == 2
    assert [document.source_file for document in documents_arg] == [
        "a/support-alpha.docx",
        "b/support-beta.docx",
    ]
    assert all(document.document_id == document_id_for(
        "stored/r5s260009.zip", document.source_file
    ) for document in documents_arg)
    assert result.details.cr_num == "3790"
    assert result.extract_meta.document_count == 2
    cr_repo.upsert.assert_not_called()
    cr_repo.upsert_extract_meta.assert_not_called()
    cr_ttcn_repo.upsert.assert_not_called()


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_prepare_extract_defers_atomic_bundle_write_until_persist(tmp_path: Path) -> None:
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    service, _, _, cr_repo, cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=fixture.read_bytes(),
    )
    service._parser = _FakeParser(  # type: ignore[attr-defined]
        TDocCRParseResult(
            cover=TDocCRDetails(tdoc_id="R5s260009", spec="38.523-3", cr_num="3790"),
            ttcn=TDocCRTTCNDetails(tdoc_id="R5s260009", testcase="7.1.3.5.3"),
        )
    )
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009", type="CR", ftp_url="stored/r5s260009.zip"
    )

    prepared = service.prepare_extract("R5s260009")

    service._content_repo.replace_cr_bundle.assert_not_called()  # type: ignore[attr-defined]
    result = service.persist_prepared_extract(prepared)
    assert result.details.tdoc_id == "R5s260009"
    service._content_repo.replace_cr_bundle.assert_called_once()  # type: ignore[attr-defined]
    cr_repo.upsert.assert_not_called()
    cr_ttcn_repo.upsert.assert_not_called()
    cr_repo.upsert_extract_meta.assert_not_called()


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_atomic_bundle_clears_absent_optional_sidecars(tmp_path: Path) -> None:
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    parser = _FakeParser(
        TDocCRParseResult(
            cover=TDocCRDetails(
                tdoc_id="R5s260009", spec="38.523-3", cr_num="3790"
            ),
            ttcn=None,
            changes=None,
        )
    )
    service, _, _, _, _, tdoc_repo = _build_service(
        tmp_path, zip_bytes=fixture.read_bytes(),
    )
    service._parser = parser  # type: ignore[attr-defined]
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009", type="CR", ftp_url="stored/r5s260009.zip"
    )

    result = service.extract("R5s260009")

    service._content_repo.replace_cr_bundle.assert_called_once()  # type: ignore[attr-defined]
    _details, ttcn, changes, _meta, _documents = (
        service._content_repo.replace_cr_bundle.call_args.args  # type: ignore[attr-defined]
    )
    assert ttcn is None
    assert changes is None
    assert result.from_cache is False


def test_extract_three_upsert_order_preserved(tmp_path: Path) -> None:
    """One aggregate replacement owns cover, sidecars, metadata, and documents."""
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    cover = TDocCRDetails(tdoc_id="R5s260009", spec="38.523-3", cr_num="3790")
    ttcn = TDocCRTTCNDetails(tdoc_id="R5s260009", testcase="7.1.3.5.3")
    service, _, _, cr_repo, cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=fixture.read_bytes(),
    )
    service._parser = _FakeParser(TDocCRParseResult(cover=cover, ttcn=ttcn))  # type: ignore[attr-defined]
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009", type="CR", ftp_url="stored/r5s260009.zip"
    )

    service.extract("R5s260009")

    service._content_repo.replace_cr_bundle.assert_called_once()  # type: ignore[attr-defined]
    _details, stored_ttcn, _changes, _meta, docs = (
        service._content_repo.replace_cr_bundle.call_args.args  # type: ignore[attr-defined]
    )
    assert stored_ttcn is not None
    assert docs == ()
    cr_repo.upsert.assert_not_called()
    cr_ttcn_repo.upsert.assert_not_called()
    cr_repo.upsert_extract_meta.assert_not_called()


def test_extract_atomic_bundle_uses_matching_ftp_url(tmp_path: Path) -> None:
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    cover = TDocCRDetails(tdoc_id="R5s260009", spec="38.523-3", cr_num="3790")
    ttcn = TDocCRTTCNDetails(tdoc_id="R5s260009", testcase="7.1.3.5.3")
    service, _, _, _, _, tdoc_repo = _build_service(
        tmp_path,
        zip_bytes=_multifile_zip_with_main(fixture.read_bytes()),
    )
    service._parser = _FakeParser(TDocCRParseResult(cover=cover, ttcn=ttcn))  # type: ignore[attr-defined]
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009", type="CR", ftp_url="stored/r5s260009.zip"
    )

    service.extract("R5s260009")

    details_arg, ttcn_arg, _changes, meta_arg, documents = (
        service._content_repo.replace_cr_bundle.call_args.args  # type: ignore[attr-defined]
    )
    assert details_arg.ftp_url == ttcn_arg.ftp_url == meta_arg.ftp_url
    assert meta_arg.ftp_url == "stored/r5s260009.zip"
    assert documents[0].source_file == "a/support-alpha.docx"
    assert documents[0].document_id == document_id_for(meta_arg.ftp_url, "a/support-alpha.docx")


# ---------------------------------------------------------------------------
# TTCN ``before_change`` / ``after_change`` / ``new_change`` extraction runs
# on every parse; there is no opt-out.
# ---------------------------------------------------------------------------


def test_extract_invokes_parser_for_full_ttcn_extraction(tmp_path: Path) -> None:
    """``extract(...)`` hands the TDoc to the parser on every call.

    The TTCN corrections sub-parser always extracts the
    ``before_change`` / ``after_change`` / ``new_change`` bodies, so the
    service has no mode switch to forward.
    """
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    zip_bytes = fixture.read_bytes()

    cover = TDocCRDetails(tdoc_id="R5s260009", spec="38.523-3", cr_num="3790")
    parser = _FakeParser(TDocCRParseResult(cover=cover))

    service, _, _, _cr_repo, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=zip_bytes,
    )
    service._parser = parser  # type: ignore[attr-defined]
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009",
        type="CR",
        ftp_url="stored/r5s260009.zip",
    )

    service.extract("R5s260009")

    assert parser.calls == ["R5s260009"]


def test_extract_many_invokes_parser_for_each_tdoc(tmp_path: Path) -> None:
    """``extract_many(...)`` runs the parser once per TDoc in the batch."""
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    zip_bytes = fixture.read_bytes()

    cover_a = TDocCRDetails(tdoc_id="R5s260009", spec="38.523-3", cr_num="3790")
    cover_b = TDocCRDetails(tdoc_id="R5s260051", spec="38.523-3", cr_num="3806")

    extract_calls: list[str] = []

    class _RecordingParser:
        parser_version = "1.0.0"

        def supports(
            self, tdoc_id: str, *, tdoc_type: str | None = None, spec: str | None = None,
        ) -> bool:
            return True

        def parse(
            self,
            markdown: str,
            *,
            tdoc_id: str,
            max_text_length: int = 0,
        ) -> TDocCRParseResult:
            extract_calls.append(tdoc_id)
            return TDocCRParseResult(
                cover=cover_a if tdoc_id == "R5s260009" else cover_b,
            )

    service, _, _, _cr_repo, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=zip_bytes,
    )
    service._parser = _RecordingParser()  # type: ignore[attr-defined]
    tdoc_repo.get_by_id.side_effect = lambda tdoc_id: TDoc(
        tdoc_id=tdoc_id,
        type="CR",
        ftp_url=f"stored/{tdoc_id}.zip",
    )

    service.extract_many(["R5s260009", "R5s260051"])

    assert extract_calls == ["R5s260009", "R5s260051"]


def test_extract_raises_skip_when_ftp_url_is_null(tmp_path: Path) -> None:
    """Regression: ``tdocs.ftp_url`` is nullable (R5-112 had 43/62 CR rows
    with NULL). Previously ``derive_cache_file(None)`` raised a bare
    ``TypeError`` from ``Path(None).name``. The contract is now that a
    NULL ``ftp_url`` is the upstream sign "3GPP hasn't uploaded this
    yet", so ``extract`` raises :class:`TDocNotYetOnFTPError` — the
    service is the right layer to flag this because the only path
    that consumes ``derive_cache_file(ftp_url)`` is gated on
    ``ftp_url`` being truthy.
    """
    from doc3gpp.services.tdoc_cr_service import TDocNotYetOnFTPError

    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    scraper = MagicMock()
    cr_repo = MagicMock()
    cr_repo.get_by_url.return_value = None
    cr_ttcn_repo = MagicMock()
    cr_ttcn_repo.get_by_url.return_value = None
    tdoc_repo = MagicMock()
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s263431",
        type="CR",
        ftp_url=None,
    )

    service = TDocCrService(
        cache=cache,
        scraper_client=scraper,
        cr_repository=cr_repo,
        cr_ttcn_repository=cr_ttcn_repo,
        cr_change_details_repository=MagicMock(),
        tdoc_repository=tdoc_repo,
        content_repo=MagicMock(),
    )

    with pytest.raises(TDocNotYetOnFTPError) as info:
        service.extract("R5s263431")
    assert info.value.tdoc_id == "R5s263431"

    # And: the scraper must not have been hit — there is nothing to
    # download until the 3GPP pipeline publishes a final URL.
    scraper.get_bytes.assert_not_called()


def test_extract_many_routes_null_ftp_url_into_skip_bucket(
    tmp_path: Path,
) -> None:
    """``extract_many`` routes :class:`TDocNotYetOnFTPError` into
    :attr:`BatchExtractResult.skipped` (not failures) so the CLI can
    surface "FTP hasn't published yet" as its own summary line.
    Mixed batch: one row's ftp_url is None (skipped), one row has a
    real URL (would succeed but the fake parse layer just skips it
    here), one row is type="LS" (real failure).
    """

    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    scraper = MagicMock()
    cr_repo = MagicMock()
    cr_repo.get_by_url.return_value = None
    cr_ttcn_repo = MagicMock()
    cr_ttcn_repo.get_by_url.return_value = None
    tdoc_repo = MagicMock()
    tdoc_repo.get_by_id.side_effect = lambda tdoc_id: {
        "R5s263431": TDoc(tdoc_id="R5s263431", type="CR", ftp_url=None),
        "R5s263432": TDoc(
            tdoc_id="R5s263432",
            type="CR",
            ftp_url=f"tsg_ran/{tdoc_id}.zip",
        ),
        "R5s263499": TDoc(
            tdoc_id="R5s263499",
            type="other",
            ftp_url=f"tsg_ran/{tdoc_id}.zip",
        ),
    }[tdoc_id]

    service = TDocCrService(
        cache=cache,
        scraper_client=scraper,
        cr_repository=cr_repo,
        cr_ttcn_repository=cr_ttcn_repo,
        cr_change_details_repository=MagicMock(),
        tdoc_repository=tdoc_repo,
        content_repo=MagicMock(),
    )

    result = service.extract_many(["R5s263431", "R5s263432", "R5s263499"])

    assert "R5s263431" in result.skipped
    assert "TDocNotYetOnFTPError" in result.skipped["R5s263431"]
    # The "no ftp_url" row is NOT in failures.
    assert "R5s263431" not in result.failures
    # The ineligible-type row is skipped before any download.
    assert "R5s263499" in result.skipped
    assert "TDocTypeUnsupportedError" in result.skipped["R5s263499"]
    assert "R5s263499" not in result.failures


def test_extract_many_skipped_only_batch_exits_cleanly(
    tmp_path: Path,
) -> None:
    """A batch where every row has NULL ftp_url returns a
    :class:`BatchExtractResult` with all rows in the ``skipped`` dict
    and no failures — letting the CLI exit 0 even though no work was
    done (FTP hasn't published anything yet, not an error from our
    side).
    """

    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    scraper = MagicMock()
    cr_repo = MagicMock()
    cr_repo.get_by_url.return_value = None
    cr_ttcn_repo = MagicMock()
    cr_ttcn_repo.get_by_url.return_value = None
    tdoc_repo = MagicMock()
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s263431",
        type="CR",
        ftp_url=None,
    )

    service = TDocCrService(
        cache=cache,
        scraper_client=scraper,
        cr_repository=cr_repo,
        cr_ttcn_repository=cr_ttcn_repo,
        cr_change_details_repository=MagicMock(),
        tdoc_repository=tdoc_repo,
        content_repo=MagicMock(),
    )

    result = service.extract_many(["R5s263431"])
    assert result.failures == {}
    assert "R5s263431" in result.skipped
    assert "TDocNotYetOnFTPError" in result.skipped["R5s263431"]


# ---------------------------------------------------------------------------
# TDocTooLargeError + max_tdoc_size_bytes kwarg
# ---------------------------------------------------------------------------


def test_tdoc_parse_archive_limits_default_and_allow_zero() -> None:
    settings = TDocParseSettings()
    assert settings.max_documents_per_zip == 100
    assert settings.max_uncompressed_size_kb == 102400

    unlimited = TDocParseSettings(
        max_documents_per_zip=0,
        max_uncompressed_size_kb=0,
    )
    assert unlimited.max_documents_per_zip == 0
    assert unlimited.max_uncompressed_size_kb == 0


def test_constructor_default_max_tdoc_size_bytes_is_zero(tmp_path) -> None:
    """``TDocCrService(...)`` defaults to ``max_tdoc_size_bytes=0`` (no check).

    Existing test fixtures construct the service without the new
    kwarg; the default must be backwards-compatible.
    """
    _, _, _, _, _, _ = _build_service(tmp_path)
    # Re-build via the kwargs the test cares about — go through the
    # constructor explicitly so the kwarg default is verified.
    service = TDocCrService(
        cache=TDocCache(root=tmp_path / "cache2", size_limit_bytes=0),
        scraper_client=MagicMock(),
        cr_repository=MagicMock(),
        cr_ttcn_repository=MagicMock(),
        cr_change_details_repository=MagicMock(),
        tdoc_repository=MagicMock(),
        content_repo=MagicMock(),
    )
    assert service._max_tdoc_size_bytes == 0


def test_constructor_accepts_max_tdoc_size_bytes(tmp_path) -> None:
    """Passing ``max_tdoc_size_bytes=N`` is stored verbatim."""
    service = TDocCrService(
        cache=TDocCache(root=tmp_path / "cache3", size_limit_bytes=0),
        scraper_client=MagicMock(),
        cr_repository=MagicMock(),
        cr_ttcn_repository=MagicMock(),
        cr_change_details_repository=MagicMock(),
        tdoc_repository=MagicMock(),
        content_repo=MagicMock(),
        max_tdoc_size_bytes=2_500_000,
    )
    assert service._max_tdoc_size_bytes == 2_500_000


def test_too_large_error_carries_size_and_limit() -> None:
    """``TDocTooLargeError`` exposes ``source``/``size``/``limit`` attributes
    so the CLI summary formatter can render a precise reason string.
    """
    from doc3gpp.services.tdoc_cr_service import TDocTooLargeError

    exc = TDocTooLargeError(source="x.zip", size=5_000_000, limit=1_024_000)
    assert exc.source == "x.zip"
    assert exc.size == 5_000_000
    assert exc.limit == 1_024_000
    assert "5_000_000" in str(exc) or "5000000" in str(exc)
    assert "1_024_000" in str(exc) or "1024000" in str(exc)


def test_extract_many_routes_too_large_to_skipped(tmp_path) -> None:
    """When ``extract`` raises ``TDocTooLargeError``, ``extract_many``
    puts the id in ``skipped`` (NOT in ``failures``).
    """
    from doc3gpp.services.tdoc_cr_service import TDocTooLargeError

    service, _, _, _, _, _ = _build_service(tmp_path)

    def _boom(tdoc_id: str, *, force: bool = False):
        raise TDocTooLargeError(source=tdoc_id, size=10, limit=5)

    service.prepare_extract = _boom  # type: ignore[method-assign]

    batch = service.extract_many(["R5-260100", "R5-260101"])
    assert set(batch.failures) == set()
    assert set(batch.skipped.keys()) == {"R5-260100", "R5-260101"}
    assert all(
        reason.startswith("TDocTooLargeError:")
        for reason in batch.skipped.values()
    )


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
@pytest.mark.parametrize("limit_name", ["document_count", "uncompressed_bytes"])
def test_archive_limit_violations_use_batch_skip_bucket(
    tmp_path: Path, limit_name: str,
) -> None:
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    payload = _multifile_zip_with_main(fixture.read_bytes())
    service, _, _, _, _, tdoc_repo = _build_service(tmp_path, zip_bytes=payload)
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009", type="CR", ftp_url="stored/r5s260009.zip"
    )
    if limit_name == "document_count":
        service._max_documents_per_zip = 1  # type: ignore[attr-defined]
    else:
        service._max_uncompressed_size_bytes = 32  # type: ignore[attr-defined]

    batch = service.extract_many(["R5s260009"])

    assert batch.failures == {}
    assert set(batch.skipped) == {"R5s260009"}
    assert batch.skipped["R5s260009"].startswith("TDocZipLimitError:")
    service._content_repo.replace_cr_bundle.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
@pytest.mark.parametrize("limit_name", ["document_count", "uncompressed_bytes"])
def test_url_folder_archive_limit_violations_use_skip_bucket(
    tmp_path: Path, limit_name: str,
) -> None:
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    payload = _multifile_zip_with_main(fixture.read_bytes())
    service, _, _, _, _, tdoc_repo = _build_service(tmp_path, zip_bytes=payload)
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009", type="CR", ftp_url="Docs/R5s260009.zip"
    )
    file_url = "https://www.3gpp.org/ftp/Docs/R5s260009.zip"
    service.collect_3gpp_file_urls = (  # type: ignore[method-assign]
        lambda _url, *, max_depth: [file_url]
    )
    if limit_name == "document_count":
        service._max_documents_per_zip = 1  # type: ignore[attr-defined]
    else:
        service._max_uncompressed_size_bytes = 32  # type: ignore[attr-defined]

    batch = service.extract_from_url_batch(
        "https://www.3gpp.org/ftp/Docs/", max_depth=0,
    )

    assert batch.failures == {}
    assert set(batch.skipped) == {file_url}
    assert batch.skipped[file_url].startswith("TDocZipLimitError:")
    service._content_repo.replace_cr_bundle.assert_not_called()  # type: ignore[attr-defined]


def test_extract_from_url_batch_routes_too_large_to_skipped(tmp_path) -> None:
    """``extract_from_url_batch`` puts oversized URLs in ``skipped``
    (NOT in ``failures``).
    """
    from doc3gpp.services.tdoc_cr_service import TDocTooLargeError

    service, _, _, _, _, _ = _build_service(tmp_path)

    urls = [
        "https://www.3gpp.org/ftp/R5s260001.zip",
        "https://www.3gpp.org/ftp/R5s260002.zip",
    ]

    service.collect_3gpp_file_urls = lambda url, *, max_depth: urls  # type: ignore[method-assign]

    def _boom(url: str, *, force: bool = False,
              max_tdoc_size_bytes: int | None = None):
        raise TDocTooLargeError(source=url, size=99, limit=10)

    service.prepare_from_url = _boom  # type: ignore[method-assign]

    batch = service.extract_from_url_batch(
        "https://www.3gpp.org/ftp/folder/", max_depth=0,
    )

    assert batch.failures == {}
    assert set(batch.skipped.keys()) == set(urls)
    for url, reason in batch.skipped.items():
        assert reason.startswith("TDocTooLargeError:"), url


def test_extract_many_is_cancelled_aborts_batch(tmp_path) -> None:
    """``extract_many`` honours ``is_cancelled``: when the callback
    returns True, the loop raises :class:`asyncio.CancelledError` and
    the in-flight batch aborts (so the worker can land the job in
    CANCELLED instead of running the whole list to completion).
    """
    service, _, _, _, _, _ = _build_service(tmp_path)

    cancel_calls = {"n": 0}

    def _is_cancelled() -> bool:
        cancel_calls["n"] += 1
        # Fire after the second poll: the first iteration runs to
        # completion (one extract), the second aborts.
        return cancel_calls["n"] >= 2

    with pytest.raises(asyncio.CancelledError):
        service.extract_many(
            ["TDoc-1", "TDoc-2", "TDoc-3", "TDoc-4"],
            is_cancelled=_is_cancelled,
        )
    assert cancel_calls["n"] >= 2


def test_extract_from_url_batch_is_cancelled_aborts_batch(tmp_path) -> None:
    """``extract_from_url_batch`` honours ``is_cancelled`` between files.

    Fires the cancel before the second iteration so the first file
    still completes (allowing its on_progress to land) but the rest
    never start.
    """
    service, _, _, _, _, _ = _build_service(tmp_path)

    progress_lines: list[str] = []
    seen: list[str] = []

    def _extract(url: str, **kwargs):
        seen.append(url)
        document = _test_document("direct body", ftp_url="direct/test.zip")
        return DirectParseResult(
            source_kind="url-3gpp",
            documents=(document,),
            main_source_file=document.source_file,
            details=None,
            extract_meta=None,
            from_cache=False,
            persisted=False,
            tdoc_id=None,
            tdoc_id_in_tdocs=False,
            source_url=url,
        )

    service.prepare_from_url = _extract  # type: ignore[assignment]

    file_urls = [
        f"https://www.3gpp.org/ftp/R5s26000{i}.docx" for i in range(5)
    ]
    service.collect_3gpp_file_urls = lambda url, *, max_depth: list(file_urls)  # type: ignore[assignment]

    cancel_calls = {"n": 0}

    def _is_cancelled() -> bool:
        cancel_calls["n"] += 1
        return cancel_calls["n"] >= 2

    with pytest.raises(asyncio.CancelledError):
        service.extract_from_url_batch(
            "https://www.3gpp.org/ftp/folder/",
            max_depth=0,
            on_progress=progress_lines.append,
            is_cancelled=_is_cancelled,
        )

    # First file runs, second observes the cancel and aborts.
    assert len(seen) < 5, f"expected cancel to short-circuit; saw {seen}"


def test_extract_many_uses_runner_for_parallel_prepare_and_serial_persist(
    tmp_path: Path,
) -> None:
    service, _scraper, _cache, _cr_repo, _cr_ttcn_repo, _tdoc_repo = _build_service(
        tmp_path,
    )
    runtime = PipelineRuntime(prepare_workers=2, max_pending=4)
    runner = TDocPipeline(runtime)
    prepare_barrier = threading.Barrier(2)
    first_writer_started = threading.Event()
    second_writer_started = threading.Event()
    release_first_writer = threading.Event()
    writer_lock = threading.Lock()
    active_writers = 0
    max_active_writers = 0
    writer_calls = 0
    result_box: list[BatchExtractResult] = []
    error_box: list[BaseException] = []

    def prepared(tdoc_id: str) -> PreparedTDocExtract:
        return PreparedTDocExtract(
            details=_dummy_details(tdoc_id),
            documents=(
                _test_document(
                    "bundle markdown",
                    ftp_url=f"stored/{tdoc_id}.zip",
                    source_file=f"{tdoc_id}.docx",
                ),
            ),
            main_source_file=f"{tdoc_id}.docx",
            ttcn=None,
            changes=None,
            extract_meta=TDocExtractMeta(
                ftp_url=f"stored/{tdoc_id}.zip",
                tdoc_id=tdoc_id,
                cache_file=f"{tdoc_id}.zip",
                doc_filename=f"{tdoc_id}.docx",
            ),
        )

    def prepare(raw_id: str, *, force: bool = False):
        del force
        prepare_barrier.wait(timeout=2)
        return prepared(raw_id.strip())

    def persist(value: PreparedTDocExtract) -> ExtractResult:
        nonlocal active_writers, max_active_writers, writer_calls
        with writer_lock:
            writer_calls += 1
            active_writers += 1
            max_active_writers = max(max_active_writers, active_writers)
            call_number = writer_calls
        try:
            if call_number == 1:
                first_writer_started.set()
                assert release_first_writer.wait(timeout=2)
            else:
                second_writer_started.set()
            return ExtractResult(
                details=value.details,
                extract_meta=value.extract_meta,
                from_cache=False,
            )
        finally:
            with writer_lock:
                active_writers -= 1

    service.prepare_extract = prepare  # type: ignore[method-assign]
    service.persist_prepared_extract = persist  # type: ignore[method-assign]

    def run() -> None:
        try:
            result_box.append(
                service.extract_many(
                    [" R5s260001 ", "R5s260002"],
                    runner=runner,
                )
            )
        except BaseException as exc:  # noqa: BLE001 - assertion aid
            error_box.append(exc)

    thread = threading.Thread(target=run)
    try:
        thread.start()
        assert first_writer_started.wait(timeout=2)
        assert not second_writer_started.is_set()
        release_first_writer.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert not error_box
        assert max_active_writers == 1
        assert set(result_box[0].successes) == {"R5s260001", "R5s260002"}
    finally:
        release_first_writer.set()
        thread.join(timeout=2)
        runtime.shutdown()


def test_extract_many_writer_failure_does_not_stop_later_item(tmp_path: Path) -> None:
    service, _scraper, _cache, _cr_repo, _cr_ttcn_repo, _tdoc_repo = _build_service(
        tmp_path,
    )
    runtime = PipelineRuntime(prepare_workers=2, max_pending=4)
    runner = TDocPipeline(runtime)
    persisted: list[str] = []

    def prepared(tdoc_id: str) -> PreparedTDocExtract:
        return PreparedTDocExtract(
            details=_dummy_details(tdoc_id),
            documents=(
                _test_document(
                    "bundle markdown",
                    ftp_url=f"stored/{tdoc_id}.zip",
                    source_file=f"{tdoc_id}.docx",
                ),
            ),
            main_source_file=f"{tdoc_id}.docx",
            ttcn=None,
            changes=None,
            extract_meta=TDocExtractMeta(
                ftp_url=f"stored/{tdoc_id}.zip",
                tdoc_id=tdoc_id,
                cache_file=f"{tdoc_id}.zip",
                doc_filename=f"{tdoc_id}.docx",
            ),
        )

    def prepare(raw_id: str, *, force: bool = False):
        del force
        return prepared(raw_id)

    def persist(value: PreparedTDocExtract) -> ExtractResult:
        persisted.append(value.details.tdoc_id)
        if value.details.tdoc_id == "first":
            raise RuntimeError("writer failed")
        return ExtractResult(
            details=value.details,
            extract_meta=value.extract_meta,
            from_cache=False,
        )

    service.prepare_extract = prepare  # type: ignore[method-assign]
    service.persist_prepared_extract = persist  # type: ignore[method-assign]
    try:
        result = service.extract_many(["first", "later"], runner=runner)
    finally:
        runtime.shutdown()

    assert persisted == ["first", "later"]
    assert result.successes["later"].details.tdoc_id == "later"
    assert result.failures == {"first": "RuntimeError: writer failed"}


# ---------------------------------------------------------------------------
# CR bundle contract: annexes in SQL, main only in the archive (P9, P13, R24).
# ---------------------------------------------------------------------------


def _main_docx() -> bytes:
    with zipfile.ZipFile(FIXTURES_DIR / "R5s260009.zip") as source:
        return source.read("R5s260009.docx")


def _zip_of(*members: tuple[str, bytes]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for name, data in members:
            archive.writestr(name, data)
    return payload.getvalue()


def _cr_zip(*annexes: tuple[str, bytes]) -> bytes:
    """Return a TDoc ZIP holding the CR fixture as ``R5s260009.docx`` plus annexes."""
    return _zip_of(("R5s260009.docx", _main_docx()), *annexes)


def _make_empty_docx() -> bytes:
    from docx import Document

    payload = io.BytesIO()
    Document().save(payload)
    return payload.getvalue()


def _tdoc_row() -> TDoc:
    return TDoc(tdoc_id="R5s260009", type="CR", ftp_url="tsg_ran/r5s260009.zip")


@pytest.fixture()
def cr_db(sqlite_env) -> None:
    create_schema("main")
    with get_session_factory()() as session:
        session.add(
            TDocORM(
                tdoc_id="R5s260009",
                type="CR",
                ftp_url="tsg_ran/r5s260009.zip",
            )
        )
        session.commit()


def _real_service(tmp_path: Path, zip_bytes: bytes) -> tuple[TDocCrService, MagicMock, TDocCache]:
    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    scraper = MagicMock()
    scraper.get_bytes.return_value = zip_bytes
    tdoc_repo = MagicMock()
    tdoc_repo.get_by_id.return_value = _tdoc_row()
    service = TDocCrService(
        cache=cache,
        scraper_client=scraper,
        cr_repository=SQLAlchemyTDocCrRepository(),
        cr_ttcn_repository=SQLAlchemyTDocCrTtcnRepository(),
        cr_change_details_repository=SQLAlchemyTDocCrChangeDetailsRepository(),
        tdoc_repository=tdoc_repo,
        content_repo=SQLAlchemyTDocContentRepository(),
        family_service=MagicMock(),
    )
    return service, scraper, cache


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_cr_persist_writes_annexes_only(cr_db, tmp_path: Path) -> None:
    service, _scraper, cache = _real_service(
        tmp_path, _cr_zip(("b/support-beta.docx", _make_docx("support beta text")))
    )

    prepared = service.prepare_extract("R5s260009", force=True)
    result = service.persist_prepared_extract(prepared)

    assert isinstance(result, ExtractResult)
    assert result.extract_meta.document_count == 1
    with get_session_factory()() as session:
        annex_sources = list(session.scalars(select(TDocCrAnnexOrm.doc_filename)))
    assert annex_sources == ["b/support-beta.docx"]
    archive = read_markdown_archive(cache.get_bytes(result.extract_meta.cache_file, "markdown"))
    assert [document.source_file for document in archive.documents] == [
        "R5s260009.docx",
        "b/support-beta.docx",
    ]


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_cr_cache_hit_requires_archive(cr_db, tmp_path: Path) -> None:
    service, _scraper, cache = _real_service(
        tmp_path, _cr_zip(("b/support-beta.docx", _make_docx("support beta text")))
    )
    first = service.extract("R5s260009")
    assert first.from_cache is False

    hit = service.prepare_extract("R5s260009")
    assert isinstance(hit, ExtractResult)
    assert hit.from_cache is True
    assert hit.tdoc_id == "R5s260009"

    archive_path = cache.path_for(first.extract_meta.cache_file, "markdown")
    archive_path.unlink()

    again = service.prepare_extract("R5s260009")
    assert isinstance(again, PreparedTDocExtract)
    assert archive_path.is_file()


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_cr_zero_annex_bundle_is_complete_with_cover(tmp_path: Path) -> None:
    service, _scraper, _cache, _cr_repo, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=_cr_zip()
    )
    tdoc_repo.get_by_id.return_value = _tdoc_row()

    prepared = service.prepare_extract("R5s260009", force=True)

    assert isinstance(prepared, PreparedTDocExtract)
    assert prepared.annexes == ()
    assert prepared.extract_meta.document_count == 0
    assert is_complete_cr_extraction(prepared.extract_meta, prepared.annexes, has_cover=True) is True


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_cr_annex_failure_is_tolerant(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    payload = _cr_zip(
        ("a/support-alpha.docx", b"not a docx"),
        ("b/support-beta.docx", _make_docx("support beta text")),
    )
    service, _scraper, _cache, _cr_repo, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=payload
    )
    tdoc_repo.get_by_id.return_value = _tdoc_row()

    with caplog.at_level(logging.WARNING):
        prepared = service.prepare_extract("R5s260009", force=True)

    assert isinstance(prepared, PreparedTDocExtract)
    assert prepared.extract_meta.skipped_sources == ("a/support-alpha.docx",)
    assert is_complete_cr_extraction(prepared.extract_meta, prepared.annexes, has_cover=True) is False
    assert prepared.extract_meta.document_count == 1
    assert [document.source_file for document in prepared.documents] == [
        "R5s260009.docx",
        "b/support-beta.docx",
    ]
    assert [document.source_file for document in prepared.annexes] == [
        "b/support-beta.docx",
    ]
    assert "a/support-alpha.docx" in caplog.text


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_cr_candidate_annex_failure_fails_tdoc(tmp_path: Path) -> None:
    """A failed member named for the TDoc could be the main, so the TDoc fails."""
    service, _scraper, _cache, _cr_repo, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=_cr_zip(("R5s260009_annex.docx", b"not a docx"))
    )
    tdoc_repo.get_by_id.return_value = _tdoc_row()

    with pytest.raises(
        ValueError,
        match="Failed to convert supported DOCX member 'R5s260009_annex.docx'",
    ):
        service.prepare_extract("R5s260009", force=True)


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_cr_zero_text_annex_is_stored_as_empty_document(tmp_path: Path) -> None:
    service, _scraper, _cache, _cr_repo, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=_cr_zip(("b/support-empty.docx", _make_empty_docx()))
    )
    tdoc_repo.get_by_id.return_value = _tdoc_row()

    prepared = service.prepare_extract("R5s260009", force=True)

    assert isinstance(prepared, PreparedTDocExtract)
    empty = next(
        document for document in prepared.annexes if document.source_file == "b/support-empty.docx"
    )
    assert empty.markdown_text == ""
    assert prepared.extract_meta.skipped_sources == ()
    assert is_complete_cr_extraction(prepared.extract_meta, prepared.annexes, has_cover=True) is True


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_cr_zero_text_main_fails_tdoc(tmp_path: Path) -> None:
    service, _scraper, _cache, _cr_repo, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=_zip_of(("R5s260009.docx", _make_empty_docx()))
    )
    tdoc_repo.get_by_id.return_value = _tdoc_row()

    with pytest.raises(
        ValueError,
        match="Failed to convert supported DOCX member 'R5s260009.docx'",
    ):
        service.prepare_extract("R5s260009", force=True)


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_direct_parse_bytes_fails_when_no_main_and_an_annex_is_corrupt() -> None:
    from doc3gpp.parsers.direct_extractor import direct_parse_bytes

    with pytest.raises(
        ValueError,
        match="Failed to convert supported DOCX member 'a/x.docx'",
    ):
        direct_parse_bytes(
            _zip_of(("a/x.docx", b"not a docx")),
            filename="R5s260009.zip",
        )
