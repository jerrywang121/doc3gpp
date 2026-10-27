"""Direct-mode family and CR routing with SQLite persistence rules (spec section 6, P7)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_content_families import TDOC_CONTENT_STORES
from doc3gpp.parsers.normalizers import normalize_ftp_path
from doc3gpp.parsers.tdoc_zip import read_markdown_archive
from doc3gpp.scraping.cache import TDocCache
from doc3gpp.services.tdoc_cr_service import TDocCrService
from doc3gpp.services.tdoc_errors import TDocTypeSkippedError
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import TDocExtractOrm
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
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tdoc_cr_doc"
STORES = {store.key: store for store in TDOC_CONTENT_STORES}
WID_MARKER = "3GPP™ Work Item Description"
FAMILY_URL = "https://www.3gpp.org/ftp/tsg_ran/wg5/R5-123456.zip"
FAMILY_FTP_URL = "tsg_ran/wg5/R5-123456.zip"
CR_URL = "https://www.3gpp.org/ftp/tsg_ran/wg5/R5s260009.zip"
CR_FTP_URL = "tsg_ran/wg5/R5s260009.zip"


def _docx_available() -> bool:
    try:
        from docx import Document  # noqa: F401
    except ImportError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)


def _make_docx(*paragraphs: str) -> bytes:
    from docx import Document

    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    payload = io.BytesIO()
    document.save(payload)
    return payload.getvalue()


def _make_zip(files: dict[str, bytes]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return payload.getvalue()


def _wid_zip() -> bytes:
    return _make_zip({
        "R5-123456.docx": _make_docx(WID_MARKER, "Title: Work item on widgets"),
        "annex/info.docx": _make_docx("annex body about gadgets"),
    })


class _Scraper:
    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {}
        self.calls: list[str] = []

    def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        return self.payloads[url]


class _Offline:
    """Fails the test if the direct path reaches the network."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        raise AssertionError(f"network used for {url}")


def _service(tmp_path: Path, scraper: _Scraper) -> TDocCrService:
    return TDocCrService(
        cache=TDocCache(root=tmp_path / "cache", size_limit_bytes=0),
        scraper_client=scraper,
        cr_repository=SQLAlchemyTDocCrRepository(),
        cr_ttcn_repository=SQLAlchemyTDocCrTtcnRepository(),
        cr_change_details_repository=SQLAlchemyTDocCrChangeDetailsRepository(),
        tdoc_repository=SQLAlchemyTDocRepository(),
        content_repo=SQLAlchemyTDocContentRepository(),
    )


def _extract_row_count() -> int:
    with get_session_factory()() as session:
        return session.scalar(select(func.count()).select_from(TDocExtractOrm)) or 0


def _seed_tdoc(tdoc_id: str, tdoc_type: str | None, ftp_url: str | None) -> None:
    create_schema()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id=tdoc_id, type=tdoc_type, ftp_url=ftp_url))


def test_from_path_family_is_not_persisted(sqlite_env, tmp_path: Path) -> None:
    _seed_tdoc("R5-123456", "WID revised", FAMILY_FTP_URL)
    service = _service(tmp_path, _Scraper())

    result = service.extract_from_bytes(_wid_zip(), "R5-123456.zip")

    assert result.persisted is False
    assert result.content_store is not None
    assert result.content_store.key == "wid"
    assert result.main_source_file == "R5-123456.docx"
    assert [document.source_file for document in result.documents] == [
        "R5-123456.docx",
        "annex/info.docx",
    ]
    assert _extract_row_count() == 0
    assert SQLAlchemyTDocContentRepository().list_documents(
        STORES["wid"], normalize_ftp_path(FAMILY_FTP_URL),
    ) == ()


def test_from_path_sniffs_family_when_tdoc_has_no_row(sqlite_env, tmp_path: Path) -> None:
    create_schema()
    service = _service(tmp_path, _Scraper())

    result = service.extract_from_bytes(_wid_zip(), "R5-123456.zip")

    assert result.persisted is False
    assert result.content_store is not None
    assert result.content_store.key == "wid"
    assert result.tdoc_id == "R5-123456"
    assert _extract_row_count() == 0


def test_from_path_unknown_type_is_skipped(sqlite_env, tmp_path: Path) -> None:
    create_schema()
    plain = _make_zip({"R5-123456.docx": _make_docx("plain text without markers")})
    service = _service(tmp_path, _Scraper())

    with pytest.raises(TDocTypeSkippedError, match="type could not be determined"):
        service.extract_from_bytes(plain, "R5-123456.zip")


def test_from_path_known_unparsed_type_is_skipped(sqlite_env, tmp_path: Path) -> None:
    _seed_tdoc("R5-123456", "other", FAMILY_FTP_URL)
    service = _service(tmp_path, _Scraper())

    with pytest.raises(TDocTypeSkippedError, match="type 'other' is not parsed"):
        service.extract_from_bytes(_wid_zip(), "R5-123456.zip")


def test_from_url_family_with_row_persists(sqlite_env, tmp_path: Path) -> None:
    _seed_tdoc("R5-123456", "WID revised", FAMILY_FTP_URL)
    scraper = _Scraper()
    scraper.payloads[FAMILY_URL] = _wid_zip()
    service = _service(tmp_path, scraper)

    result = service.extract_from_url(FAMILY_URL)

    canonical = normalize_ftp_path(FAMILY_URL)
    assert result.persisted is True
    assert result.from_cache is False
    assert result.content_store is not None
    assert result.content_store.key == "wid"
    assert result.tdoc_id_in_tdocs is True
    stored = SQLAlchemyTDocContentRepository().list_documents(STORES["wid"], canonical)
    assert [document.source_file for document in stored] == [
        "R5-123456.docx",
        "annex/info.docx",
    ]
    assert _extract_row_count() == 1


def test_from_url_family_second_run_is_cache_hit(sqlite_env, tmp_path: Path) -> None:
    _seed_tdoc("R5-123456", "WID revised", FAMILY_FTP_URL)
    scraper = _Scraper()
    scraper.payloads[FAMILY_URL] = _wid_zip()
    service = _service(tmp_path, scraper)
    service.extract_from_url(FAMILY_URL)

    result = service.extract_from_url(FAMILY_URL)

    assert result.from_cache is True
    assert result.persisted is False
    assert len(scraper.calls) == 1


def test_from_url_family_without_row_stays_in_memory(sqlite_env, tmp_path: Path) -> None:
    create_schema()
    scraper = _Scraper()
    scraper.payloads[FAMILY_URL] = _wid_zip()
    service = _service(tmp_path, scraper)

    result = service.extract_from_url(FAMILY_URL)

    assert result.persisted is False
    assert result.tdoc_id == "R5-123456"
    assert result.tdoc_id_in_tdocs is False
    assert result.content_store is not None
    assert result.content_store.key == "wid"
    assert _extract_row_count() == 0


def test_from_url_sniffed_family_with_row_persists(sqlite_env, tmp_path: Path) -> None:
    _seed_tdoc("R5-123456", None, FAMILY_FTP_URL)
    scraper = _Scraper()
    scraper.payloads[FAMILY_URL] = _wid_zip()
    service = _service(tmp_path, scraper)

    result = service.extract_from_url(FAMILY_URL)

    assert result.persisted is True
    assert result.content_store is not None
    assert result.content_store.key == "wid"
    assert _extract_row_count() == 1


def _cr_zip() -> bytes:
    main = (FIXTURES_DIR / "R5s260009.zip").read_bytes()
    with zipfile.ZipFile(io.BytesIO(main)) as source:
        main_docx = source.read(source.namelist()[0])
    return _make_zip({
        "R5s260009.docx": main_docx,
        "annex/info.docx": _make_docx("annex body about rates"),
    })


def test_from_url_cr_persists_annexes_only(sqlite_env, tmp_path: Path) -> None:
    _seed_tdoc("R5s260009", "CR", CR_FTP_URL)
    scraper = _Scraper()
    scraper.payloads[CR_URL] = _cr_zip()
    service = _service(tmp_path, scraper)

    result = service.extract_from_url(CR_URL)

    canonical = normalize_ftp_path(CR_URL)
    assert result.persisted is True
    assert result.tdoc_id == "R5s260009"
    annexes = SQLAlchemyTDocContentRepository().list_documents(STORES["cr"], canonical)
    assert [document.source_file for document in annexes] == ["annex/info.docx"]
    meta = SQLAlchemyTDocContentRepository().get_extract_meta(canonical)
    assert meta is not None
    assert meta.doc_filename == "R5s260009.docx"
    assert meta.document_count == 1
    archive = read_markdown_archive(
        (tmp_path / "cache" / "markdown" / meta.cache_file).read_bytes()
    )
    assert sorted(document.source_file for document in archive.documents) == [
        "R5s260009.docx",
        "annex/info.docx",
    ]


def _auto_sync_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "false")
    get_settings.cache_clear()


def _zip_with_bad_annex() -> bytes:
    return _make_zip({
        "R5-123456.docx": _make_docx(WID_MARKER, "Title: Work item on widgets"),
        "annex/bad.docx": b"not a word document",
    })


def test_from_url_null_type_complete_family_cache_hit_makes_no_download(
    sqlite_env, tmp_path: Path,
) -> None:
    _seed_tdoc("R5-123456", "WID revised", FAMILY_FTP_URL)
    scraper = _Scraper()
    scraper.payloads[FAMILY_URL] = _wid_zip()
    _service(tmp_path, scraper).extract_from_url(FAMILY_URL)
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-123456", type=None, ftp_url=FAMILY_FTP_URL),
    )
    offline = _Offline()

    result = _service(tmp_path, offline).extract_from_url(FAMILY_URL)

    assert offline.calls == []
    assert result.from_cache is True
    assert result.persisted is False
    assert result.content_store is not None
    assert result.content_store.key == "wid"


def test_from_url_null_type_complete_cr_cache_hit_makes_no_download(
    sqlite_env, tmp_path: Path,
) -> None:
    _seed_tdoc("R5s260009", "CR", CR_FTP_URL)
    scraper = _Scraper()
    scraper.payloads[CR_URL] = _cr_zip()
    _service(tmp_path, scraper).extract_from_url(CR_URL)
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260009", type=None, ftp_url=CR_FTP_URL),
    )
    offline = _Offline()

    result = _service(tmp_path, offline).extract_from_url(CR_URL)

    assert offline.calls == []
    assert result.from_cache is True
    assert result.tdoc_id == "R5s260009"


def test_from_url_known_unparsed_type_is_never_downloaded(
    sqlite_env, tmp_path: Path,
) -> None:
    _seed_tdoc("R5-123456", "other", FAMILY_FTP_URL)
    offline = _Offline()

    with pytest.raises(TDocTypeSkippedError, match="type 'other' is not parsed"):
        _service(tmp_path, offline).extract_from_url(FAMILY_URL)

    assert offline.calls == []


def test_cli_from_url_skipped_type_prints_skipped_and_exits_zero(
    sqlite_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _auto_sync_off(monkeypatch)
    _seed_tdoc("R5-123456", "other", FAMILY_FTP_URL)
    offline = _Offline()
    monkeypatch.setattr("doc3gpp.services.factory.ScraperClient", lambda: offline)

    result = CliRunner().invoke(app, ["tdoc", "parse", "--from-url", FAMILY_URL])

    assert result.exit_code == 0, result.output
    assert "SKIPPED - type 'other' is not parsed" in result.output
    assert offline.calls == []


def test_cli_from_path_family_prints_tdoc_parse_summary_line(
    sqlite_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    create_schema()
    _auto_sync_off(monkeypatch)
    source = tmp_path / "R5-123456.zip"
    source.write_bytes(_wid_zip())

    result = CliRunner().invoke(app, ["tdoc", "parse", "--from-path", str(source)])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "TDoc R5-123456: wid, 2 documents, extraction_version 2",
    ]


def test_cli_from_path_family_prints_warning_for_skipped_annex(
    sqlite_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    create_schema()
    _auto_sync_off(monkeypatch)
    source = tmp_path / "R5-123456.zip"
    source.write_bytes(_zip_with_bad_annex())

    result = CliRunner().invoke(app, ["tdoc", "parse", "--from-path", str(source)])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "TDoc R5-123456: wid, 1 documents, extraction_version 2",
        "warning: skipped annex/bad.docx",
    ]
