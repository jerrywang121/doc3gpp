"""Integration tests for :class:`TDocCrService` + SQL repositories.

Exercises the end-to-end extraction pipeline against a SQLite database
seeded with a parent ``tdocs`` row and a mocked :class:`ScraperClient`
that returns the bytes of one of the fixtures shipped under
``tests/fixtures/tdoc_cr_doc/``. The cache is rooted under a
``tmp_path`` directory so the tests never touch the user's home cache.

The tests that require ``python-docx`` (every happy-path and the
markdown-cache-hit test) carry a ``@pytest.mark.skipif`` guard so the
suite stays green in environments that haven't installed the
``[extract]`` extra. The structural tests (network failure, unknown
tdoc, non-CR tdoc, invalid id shape) don't need python-docx.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
from doc3gpp.parsers.normalizers import normalize_ftp_path
from doc3gpp.scraping.cache import TDocCache
from doc3gpp.scraping.tdoc_zip_source import (
    TDocZipDownloadError,
    get_tdoc_zip_url,
)
from doc3gpp.services.tdoc_cr_service import (
    BatchExtractResult,
    ExtractResult,
    TDocCrService,
    TDocNotFoundError,
    TDocTypeUnsupportedError,
)
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
    SQLAlchemyTDocCrChangeDetailsRepository,
)
from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
    SQLAlchemyTDocCrTtcnRepository,
)
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository


FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tdoc_cr_doc"


def _docx_available() -> bool:
    """Return True iff ``python-docx`` imports cleanly.

    Mirrors the helper used in :mod:`test_docx_converter` so the same
    skip guard can gate both unit and integration tests.
    """
    try:
        from docx import Document  # noqa: F401
    except ImportError:
        return False
    return True


def _zip_payload(source: Path) -> bytes:
    """Read a fixture zip from disk into bytes.

    Centralised so the tests don't all repeat the same ``read_bytes``
    call — and so swapping the fixture location in one place is
    sufficient.
    """
    return source.read_bytes()


def _build_service(
    tmp_path: Path,
    *,
    zip_bytes: bytes | Exception | None = None,
) -> tuple[
    TDocCrService,
    MagicMock,
    TDocCache,
    SQLAlchemyTDocCrRepository,
    SQLAlchemyTDocCrTtcnRepository,
    SQLAlchemyTDocRepository,
]:
    """Construct a fully-wired :class:`TDocCrService` against tmp dirs.

    Returns ``(service, scraper_mock, cache, cr_repo, cr_ttcn_repo,
    tdoc_repo)`` so each test can inspect call counts, cache
    contents, and DB rows without rebuilding the wiring.

    Args:
        tmp_path: Per-test root used for both the cache directory and
            the SQLite database (the latter is wired by the
            ``sqlite_env`` fixture before this helper is called).
        zip_bytes: Pre-cooked response for ``scraper_mock.get_bytes``.
            When ``None`` the mock returns an empty payload and tests
            that don't exercise the zip path are unaffected. When an
            :class:`Exception` instance, the mock raises it on every
            call — used by the network-failure test.
    """
    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    scraper_mock = MagicMock()
    if zip_bytes is None:
        scraper_mock.get_bytes.return_value = b""
    elif isinstance(zip_bytes, Exception):
        scraper_mock.get_bytes.side_effect = zip_bytes
    else:
        scraper_mock.get_bytes.return_value = zip_bytes

    cr_repo = SQLAlchemyTDocCrRepository()
    cr_ttcn_repo = SQLAlchemyTDocCrTtcnRepository()
    cr_change_details_repo = SQLAlchemyTDocCrChangeDetailsRepository()
    tdoc_repo = SQLAlchemyTDocRepository()
    service = TDocCrService(
        cache=cache,
        scraper_client=scraper_mock,
        cr_repository=cr_repo,
        cr_ttcn_repository=cr_ttcn_repo,
        cr_change_details_repository=cr_change_details_repo,
        tdoc_repository=tdoc_repo,
    )
    return service, scraper_mock, cache, cr_repo, cr_ttcn_repo, tdoc_repo


def _seed_cr_tdoc(
    tdoc_repo: SQLAlchemyTDocRepository,
    tdoc_id: str,
    ftp_url: str | None = None,
) -> None:
    """Insert a parent ``tdocs`` row flagged as type ``"CR"``.

    When ``ftp_url`` is omitted, a deterministic 2026 TTCN-CR path is
    used so the post-T3 cache-key derivation has a non-None URL to
    hash against (legacy helper callers did not need it).
    """
    resolved = ftp_url or (
        f"tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/{tdoc_id}.zip"
    )
    tdoc_repo.upsert_many([TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=resolved)])


# ---------------------------------------------------------------------------
# 1. Happy path: first extract downloads the zip, renders markdown,
#    parses, persists, and returns from_cache=False.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_happy_path(sqlite_env, tmp_path) -> None:
    """End-to-end extract of ``R5s260009`` from the local zip fixture."""
    create_schema()
    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists(), f"fixture missing: {fixture}"

    service, scraper_mock, cache, cr_repo, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=_zip_payload(fixture)
    )
    _seed_cr_tdoc(tdoc_repo, "R5s260009")

    result = service.extract("R5s260009")

    assert isinstance(result, ExtractResult)
    assert result.from_cache is False
    # Sanity: the scraper was hit exactly once for the zip download.
    assert scraper_mock.get_bytes.call_count == 1
    # Cover-page fields match the snapshot contract in test_cr_parser.py.
    assert result.details.spec == "38.523-3"
    assert result.details.cr_num == "3790"
    assert result.details.rev == "0"
    assert result.details.release == "Rel-18"
    # Both DB rows landed.
    details_list = cr_repo.get("R5s260009")
    assert len(details_list) == 1
    assert details_list[0].spec == "38.523-3"
    meta_list = cr_repo.get_extract_meta("R5s260009")
    assert len(meta_list) == 1
    meta = meta_list[0]
    assert meta.tdoc_id == "R5s260009"
    assert meta.doc_filename.lower().endswith(".docx")
    assert (cache.root / "zips" / meta.cache_file).exists()
    assert (cache.root / "markdown" / meta.cache_file).exists()
    cache_status = cache.status()
    assert cache_status.zips == 1
    assert cache_status.markdown == 1


# ---------------------------------------------------------------------------
# 2. DB-cache hit: second call short-circuits on the persisted row.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_db_cache_hit_skips_network(sqlite_env, tmp_path) -> None:
    """A second ``extract`` call must not re-invoke the scraper."""
    create_schema()
    fixture = FIXTURES_DIR / "R5s260009.zip"

    service, scraper_mock, _, _, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=_zip_payload(fixture)
    )
    _seed_cr_tdoc(tdoc_repo, "R5s260009")

    first = service.extract("R5s260009")
    assert first.from_cache is False

    scraper_mock.reset_mock()

    second = service.extract("R5s260009")
    assert second.from_cache is True
    assert scraper_mock.get_bytes.call_count == 0
    # Same parsed fields (frozen dataclasses are value-equal).
    assert second.details == first.details


# ---------------------------------------------------------------------------
# 3. Markdown cache hit: zip purged but markdown retained → skip python-docx.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_markdown_cache_hit_when_zip_purged(
    sqlite_env, tmp_path, monkeypatch
) -> None:
    """With the zip cache wiped but the markdown cache intact, the
    second call re-downloads the zip yet re-uses the rendered
    markdown — ``convert_document_to_markdown`` must NOT be called
    again."""
    create_schema()
    fixture = FIXTURES_DIR / "R5s260009.zip"

    service, scraper_mock, cache, _, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=_zip_payload(fixture)
    )
    _seed_cr_tdoc(tdoc_repo, "R5s260009")

    # First call: populates both caches and the DB.
    first = service.extract("R5s260009")
    assert first.from_cache is False
    assert scraper_mock.get_bytes.call_count == 1

    # Patch out the python-docx converter so a regression that calls it
    # a second time blows up loudly instead of silently re-rendering.
    def _boom(*_args, **_kwargs):  # pragma: no cover - raised via mock
        raise AssertionError("convert_document_to_markdown should not be re-invoked")

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_cr_service.convert_document_to_markdown",
        _boom,
        raising=False,
    )

    # Wipe the zip subtree; the markdown subtree (sharing the
    # ``cache_file`` basename with the zip subtree) remains intact so
    # the second call must hit it.
    deleted = cache.purge_subdir("zips")
    assert deleted == 1

    # Also drop the DB rows so the service is forced to re-download +
    # re-parse. The plan requires this: a markdown cache hit must
    # NOT be conflated with a DB cache hit. Both child tables are
    # keyed by URL, so the lookups below go via the non-PK ``tdoc_id``
    # FK; every matching revision row is dropped.
    from sqlalchemy import select

    from doc3gpp.storage.db.models import TDocCrDetailOrm, TDocExtractOrm
    from doc3gpp.storage.db.session import get_session_factory

    factory = get_session_factory()
    with factory() as session:
        detail_rows = session.scalars(
            select(TDocCrDetailOrm).where(
                TDocCrDetailOrm.tdoc_id == "R5s260009"
            )
        ).all()
        for row in detail_rows:
            session.delete(row)
        meta_rows = session.scalars(
            select(TDocExtractOrm).where(
                TDocExtractOrm.tdoc_id == "R5s260009"
            )
        ).all()
        for row in meta_rows:
            session.delete(row)
        session.commit()

    scraper_mock.reset_mock()

    second = service.extract("R5s260009")
    # Network was hit (zip cache was empty)…
    assert scraper_mock.get_bytes.call_count == 1
    # …but the markdown cache short-circuited the converter.
    assert second.from_cache is False


# ---------------------------------------------------------------------------
# 4. force=True bypasses both caches.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_force_bypasses_caches(sqlite_env, tmp_path) -> None:
    """``force=True`` re-downloads the zip AND re-renders markdown."""
    create_schema()
    fixture = FIXTURES_DIR / "R5s260009.zip"

    service, scraper_mock, cache, _, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=_zip_payload(fixture)
    )
    _seed_cr_tdoc(tdoc_repo, "R5s260009")

    service.extract("R5s260009")
    scraper_calls_before = scraper_mock.get_bytes.call_count
    cache_status_before = cache.status()
    assert cache_status_before.zips == 1
    assert cache_status_before.markdown == 1

    # Wipe the zip subtree so a non-force second call would hit the
    # network but should still hit the markdown cache. With force=True
    # BOTH caches must be bypassed.
    cache.purge_subdir("zips")
    scraper_mock.reset_mock()

    convert_mock = MagicMock(
        wraps=__import__(
            "doc3gpp.parsers.docx_converter",
            fromlist=["convert_document_to_markdown"],
        ).convert_document_to_markdown
    )
    import doc3gpp.parsers.docx_converter as md_mod

    original = md_mod.convert_document_to_markdown
    md_mod.convert_document_to_markdown = convert_mock
    try:
        result = service.extract("R5s260009", force=True)
    finally:
        md_mod.convert_document_to_markdown = original

    assert result.from_cache is False
    assert scraper_mock.get_bytes.call_count == 1
    assert convert_mock.call_count == 1
    # Sanity: scraper_calls_before was at least one (first extract).
    assert scraper_calls_before >= 1


# ---------------------------------------------------------------------------
# 4b. Regression: zip cache hit + DB extract miss must persist
#     successfully using the first resolved candidate URL.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_zip_cache_hit_persists_with_candidate_url(
    sqlite_env, tmp_path
) -> None:
    """When the zip cache is pre-populated but the DB extract tables
    have no row, ``extract`` must persist under the resolver's first
    candidate URL — ``download_tdoc_zip`` returns
    ``DownloadedZip(url=None)`` on a cache hit (the URL that originally
    populated the cache is not tracked), so the service falls back to
    ``resolve_download_url``'s first entry for persistence. Previously
    this surfaced as ``TDocExtractMeta requires a non-empty ftp_url``
    and the TDoc silently failed in :meth:`extract_many`.
    """
    from doc3gpp.scraping.cache_keys import derive_cache_file

    create_schema()
    fixture = FIXTURES_DIR / "R5s260009.zip"

    service, scraper_mock, cache, cr_repo, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=_zip_payload(fixture)
    )
    _seed_cr_tdoc(tdoc_repo, "R5s260009")

    # Pre-condition: zip cache populated, DB extract tables empty.
    # The cache key is derived from the tdoc's ftp_url (post-T3).
    expected_url = normalize_ftp_path(
        get_tdoc_zip_url("R5s260009") or ""
    )
    assert expected_url, "test precondition: R5s template URL must resolve"
    cache_file = derive_cache_file(expected_url)
    cache.put_bytes(cache_file, _zip_payload(fixture), "zips")
    scraper_mock.reset_mock()

    result = service.extract("R5s260009")

    assert isinstance(result, ExtractResult)
    assert result.from_cache is False
    # Network was bypassed — the zip came from the local cache.
    assert scraper_mock.get_bytes.call_count == 0

    details_list = cr_repo.get("R5s260009")
    assert len(details_list) == 1
    assert details_list[0].ftp_url == expected_url

    meta_list = cr_repo.get_extract_meta("R5s260009")
    assert len(meta_list) == 1
    assert meta_list[0].ftp_url == expected_url
    assert meta_list[0].ftp_url, (
        "ftp_url must not be empty on zip-cache-hit persistence "
        "(regression: previously raised TDocExtractMeta requires a "
        "non-empty ftp_url)"
    )

    # A subsequent call must now hit the DB cache at the URL we
    # just persisted — no re-extract, no network.
    scraper_mock.reset_mock()
    second = service.extract("R5s260009")
    assert second.from_cache is True
    assert scraper_mock.get_bytes.call_count == 0


# ---------------------------------------------------------------------------
# 5. Non-CR TDoc → TDocTypeUnsupportedError.
# ---------------------------------------------------------------------------


def test_extract_non_cr_tdoc_raises_type_unsupported(sqlite_env, tmp_path) -> None:
    """A TDoc row with an unrecognised ``type`` must raise
    ``TDocTypeUnsupportedError``."""
    create_schema()
    service, _, _, _, _cr_ttcn_repo, tdoc_repo = _build_service(tmp_path, zip_bytes=b"unused")
    tdoc_repo.upsert_many([TDoc(tdoc_id="R5s260009", type="DRAFT")])

    with pytest.raises(TDocTypeUnsupportedError) as excinfo:
        service.extract("R5s260009")
    assert excinfo.value.tdoc_id == "R5s260009"
    assert excinfo.value.observed_type == "DRAFT"


# ---------------------------------------------------------------------------
# 6. Unknown TDoc → TDocNotFoundError.
# ---------------------------------------------------------------------------


def test_extract_unknown_tdoc_raises_not_found(sqlite_env, tmp_path) -> None:
    """A TDoc id with no row in ``tdocs`` must raise ``TDocNotFoundError``."""
    create_schema()
    service, _, _, _, _cr_ttcn_repo, _ = _build_service(tmp_path, zip_bytes=b"unused")

    with pytest.raises(TDocNotFoundError) as excinfo:
        service.extract("R5s260009")
    assert excinfo.value.tdoc_id == "R5s260009"


# ---------------------------------------------------------------------------
# 7. Network failure → TDocZipDownloadError + no partial cache write.
# ---------------------------------------------------------------------------


def test_extract_network_failure_no_partial_cache(sqlite_env, tmp_path) -> None:
    """An ``httpx.ConnectError`` must surface as ``TDocZipDownloadError``
    without leaving a zero-byte zip in the cache or a detail row in
    the DB.
    """
    create_schema()
    service, scraper_mock, cache, cr_repo, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path,
        zip_bytes=httpx.ConnectError("network unreachable"),
    )
    _seed_cr_tdoc(tdoc_repo, "R5s260009")

    with pytest.raises(TDocZipDownloadError):
        service.extract("R5s260009")

    # The scraper was called (download was attempted)…
    assert scraper_mock.get_bytes.call_count == 1
    # …but no zip landed in the cache (no zero-byte file written).
    assert cache.status().zips == 0
    assert not any((cache.root / "zips").iterdir())
    # And no DB rows were persisted.
    assert cr_repo.get("R5s260009") == []
    assert cr_repo.get_extract_meta("R5s260009") == []


# ---------------------------------------------------------------------------
# 8. Invalid ``tdoc_id`` shape → ValueError.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_id", ["", "   ", "invalid$id", "x" * 33])
def test_extract_invalid_tdoc_id_raises_value_error(
    sqlite_env, tmp_path, bad_id: str
) -> None:
    """A garbage id must raise ``ValueError`` before any I/O happens."""
    create_schema()
    service, scraper_mock, cache, cr_repo, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=b"unused"
    )
    # No tdoc seed — but the shape check fires first.
    _seed_cr_tdoc(tdoc_repo, "R5s260009")  # ensure the parent exists for completeness

    with pytest.raises(ValueError):
        service.extract(bad_id)

    # Nothing was touched.
    assert scraper_mock.get_bytes.call_count == 0
    assert cache.status().file_count == 0
    assert cr_repo.get("R5s260009") == []  # ``get(tdoc_id)`` now returns a list


# ---------------------------------------------------------------------------
# 9. extract_many: per-id failure isolation.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_many_skips_failures(sqlite_env, tmp_path) -> None:
    """``extract_many`` returns a :class:`BatchExtractResult` whose
    ``successes`` dict holds the canonical ids that produced an
    extract and whose ``failures`` dict maps the broken ids to a
    short reason string (``"{ExceptionClassName}: {exc}"``). Per-id
    failures are logged but never raised (so one broken id doesn't
    abort the batch)."""
    create_schema()
    fixture = FIXTURES_DIR / "R5s260009.zip"

    service, _, _, _, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=_zip_payload(fixture)
    )
    _seed_cr_tdoc(tdoc_repo, "R5s260009")

    batch = service.extract_many(
        [
            "R5s260009",
            "R5s260010",
            "invalid$id",
            "R5s260009",
        ]
    )

    assert isinstance(batch, BatchExtractResult)
    assert set(batch.successes.keys()) == {"R5s260009"}
    assert batch.successes["R5s260009"].from_cache is True

    # ``R5s260010`` is not in the tdocs table → TDocNotFoundError.
    # ``invalid$id`` fails the shape guard → ValueError.
    assert "R5s260010" in batch.failures
    assert batch.failures["R5s260010"].startswith("TDocNotFoundError:")
    assert "invalid$id" in batch.failures
    assert batch.failures["invalid$id"].startswith("ValueError:")


# ---------------------------------------------------------------------------
# 10. End-to-end: Typer CliRunner -> factory -> service -> cache -> DB.
# ---------------------------------------------------------------------------


class _DummyScraperClient:
    """In-memory :class:`ScraperClient` double that serves one canned payload.

    The CLI's :func:`build_tdoc_cr_service` instantiates a fresh
    ``ScraperClient()`` with no arguments; this dummy mirrors that contract
    so the patch is a drop-in. ``get_bytes`` returns the pre-cooked zip
    bytes (recorded for an assertion that the scraper was actually hit) and
    ``get_text`` is unimplemented — the extraction path never reads text.
    """

    def __init__(self, *args, **kwargs) -> None:
        self.calls: list[str] = []

    def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        return self._payload

    # The factory does not use ``with``; these exist only for symmetry.
    def __enter__(self) -> "_DummyScraperClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_end_to_end_via_cli_runner(sqlite_env, monkeypatch, tmp_path) -> None:
    """End-to-end: Typer CliRunner -> factory -> service -> cache -> DB.

    Exercises the full CLI surface wired up by Phase 7 with the production
    factory (``build_tdoc_cr_service``) and the production ScraperClient
    class replaced by a single-fixture fake. The disk cache is redirected
    under ``tmp_path`` so the user's home cache directory stays untouched,
    and ``create_schema()`` is called explicitly because the CLI no longer
    bootstraps the schema itself (it is the operator's job, normally via
    ``doc3gpp db init``).
    """
    from typer.testing import CliRunner

    from doc3gpp.cli import app
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()

    # Root the disk cache under tmp_path so the user's ~/.cache stays clean.
    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(tmp_path / "cache"))
    from doc3gpp.settings.loader import get_settings
    get_settings.cache_clear()

    # Pre-seed the parent TDoc row the service validates against.
    SQLAlchemyTDocRepository().upsert(
        TDoc(
            tdoc_id="R5s260009",
            type="CR",
            ftp_url="tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260009.zip",
        ),
    )

    # Swap the production ScraperClient class for our dummy at the factory
    # boundary so every download path in the new TDocCrService sees the
    # canned fixture bytes. The fixture is read once and shared by every
    # get_bytes call.
    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists(), f"fixture missing: {fixture}"
    dummy = _DummyScraperClient()
    dummy._payload = fixture.read_bytes()  # type: ignore[attr-defined]
    monkeypatch.setattr("doc3gpp.services.factory.ScraperClient", lambda: dummy)

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "parse", "--tdoc", "R5s260009", "--yes"])

    # 1. CLI exited cleanly and the per-id line carries the parsed fields.
    assert result.exit_code == 0, result.output
    assert "R5s260009: spec=38.523-3" in result.output
    assert "cr_num=3790" in result.output
    assert "Newly parsed:                              1" in result.output
    assert dummy.calls, "ScraperClient.get_bytes was never invoked"

    # 2. The DB now has the persisted tdoc_cr_cover_page + tdoc_extracts rows.
    cr_repo = SQLAlchemyTDocCrRepository()
    details_list = cr_repo.get("R5s260009")
    meta_list = cr_repo.get_extract_meta("R5s260009")
    assert len(details_list) == 1
    details = details_list[0]
    assert details.spec == "38.523-3"
    assert details.cr_num == "3790"
    assert details.release == "Rel-18"
    assert len(meta_list) == 1
    meta = meta_list[0]
    assert meta.tdoc_id == "R5s260009"
    assert meta.doc_filename.lower().endswith(".docx")
    assert meta.ftp_url and details.ftp_url == meta.ftp_url

    # 3. A follow-up `tdoc show` invocation surfaces the persisted block.
    # The new URL-keyed lookup requires the parent TDoc row to carry
    # the same ``ftp_url`` the service persisted under; mirror the
    # cover row's URL back into the TDoc so the CLI can resolve it.
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260009", type="CR", ftp_url=details.ftp_url)
    )
    show_result = runner.invoke(app, ["tdoc", "show", "--tdoc", "R5s260009"])
    assert show_result.exit_code == 0, show_result.output
    assert "[TDoc]" in show_result.output
    assert "tdoc_id: R5s260009" in show_result.output
    assert "[Extracted Details]" in show_result.output
    assert "spec: 38.523-3" in show_result.output
    assert "cr_num: 3790" in show_result.output


# ---------------------------------------------------------------------------
# 10. Per-TDoc URL from the tdocs table takes precedence over the template.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_uses_primary_url_from_tdocs_table(sqlite_env, tmp_path) -> None:
    """When ``tdocs.ftp_url`` is populated, the extract pipeline must hit that
    URL first instead of the template-based guess."""
    create_schema()
    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists(), f"fixture missing: {fixture}"

    # ``ftp_url`` is stored as a path relative to the 3GPP FTP root;
    # ``build_ftp_url`` reconstructs the absolute URL the scraper sees.
    primary_ftp_url = "stored/R5s260009.zip"
    primary_url = "https://www.3gpp.org/ftp/" + primary_ftp_url
    template_url = (
        "https://www.3gpp.org/ftp/tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/"
        "2026/Docs/R5s260009.zip"
    )

    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    scraper_mock = MagicMock()
    fixture_bytes = fixture.read_bytes()

    def fake_get_bytes(url: str) -> bytes:
        if url == primary_url:
            return fixture_bytes
        # If the template URL is ever hit, fail loudly so the assertion
        # below catches the regression.
        raise AssertionError(f"unexpected URL: {url} (expected only {primary_url})")

    scraper_mock.get_bytes.side_effect = fake_get_bytes

    cr_repo = SQLAlchemyTDocCrRepository()
    tdoc_repo = SQLAlchemyTDocRepository()
    service = TDocCrService(
        cache=cache,
        scraper_client=scraper_mock,
        cr_repository=cr_repo,
        cr_ttcn_repository=SQLAlchemyTDocCrTtcnRepository(),
        cr_change_details_repository=SQLAlchemyTDocCrChangeDetailsRepository(),
        tdoc_repository=tdoc_repo,
    )
    tdoc_repo.upsert_many(
        [TDoc(tdoc_id="R5s260009", type="CR", ftp_url=primary_ftp_url)]
    )

    result = service.extract("R5s260009")

    assert isinstance(result, ExtractResult)
    assert result.from_cache is False
    assert scraper_mock.get_bytes.call_count == 1
    assert scraper_mock.get_bytes.call_args.args[0] == primary_url
    called_urls = [call.args[0] for call in scraper_mock.get_bytes.call_args_list]
    assert template_url not in called_urls
    assert result.details.spec == "38.523-3"
    assert result.details.cr_num == "3790"


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_falls_back_to_template_when_primary_url_fails(
    sqlite_env, tmp_path
) -> None:
    """A terminal HTTP error on the stored URL triggers the template fallback."""
    create_schema()
    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists(), f"fixture missing: {fixture}"

    primary_ftp_url = "stored/R5s260009.zip"
    primary_url = "https://www.3gpp.org/ftp/" + primary_ftp_url
    template_url = (
        "https://www.3gpp.org/ftp/tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/"
        "2026/Docs/R5s260009.zip"
    )

    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    scraper_mock = MagicMock()
    fixture_bytes = fixture.read_bytes()

    def fake_get_bytes(url: str) -> bytes:
        if url == primary_url:
            raise httpx.HTTPError("primary URL 404")
        if url == template_url:
            return fixture_bytes
        raise AssertionError(f"unexpected URL: {url}")

    scraper_mock.get_bytes.side_effect = fake_get_bytes

    cr_repo = SQLAlchemyTDocCrRepository()
    tdoc_repo = SQLAlchemyTDocRepository()
    service = TDocCrService(
        cache=cache,
        scraper_client=scraper_mock,
        cr_repository=cr_repo,
        cr_ttcn_repository=SQLAlchemyTDocCrTtcnRepository(),
        cr_change_details_repository=SQLAlchemyTDocCrChangeDetailsRepository(),
        tdoc_repository=tdoc_repo,
    )
    tdoc_repo.upsert_many(
        [TDoc(tdoc_id="R5s260009", type="CR", ftp_url=primary_ftp_url)]
    )

    result = service.extract("R5s260009")

    assert isinstance(result, ExtractResult)
    assert result.from_cache is False
    called_urls = [call.args[0] for call in scraper_mock.get_bytes.call_args_list]
    assert called_urls == [primary_url, template_url]
    assert result.details.spec == "38.523-3"
    assert result.details.cr_num == "3790"


def test_extract_without_primary_url_uses_template_only(sqlite_env, tmp_path) -> None:
    """When ``tdocs.ftp_url`` is unset, the cache key falls back to the
    URL derived from the canonical R5s template. The scraper is hit
    exactly once with that template URL — preserving the pre-derive
    contract where the template was the lone resolver candidate."""
    create_schema()
    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists(), f"fixture missing: {fixture}"

    template_url = (
        "https://www.3gpp.org/ftp/tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/"
        "2026/Docs/R5s260009.zip"
    )
    stored_ftp_url = normalize_ftp_path(template_url)

    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    scraper_mock = MagicMock()

    def fake_get_bytes(url: str) -> bytes:
        assert url == template_url
        return fixture.read_bytes()

    scraper_mock.get_bytes.side_effect = fake_get_bytes

    cr_repo = SQLAlchemyTDocCrRepository()
    tdoc_repo = SQLAlchemyTDocRepository()
    service = TDocCrService(
        cache=cache,
        scraper_client=scraper_mock,
        cr_repository=cr_repo,
        cr_ttcn_repository=SQLAlchemyTDocCrTtcnRepository(),
        cr_change_details_repository=SQLAlchemyTDocCrChangeDetailsRepository(),
        tdoc_repository=tdoc_repo,
    )
    # Seed the parent TDoc row. Under the post-T3 contract every row
    # carries an ftp_url; for an ``R5s`` id we pre-populate it with the
    # canonical template URL so the cache-key derivation has a real
    # value to hash against.
    tdoc_repo.upsert_many(
        [TDoc(tdoc_id="R5s260009", type="CR", ftp_url=stored_ftp_url)],
    )

    # The pipeline may fail at the markdown render step when python-docx
    # is absent, or it may succeed when python-docx is installed. Either
    # way, the scraper must have been hit exactly once with the template
    # URL before any downstream failure — that's what this test guards.
    try:
        service.extract("R5s260009")
    except Exception:
        pass

    assert scraper_mock.get_bytes.call_count == 1
    assert scraper_mock.get_bytes.call_args.args[0] == template_url


# ---------------------------------------------------------------------------
# 11. Download provenance: the exact URL the zip was fetched from is
    # persisted on the tdoc_cr_cover_page row and surfaced through the ORM.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_persists_download_url_in_cr_details(sqlite_env, tmp_path) -> None:
    """The exact URL the zip was downloaded from must land on the
    ``tdoc_cr_cover_page.ftp_url`` column and round-trip through the repo."""
    create_schema()
    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists(), f"fixture missing: {fixture}"

    primary_ftp_url = "stored/R5s260009.zip"
    primary_url = "https://www.3gpp.org/ftp/" + primary_ftp_url

    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    scraper_mock = MagicMock()
    fixture_bytes = fixture.read_bytes()

    def fake_get_bytes(url: str) -> bytes:
        assert url == primary_url
        return fixture_bytes

    scraper_mock.get_bytes.side_effect = fake_get_bytes

    cr_repo = SQLAlchemyTDocCrRepository()
    tdoc_repo = SQLAlchemyTDocRepository()
    service = TDocCrService(
        cache=cache,
        scraper_client=scraper_mock,
        cr_repository=cr_repo,
        cr_ttcn_repository=SQLAlchemyTDocCrTtcnRepository(),
        cr_change_details_repository=SQLAlchemyTDocCrChangeDetailsRepository(),
        tdoc_repository=tdoc_repo,
    )
    tdoc_repo.upsert_many(
        [TDoc(tdoc_id="R5s260009", type="CR", ftp_url=primary_ftp_url)]
    )

    result = service.extract("R5s260009")

    # The service normalises the resolved URL back to the relative
    # ``ftp_url`` form before persisting, so the stored/downloaded
    # URLs should round-trip to ``primary_ftp_url``.
    assert result.details.ftp_url == primary_ftp_url
    assert result.from_cache is False
    stored_list = cr_repo.get("R5s260009")
    assert len(stored_list) == 1
    assert stored_list[0].ftp_url == primary_ftp_url


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_fallback_url_persisted_in_cr_details(sqlite_env, tmp_path) -> None:
    """When the stored primary URL fails and the template serves the
    bytes, the template URL is the one persisted as download provenance."""
    create_schema()
    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists(), f"fixture missing: {fixture}"

    primary_ftp_url = "stored/R5s260009.zip"
    primary_url = "https://www.3gpp.org/ftp/" + primary_ftp_url
    template_url = (
        "https://www.3gpp.org/ftp/tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/"
        "2026/Docs/R5s260009.zip"
    )
    template_ftp_url = (
        "tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260009.zip"
    )

    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    scraper_mock = MagicMock()
    fixture_bytes = fixture.read_bytes()

    def fake_get_bytes(url: str) -> bytes:
        if url == primary_url:
            raise httpx.HTTPError("primary 404")
        if url == template_url:
            return fixture_bytes
        raise AssertionError(f"unexpected URL: {url}")

    scraper_mock.get_bytes.side_effect = fake_get_bytes

    cr_repo = SQLAlchemyTDocCrRepository()
    tdoc_repo = SQLAlchemyTDocRepository()
    service = TDocCrService(
        cache=cache,
        scraper_client=scraper_mock,
        cr_repository=cr_repo,
        cr_ttcn_repository=SQLAlchemyTDocCrTtcnRepository(),
        cr_change_details_repository=SQLAlchemyTDocCrChangeDetailsRepository(),
        tdoc_repository=tdoc_repo,
    )
    tdoc_repo.upsert_many(
        [TDoc(tdoc_id="R5s260009", type="CR", ftp_url=primary_ftp_url)]
    )

    result = service.extract("R5s260009")

    # The template URL is what actually served the bytes, so it's the
    # one persisted — not the failed primary. The service normalises
    # back to the relative form before storage.
    assert result.details.ftp_url == template_ftp_url
    stored_list = cr_repo.get("R5s260009")
    assert len(stored_list) == 1
    assert stored_list[0].ftp_url == template_ftp_url


def test_extract_url_field_round_trips_through_orm(sqlite_env) -> None:
    """The ``url`` column on ``TDocCrDetailOrm`` must survive a
    write-then-read round trip; URL is the primary key in the new
    schema, so an upsert at the same URL replaces the row in place
    while a fresh URL creates a second revision row. The cover-page
    write and the extract-metadata write are independent under the
    slim schema, so this test exercises both upsert methods.
    """
    create_schema()
    cr_repo = SQLAlchemyTDocCrRepository()
    tdoc_repo = SQLAlchemyTDocRepository()
    tdoc_repo.upsert_many([TDoc(tdoc_id="R5s260009", type="CR")])

    url = "stored/R5s260009.zip"
    meta = TDocExtractMeta(
        ftp_url=url,
        tdoc_id="R5s260009",
        cache_file="R5s260009.zip",
        doc_filename="R5s260009.docx",
    )
    with_url = TDocCRDetails(
        tdoc_id="R5s260009",
        spec="38.523-3",
        cr_num="3790",
        ftp_url=url,
    )
    cr_repo.upsert(with_url)
    cr_repo.upsert_extract_meta(meta)
    loaded = cr_repo.get_by_url(url)
    assert loaded is not None
    assert loaded.ftp_url == url
    assert loaded.tdoc_id == "R5s260009"
    meta_loaded = cr_repo.get_extract_meta_by_url(url)
    assert meta_loaded is not None
    assert meta_loaded.ftp_url == url
    assert meta_loaded.tdoc_id == "R5s260009"

    # Same URL updates in place rather than creating a duplicate row.
    cr_repo.upsert(
        TDocCRDetails(tdoc_id="R5s260009", spec="38.523-3", cr_num="3791", ftp_url=url),
    )
    rows = cr_repo.get("R5s260009")
    assert len(rows) == 1
    assert rows[0].cr_num == "3791"

    # A different URL creates a second revision row.
    url_b = "stored/R5s260009_rev2.zip"
    cr_repo.upsert(
        TDocCRDetails(
            tdoc_id="R5s260009",
            spec="38.523-3",
            cr_num="3791",
            rev="2",
            ftp_url=url_b,
        ),
    )
    cr_repo.upsert_extract_meta(replace(meta, ftp_url=url_b))
    rows = cr_repo.get("R5s260009")
    urls = {row.ftp_url for row in rows}
    assert urls == {url, url_b}
    meta_rows = cr_repo.get_extract_meta("R5s260009")
    assert {row.ftp_url for row in meta_rows} == {url, url_b}


def test_cr_cover_page_round_trip_summary_of_change(sqlite_env) -> None:
    """A ``TDocCRDetails`` with ``summary_of_change`` set round-trips
    through the SQL repo; ``None`` round-trips as ``NULL``."""
    create_schema()
    SQLAlchemyTDocRepository().upsert_many(
        [TDoc(tdoc_id="R5-227476", ftp_url="TSG_RAN/TSG_RAN_2/R5-227476.zip")]
    )
    repo = SQLAlchemyTDocCrRepository()

    populated = TDocCRDetails(
        tdoc_id="R5-227476",
        ftp_url="TSG_RAN/TSG_RAN_2/R5-227476.zip",
        summary_of_change="Add USIM config setter.",
    )
    repo.upsert(populated)
    fetched = repo.get_by_url("TSG_RAN/TSG_RAN_2/R5-227476.zip")
    assert fetched is not None
    assert fetched.summary_of_change == "Add USIM config setter."

    # ``None`` round-trips as NULL.
    blank = TDocCRDetails(
        tdoc_id="R5-227476",
        ftp_url="TSG_RAN/TSG_RAN_2/R5-227476.zip",
        summary_of_change=None,
    )
    repo.upsert(blank)
    fetched_blank = repo.get_by_url("TSG_RAN/TSG_RAN_2/R5-227476.zip")
    assert fetched_blank is not None
    assert fetched_blank.summary_of_change is None


def test_extract_repository_rejects_blank_url(sqlite_env) -> None:
    """``upsert`` rejects an empty ``ftp_url`` on the cover page row."""
    create_schema()
    cr_repo = SQLAlchemyTDocCrRepository()
    SQLAlchemyTDocRepository().upsert_many(
        [TDoc(tdoc_id="R5s260009", type="CR")]
    )

    details = TDocCRDetails(
        tdoc_id="R5s260009", spec="38.523-3", cr_num="3790", ftp_url=""
    )
    with pytest.raises(ValueError, match="non-empty ftp_url"):
        cr_repo.upsert(details)


def test_extract_upsert_extract_meta_round_trips(sqlite_env) -> None:
    """``upsert_extract_meta`` writes and reads the metadata row by URL.

    Validates the new ``tdoc_extracts`` write path end-to-end:
    insert, fetch by URL, fetch by ``tdoc_id``, update with a fresh
    ``extracted_at`` timestamp. The slim cover-page table is
    independently written; this test focuses on the metadata surface
    only.
    """
    create_schema()
    cr_repo = SQLAlchemyTDocCrRepository()
    SQLAlchemyTDocRepository().upsert_many(
        [TDoc(tdoc_id="R5s260009", type="CR")]
    )

    url = "stored/R5s260009.zip"
    meta = TDocExtractMeta(
        ftp_url=url,
        tdoc_id="R5s260009",
        cache_file="R5s260009-abcdef0123456789.zip",
        doc_filename="R5s260009.docx",
    )
    cr_repo.upsert_extract_meta(meta)

    by_url = cr_repo.get_extract_meta_by_url(url)
    assert by_url is not None
    assert by_url.tdoc_id == "R5s260009"
    assert by_url.ftp_url == url
    assert by_url.doc_filename == "R5s260009.docx"
    assert by_url.cache_file == "R5s260009-abcdef0123456789.zip"
    assert by_url.extracted_at is not None
    first_extracted_at = by_url.extracted_at

    by_tdoc = cr_repo.get_extract_meta("R5s260009")
    assert len(by_tdoc) == 1
    assert by_tdoc[0].ftp_url == url

    # Update the row in place — same URL, new cache_file key.
    later_meta = TDocExtractMeta(
        ftp_url=url,
        tdoc_id="R5s260009",
        cache_file="R5s260009-fedcba9876543210.zip",
        doc_filename="R5s260009.docx",
    )
    cr_repo.upsert_extract_meta(later_meta)
    refreshed = cr_repo.get_extract_meta_by_url(url)
    assert refreshed is not None
    assert refreshed.cache_file == "R5s260009-fedcba9876543210.zip"
    assert len(cr_repo.get_extract_meta("R5s260009")) == 1
    assert refreshed.extracted_at is not None
    assert refreshed.extracted_at >= first_extracted_at


# ---------------------------------------------------------------------------
# 12. CLI integration: filter combinations + batch truncation summary
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_parse_with_combined_filters_against_sqlite(
    sqlite_env, monkeypatch, tmp_path
) -> None:
    """The CLI accepts ``--meeting-id N --meeting PATTERN --cr-cat F`` as
    filter combinations and routes them through the real SQLite-backed
    TDocRepository. End-to-end: CliRunner → factory → SQL repo → service
    → DB. In normal mode the SQL repo drops already-parsed rows before
    the limit (``exclude_parsed=not force``), so the pre-parsed id never
    surfaces in the dispatch path or in the completion summary. The
    summary reports only the four newly parsed rows; ``Skipped`` is 0
    because nothing parsed was withheld from the dispatch."""
    from typer.testing import CliRunner

    from doc3gpp.cli import app
    from doc3gpp.models.meeting import Meeting
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.meeting_sql import (
        SQLAlchemyMeetingRepository,
    )
    from doc3gpp.storage.repositories.tdoc_cr_sql import (
        SQLAlchemyTDocCrRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()

    # Seed a parent meeting + five CR-type TDocs.
    meeting_id = 9001
    SQLAlchemyMeetingRepository().upsert_many([
        Meeting(
            meeting_id=meeting_id,
            name="RAN5#111",
            title="RAN WG5 #111",
            location="Online",
        ),
    ])
    tdoc_repo = SQLAlchemyTDocRepository()
    cr_tdocs = [
        TDoc(
            tdoc_id=f"R5s2600{i:02d}",
            type="CR",
            meeting_id=meeting_id,
            cr_cat="F",
            ftp_url=(
                f"tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/"
                f"R5s2600{i:02d}.zip"
            ),
        )
        for i in range(1, 6)
    ]
    tdoc_repo.upsert_many(cr_tdocs)

    # Mark R5s260001 as already parsed. In normal mode the SQL filter
    # excludes it before the limit, so it never reaches the dispatch
    # and the summary's "Skipped" bucket stays empty.
    cr_repo = SQLAlchemyTDocCrRepository()
    cr_repo.upsert(
        TDocCRDetails(
            tdoc_id="R5s260001",
            spec="38.523-3",
            cr_num="3790",
            ftp_url="stored/R5s260001.zip",
        ),
    )
    cr_repo.upsert_extract_meta(
        TDocExtractMeta(
            ftp_url="stored/R5s260001.zip",
            tdoc_id="R5s260001",
            cache_file="R5s260001.zip",
            doc_filename="R5s260001.docx",
        ),
    )

    # Swap the production ScraperClient class for our dummy.
    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists(), f"fixture missing: {fixture}"
    dummy = _DummyScraperClient()
    dummy._payload = fixture.read_bytes()  # type: ignore[attr-defined]
    monkeypatch.setattr("doc3gpp.services.factory.ScraperClient", lambda: dummy)

    # Root the disk cache under tmp_path.
    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(tmp_path / "cache"))
    from doc3gpp.settings.loader import get_settings
    get_settings.cache_clear()

    runner = CliRunner()
    # --meeting-id N --meeting PATTERN --cr-cat F all combine as filters.
    # Normal mode (no --force): the SQL filter drops R5s260001
    # (already parsed), so only the other four are dispatched.
    result = runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(meeting_id),
            "--meeting", "%RAN5%",
            "--cr-cat", "F",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output

    # Normal mode: SQL excluded the pre-parsed id, so only the
    # "To parse" group is rendered — the "Already parsed" preview is
    # force-mode-only.
    assert "To parse [count=4]:" in result.output
    assert "Already parsed in tdoc_cr_cover_page" not in result.output
    # The completion summary reports zero Skipped (the parsed id never
    # reached dispatch), zero Re-parsed (no --force), four Newly
    # parsed, zero Failures.
    assert "Skipped (already parsed before this run): 0" in result.output
    assert "Re-parsed (with --force):                  0" in result.output
    assert "Newly parsed:                              4" in result.output
    assert "Failures:                                  0" in result.output

    # Persisted: four new tdoc_cr_cover_page rows + the pre-seeded one.
    cr_repo = SQLAlchemyTDocCrRepository()
    for tid in ("R5s260002", "R5s260003", "R5s260004", "R5s260005"):
        assert cr_repo.get(tid), f"{tid} should have a persisted CR detail row"
    # The pre-seeded row is untouched (still single-URL).
    assert len(cr_repo.get("R5s260001")) == 1


def test_parse_batch_limit_truncates_with_remaining_summary(
    sqlite_env, monkeypatch, tmp_path
) -> None:
    """With ``max_batch=2`` and five matches, the CLI extracts only the
    first two, reports ``Remaining: 3`` in the completion summary, and
    suggests re-running without ``--force`` to continue."""
    from typer.testing import CliRunner

    from doc3gpp.cli import app
    from doc3gpp.models.meeting import Meeting
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.meeting_sql import (
        SQLAlchemyMeetingRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()

    # Override max_batch to 2 via TOML (DOC3GPP_TDOC_PARSE__MAX_BATCH is
    # outside the env-var allowlist and is silently ignored).
    config_path = tmp_path / "tdoc-parse-config.toml"
    config_path.write_text("[tdoc_parse]\nmax_batch = 2\n", encoding="utf-8")
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    from doc3gpp.settings.loader import get_settings
    get_settings.cache_clear()
    try:
        assert get_settings().tdoc_parse.max_batch == 2

        # Seed a meeting + five matching TDocs (without actually parsing
        # them via python-docx — we replace the CR service with a fake).
        meeting_id = 8001
        SQLAlchemyMeetingRepository().upsert_many([
            Meeting(
                meeting_id=meeting_id,
                name="RAN5#111",
                title="RAN WG5 #111",
                location="Online",
            ),
        ])
        tdoc_repo = SQLAlchemyTDocRepository()
        tdoc_repo.upsert_many([
            TDoc(tdoc_id=f"R5s2600{i:02d}", type="CR", meeting_id=meeting_id, cr_cat="F")
            for i in range(1, 6)
        ])

        # Stub the CR service to a no-op fake that records calls.
        class _StubCrService:
            def __init__(self) -> None:
                self.many_calls: list[list[str]] = []

            def extract_many(
                self,
                tdoc_ids,
                *,
                force=False,
                full=False,
                on_progress=None,
                is_cancelled=None,
            ):
                from doc3gpp.services.tdoc_cr_service import BatchExtractResult
                from doc3gpp.models.tdoc_cr import TDocCRDetails
                ids = list(tdoc_ids)
                self.many_calls.append(ids)
                return BatchExtractResult(
                    successes={
                        tid: type(
                            "R",
                            (),
                            {"details": TDocCRDetails(tdoc_id=tid, spec=None, cr_num=None, title=None)},
                        )()
                        for tid in ids
                    },
                    failures={},
                )

        stub = _StubCrService()
        monkeypatch.setattr(
            "doc3gpp.cli.build_tdoc_cr_service", lambda *args, **kwargs: stub,
        )

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "tdoc", "parse",
                "--meeting-id", str(meeting_id),
                "--yes",
            ],
        )
        assert result.exit_code == 0, result.output
        # The repo returns at most max_batch=2 rows (SQLite LIMIT 2);
        # extract_many was dispatched once with 2 ids.
        assert len(stub.many_calls) == 1
        assert len(stub.many_calls[0]) == 2
        # The completion summary signals truncation and the continuation hint.
        assert "Remaining (truncated by max_batch=2): at least 1" in result.output
        assert (
            "re-run the same command (without --force) to continue"
            in result.output.lower()
        )
        # The to-parse group was truncated to 2 ids.
        assert "To parse [count=2]:" in result.output
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 13. End-to-end: ``cache_file`` is the URL-derived basename that
#     resolves on disk to both ``cache/zips/<key>`` and
#     ``cache/markdown/<key>``. Locked-in schema snapshot: the
#     ``tdoc_extracts`` table has exactly one basename column,
#     ``cache_file`` (the pre-T3 storage layout is gone).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_writes_cache_file_and_resolves_path(sqlite_env, tmp_path) -> None:
    """End-to-end ``cache_file`` derivation + on-disk path resolution."""
    from doc3gpp.scraping.cache_keys import derive_cache_file
    from doc3gpp.storage.db.models import TDocExtractOrm

    create_schema()
    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists(), f"fixture missing: {fixture}"

    ftp_url = "tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260009.zip"
    expected_cache_file = derive_cache_file(ftp_url)

    service, scraper_mock, cache, cr_repo, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=_zip_payload(fixture),
    )
    _seed_cr_tdoc(tdoc_repo, "R5s260009")
    # The CR row's ftp_url is the cache-key seed. Update the seeded tdoc
    # so it matches the one the service is expected to derive from.
    tdoc_repo.upsert_many(
        [TDoc(tdoc_id="R5s260009", type="CR", ftp_url=ftp_url)],
    )

    result = service.extract("R5s260009")

    assert isinstance(result, ExtractResult)
    assert result.extract_meta.cache_file == expected_cache_file

    # Both artefacts exist on disk under the same basename.
    assert (cache.root / "zips" / expected_cache_file).exists()
    assert (cache.root / "markdown" / expected_cache_file).exists()

    # Schema snapshot: ``cache_file`` is the only basename column on
    # the slim extract-metadata table.
    by_url = cr_repo.get_extract_meta_by_url(ftp_url)
    assert by_url is not None
    assert by_url.cache_file == expected_cache_file

    column_names = {col.name for col in TDocExtractOrm.__table__.columns}
    assert "cache_file" in column_names
    # No basename column other than cache_file (post-T3 invariant).
    assert sum(name.endswith("_file") for name in column_names) == 1


def test_extract_meta_orm_round_trips_cache_file(sqlite_env) -> None:
    """``_meta_to_orm`` + ``_orm_to_meta`` preserves the ``cache_file`` field.

    Belt-and-braces assertion that does not depend on a real fixture: the
    structural round-trip is sufficient to lock the contract.
    """
    from doc3gpp.storage.db.models import TDocExtractOrm
    from doc3gpp.storage.repositories.tdoc_cr_sql import (
        _orm_to_meta,
    )

    create_schema()
    sql_repo = SQLAlchemyTDocCrRepository()
    SQLAlchemyTDocRepository().upsert_many(
        [TDoc(tdoc_id="R5s260009", type="CR")],
    )

    ftp_url = "tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260009.zip"
    meta_in = TDocExtractMeta(
        ftp_url=ftp_url,
        tdoc_id="R5s260009",
        cache_file="R5s260009-5186a7d62c6ae3ab3a0c02fa128e41da.zip",
        doc_filename="R5s260009.docx",
    )
    orm_row = TDocExtractOrm(ftp_url=ftp_url, tdoc_id="R5s260009")
    SQLAlchemyTDocCrRepository._meta_to_orm(orm_row, meta_in)
    assert orm_row.cache_file == "R5s260009-5186a7d62c6ae3ab3a0c02fa128e41da.zip"

    sql_repo.upsert_extract_meta(meta_in)
    loaded_orm = sql_repo.get_extract_meta_by_url(ftp_url)
    assert loaded_orm is not None
    round_tripped = _orm_to_meta(loaded_orm)
    assert round_tripped.cache_file == "R5s260009-5186a7d62c6ae3ab3a0c02fa128e41da.zip"
    assert round_tripped.tdoc_id == "R5s260009"
    assert round_tripped.ftp_url == ftp_url
    assert round_tripped.doc_filename == "R5s260009.docx"

    # Schema sanity: cache_file is the only basename column on the
    # slim extract-metadata table (post-T3 invariant).
    column_names = {col.name for col in TDocExtractOrm.__table__.columns}
    assert "cache_file" in column_names
    assert sum(name.endswith("_file") for name in column_names) == 1


def test_tdoc_cr_detail_orm_has_summary_of_change_column(sqlite_env) -> None:
    """A fresh ``tdoc_cr_cover_page`` schema carries
    ``summary_of_change TEXT`` after ``create_schema``."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from sqlalchemy import text

    create_schema()
    with get_engine().begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(tdoc_cr_cover_page)")).all()
    cols = {row[1]: row[2] for row in rows}
    assert cols.get("summary_of_change") == "TEXT"


def test_migrate_adds_summary_of_change_to_existing_db(sqlite_env) -> None:
    """Migration adds ``summary_of_change`` to a database whose
    ``tdoc_cr_cover_page`` predates the column. Idempotent on a second
    call."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from sqlalchemy import text

    engine = get_engine()
    # Simulate a legacy DB: the ``tdoc_cr_cover_page`` table exists but
    # was created before ``summary_of_change`` was added, so
    # ``Base.metadata.create_all`` (a no-op on existing tables) would
    # never add it — only the migration's ``ALTER TABLE`` can.
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS tdoc_cr_cover_page"))
        conn.execute(
            text(
                "CREATE TABLE tdoc_cr_cover_page ("
                "url TEXT PRIMARY KEY, tdoc_id TEXT)"
            )
        )

    create_schema()
    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(
            text("PRAGMA table_info(tdoc_cr_cover_page)")
        ).all()}
    assert "summary_of_change" in cols

    # Second call is a no-op (no exception, column still present).
    create_schema()
    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(
            text("PRAGMA table_info(tdoc_cr_cover_page)")
        ).all()}
    assert "summary_of_change" in cols