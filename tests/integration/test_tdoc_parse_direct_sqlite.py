"""Integration tests for the ``tdoc parse --from-path/--from-url`` local path.

End-to-end coverage of the behaviour matrix against a real SQLite
database. The CLI is exercised via Typer's ``CliRunner``; the
``ScraperClient`` is stubbed at the factory boundary so every URL the
direct path fetches is served from a local fixture. The cache is
rooted under ``tmp_path`` and the schema is bootstrapped via the
``sqlite_env`` + ``create_schema`` pair so the FK constraints on
``tdoc_extracts`` / ``tdoc_cr_cover_page`` are real and exercised.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner
from typing_extensions import Self

from doc3gpp.cli import app
from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_content_families import TDOC_CONTENT_STORES
from doc3gpp.parsers.tdoc_zip import read_markdown_archive
from doc3gpp.scraping.cache import TDocCache
from doc3gpp.services.tdoc_cr_service import PreparedDirectTDoc, TDocCrService
from doc3gpp.services.tdoc_errors import TDocTypeSkippedError
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import (
    TDocCrAnnexOrm,
    TDocDiscussionContentOrm,
    TDocLsContentOrm,
    TDocWidContentOrm,
)
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
from doc3gpp.storage.repositories.tdoc_zip_inventory_sql import (
    SQLAlchemyTDocZipInventoryRepository,
)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tdoc_cr_doc"
CR_STORE = next(store for store in TDOC_CONTENT_STORES if store.key == "cr")


def _docx_available() -> bool:
    try:
        from docx import Document  # noqa: F401
    except ImportError:
        return False
    return True


class _DummyScraperClient:
    """In-memory :class:`ScraperClient` that returns a per-URL canned payload.

    The production ``build_tdoc_cr_service`` instantiates a fresh
    ``ScraperClient()``; this dummy mirrors the same call signature so
    a ``monkeypatch.setattr`` on the factory is a drop-in. Each
    URL is mapped to a separate ``bytes`` payload via the
    ``payloads`` dict, falling back to a default payload.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.payloads: dict[str, bytes] = {}

    def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        if url in self.payloads:
            return self.payloads[url]
        raise RuntimeError(f"No canned payload for {url}")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def close(self) -> None:
        return None


def _build_dummy_zip(docx_bytes: bytes, *, inner_name: str = "R5s260009.docx") -> bytes:
    """Wrap a docx payload in a zip with one ``.docx`` entry."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(inner_name, docx_bytes)
    return buf.getvalue()


def _make_docx(text: str) -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph(text)
    payload = io.BytesIO()
    document.save(payload)
    return payload.getvalue()


def _build_multifile_zip(main_zip: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(main_zip)) as source:
        main_docx = source.read(next(
            name for name in source.namelist() if name.lower().endswith(".docx")
        ))
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("a/support-alpha.docx", _make_docx("support alpha distinctive body"))
        archive.writestr("b/support-beta.docx", _make_docx("support beta distinctive body"))
        archive.writestr("z/R5s260009.docx", main_docx)
    return payload.getvalue()


# ---------------------------------------------------------------------------
# 1. 3GPP URL + tdoc_id ∈ tdocs: full happy path with cache + DB writes.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_direct_3gpp_url_happy_path_writes_cache_and_db(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    create_schema()
    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists(), f"fixture missing: {fixture}"

    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(tmp_path / "cache"))
    from doc3gpp.settings.loader import get_settings
    get_settings.cache_clear()

    SQLAlchemyTDocRepository().upsert(
        TDoc(
            tdoc_id="R5s260009",
            type="CR",
            ftp_url=(
                "tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/"
                "R5s260009.zip"
            ),
        ),
    )

    url = "https://www.3gpp.org/ftp/.../R5s260009.zip"
    dummy = _DummyScraperClient()
    dummy.payloads[url] = _build_multifile_zip(fixture.read_bytes())
    monkeypatch.setattr("doc3gpp.services.factory.ScraperClient", lambda: dummy)

    result = CliRunner().invoke(
        app, ["tdoc", "parse", "--from-url", url]
    )
    assert result.exit_code == 0, result.output

    cr_repo = SQLAlchemyTDocCrRepository()
    details_list = cr_repo.get("R5s260009")
    meta_list = cr_repo.get_extract_meta("R5s260009")
    assert len(details_list) == 1
    assert details_list[0].spec == "38.523-3"
    assert len(meta_list) == 1
    meta = meta_list[0]
    assert meta.extraction_version == 2
    assert meta.document_count == 2
    assert meta.doc_filename == "z/R5s260009.docx"

    documents = SQLAlchemyTDocContentRepository().list_documents(CR_STORE, meta.ftp_url)
    assert [document.source_file for document in documents] == [
        "a/support-alpha.docx",
        "b/support-beta.docx",
    ]
    assert "support alpha distinctive body" in documents[0].markdown_text
    assert "support beta distinctive body" in documents[1].markdown_text

    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    assert (cache.root / "zips" / meta.cache_file).exists()
    markdown_path = cache.root / "markdown" / meta.cache_file
    assert markdown_path.exists()
    archive = read_markdown_archive(markdown_path.read_bytes())
    assert archive.manifest_version == 1
    assert archive.main_source_file == meta.doc_filename
    assert len(archive.documents) == 3
    assert meta.cache_file.endswith(".zip")


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_forced_support_conversion_failure_is_tolerated_and_recorded(
    sqlite_env, tmp_path,
) -> None:
    create_schema()
    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists()
    url = "https://www.3gpp.org/ftp/Docs/R5s260009.zip"
    ftp_url = "Docs/R5s260009.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260009", type="CR", ftp_url=ftp_url)
    )

    with zipfile.ZipFile(io.BytesIO(fixture.read_bytes())) as source:
        main_docx = source.read(next(
            name for name in source.namelist() if name.lower().endswith(".docx")
        ))
    good_payload = _build_multifile_zip(fixture.read_bytes())
    partial_stream = io.BytesIO()
    with zipfile.ZipFile(partial_stream, "w") as archive:
        archive.writestr("a/broken-support.docx", b"not a Word document")
        archive.writestr("b/support-beta.docx", _make_docx("support beta distinctive body"))
        archive.writestr("z/R5s260009.docx", main_docx)

    dummy = _DummyScraperClient()
    dummy.payloads[url] = good_payload
    tdoc_repo = SQLAlchemyTDocRepository()
    cr_repo = SQLAlchemyTDocCrRepository()
    content_repo = SQLAlchemyTDocContentRepository()
    service = TDocCrService(
        cache=TDocCache(root=tmp_path / "cache", size_limit_bytes=0),
        scraper_client=dummy,
        cr_repository=cr_repo,
        cr_ttcn_repository=SQLAlchemyTDocCrTtcnRepository(),
        cr_change_details_repository=SQLAlchemyTDocCrChangeDetailsRepository(),
        tdoc_repository=tdoc_repo,
        content_repo=content_repo,
    )

    prepared = service.prepare_from_url(url, force=True)
    assert isinstance(prepared, PreparedDirectTDoc)
    service.persist_prepared_url(prepared)
    url_key = prepared.extract_meta.ftp_url
    assert [
        document.source_file for document in content_repo.list_documents(CR_STORE, url_key)
    ] == ["a/support-alpha.docx", "b/support-beta.docx"]

    dummy.payloads[url] = partial_stream.getvalue()
    partial = service.prepare_from_url(url, force=True)
    assert isinstance(partial, PreparedDirectTDoc)
    assert partial.extract_meta.skipped_sources == ("a/broken-support.docx",)
    service.persist_prepared_url(partial)

    assert [
        document.source_file for document in content_repo.list_documents(CR_STORE, url_key)
    ] == ["b/support-beta.docx"]
    stored = cr_repo.get_extract_meta_by_url(url_key)
    assert stored is not None
    assert stored.skipped_sources == ("a/broken-support.docx",)



# ---------------------------------------------------------------------------
# 2. 3GPP URL + tdoc_id ∉ tdocs: warning + no cache + no DB.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_direct_3gpp_url_missing_tdoc_warns_and_skips_db(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """3GPP URL with the tdoc_id missing from ``tdocs``: warning + output, no rows."""
    create_schema()

    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists()

    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(tmp_path / "cache"))
    from doc3gpp.settings.loader import get_settings
    get_settings.cache_clear()

    # NB: we do NOT seed a tdoc row for R5s260043, so the FK probe
    # misses. The service must skip cache + DB writes and still emit
    # the parsed record.
    dummy = _DummyScraperClient()
    dummy.payloads[
        "https://www.3gpp.org/ftp/.../R5s260043_MCC160Comments_r1.zip"
    ] = fixture.read_bytes()
    monkeypatch.setattr("doc3gpp.services.factory.ScraperClient", lambda: dummy)

    runner = CliRunner()
    result = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-url", "https://www.3gpp.org/ftp/.../R5s260043_MCC160Comments_r1.zip",
        ],
    )
    # Exit 0 per the plan's D9 — output is still produced.
    assert result.exit_code == 0, result.output

    combined = (result.stdout or "") + (result.stderr or "")
    assert "not present in the 'tdocs' table" in combined
    assert "doc3gpp meeting sync --tsg R5" in combined
    # Parsed record is still on stdout.
    assert "R5s260043" in (result.stdout or "")
    assert "38.523-3" in (result.stdout or "")

    # No rows in either DB table.
    cr_repo = SQLAlchemyTDocCrRepository()
    assert cr_repo.get("R5s260043") == []
    assert cr_repo.get_extract_meta("R5s260043") == []

    # No cache files were written.
    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    snapshot = cache.status()
    assert snapshot.zips == 0
    assert snapshot.markdown == 0


# ---------------------------------------------------------------------------
# 3. 3GPP URL + filename with no tdoc_id pattern: rejected before download.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_direct_3gpp_url_no_pattern_in_filename_is_rejected(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """A 3GPP document URL must have a TDoc-shaped basename."""
    create_schema()

    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists()

    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(tmp_path / "cache"))
    from doc3gpp.settings.loader import get_settings
    get_settings.cache_clear()

    dummy = _DummyScraperClient()
    dummy.payloads["https://www.3gpp.org/ftp/.../meeting_minutes.zip"] = fixture.read_bytes()
    monkeypatch.setattr("doc3gpp.services.factory.ScraperClient", lambda: dummy)

    runner = CliRunner()
    result = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-url", "https://www.3gpp.org/ftp/.../meeting_minutes.zip",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "TDoc" in (result.output or "")
    assert "meeting_minutes.zip" in (result.output or "")

    cr_repo = SQLAlchemyTDocCrRepository()
    # The synthetic LOCAL-* id is what would be used internally; we
    # don't expect a row keyed by that name.
    assert cr_repo.get("LOCAL-meeting_minutes") == []
    assert cr_repo.get_extract_meta("LOCAL-meeting_minutes") == []

    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    snapshot = cache.status()
    assert snapshot.zips == 0
    assert snapshot.markdown == 0


# ---------------------------------------------------------------------------
# 4. Non-3GPP URL: in-memory parse only; no cache, no DB.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_direct_non_3gpp_url_skips_cache_and_db(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """A non-3GPP URL: in-memory parse, no cache writes, no DB writes."""
    create_schema()

    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists()

    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(tmp_path / "cache"))
    from doc3gpp.settings.loader import get_settings
    get_settings.cache_clear()

    dummy = _DummyScraperClient()
    dummy.payloads["https://example.com/R5s260009.zip"] = fixture.read_bytes()
    monkeypatch.setattr("doc3gpp.services.factory.ScraperClient", lambda: dummy)

    runner = CliRunner()
    result = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-url", "https://example.com/R5s260009.zip",
        ],
    )
    assert result.exit_code == 0, result.output

    cr_repo = SQLAlchemyTDocCrRepository()
    assert cr_repo.get("R5s260009") == []
    assert cr_repo.get_extract_meta("R5s260009") == []

    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    snapshot = cache.status()
    assert snapshot.zips == 0
    assert snapshot.markdown == 0

    # Output is still produced.
    assert "R5s260009" in (result.stdout or "")


# ---------------------------------------------------------------------------
# 5. Local file: no cache, no DB, output always.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_direct_from_local_file_emits_all_document_markdown_without_db(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    create_schema()
    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists()
    source = tmp_path / "R5s260009.zip"
    source.write_bytes(_build_multifile_zip(fixture.read_bytes()))

    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(tmp_path / "cache"))
    from doc3gpp.settings.loader import get_settings
    get_settings.cache_clear()

    result = CliRunner().invoke(
        app, ["tdoc", "parse", "--from-path", str(source), "--format", "raw"]
    )
    assert result.exit_code == 0, result.output
    output = result.stdout or ""
    assert "support alpha distinctive body" in output
    assert "support beta distinctive body" in output
    assert "R5s260009" in output
    assert '<!-- doc3gpp-source: "z/R5s260009.docx" -->' in output
    assert '<!-- doc3gpp-source: "a/support-alpha.docx" -->' in output
    assert '<!-- doc3gpp-source: "b/support-beta.docx" -->' in output
    assert output.count("<!-- doc3gpp-source: ") == 3

    cr_repo = SQLAlchemyTDocCrRepository()
    assert cr_repo.get("R5s260009") == []
    assert cr_repo.get_extract_meta("R5s260009") == []
    with get_session_factory()() as session:
        content_rows = [
            session.scalar(
                select(func.count()).select_from(orm).where(orm.tdoc_id == "R5s260009")
            )
            for orm in (
                TDocCrAnnexOrm,
                TDocWidContentOrm,
                TDocLsContentOrm,
                TDocDiscussionContentOrm,
            )
        ]
    assert content_rows == [0, 0, 0, 0]

    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    snapshot = cache.status()
    assert snapshot.zips == 0
    assert snapshot.markdown == 0


# ---------------------------------------------------------------------------
# 6. D10 fix: two revisions of the same tdoc_id land in distinct slots.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_direct_two_revisions_never_collide(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """The D10 fix: r1 and r2 of the same tdoc_id get distinct cache slots."""
    create_schema()

    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists()

    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(tmp_path / "cache"))
    from doc3gpp.settings.loader import get_settings
    get_settings.cache_clear()

    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260008", type="CR"))

    dummy = _DummyScraperClient()
    dummy.payloads[
        "https://www.3gpp.org/ftp/.../R5s260008_MCC160Comments_r1.zip"
    ] = fixture.read_bytes()
    dummy.payloads[
        "https://www.3gpp.org/ftp/.../R5s260008_MCC160Comments_r2.zip"
    ] = fixture.read_bytes()
    monkeypatch.setattr("doc3gpp.services.factory.ScraperClient", lambda: dummy)

    runner = CliRunner()
    result1 = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-url", "https://www.3gpp.org/ftp/.../R5s260008_MCC160Comments_r1.zip",
        ],
    )
    result2 = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-url", "https://www.3gpp.org/ftp/.../R5s260008_MCC160Comments_r2.zip",
        ],
    )
    assert result1.exit_code == 0, result1.output
    assert result2.exit_code == 0, result2.output

    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    snapshot = cache.status()
    # Each source ZIP and its provenance sidecar are counted in the zip subtree.
    assert snapshot.zips == 4
    # Verify the keys are URL-derived (post-T3) — each ftp_url yields a
    # distinct <stem>-<md5>.zip key. The legacy filename is still the
    # leading component of the cached key.
    zip_names = sorted(p.name for p in (tmp_path / "cache" / "zips").glob("*.zip"))
    assert any("R5s260008_MCC160Comments_r1" in name for name in zip_names)
    assert any("R5s260008_MCC160Comments_r2" in name for name in zip_names)
    assert all(name.endswith(".zip") for name in zip_names)

    # Both rows land in the DB keyed by the immutable URL (one per URL).
    cr_repo = SQLAlchemyTDocCrRepository()
    details_list = cr_repo.get("R5s260008")
    assert len(details_list) == 2
    urls = sorted(d.ftp_url for d in details_list)
    assert any("r1.zip" in u for u in urls)
    assert any("r2.zip" in u for u in urls)



def _build_inventory_zip(entries: tuple[tuple[str, bytes], ...]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return payload.getvalue()


def _fixture_main_docx() -> bytes:
    fixture = FIXTURES_DIR / "R5s260009.zip"
    with zipfile.ZipFile(io.BytesIO(fixture.read_bytes())) as archive:
        name = next(item for item in archive.namelist() if item.lower().endswith(".docx"))
        return archive.read(name)


def _build_inventory_service(
    tmp_path: Path,
    scraper: _DummyScraperClient,
) -> tuple[TDocCrService, SQLAlchemyTDocZipInventoryRepository]:
    inventory_repo = SQLAlchemyTDocZipInventoryRepository()
    service = TDocCrService(
        cache=TDocCache(root=tmp_path / "cache", size_limit_bytes=0),
        scraper_client=scraper,
        cr_repository=SQLAlchemyTDocCrRepository(),
        cr_ttcn_repository=SQLAlchemyTDocCrTtcnRepository(),
        cr_change_details_repository=SQLAlchemyTDocCrChangeDetailsRepository(),
        tdoc_repository=SQLAlchemyTDocRepository(),
        content_repo=SQLAlchemyTDocContentRepository(),
        inventory_repository=inventory_repo,
    )
    return service, inventory_repo


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed",
)
def test_known_eligible_3gpp_zip_success_and_parse_failure_both_store_inventory(
    sqlite_env, tmp_path: Path, monkeypatch,
) -> None:
    from doc3gpp.services.tdoc_zip_inventory import PreparedInventoryFailure

    create_schema()
    success_url = "https://www.3gpp.org/ftp/Docs/R5s260009.zip"
    failed_url = "https://www.3gpp.org/ftp/Docs/R5s260009_MCC160Comments_r1.zip"
    success_payload = _build_inventory_zip((
        ("main.docx", _fixture_main_docx()),
        ("support.pdf", b"%PDF-1.7 support attachment"),
    ))
    failed_payload = _build_inventory_zip((("only.pdf", b"%PDF-1.7 no Word documents"),))

    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260009", type="CR", ftp_url="Docs/R5s260009.zip")
    )
    scraper = _DummyScraperClient()
    scraper.payloads[success_url] = success_payload
    scraper.payloads[failed_url] = failed_payload
    service, inventory_repo = _build_inventory_service(tmp_path, scraper)

    success = service.extract_from_url(success_url)
    successful_url_inventory = inventory_repo.get_by_url(success_url)
    assert success.persisted is True
    assert successful_url_inventory is not None
    assert successful_url_inventory.zip_files == ("main.docx", "support.pdf")

    original_errors: list[Exception] = []
    prepared_failures: list[object] = []
    real_parse = service._parse_cr_bundle
    real_prepare = service.prepare_from_url

    def capture_parse(*args, **kwargs):
        try:
            return real_parse(*args, **kwargs)
        except Exception as exc:
            original_errors.append(exc)
            raise

    def capture_prepare(url: str, **kwargs):
        prepared = real_prepare(url, **kwargs)
        if url == failed_url:
            assert inventory_repo.get_by_url(failed_url) is None
            prepared_failures.append(prepared)
        return prepared

    monkeypatch.setattr(service, "_parse_cr_bundle", capture_parse)
    monkeypatch.setattr(service, "prepare_from_url", capture_prepare)
    failed_url_extract = None
    with pytest.raises(Exception) as caught:
        failed_url_extract = service.extract_from_url(failed_url, force=True)
    caught_error = caught.value

    assert failed_url_extract is None
    assert len(prepared_failures) == 1
    assert isinstance(prepared_failures[0], PreparedInventoryFailure)
    assert len(original_errors) == 1
    assert prepared_failures[0].error is original_errors[0]
    assert caught_error is original_errors[0]
    error_type_and_message = (type(caught_error), str(caught_error))
    original_error_type_and_message = (type(original_errors[0]), str(original_errors[0]))
    assert error_type_and_message == original_error_type_and_message

    failed_url_inventory = inventory_repo.get_by_url(failed_url)
    assert failed_url_inventory is not None
    assert failed_url_inventory.zip_files == ("only.pdf",)


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed",
)
def test_direct_docx_does_not_inventory_its_internal_zip(
    sqlite_env, tmp_path: Path,
) -> None:
    create_schema()
    docx_url = "https://www.3gpp.org/ftp/Docs/R5s260009.docx"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260009", type="CR", ftp_url="Docs/R5s260009.zip")
    )
    scraper = _DummyScraperClient()
    scraper.payloads[docx_url] = _fixture_main_docx()
    service, inventory_repo = _build_inventory_service(tmp_path, scraper)

    result = service.extract_from_url(docx_url)

    assert result.persisted is True
    assert inventory_repo.get_by_url(docx_url) is None


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed",
)
def test_local_non_3gpp_unknown_id_and_ineligible_types_do_not_persist(
    sqlite_env, tmp_path: Path,
) -> None:
    create_schema()
    payload = _build_inventory_zip((("main.docx", _fixture_main_docx()),))
    SQLAlchemyTDocRepository().upsert_many([
        TDoc(tdoc_id="R5s260009", type="CR", ftp_url="Docs/R5s260009.zip"),
        TDoc(tdoc_id="R5-123456", type="other", ftp_url="Docs/R5-123456.zip"),
    ])

    non_3gpp_url = "https://example.com/R5s260009.zip"
    unknown_id_url = (
        "https://www.3gpp.org/ftp/Docs/R5s260043_MCC160Comments_r1.zip"
    )
    ineligible_url = "https://www.3gpp.org/ftp/Docs/R5-123456.zip"
    scraper = _DummyScraperClient()
    scraper.payloads[non_3gpp_url] = payload
    scraper.payloads[unknown_id_url] = payload
    service, inventory_repo = _build_inventory_service(tmp_path, scraper)

    local_result = service.extract_from_bytes(payload, "R5s260009.zip")
    non_3gpp_result = service.prepare_from_url(non_3gpp_url)
    unknown_id_result = service.prepare_from_url(unknown_id_url)
    with pytest.raises(TDocTypeSkippedError, match="type 'other' is not parsed"):
        service.prepare_from_url(ineligible_url)

    assert local_result.persisted is False
    assert non_3gpp_result.persisted is False
    assert non_3gpp_result.tdoc_id_in_tdocs is False
    assert unknown_id_result.persisted is False
    assert unknown_id_result.tdoc_id == "R5s260043"
    assert unknown_id_result.tdoc_id_in_tdocs is False
    assert scraper.calls == [non_3gpp_url, unknown_id_url]
    assert all(
        inventory_repo.get_by_url(url) is None
        for url in (
            non_3gpp_url,
            unknown_id_url,
            ineligible_url,
            "R5s260009.zip",
        )
    )


@pytest.mark.parametrize("stored_type", [None, "", " \t "])
@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed",
)
def test_blank_or_missing_stored_type_keeps_direct_parse_but_not_inventory(
    sqlite_env,
    tmp_path: Path,
    stored_type: str | None,
) -> None:
    create_schema()
    tdoc_id = "R5s260009"
    stored_url = "Docs/R5s260009.zip"
    url = f"https://www.3gpp.org/ftp/{stored_url}"
    payload = _build_inventory_zip(
        (
            ("main.docx", _fixture_main_docx()),
            ("support.pdf", b"%PDF-1.7 support attachment"),
        )
    )
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id=tdoc_id, type=stored_type, ftp_url=stored_url)
    )
    scraper = _DummyScraperClient()
    scraper.payloads[url] = payload
    service, inventory_repo = _build_inventory_service(tmp_path, scraper)

    result = service.extract_from_url(url, force=True)

    assert result.persisted is True
    assert result.tdoc_id == tdoc_id
    assert result.tdoc_id_in_tdocs is True
    assert result.details is not None
    assert scraper.calls == [url]
    assert inventory_repo.get_by_url(url) is None
