"""Unit tests for the ``tdoc parse --from-file/--from-url`` direct path.

Covers the parser / helper layer (``parsers/direct_extractor``), the
service-layer branches (``TDocCrService.extract_from_url`` /
``extract_from_bytes``), and the CLI dispatch surface. Network calls
are stubbed at the ``ScraperClient`` boundary; the SQLite database is
stubbed by patching the repository methods on the service's injected
instance.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_cr import (
    DirectParseResult,
    TDocCRDetails,
    TDocExtractMeta,
)
from doc3gpp.parsers.cr_parser import CRHeaderMissingError
from doc3gpp.parsers.direct_extractor import (
    build_missing_tdoc_id_warning_message,
    build_no_pattern_warning_message,
    derive_zip_cache_key,
    direct_parse_bytes,
    extract_tdoc_id_from_filename,
    is_3gpp_ftp_url,
    read_source_bytes,
)
from doc3gpp.scraping.tdoc_zip_source import download_tdoc_zip
from doc3gpp.services.tdoc_cr_service import TDocCrService


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tdoc_cr_doc"


def _docx_available() -> bool:
    try:
        from docx import Document  # noqa: F401
    except ImportError:
        return False
    return True


def _make_dummy_zip(entries: dict[str, bytes]) -> bytes:
    """Build an in-memory zip with the given ``{name: bytes}`` entries."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# is_3gpp_ftp_url
# ---------------------------------------------------------------------------


def test_is_3gpp_ftp_url_returns_true_for_canonical_https_root() -> None:
    """The canonical https root must be accepted."""
    assert is_3gpp_ftp_url("https://www.3gpp.org/ftp/foo/bar.zip") is True


def test_is_3gpp_ftp_url_accepts_uppercase_scheme() -> None:
    """Uppercase scheme is case-insensitive per the plan's D3 rule."""
    assert is_3gpp_ftp_url("HTTP://www.3gpp.org/ftp/foo/bar.zip") is True


def test_is_3gpp_ftp_url_returns_false_for_unrelated_domain() -> None:
    """A different host is rejected."""
    assert is_3gpp_ftp_url("https://example.com/foo.zip") is False


def test_is_3gpp_ftp_url_rejects_ftp_scheme() -> None:
    """ftp:// is rejected — operators should use --from-file for ftp."""
    assert is_3gpp_ftp_url("ftp://www.3gpp.org/ftp/foo.zip") is False


def test_is_3gpp_ftp_url_rejects_file_scheme() -> None:
    """file:// is rejected — operators should use --from-file for local files."""
    assert is_3gpp_ftp_url("file:///tmp/foo.docx") is False


# ---------------------------------------------------------------------------
# extract_tdoc_id_from_filename
# ---------------------------------------------------------------------------


def test_extract_tdoc_id_from_filename_finds_canonical_id() -> None:
    """``R5s260043_MCC160Comments_r1.zip`` returns ``R5s260043``."""
    assert extract_tdoc_id_from_filename("R5s260043_MCC160Comments_r1.zip") == "R5s260043"


def test_extract_tdoc_id_from_filename_returns_none_for_random_name() -> None:
    """``random.docx`` has no 3GPP id pattern."""
    assert extract_tdoc_id_from_filename("random.docx") is None


def test_extract_tdoc_id_from_filename_handles_dash_form() -> None:
    """``R5-227476`` is matched (dash variant)."""
    assert extract_tdoc_id_from_filename("R5-227476.zip") == "R5-227476"


def test_extract_tdoc_id_from_filename_handles_c6_form() -> None:
    """``C6-250028`` is matched."""
    assert extract_tdoc_id_from_filename("C6-250028.zip") == "C6-250028"


def test_extract_tdoc_id_from_filename_returns_none_for_empty() -> None:
    """Empty input returns ``None`` without raising."""
    assert extract_tdoc_id_from_filename("") is None


# ---------------------------------------------------------------------------
# derive_zip_cache_key
# ---------------------------------------------------------------------------


def test_derive_zip_cache_key_for_url_uses_path_basename() -> None:
    """A 3GPP-style URL's basename becomes the cache key."""
    assert (
        derive_zip_cache_key("https://www.3gpp.org/ftp/foo/R5s260008.zip")
        == "R5s260008.zip"
    )


def test_derive_zip_cache_key_for_local_path_uses_filename() -> None:
    """A local ``Path`` uses ``Path.name`` (no parent dirs)."""
    assert (
        derive_zip_cache_key(Path("/tmp/somewhere/R5s260009.zip"))
        == "R5s260009.zip"
    )


def test_derive_zip_cache_key_preserves_extension() -> None:
    """The extension is preserved so zip and markdown caches don't collide."""
    assert derive_zip_cache_key("R5s260009.zip") == "R5s260009.zip"


def test_derive_zip_cache_key_distinguishes_revisions() -> None:
    """The D10 fix: r1 and r2 land in distinct cache slots."""
    r1 = derive_zip_cache_key("R5s260008_MCC160Comments_r1.zip")
    r2 = derive_zip_cache_key("R5s260008_MCC160Comments_r2.zip")
    assert r1 != r2
    assert r1.endswith("_r1.zip")
    assert r2.endswith("_r2.zip")


def test_derive_zip_cache_key_sanitises_hostile_characters() -> None:
    """Hostile characters (path traversal, slashes) are stripped."""
    key = derive_zip_cache_key("../../../etc/passwd.zip")
    assert "/" not in key
    assert ".." not in key
    assert key.endswith("passwd.zip")


def test_derive_zip_cache_key_truncates_oversized_names() -> None:
    """Names longer than 128 chars are truncated to fit the cache key regex."""
    long_name = "A" * 200 + ".zip"
    key = derive_zip_cache_key(long_name)
    assert len(key) <= 128


# ---------------------------------------------------------------------------
# read_source_bytes
# ---------------------------------------------------------------------------


def test_read_source_bytes_returns_local_kind(tmp_path: Path) -> None:
    """Local files return ``(bytes, "local")``."""
    p = tmp_path / "x.bin"
    p.write_bytes(b"hello")
    payload, kind = read_source_bytes(p)
    assert payload == b"hello"
    assert kind == "local"


def test_read_source_bytes_rejects_url_input() -> None:
    """URLs are rejected with a clear ``ValueError``."""
    with pytest.raises(ValueError, match="URL sources must be downloaded"):
        read_source_bytes("https://example.com/x.zip")


def test_read_source_bytes_raises_for_missing_file(tmp_path: Path) -> None:
    """Missing files raise ``FileNotFoundError`` (caller maps to exit 1)."""
    with pytest.raises(FileNotFoundError):
        read_source_bytes(tmp_path / "missing.bin")


# ---------------------------------------------------------------------------
# Warning message builders
# ---------------------------------------------------------------------------


def test_build_missing_tdoc_id_warning_message_contains_recipe() -> None:
    """The actionable warning carries the suggested sync / list / sync steps."""
    msg = build_missing_tdoc_id_warning_message("R5s260043", "R5s260043.zip")
    assert "R5s260043" in msg
    assert "tdocs" in msg
    assert "doc3gpp meeting sync --tsg R5" in msg
    assert "doc3gpp meeting list --tdoc R5s260043" in msg
    assert "doc3gpp tdoc sync --meeting-id" in msg


def test_build_no_pattern_warning_message_mentions_pattern() -> None:
    """The no-pattern warning documents the regex it tried to match."""
    msg = build_no_pattern_warning_message("random.docx")
    assert "random.docx" in msg
    assert "[RSC][1-6][-sw]\\d{6}" in msg
    assert "skipping cache" in msg


# ---------------------------------------------------------------------------
# download_tdoc_zip cache_key_override
# ---------------------------------------------------------------------------


class _StubCache:
    """In-memory ``TDocCacheLike`` double for cache_key_override tests.

    Records every ``get_bytes`` / ``put_bytes`` / ``path_for`` call so
    the test can assert which key the function probed and which path
    the bytes landed at.
    """

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], bytes] = {}
        self.get_calls: list[tuple[str, str]] = []
        self.put_calls: list[tuple[str, bytes, str]] = []
        self.path_calls: list[tuple[str, str]] = []

    def get_bytes(self, key: str, subdir: str) -> bytes | None:
        self.get_calls.append((key, subdir))
        return self.store.get((key, subdir))

    def put_bytes(self, key: str, payload: bytes, subdir: str) -> Path:
        self.put_calls.append((key, payload, subdir))
        self.store[(key, subdir)] = payload
        return Path(f"/cache/{subdir}/{key}")

    def path_for(self, key: str, subdir: str) -> Path:
        self.path_calls.append((key, subdir))
        return Path(f"/cache/{subdir}/{key}")


def test_download_tdoc_zip_uses_override_key_when_provided(tmp_path: Path) -> None:
    """The override key, not ``tdoc.lower()``, drives both probe and write."""
    client = MagicMock()
    client.get_bytes.return_value = b"payload"
    cache = _StubCache()
    download_tdoc_zip(
        "R5s260008",
        client=client,
        cache=cache,
        primary_url=None,
        cache_key_override="R5s260008_MCC160Comments_r1.zip",
    )
    assert ("R5s260008_MCC160Comments_r1.zip", "zips") in cache.get_calls
    assert cache.put_calls and cache.put_calls[0][0] == "R5s260008_MCC160Comments_r1.zip"


def test_download_tdoc_zip_default_key_is_tdoc_lower(tmp_path: Path) -> None:
    """With no override the function behaves exactly as before."""
    client = MagicMock()
    client.get_bytes.return_value = b"payload"
    cache = _StubCache()
    download_tdoc_zip("R5s260008", client=client, cache=cache, primary_url=None)
    assert ("r5s260008", "zips") in cache.get_calls
    assert cache.put_calls and cache.put_calls[0][0] == "r5s260008"


def test_download_tdoc_zip_override_revisions_never_collide() -> None:
    """Two revisions with the same tdoc_id land in distinct cache slots."""
    client = MagicMock()
    client.get_bytes.return_value = b"payload"
    cache = _StubCache()
    download_tdoc_zip(
        "R5s260008",
        client=client,
        cache=cache,
        primary_url=None,
        cache_key_override="R5s260008_MCC160Comments_r1.zip",
    )
    download_tdoc_zip(
        "R5s260008",
        client=client,
        cache=cache,
        primary_url=None,
        cache_key_override="R5s260008_MCC160Comments_r2.zip",
    )
    keys = [call[0] for call in cache.put_calls]
    assert keys == [
        "R5s260008_MCC160Comments_r1.zip",
        "R5s260008_MCC160Comments_r2.zip",
    ]


# ---------------------------------------------------------------------------
# direct_parse_bytes
# ---------------------------------------------------------------------------


def test_direct_parse_bytes_with_bare_docx_returns_synthetic_id(
    tmp_path: Path,
) -> None:
    """A bare ``.docx`` payload with no 3GPP id gets a ``LOCAL-`` synthetic id."""
    if not _docx_available():
        pytest.skip("python-docx not installed")
    from docx import Document
    doc = Document()
    doc.add_heading("3GPP TSG-RAN WG5 Meeting #1234-TTCN email", level=1)
    doc.add_paragraph("Random non-CR document body.")
    buf = io.BytesIO()
    doc.save(buf)
    docx_bytes = buf.getvalue()
    markdown, doc_filename, details = direct_parse_bytes(
        docx_bytes, filename="random.docx", full=False,
    )
    assert details.tdoc_id.startswith("LOCAL-")
    assert doc_filename == "random.docx"


def test_direct_parse_bytes_with_zip_picks_inner_docx(
    tmp_path: Path,
) -> None:
    """A zip payload dispatches to ``extract_docx_from_zip`` and returns the inner name."""
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists() or not _docx_available():
        pytest.skip("python-docx or fixture missing")
    zip_bytes = fixture.read_bytes()
    markdown, doc_filename, details = direct_parse_bytes(
        zip_bytes, filename="R5s260009.zip", full=False,
    )
    assert doc_filename == "R5s260009.docx"
    assert details.tdoc_id == "R5s260009"


def test_direct_parse_bytes_rejects_malformed_zip() -> None:
    """A zip-shaped but malformed payload raises ``ValueError``."""
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        direct_parse_bytes(b"PK\x03\x04 corrupt", filename="x.zip", full=False)


# ---------------------------------------------------------------------------
# Service layer: extract_from_bytes
# ---------------------------------------------------------------------------


def _build_service_with_fake_repos(
    tmp_path: Path,
    *,
    zip_bytes: bytes | None = None,
) -> tuple[TDocCrService, MagicMock, _StubCache, MagicMock, MagicMock]:
    """Build a service with stubbed repos and a stubbed scraper.

    The cache is the in-memory ``_StubCache`` (records every call);
    the scraper is a ``MagicMock`` that returns a pre-cooked payload
    for every ``get_bytes`` call. The two repos are bare
    ``MagicMock`` instances so tests can configure specific
    per-method return values.
    """
    cache = _StubCache()
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


def _read_zip(path: Path) -> tuple[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        target = zf.namelist()[0]
        return target, zf.read(target)


def test_extract_from_bytes_never_touches_cache_or_db(tmp_path: Path) -> None:
    """Local files never invoke cache writes or DB upserts."""
    if not _docx_available():
        pytest.skip("python-docx not installed")
    from docx import Document
    doc = Document()
    doc.add_heading("3GPP TSG-RAN WG5 Meeting #1234-TTCN email", level=1)
    doc.add_paragraph("dummy body")
    buf = io.BytesIO()
    doc.save(buf)
    payload = buf.getvalue()

    service, scraper, cache, cr_repo, tdoc_repo = _build_service_with_fake_repos(
        tmp_path, zip_bytes=b"unused",
    )
    result = service.extract_from_bytes(payload, "local.docx", full=False)
    assert isinstance(result, DirectParseResult)
    assert result.source_kind == "local"
    assert result.from_cache is False
    assert result.persisted is False
    assert result.extract_meta is None
    assert cache.put_calls == []
    assert cr_repo.upsert.call_count == 0
    # tdoc_repo.get_by_id is not consulted for local files.
    assert tdoc_repo.get_by_id.call_count == 0


# ---------------------------------------------------------------------------
# Service layer: extract_from_url
# ---------------------------------------------------------------------------


def test_extract_from_url_other_url_skips_cache_and_db(tmp_path: Path) -> None:
    """A non-3GPP URL parses in-memory; cache and DB stay untouched."""
    if not _docx_available():
        pytest.skip("python-docx not installed")
    from docx import Document
    doc = Document()
    doc.add_heading("3GPP TSG-RAN WG5 Meeting #1234-TTCN email", level=1)
    doc.add_paragraph("dummy body")
    buf = io.BytesIO()
    doc.save(buf)
    docx_bytes = buf.getvalue()

    service, scraper, cache, cr_repo, _ = _build_service_with_fake_repos(
        tmp_path, zip_bytes=b"unused",
    )
    scraper.get_bytes.return_value = docx_bytes
    result = service.extract_from_url(
        "https://example.com/R5s260099.docx", force=False, full=False,
    )
    assert result.source_kind == "url-other"
    assert result.from_cache is False
    assert result.persisted is False
    assert cache.put_calls == []
    assert cr_repo.upsert.call_count == 0


def test_extract_from_url_3gpp_with_tdoc_in_tdocs_writes_cache_and_db(
    tmp_path: Path,
) -> None:
    """3GPP URL + ``tdoc_id ∈ tdocs`` is the full happy path."""
    if not _docx_available():
        pytest.skip("python-docx not installed")
    fixture = FIXTURES_DIR / "R5s260009.zip"
    zip_bytes = fixture.read_bytes()
    service, scraper, cache, cr_repo, tdoc_repo = _build_service_with_fake_repos(
        tmp_path, zip_bytes=zip_bytes,
    )
    tdoc_repo.get_by_id.return_value = TDoc(tdoc_id="R5s260009", type="CR")

    result = service.extract_from_url(
        "https://www.3gpp.org/ftp/.../R5s260009.zip", force=False, full=False,
    )
    assert result.source_kind == "url-3gpp"
    assert result.persisted is True
    assert result.from_cache is False
    assert result.details is not None
    assert result.extract_meta is not None
    # Both rows written.
    assert cr_repo.upsert.call_count == 1
    # The zip lands under the override key (filename), not the tdoc_id.
    assert any(call[0].endswith("R5s260009.zip") for call in cache.put_calls)


def test_extract_from_url_3gpp_with_tdoc_missing_in_tdocs_skips_db(
    tmp_path: Path,
) -> None:
    """3GPP URL with the tdoc_id missing from ``tdocs``: no cache, no DB."""
    if not _docx_available():
        pytest.skip("python-docx not installed")
    from docx import Document
    doc = Document()
    doc.add_heading("3GPP TSG-RAN WG5 Meeting #1234-TTCN email", level=1)
    doc.add_paragraph("dummy body")
    buf = io.BytesIO()
    doc.save(buf)
    docx_bytes = buf.getvalue()

    service, scraper, cache, cr_repo, tdoc_repo = _build_service_with_fake_repos(
        tmp_path, zip_bytes=b"unused",
    )
    scraper.get_bytes.return_value = docx_bytes
    tdoc_repo.get_by_id.return_value = None

    result = service.extract_from_url(
        "https://www.3gpp.org/ftp/.../R5s260043_MCC160Comments_r1.docx",
        force=False, full=False,
    )
    assert result.source_kind == "url-3gpp"
    assert result.tdoc_id == "R5s260043"
    assert result.tdoc_id_in_tdocs is False
    assert result.persisted is False
    assert result.extract_meta is None
    assert cr_repo.upsert.call_count == 0
    assert cache.put_calls == []


def test_extract_from_url_3gpp_with_no_pattern_in_filename_skips_db(
    tmp_path: Path,
) -> None:
    """A 3GPP URL whose filename has no TDoc id pattern: no cache, no DB."""
    if not _docx_available():
        pytest.skip("python-docx not installed")
    from docx import Document
    doc = Document()
    doc.add_heading("3GPP TSG-RAN WG5 Meeting #1234-TTCN email", level=1)
    doc.add_paragraph("dummy body")
    buf = io.BytesIO()
    doc.save(buf)
    docx_bytes = buf.getvalue()

    service, scraper, cache, cr_repo, tdoc_repo = _build_service_with_fake_repos(
        tmp_path, zip_bytes=b"unused",
    )
    scraper.get_bytes.return_value = docx_bytes
    result = service.extract_from_url(
        "https://www.3gpp.org/ftp/.../meeting_minutes.docx",
        force=False, full=False,
    )
    assert result.source_kind == "url-3gpp"
    assert result.tdoc_id is None
    assert result.tdoc_id_in_tdocs is False
    assert result.persisted is False
    assert cr_repo.upsert.call_count == 0
    assert cache.put_calls == []
    # tdoc_repo.get_by_id is not consulted (no tdoc id to probe).
    assert tdoc_repo.get_by_id.call_count == 0


def test_extract_from_url_3gpp_db_cache_hit_short_circuits(
    tmp_path: Path,
) -> None:
    """A pre-existing ``tdoc_cr_details`` row means ``from_cache=True`` and no new network call."""
    if not _docx_available():
        pytest.skip("python-docx not installed")
    fixture = FIXTURES_DIR / "R5s260009.zip"
    zip_bytes = fixture.read_bytes()
    service, scraper, cache, cr_repo, tdoc_repo = _build_service_with_fake_repos(
        tmp_path, zip_bytes=zip_bytes,
    )
    tdoc_repo.get_by_id.return_value = TDoc(tdoc_id="R5s260009", type="CR")
    # Pre-populate the DB cache probes.
    cr_repo.get_by_url.return_value = _dummy_details("R5s260009")
    cr_repo.get_extract_meta_by_url.return_value = _make_meta(
        "R5s260009", "/cache/zips/R5s260009.zip",
    )

    result = service.extract_from_url(
        "https://www.3gpp.org/ftp/.../R5s260009.zip", force=False, full=False,
    )
    assert result.from_cache is True
    assert result.persisted is False
    # Network was NOT touched on a cache hit.
    assert scraper.get_bytes.call_count == 0


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------


def _dummy_details(tdoc_id: str) -> TDocCRDetails:
    return TDocCRDetails(tdoc_id=tdoc_id, spec="38.523-3", cr_num="3790", rev="0")


def _make_meta(tdoc_id: str, zip_path: str) -> TDocExtractMeta:
    return TDocExtractMeta(
        ftp_url="tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/" + tdoc_id + ".zip",
        tdoc_id=tdoc_id,
        zip_path=zip_path,
        markdown_path="/cache/markdown/abc.bin",
        doc_filename=tdoc_id + ".docx",
    )


@dataclass
class _FakeDirectResult:
    source_kind: str = "local"
    markdown: str = "3GPP TSG- blah"
    details: Any = None
    extract_meta: Any = None
    from_cache: bool = False
    persisted: bool = False
    tdoc_id: str | None = None
    tdoc_id_in_tdocs: bool = False


class _FakeService:
    """Stub :class:`TDocCrService` exposing the direct-parse surface only."""

    def __init__(self, result: DirectParseResult | Exception) -> None:
        self._result = result
        self.url_calls: list[tuple[str, bool, bool]] = []
        self.bytes_calls: list[tuple[bytes, str, bool, bool]] = []

    def extract_from_url(self, url: str, *, force: bool, full: bool) -> DirectParseResult:
        self.url_calls.append((url, force, full))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    def extract_from_bytes(
        self, payload: bytes, filename: str, *, force: bool, full: bool,
    ) -> DirectParseResult:
        self.bytes_calls.append((payload, filename, force, full))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _build_service_factory(monkeypatch, service: TDocCrService) -> None:
    """Patch the CLI's service factory to return ``service``."""
    monkeypatch.setattr("doc3gpp.cli.build_tdoc_cr_service", lambda: service)


def test_cli_from_file_table_format_emits_single_row(tmp_path: Path, monkeypatch) -> None:
    """``--from-file foo.docx --format table`` writes header + 1 data row."""
    source = tmp_path / "in.docx"
    source.write_bytes(b"dummy")
    out = tmp_path / "out.tsv"
    fake = _FakeService(DirectParseResult(
        source_kind="local",
        markdown="3GPP TSG- blah",
        details=_dummy_details("R5s260009"),
        extract_meta=None,
        from_cache=False,
        persisted=False,
        tdoc_id="R5s260009",
        tdoc_id_in_tdocs=False,
    ))
    _build_service_factory(monkeypatch, fake)

    runner = CliRunner()
    result = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-file", str(source),
            "--format", "table",
            "--output", str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    body = out.read_text()
    lines = body.rstrip().split("\n")
    assert len(lines) == 2
    assert lines[0].startswith("tdoc_id\t")
    assert "R5s260009" in lines[1]
    assert "38.523-3" in lines[1]
    assert fake.bytes_calls, "extract_from_bytes was not called"


def test_cli_from_file_markdown_format_emits_gfm_table(tmp_path: Path, monkeypatch) -> None:
    """``--format markdown`` writes a header + separator + 1-row GFM table."""
    source = tmp_path / "in.docx"
    source.write_bytes(b"dummy")
    out = tmp_path / "out.md"
    fake = _FakeService(DirectParseResult(
        source_kind="local",
        markdown="3GPP TSG- blah",
        details=_dummy_details("R5s260009"),
        extract_meta=None,
        from_cache=False,
        persisted=False,
        tdoc_id="R5s260009",
        tdoc_id_in_tdocs=False,
    ))
    _build_service_factory(monkeypatch, fake)

    runner = CliRunner()
    result = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-file", str(source),
            "--format", "markdown",
            "--output", str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    body = out.read_text()
    lines = body.rstrip().split("\n")
    assert lines[0].startswith("| tdoc_id |")
    assert lines[1].startswith("|---")
    assert lines[2].startswith("| R5s260009 |")
    assert "38.523-3" in lines[2]


def test_cli_from_file_json_format_emits_object(tmp_path: Path, monkeypatch) -> None:
    """``--format json`` writes a single JSON object."""
    source = tmp_path / "in.docx"
    source.write_bytes(b"dummy")
    out = tmp_path / "out.json"
    fake = _FakeService(DirectParseResult(
        source_kind="local",
        markdown="3GPP TSG- blah",
        details=_dummy_details("R5s260009"),
        extract_meta=None,
        from_cache=False,
        persisted=False,
        tdoc_id="R5s260009",
        tdoc_id_in_tdocs=False,
    ))
    _build_service_factory(monkeypatch, fake)

    runner = CliRunner()
    result = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-file", str(source),
            "--format", "json",
            "--output", str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text())
    assert payload["tdoc_id"] == "R5s260009"
    assert payload["spec"] == "38.523-3"
    assert payload["corrections"] == []


def test_cli_from_file_raw_format_writes_markdown_verbatim(
    tmp_path: Path, monkeypatch,
) -> None:
    """``--format raw`` writes the converted markdown and never calls the parser."""
    source = tmp_path / "in.docx"
    source.write_bytes(b"dummy")
    fake = _FakeService(DirectParseResult(
        source_kind="local",
        markdown="3GPP TSG- raw markdown body\n\n",
        details=None,
        extract_meta=None,
        from_cache=False,
        persisted=False,
        tdoc_id="R5s260009",
        tdoc_id_in_tdocs=False,
    ))
    _build_service_factory(monkeypatch, fake)

    runner = CliRunner()
    result = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-file", str(source),
            "--format", "raw",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "raw markdown body" in result.output


def test_cli_from_file_and_from_url_are_mutually_exclusive(tmp_path: Path, monkeypatch) -> None:
    """Both flags set → BadParameter."""
    a = tmp_path / "a.docx"
    a.write_bytes(b"x")
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-file", str(a),
            "--from-url", "https://example.com/b.zip",
        ],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in (result.output or "")


def test_cli_from_file_with_filter_flag_warns_and_proceeds(
    tmp_path: Path, monkeypatch,
) -> None:
    """A filter flag set with ``--from-file`` warns on stderr and continues."""
    source = tmp_path / "in.docx"
    source.write_bytes(b"dummy")
    fake = _FakeService(DirectParseResult(
        source_kind="local",
        markdown="3GPP TSG- blah",
        details=_dummy_details("R5s260009"),
        extract_meta=None,
        from_cache=False,
        persisted=False,
        tdoc_id="R5s260009",
        tdoc_id_in_tdocs=False,
    ))
    _build_service_factory(monkeypatch, fake)

    runner = CliRunner()
    result = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-file", str(source),
            "--tdoc", "R5s260009",
            "--spec", "38.523",
        ],
    )
    assert result.exit_code == 0
    combined = (result.stdout or "") + (result.stderr or "")
    assert "ignoring filter flag" in combined
    assert "--tdoc" in combined
    assert "--spec" in combined


def test_cli_from_file_with_force_is_rejected(tmp_path: Path, monkeypatch) -> None:
    """``--from-file --force`` is rejected — no DB row to force."""
    source = tmp_path / "in.docx"
    source.write_bytes(b"x")
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-file", str(source),
            "--force",
        ],
    )
    assert result.exit_code != 0
    assert "--force is not applicable" in (result.output or "")


def test_cli_from_file_with_yes_is_rejected(tmp_path: Path, monkeypatch) -> None:
    """``--from-file --yes`` is rejected — no batch to confirm."""
    source = tmp_path / "in.docx"
    source.write_bytes(b"x")
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-file", str(source),
            "--yes",
        ],
    )
    assert result.exit_code != 0
    assert "--yes is not applicable" in (result.output or "")


def test_cli_from_file_missing_file_exits_1(monkeypatch) -> None:
    """A missing ``--from-file`` path produces a clear error and exit 1."""
    runner = CliRunner()
    result = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-file", "/tmp/definitely-not-here-xyz.docx",
        ],
    )
    assert result.exit_code == 1
    assert "FAILED" in (result.output or "")


def test_cli_from_file_non_cr_raises_cr_header_missing(
    tmp_path: Path, monkeypatch,
) -> None:
    """``CRHeaderMissingError`` formats as ``FAILED - CRHeaderMissingError: ...`` and exits 1."""
    source = tmp_path / "in.docx"
    source.write_bytes(b"x")
    fake = _FakeService(CRHeaderMissingError(
        "Markdown does not contain a '3GPP TSG-' header",
        snippet="garbage",
    ))
    _build_service_factory(monkeypatch, fake)

    runner = CliRunner()
    result = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-file", str(source),
        ],
    )
    assert result.exit_code == 1
    assert "FAILED - CRHeaderMissingError" in (result.output or "")


def test_cli_from_url_unreachable_exits_1(monkeypatch) -> None:
    """A network failure exits 1 with ``FAILED - TDocZipDownloadError``."""
    from doc3gpp.scraping.tdoc_zip_source import TDocZipDownloadError

    fake = _FakeService(TDocZipDownloadError(
        url="https://www.3gpp.org/ftp/x.zip",
        original=RuntimeError("boom"),
    ))
    _build_service_factory(monkeypatch, fake)

    runner = CliRunner()
    result = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-url", "https://www.3gpp.org/ftp/x.zip",
        ],
    )
    assert result.exit_code == 1
    assert "FAILED - TDocZipDownloadError" in (result.output or "")


def test_cli_from_url_3gpp_missing_tdoc_emits_warning(
    tmp_path: Path, monkeypatch,
) -> None:
    """A 3GPP URL with the tdoc_id missing from ``tdocs`` warns on stderr + still emits output."""
    fake = _FakeService(DirectParseResult(
        source_kind="url-3gpp",
        markdown="3GPP TSG- blah",
        details=_dummy_details("R5s260043"),
        extract_meta=None,
        from_cache=False,
        persisted=False,
        tdoc_id="R5s260043",
        tdoc_id_in_tdocs=False,
    ))
    _build_service_factory(monkeypatch, fake)

    runner = CliRunner()
    result = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-url",
            "https://www.3gpp.org/ftp/.../R5s260043_MCC160Comments_r1.docx",
        ],
    )
    assert result.exit_code == 0
    combined = (result.stdout or "") + (result.stderr or "")
    assert "not present in the 'tdocs' table" in combined
    assert "doc3gpp meeting sync --tsg R5" in combined
    # Output is still produced on stdout.
    assert "R5s260043" in (result.stdout or "")
    assert "38.523-3" in (result.stdout or "")


def test_cli_from_url_3gpp_no_pattern_emits_warning(
    tmp_path: Path, monkeypatch,
) -> None:
    """A 3GPP URL whose filename has no tdoc_id pattern warns on stderr + still emits output."""
    fake = _FakeService(DirectParseResult(
        source_kind="url-3gpp",
        markdown="3GPP TSG- blah",
        details=_dummy_details("LOCAL-x"),
        extract_meta=None,
        from_cache=False,
        persisted=False,
        tdoc_id=None,
        tdoc_id_in_tdocs=False,
    ))
    _build_service_factory(monkeypatch, fake)

    runner = CliRunner()
    result = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-url", "https://www.3gpp.org/ftp/.../meeting_minutes.docx",
        ],
    )
    assert result.exit_code == 0
    combined = (result.stdout or "") + (result.stderr or "")
    assert "does not match the 3GPP TDoc id" in combined
    assert "meeting_minutes.docx" in combined
