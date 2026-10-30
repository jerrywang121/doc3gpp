from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_zip_inventory import TDocZipInventory
from doc3gpp.parsers.normalizers import build_ftp_url, normalize_ftp_path
from doc3gpp.parsers.tdoc_zip import document_id_for
from doc3gpp.scraping.cache import TDocCache
from doc3gpp.scraping.tdoc_zip_source import get_tdoc_zip_url
from doc3gpp.services.pipeline_runtime import PipelineRuntime
from doc3gpp.services.tdoc_cr_service import ExtractResult
from doc3gpp.services.tdoc_family_service import FamilyExtractResult
from doc3gpp.services.tdoc_pipeline import PipelineItem, TDocPipeline
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import TDocZipInventoryOrm
from doc3gpp.storage.db.session import get_session_factory
from doc3gpp.storage.repositories.tdoc_content_sql import (
    SQLAlchemyTDocContentRepository,
)
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
from doc3gpp.storage.repositories.tdoc_zip_inventory_sql import (
    SQLAlchemyTDocZipInventoryRepository,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tdoc_cr_doc" / "R5s260009.zip"


class _FakeScraper:
    def __init__(
        self,
        *,
        payload: bytes | None = None,
        payloads: dict[str, bytes] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.payload = payload
        self.payloads = payloads or {}
        self.errors = errors or {}
        self.calls: list[str] = []

    def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        if url in self.errors:
            raise self.errors[url]
        if url in self.payloads:
            return self.payloads[url]
        if self.payload is not None:
            return self.payload
        raise AssertionError(f"unexpected download URL: {url}")


def _archive(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return buffer.getvalue()


def _sample_docx() -> bytes:
    with zipfile.ZipFile(FIXTURE) as archive:
        return archive.read("R5s260009.docx")


def _ftp_path(tdoc_id: str) -> str:
    return (
        "tsg_ran/wg5_test_ex-t1/ttcn/ttcn_crs/2026/docs/"
        f"{tdoc_id.lower()}.zip"
    )


def _seed_tdoc(tdoc_id: str, tdoc_type: str, ftp_url: str | None = None) -> str:
    path = ftp_url or _ftp_path(tdoc_id)
    SQLAlchemyTDocRepository().upsert_many(
        [TDoc(tdoc_id=tdoc_id, type=tdoc_type, ftp_url=path)]
    )
    return path


def _build_service(monkeypatch, tmp_path: Path, scraper: _FakeScraper, *, max_documents: int = 100):
    from doc3gpp.services import factory

    create_schema("main")
    cache = TDocCache(tmp_path / "cache", size_limit_bytes=0)
    monkeypatch.setattr(factory, "TDocCache", lambda **_kwargs: cache)
    monkeypatch.setattr(factory, "ScraperClient", lambda: scraper)
    monkeypatch.setattr(factory, "build_search_service", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        factory,
        "build_semantic_search_service",
        lambda *_args, **_kwargs: None,
    )
    service = factory.build_tdoc_cr_service()
    service._max_documents_per_zip = max_documents
    return service, cache, SQLAlchemyTDocZipInventoryRepository()


def _run_pipeline(service, tdoc_id: str):
    runtime = PipelineRuntime(prepare_workers=1)
    runner = TDocPipeline(runtime)
    try:
        return runner.run_sync(
            [PipelineItem(key=tdoc_id, value=tdoc_id)],
            service.prepare_extract,
            persist=service.persist_prepared_extract,
            is_complete=lambda value: isinstance(
                value, (ExtractResult, FamilyExtractResult)
            ),
        )[0]
    finally:
        runtime.shutdown()


def test_supported_cr_and_discussion_record_all_zip_entries(
    sqlite_env, monkeypatch, tmp_path: Path
) -> None:
    docx = _sample_docx()
    cr_path = _ftp_path("R5s260009")
    discussion_path = _ftp_path("R5s260010")
    cr_payload = _archive([("main.docx", docx), ("support.pdf", b"not converted")])
    discussion_payload = _archive(
        [("R5s260010.docx", docx), ("support.pdf", b"not converted")]
    )
    scraper = _FakeScraper(
        payloads={
            build_ftp_url(cr_path): cr_payload,
            build_ftp_url(discussion_path): discussion_payload,
        }
    )
    service, _cache, inventory_repo = _build_service(monkeypatch, tmp_path, scraper)
    _seed_tdoc("R5s260009", "CR", cr_path)
    _seed_tdoc("R5s260010", "discussion", discussion_path)

    cr_extract = service.extract("R5s260009", force=True)
    discussion_extract = service.extract("R5s260010", force=True)

    cr_inventory = inventory_repo.get_by_url(cr_path)
    discussion_inventory = inventory_repo.get_by_url(discussion_path)
    assert cr_inventory is not None
    assert discussion_inventory is not None
    assert cr_inventory.zip_files == ("main.docx", "support.pdf")
    assert discussion_inventory.zip_files == ("R5s260010.docx", "support.pdf")
    assert cr_extract is not None and discussion_extract is not None


def test_pdf_only_failed_parse_records_inventory_without_extract(
    sqlite_env, monkeypatch, tmp_path: Path
) -> None:
    payload = _archive([("only.pdf", b"not a Word document")])
    service, _cache, inventory_repo = _build_service(
        monkeypatch, tmp_path, _FakeScraper(payload=payload)
    )
    ftp_path = _seed_tdoc("R5s260011", "CR")

    outcome = _run_pipeline(service, "R5s260011")

    inventory = inventory_repo.get_by_url(ftp_path)
    extract = SQLAlchemyTDocContentRepository().get_extract_meta(
        normalize_ftp_path(ftp_path)
    )
    assert inventory is not None and inventory.zip_files == ("only.pdf",)
    assert extract is None
    assert outcome.status == "failed"
    assert "CRHeaderMissingError" in (outcome.reason or "")


def test_limit_skip_records_inventory_without_changing_skip_reason(
    sqlite_env, monkeypatch, tmp_path: Path
) -> None:
    docx = _sample_docx()
    payload = _archive([("one.docx", docx), ("two.docx", docx)])
    service, _cache, inventory_repo = _build_service(
        monkeypatch,
        tmp_path,
        _FakeScraper(payload=payload),
        max_documents=1,
    )
    ftp_path = _seed_tdoc("R5s260012", "CR")

    outcome = _run_pipeline(service, "R5s260012")

    inventory = inventory_repo.get_by_url(ftp_path)
    assert inventory is not None
    assert inventory.zip_files == ("one.docx", "two.docx")
    assert outcome.status == "skipped"
    assert "TDocZipLimitError" in (outcome.reason or "")


def test_validation_failure_records_duplicate_and_unsafe_names(
    sqlite_env, monkeypatch, tmp_path: Path
) -> None:
    with pytest.warns(UserWarning, match="Duplicate name"):
        payload = _archive(
            [
                ("duplicate.docx", b"first"),
                ("duplicate.docx", b"second"),
                ("../unsafe.pdf", b"unsafe path is metadata only"),
            ]
        )
    service, _cache, inventory_repo = _build_service(
        monkeypatch, tmp_path, _FakeScraper(payload=payload)
    )
    ftp_path = _seed_tdoc("R5s260013", "CR")

    outcome = _run_pipeline(service, "R5s260013")

    inventory = inventory_repo.get_by_url(ftp_path)
    assert inventory is not None
    assert inventory.zip_files == (
        "duplicate.docx",
        "duplicate.docx",
        "../unsafe.pdf",
    )
    assert outcome.status == "failed"
    assert "TDocZipValidationError" in (outcome.reason or "")


@pytest.mark.parametrize("source", ["unreadable", "missing"])
def test_unreadable_or_missing_zip_keeps_previous_inventory(
    source: str, sqlite_env, monkeypatch, tmp_path: Path
) -> None:
    create_schema("main")
    ftp_path = _seed_tdoc("R5s260014", "CR")
    scraper = _FakeScraper(
        payload=b"not a ZIP archive" if source == "unreadable" else None,
        errors=(
            {build_ftp_url(ftp_path): httpx.ConnectError("offline")}
            if source == "missing"
            else {}
        ),
    )
    service, _cache, inventory_repo = _build_service(
        monkeypatch, tmp_path, scraper
    )
    previous = TDocZipInventory(
        ftp_url=ftp_path,
        tdoc_id="R5s260014",
        zip_files=("previous.docx",),
    )
    inventory_repo.upsert(previous)

    outcome = _run_pipeline(service, "R5s260014")

    assert inventory_repo.get_by_url(ftp_path) == previous
    assert outcome.status == "failed"
    if source == "unreadable":
        assert "BadZipFile" in (outcome.reason or "")
    else:
        assert "TDocZipDownloadError" in (outcome.reason or "")


def test_complete_cached_parse_does_not_backfill_but_force_uses_verified_cached_zip(
    sqlite_env, monkeypatch, tmp_path: Path
) -> None:
    payload = _archive([("main.docx", _sample_docx()), ("support.pdf", b"support")])
    scraper = _FakeScraper(payload=payload)
    service, _cache, inventory_repo = _build_service(monkeypatch, tmp_path, scraper)
    ftp_path = _seed_tdoc("R5s260015", "CR")
    canonical_url = normalize_ftp_path(ftp_path)

    first = service.extract("R5s260015", force=True)
    assert first is not None
    with get_session_factory()() as session:
        row = session.get(TDocZipInventoryOrm, canonical_url)
        assert row is not None
        session.delete(row)
        session.commit()

    cached = service.extract("R5s260015")

    assert cached.from_cache is True
    assert inventory_repo.get_by_url(ftp_path) is None
    assert len(scraper.calls) == 1

    forced = service.extract("R5s260015", force=True)

    assert forced.from_cache is False
    assert len(scraper.calls) == 1
    inventory = inventory_repo.get_by_url(ftp_path)
    assert inventory is not None
    assert inventory.zip_files == ("main.docx", "support.pdf")


def test_resolved_fallback_url_owns_inventory_for_cr_and_family(
    sqlite_env, monkeypatch, tmp_path: Path
) -> None:
    docx = _sample_docx()
    cr_id = "R5s260016"
    family_id = "R5s260017"
    cr_payload = _archive([("main.docx", docx), ("support.pdf", b"support")])
    family_payload = _archive(
        [(f"{family_id}.docx", docx), ("support.pdf", b"support")]
    )
    cr_path = _ftp_path(cr_id).replace("/docs/", "/docs/review/")
    family_path = _ftp_path(family_id).replace("/docs/", "/docs/review/")
    cr_fallback = get_tdoc_zip_url(cr_id)
    family_fallback = get_tdoc_zip_url(family_id)
    assert cr_fallback is not None and family_fallback is not None
    scraper = _FakeScraper(
        payloads={cr_fallback: cr_payload, family_fallback: family_payload},
        errors={
            build_ftp_url(cr_path): httpx.ConnectError("primary is unavailable"),
            build_ftp_url(family_path): httpx.ConnectError("primary is unavailable"),
        },
    )
    service, _cache, inventory_repo = _build_service(monkeypatch, tmp_path, scraper)
    _seed_tdoc(cr_id, "CR", cr_path)
    _seed_tdoc(family_id, "discussion", family_path)

    cr_prepared = service.prepare_extract(cr_id, force=True)
    family_prepared = service.prepare_extract(family_id, force=True)

    assert cr_prepared.extract_meta.ftp_url == normalize_ftp_path(cr_fallback)
    assert cr_prepared.documents[0].document_id == document_id_for(
        cr_fallback, cr_prepared.documents[0].source_file
    )
    assert family_prepared.extract_meta.ftp_url == normalize_ftp_path(family_fallback)
    assert family_prepared.documents[0].document_id == document_id_for(
        family_fallback, family_prepared.documents[0].source_file
    )
    assert inventory_repo.get_by_url(cr_fallback) is None
    assert inventory_repo.get_by_url(family_fallback) is None

    service.persist_prepared_extract(cr_prepared)
    service.persist_prepared_extract(family_prepared)

    cr_inventory = inventory_repo.get_by_url(cr_fallback)
    family_inventory = inventory_repo.get_by_url(family_fallback)
    assert cr_inventory is not None and family_inventory is not None
    assert cr_inventory.zip_files == ("main.docx", "support.pdf")
    assert family_inventory.zip_files == (f"{family_id}.docx", "support.pdf")
    assert cr_inventory.ftp_url == normalize_ftp_path(cr_fallback)
    assert family_inventory.ftp_url == normalize_ftp_path(family_fallback)


def test_sync_extract_persists_inventory_before_reraising_original_error(
    sqlite_env, monkeypatch, tmp_path: Path
) -> None:
    payload = _archive([("main.docx", _sample_docx()), ("support.pdf", b"support")])
    service, _cache, inventory_repo = _build_service(
        monkeypatch, tmp_path, _FakeScraper(payload=payload)
    )
    ftp_path = _seed_tdoc("R5s260018", "CR")
    original = RuntimeError("original parser failure")

    def fail_parse(*_args, **_kwargs):
        raise original

    monkeypatch.setattr(service, "_parse_bundle", fail_parse)
    with pytest.raises(RuntimeError, match="original parser failure") as exc_info:
        service.extract("R5s260018", force=True)

    assert exc_info.value is original
    inventory = inventory_repo.get_by_url(ftp_path)
    assert inventory is not None
    assert inventory.zip_files == ("main.docx", "support.pdf")


def test_inventory_write_failure_does_not_mask_original_parse_error(
    sqlite_env, monkeypatch, tmp_path: Path
) -> None:
    payload = _archive([("main.docx", _sample_docx())])
    service, _cache, _inventory_repo = _build_service(
        monkeypatch, tmp_path, _FakeScraper(payload=payload)
    )
    _seed_tdoc("R5s260019", "CR")
    original = RuntimeError("original parser failure")

    def fail_parse(*_args, **_kwargs):
        raise original

    def fail_inventory_write(_inventory):
        raise OSError("inventory database unavailable")

    monkeypatch.setattr(service, "_parse_bundle", fail_parse)
    monkeypatch.setattr(service._inventory_repo, "upsert", fail_inventory_write)
    with pytest.raises(RuntimeError, match="original parser failure") as exc_info:
        service.extract("R5s260019", force=True)

    assert exc_info.value is original



def test_failed_parse_inventory_does_not_set_parsed_or_extracted_at(
    sqlite_env,
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A failed parse may store names but cannot mark content extracted."""
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    payload = _archive([("main.docx", _sample_docx()), ("support.pdf", b"support")])
    service, _cache, inventory_repo = _build_service(
        monkeypatch, tmp_path, _FakeScraper(payload=payload)
    )
    tdoc_id = "R5s260020"
    ftp_path = _seed_tdoc(tdoc_id, "CR")
    original = RuntimeError("original parser failure")

    def fail_parse(*_args, **_kwargs):
        raise original

    monkeypatch.setattr(service, "_parse_bundle", fail_parse)
    with pytest.raises(RuntimeError, match="original parser failure") as exc_info:
        service.extract(tdoc_id, force=True)

    assert exc_info.value is original
    assert inventory_repo.get_by_url(ftp_path) is not None
    stored_tdoc = SQLAlchemyTDocRepository().get_by_id(tdoc_id)
    assert stored_tdoc is not None
    assert stored_tdoc.parsed is False
    assert SQLAlchemyTDocCrRepository().get_extract_meta_by_url(ftp_path) is None
