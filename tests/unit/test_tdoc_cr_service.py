"""Unit tests for :mod:`doc3gpp.services.tdoc_cr_service` helpers.

These tests exercise the markdown-cache compression logic and the DB-
cache-hit markdown population in isolation, without touching the SQLite
backend. The repository and scraper boundaries are stubbed with
:class:`unittest.mock.MagicMock`.
"""

from __future__ import annotations

import gzip
import io
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_cr import (
    TDocCRDetails,
    TDocCRParseResult,
    TDocCRTTCNDetails,
    TDocExtractMeta,
)
from doc3gpp.parsers.cr_parser import extract_docx_from_zip
from doc3gpp.scraping.cache import TDocCache
from doc3gpp.services.tdoc_cr_service import (
    TDocCrService,
    _wrap_markdown_zip,
)


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
    cr_repo.get_extract_meta_by_url.return_value = None
    cr_ttcn_repo = MagicMock()
    cr_ttcn_repo.get_by_url.return_value = None
    tdoc_repo = MagicMock()
    tdoc_repo.get_by_id.return_value = None

    service = TDocCrService(
        cache=cache,
        scraper_client=scraper,
        cr_repository=cr_repo,
        cr_ttcn_repository=cr_ttcn_repo,
        tdoc_repository=tdoc_repo,
    )
    return service, scraper, cache, cr_repo, cr_ttcn_repo, tdoc_repo


def _dummy_details(tdoc_id: str) -> TDocCRDetails:
    return TDocCRDetails(tdoc_id=tdoc_id, spec="38.523-3", cr_num="3790", rev="0")


# ---------------------------------------------------------------------------
# Markdown cache compression / round-trip.
# ---------------------------------------------------------------------------


def _zip_markdown_bytes(text: str, *, inner_name: str) -> bytes:
    """Helper: wrap ``text`` in a real ZIP archive (mirrors ``_wrap_markdown_zip``).

    Tests seed the on-disk cache directly via ``cache.put_bytes`` to
    exercise read paths without re-running the docx-to-markdown render.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(inner_name, text.encode("utf-8"))
    return buf.getvalue()


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_markdown_cache_writes_real_zip_and_hits_without_rerendering(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """On write the markdown cache is a real ``zipfile.ZipFile``; on read it is unpacked.

    The ``PK\x03\x04`` magic is the post-this-change format — the cache
    file extends with ``.zip`` and is byte-for-byte the same shape a
    ``zipfile.ZipFile(..., "w")`` call would produce, so ``unzip`` /
    7z / WinZip can open it directly off disk.
    """
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    zip_bytes = fixture.read_bytes()
    service, _, cache, _, _, _ = _build_service(tmp_path, zip_bytes=zip_bytes)

    doc_filename, docx_bytes = extract_docx_from_zip(zip_bytes)
    cache_file = "R5s260009-abcdef0123456789.zip"

    markdown = service._load_or_render_markdown(
        cache_file=cache_file,
        docx_bytes=docx_bytes,
        doc_filename=doc_filename,
        force=False,
    )
    assert markdown

    cached_path = cache.path_for(cache_file, "markdown")
    assert cached_path.exists()
    raw = cached_path.read_bytes()
    assert raw[:4] == b"PK\x03\x04", (
        "markdown cache must be a real ZIP — got magic "
        f"{raw[:4]!r}, expected b'PK\\x03\\x04'"
    )
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()
        assert len(names) == 1
        assert names[0].endswith(".md")
        assert zf.read(names[0]).decode("utf-8") == markdown

    def _boom(*_args: Any, **_kwargs: Any) -> str:  # pragma: no cover
        raise AssertionError("convert_document_to_markdown should not be re-invoked")

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_cr_service.convert_document_to_markdown",
        _boom,
        raising=False,
    )

    second = service._load_or_render_markdown(
        cache_file=cache_file,
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
    """Pre-change plain UTF-8 markdown cache files continue to work.

    The ``_decompress_markdown`` reader magic-byte-sniffs for plain UTF-8
    (no leading ``PK`` / ``\\x1f\\x8b``), so legacy unzipped cache files
    remain readable after the ZIP-wrapping change.
    """
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    zip_bytes = fixture.read_bytes()
    service, _, cache, _, _, _ = _build_service(tmp_path, zip_bytes=zip_bytes)

    doc_filename, docx_bytes = extract_docx_from_zip(zip_bytes)
    cache_file = "R5s260009-abcdef0123456789.zip"

    legacy_text = "legacy plain markdown cache content"
    cache.path_for(cache_file, "markdown").write_text(legacy_text, encoding="utf-8")

    result = service._load_or_render_markdown(
        cache_file=cache_file,
        docx_bytes=docx_bytes,
        doc_filename=doc_filename,
        force=False,
    )
    assert result == legacy_text


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_markdown_cache_reads_legacy_gzip_blob(tmp_path: Path) -> None:
    """Pre-this-change gzip-compressed cache files remain readable.

    The pre-change writer produced a gzip-compressed blob with ``.zip``
    extension; the reader's magic-byte sniff falls through to ``gzip.decompress``
    when ``PK`` is not the leading two bytes, so a cache file written by
    the previous code path still decodes cleanly.
    """
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    zip_bytes = fixture.read_bytes()
    service, _, cache, _, _, _ = _build_service(tmp_path, zip_bytes=zip_bytes)

    doc_filename, docx_bytes = extract_docx_from_zip(zip_bytes)
    cache_file = "R5s260009-abcdef0123456789.zip"

    legacy_text = "legacy gzip-blob markdown cache content"
    cache.put_bytes(
        cache_file,
        gzip.compress(legacy_text.encode("utf-8")),
        "markdown",
    )

    result = service._load_or_render_markdown(
        cache_file=cache_file,
        docx_bytes=docx_bytes,
        doc_filename=doc_filename,
        force=False,
    )
    assert result == legacy_text


def test_wrap_markdown_zip_is_a_real_zip(tmp_path: Path) -> None:
    """``_wrap_markdown_zip`` output passes ``zipfile.ZipFile``'s read path.

    Belt-and-braces unit test that does not require a docx fixture —
    locks the public contract independently of ``_load_or_render_markdown``.
    """
    payload = "# hello\nbody\n"
    archive = _wrap_markdown_zip(payload, inner_name="R5s260009.md")

    assert archive[:4] == b"PK\x03\x04"
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        assert zf.namelist() == ["R5s260009.md"]
        assert zf.read("R5s260009.md").decode("utf-8") == payload


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
    service, scraper, cache, cr_repo, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=zip_bytes
    )
    tdoc_repo.get_by_id.return_value = TDoc(tdoc_id="R5s260009", type="CR")

    url = "https://www.3gpp.org/ftp/.../R5s260009.zip"
    stored_ftp_url = "tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260009.zip"
    expected_markdown = "# Raw cache hit markdown\nCached body paragraph."

    cache_file = "R5s260009-abcdef0123456789.zip"
    cache.put_bytes(
        cache_file,
        _zip_markdown_bytes(expected_markdown, inner_name="R5s260009.md"),
        "markdown",
    )

    cr_repo.get_by_url.return_value = _dummy_details("R5s260009")
    cr_repo.get_extract_meta_by_url.return_value = TDocExtractMeta(
        ftp_url=stored_ftp_url,
        tdoc_id="R5s260009",
        cache_file=cache_file,
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
        self.calls: list[tuple[str, bool]] = []

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
        full: bool = False,
    ) -> TDocCRParseResult:
        self.calls.append((tdoc_id, full))
        return self._result


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_calls_three_upserts_for_ttcn_tdoc(tmp_path: Path) -> None:
    """A TTCN-shaped TDoc triggers the three-way write path:

    1. ``cr_repo.upsert(cover)`` — slim cover-page row.
    2. ``cr_ttcn_repo.upsert(ttcn)`` — TTCN sidecar.
    3. ``cr_repo.upsert_extract_meta(meta)`` — cache-metadata row.
    """
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    zip_bytes = fixture.read_bytes()

    cover = TDocCRDetails(
        tdoc_id="R5s260009",
        spec="38.523-3",
        cr_num="3790",
        rev="0",
    )
    ttcn = TDocCRTTCNDetails(
        tdoc_id="R5s260009",
        testcase="7.1.3.5.3",
        required_changes=[{"function_name": "fl_TC_7_1_3_5_3_Body"}],
    )
    parser = _FakeParser(TDocCRParseResult(cover=cover, ttcn=ttcn))

    service, _, _, cr_repo, cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=zip_bytes,
    )
    service._parser = parser  # type: ignore[attr-defined]
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009",
        type="CR",
        ftp_url="stored/R5s260009.zip",
    )

    result = service.extract("R5s260009")

    assert cr_repo.upsert.call_count == 1
    assert cr_ttcn_repo.upsert.call_count == 1
    assert cr_repo.upsert_extract_meta.call_count == 1

    cover_arg = cr_repo.upsert.call_args.args[0]
    assert isinstance(cover_arg, TDocCRDetails)
    assert cover_arg.tdoc_id == "R5s260009"
    assert cover_arg.ftp_url == "stored/R5s260009.zip"

    ttcn_arg = cr_ttcn_repo.upsert.call_args.args[0]
    assert isinstance(ttcn_arg, TDocCRTTCNDetails)
    assert ttcn_arg.tdoc_id == "R5s260009"
    assert ttcn_arg.ftp_url == "stored/R5s260009.zip"
    assert ttcn_arg.testcase == "7.1.3.5.3"

    meta_arg = cr_repo.upsert_extract_meta.call_args.args[0]
    assert isinstance(meta_arg, TDocExtractMeta)
    assert meta_arg.ftp_url == "stored/R5s260009.zip"
    assert meta_arg.tdoc_id == "R5s260009"
    assert meta_arg.doc_filename.lower().endswith(".docx")

    assert result.details.tdoc_id == "R5s260009"
    assert result.details.cr_num == "3790"
    assert result.extract_meta.ftp_url == "stored/R5s260009.zip"
    assert result.from_cache is False


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_skips_ttcn_upsert_for_non_ttcn_tdoc(tmp_path: Path) -> None:
    """A non-TTCN TDoc (parser returns ``ttcn=None``) writes cover and
    extract meta but NEVER touches the TTCN repo.
    """
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    zip_bytes = fixture.read_bytes()

    cover = TDocCRDetails(
        tdoc_id="R5s260009",
        spec="38.523-3",
        cr_num="3790",
    )
    parser = _FakeParser(TDocCRParseResult(cover=cover, ttcn=None))

    service, _, _, cr_repo, cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=zip_bytes,
    )
    service._parser = parser  # type: ignore[attr-defined]
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009",
        type="CR",
        ftp_url="stored/R5s260009.zip",
    )

    result = service.extract("R5s260009")

    assert cr_repo.upsert.call_count == 1
    assert cr_ttcn_repo.upsert.call_count == 0
    assert cr_repo.upsert_extract_meta.call_count == 1
    assert result.from_cache is False


def test_extract_three_upsert_order_preserved(tmp_path: Path) -> None:
    """The three upsert calls fire in a deterministic order:
    ``cr_repo.upsert(cover)`` first, then
    ``cr_ttcn_repo.upsert(ttcn)`` (if applicable), then
    ``cr_repo.upsert_extract_meta(meta)`` last. This ordering matters
    for any test that records call order on the same mock; the
    assertion below captures the production contract.
    """
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    zip_bytes = fixture.read_bytes()

    cover = TDocCRDetails(tdoc_id="R5s260009", spec="38.523-3", cr_num="3790")
    ttcn = TDocCRTTCNDetails(tdoc_id="R5s260009", testcase="7.1.3.5.3")
    parser = _FakeParser(TDocCRParseResult(cover=cover, ttcn=ttcn))

    service, _, _, cr_repo, cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=zip_bytes,
    )
    service._parser = parser  # type: ignore[attr-defined]
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009",
        type="CR",
        ftp_url="stored/R5s260009.zip",
    )

    service.extract("R5s260009")

    cr_calls = [c[0] for c in cr_repo.mock_calls]
    ttcn_calls = [c[0] for c in cr_ttcn_repo.mock_calls]

    assert "upsert" in cr_calls
    assert "upsert_extract_meta" in cr_calls
    assert cr_calls.index("upsert") < cr_calls.index("upsert_extract_meta")
    assert ttcn_calls == ["upsert"]


def test_extract_three_upserts_use_matching_ftp_url(tmp_path: Path) -> None:
    """All three upserts carry the same ``ftp_url`` (the immutable
    URL the row is keyed on). Mixing URLs across the three writes
    would break the read contract.
    """
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    zip_bytes = fixture.read_bytes()

    cover = TDocCRDetails(tdoc_id="R5s260009", spec="38.523-3", cr_num="3790")
    ttcn = TDocCRTTCNDetails(tdoc_id="R5s260009", testcase="7.1.3.5.3")
    parser = _FakeParser(TDocCRParseResult(cover=cover, ttcn=ttcn))

    service, _, _, cr_repo, cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=zip_bytes,
    )
    service._parser = parser  # type: ignore[attr-defined]
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009",
        type="CR",
        ftp_url="stored/R5s260009.zip",
    )

    service.extract("R5s260009")

    cover_ftp = cr_repo.upsert.call_args.args[0].ftp_url
    ttcn_ftp = cr_ttcn_repo.upsert.call_args.args[0].ftp_url
    meta_ftp = cr_repo.upsert_extract_meta.call_args.args[0].ftp_url
    assert cover_ftp == ttcn_ftp == meta_ftp == "stored/R5s260009.zip"


# ---------------------------------------------------------------------------
# ``full`` kwarg plumbing — TTCN ``before_change`` / ``after_change`` /
# ``new_change`` extraction requires ``full=True`` end-to-end.
# ---------------------------------------------------------------------------


def test_extract_forwards_full_true_to_parser(tmp_path: Path) -> None:
    """``extract(..., full=True)`` propagates ``full=True`` to the parser.

    Without this plumbing the TTCN corrections sub-parser skips the
    ``before_change`` / ``after_change`` / ``new_change`` extraction
    loop, even when the caller explicitly opts in via ``--full``.
    Regression-locks the wiring added so ``tdoc parse --full`` in DB
    mode produces TTCN sidecars with the change-content fields.
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
        ftp_url="stored/R5s260009.zip",
    )

    service.extract("R5s260009", full=True)

    assert parser.calls == [("R5s260009", True)]


def test_extract_full_defaults_to_false(tmp_path: Path) -> None:
    """``extract()`` without ``full=`` defaults to ``False`` (metadata only)."""
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
        ftp_url="stored/R5s260009.zip",
    )

    service.extract("R5s260009")

    assert parser.calls == [("R5s260009", False)]


def test_extract_many_forwards_full_to_each_extract(tmp_path: Path) -> None:
    """``extract_many(..., full=True)`` propagates ``full=True`` to every
    :meth:`TDocCrService.extract` call in the batch.

    A batch where ``full`` is silently dropped would re-parse every TDoc
    in metadata-only mode, producing the same empty
    ``before_change`` / ``after_change`` / ``new_change`` gap that the
    DB-mode ``tdoc parse --full`` flow used to exhibit.
    """
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    zip_bytes = fixture.read_bytes()

    cover_a = TDocCRDetails(tdoc_id="R5s260009", spec="38.523-3", cr_num="3790")
    cover_b = TDocCRDetails(tdoc_id="R5s260051", spec="38.523-3", cr_num="3806")

    extract_full_calls: list[tuple[str, bool, bool]] = []

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
            full: bool = False,
        ) -> TDocCRParseResult:
            extract_full_calls.append((tdoc_id, full))
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

    service.extract_many(["R5s260009", "R5s260051"], full=True)

    assert extract_full_calls == [
        ("R5s260009", True),
        ("R5s260051", True),
    ]
    extract_full_calls.clear()
    service.extract_many(["R5s260009", "R5s260051"])

    assert extract_full_calls == [
        ("R5s260009", False),
        ("R5s260051", False),
    ]


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
    cr_repo.get_extract_meta_by_url.return_value = None
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
        tdoc_repository=tdoc_repo,
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
    cr_repo.get_extract_meta_by_url.return_value = None
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
            type="LS",
            ftp_url=f"tsg_ran/{tdoc_id}.zip",
        ),
    }[tdoc_id]

    service = TDocCrService(
        cache=cache,
        scraper_client=scraper,
        cr_repository=cr_repo,
        cr_ttcn_repository=cr_ttcn_repo,
        tdoc_repository=tdoc_repo,
    )

    result = service.extract_many(["R5s263431", "R5s263432", "R5s263499"])

    assert "R5s263431" in result.skipped
    assert "TDocNotYetOnFTPError" in result.skipped["R5s263431"]
    # The "no ftp_url" row is NOT in failures.
    assert "R5s263431" not in result.failures
    # The non-CR row stays a real failure.
    assert "R5s263499" in result.failures
    assert "TDocTypeUnsupportedError" in result.failures["R5s263499"]


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
    cr_repo.get_extract_meta_by_url.return_value = None
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
        tdoc_repository=tdoc_repo,
    )

    result = service.extract_many(["R5s263431"])
    assert result.failures == {}
    assert "R5s263431" in result.skipped
    assert "TDocNotYetOnFTPError" in result.skipped["R5s263431"]  # noqa: E501


# ---------------------------------------------------------------------------
# TDocTooLargeError + max_tdoc_size_bytes kwarg
# ---------------------------------------------------------------------------


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
        tdoc_repository=MagicMock(),
    )
    assert service._max_tdoc_size_bytes == 0


def test_constructor_accepts_max_tdoc_size_bytes(tmp_path) -> None:
    """Passing ``max_tdoc_size_bytes=N`` is stored verbatim."""
    service = TDocCrService(
        cache=TDocCache(root=tmp_path / "cache3", size_limit_bytes=0),
        scraper_client=MagicMock(),
        cr_repository=MagicMock(),
        cr_ttcn_repository=MagicMock(),
        tdoc_repository=MagicMock(),
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

    def _boom(tdoc_id: str, *, force: bool = False, full: bool = False):
        raise TDocTooLargeError(source=tdoc_id, size=10, limit=5)

    service.extract = _boom  # type: ignore[method-assign]

    batch = service.extract_many(["R5-260100", "R5-260101"])
    assert set(batch.failures) == set()
    assert set(batch.skipped.keys()) == {"R5-260100", "R5-260101"}
    assert all(
        reason.startswith("TDocTooLargeError:")
        for reason in batch.skipped.values()
    )
