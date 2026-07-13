"""Unit tests for :mod:`doc3gpp.services.tdoc_cr_service` helpers.

These tests exercise the markdown-cache compression logic and the DB-
cache-hit markdown population in isolation, without touching the SQLite
backend. The repository and scraper boundaries are stubbed with
:class:`unittest.mock.MagicMock`.
"""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
from doc3gpp.parsers.cr_parser import extract_docx_from_zip
from doc3gpp.scraping.cache import TDocCache
from doc3gpp.services.tdoc_cr_service import TDocCrService


FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tdoc_cr_doc"


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
) -> tuple[TDocCrService, MagicMock, TDocCache, MagicMock, MagicMock]:
    """Build a service with a real disk cache and stubbed repos/scraper."""
    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    scraper = MagicMock()
    if zip_bytes is not None:
        scraper.get_bytes.return_value = zip_bytes

    cr_repo = MagicMock()
    cr_repo.get_by_url.return_value = None
    cr_repo.get_extract_meta_by_url.return_value = None
    tdoc_repo = MagicMock()
    tdoc_repo.get_by_id.return_value = None

    service = TDocCrService(
        cache=cache,
        scraper_client=scraper,
        cr_repository=cr_repo,
        tdoc_repository=tdoc_repo,
    )
    return service, scraper, cache, cr_repo, tdoc_repo


def _dummy_details(tdoc_id: str) -> TDocCRDetails:
    return TDocCRDetails(tdoc_id=tdoc_id, spec="38.523-3", cr_num="3790", rev="0")


# ---------------------------------------------------------------------------
# Markdown cache compression / round-trip.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_markdown_cache_writes_gzip_and_hits_without_rerendering(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """On write the markdown cache is gzip-compressed; on read it is decompressed."""
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    zip_bytes = fixture.read_bytes()
    service, _, cache, _, _ = _build_service(tmp_path, zip_bytes=zip_bytes)

    doc_filename, docx_bytes = extract_docx_from_zip(zip_bytes)
    doc_hash = hashlib.sha256(docx_bytes).hexdigest()

    markdown = service._load_or_render_markdown(
        doc_hash=doc_hash,
        docx_bytes=docx_bytes,
        doc_filename=doc_filename,
        force=False,
    )
    assert markdown

    cached_path = cache.path_for(doc_hash, "markdown")
    assert cached_path.exists()
    raw = cached_path.read_bytes()
    assert raw[:2] == b"\x1f\x8b"
    assert gzip.decompress(raw).decode("utf-8") == markdown

    def _boom(*_args: Any, **_kwargs: Any) -> str:  # pragma: no cover
        raise AssertionError("convert_document_to_markdown should not be re-invoked")

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_cr_service.convert_document_to_markdown",
        _boom,
        raising=False,
    )

    second = service._load_or_render_markdown(
        doc_hash=doc_hash,
        docx_bytes=docx_bytes,
        doc_filename=doc_filename,
        force=False,
    )
    assert second == markdown


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_markdown_cache_reads_legacy_plain_utf8(tmp_path: Path) -> None:
    """Pre-change plain UTF-8 markdown cache files continue to work."""
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    zip_bytes = fixture.read_bytes()
    service, _, cache, _, _ = _build_service(tmp_path, zip_bytes=zip_bytes)

    doc_filename, docx_bytes = extract_docx_from_zip(zip_bytes)
    doc_hash = hashlib.sha256(docx_bytes).hexdigest()

    legacy_text = "legacy plain markdown cache content"
    cache.path_for(doc_hash, "markdown").write_text(legacy_text, encoding="utf-8")

    result = service._load_or_render_markdown(
        doc_hash=doc_hash,
        docx_bytes=docx_bytes,
        doc_filename=doc_filename,
        force=False,
    )
    assert result == legacy_text


# ---------------------------------------------------------------------------
# DB cache hit populates markdown for --format raw.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_from_url_db_cache_hit_populates_markdown_for_raw(
    tmp_path: Path,
) -> None:
    """A DB cache hit reads the on-disk markdown so ``--format raw`` is non-empty."""
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    zip_bytes = fixture.read_bytes()
    service, scraper, cache, cr_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=zip_bytes
    )
    tdoc_repo.get_by_id.return_value = TDoc(tdoc_id="R5s260009", type="CR")

    url = "https://www.3gpp.org/ftp/.../R5s260009.zip"
    stored_ftp_url = "tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260009.zip"
    expected_markdown = "# Raw cache hit markdown\nCached body paragraph."

    # Seed the on-disk markdown cache with gzip-compressed UTF-8.
    markdown_key = "raw_cache_test_doc_hash"
    cache.put_bytes(
        markdown_key,
        gzip.compress(expected_markdown.encode("utf-8")),
        "markdown",
    )

    cr_repo.get_by_url.return_value = _dummy_details("R5s260009")
    cr_repo.get_extract_meta_by_url.return_value = TDocExtractMeta(
        ftp_url=stored_ftp_url,
        tdoc_id="R5s260009",
        zip_path=str(cache.path_for("R5s260009.zip", "zips")),
        markdown_path=str(cache.path_for(markdown_key, "markdown")),
        doc_filename="R5s260009.docx",
    )

    result = service.extract_from_url(url, force=False, full=False)

    assert result.from_cache is True
    assert result.persisted is False
    assert result.tdoc_id == "R5s260009"
    assert result.tdoc_id_in_tdocs is True
    assert result.markdown == expected_markdown
    # Network was NOT touched on a DB cache hit.
    assert scraper.get_bytes.call_count == 0
    # The repos were not asked to upsert anything.
    assert cr_repo.upsert.call_count == 0
