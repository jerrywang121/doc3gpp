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
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.tdoc import TDoc
from doc3gpp.scraping.cache import TDocCache
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository


FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tdoc_cr_doc"


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

    def __enter__(self) -> "_DummyScraperClient":
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
    """3GPP URL + ``tdoc_id ∈ tdocs`` writes both DB rows + cache files."""
    create_schema()

    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists(), f"fixture missing: {fixture}"

    # Root the cache under tmp_path.
    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(tmp_path / "cache"))
    from doc3gpp.settings.loader import get_settings
    get_settings.cache_clear()

    # Pre-seed the parent TDoc row.
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

    # Stub the scraper with the local fixture.
    dummy = _DummyScraperClient()
    dummy.payloads["https://www.3gpp.org/ftp/.../R5s260009.zip"] = fixture.read_bytes()
    monkeypatch.setattr("doc3gpp.services.factory.ScraperClient", lambda: dummy)

    runner = CliRunner()
    result = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-url", "https://www.3gpp.org/ftp/.../R5s260009.zip",
        ],
    )
    assert result.exit_code == 0, result.output

    cr_repo = SQLAlchemyTDocCrRepository()
    details_list = cr_repo.get("R5s260009")
    meta_list = cr_repo.get_extract_meta("R5s260009")
    assert len(details_list) == 1
    assert details_list[0].spec == "38.523-3"
    assert len(meta_list) == 1
    meta = meta_list[0]
    # The zip + markdown are cached under the cache_file key (post-T3).
    from doc3gpp.scraping.cache import TDocCache
    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    assert (cache.root / "zips" / meta.cache_file).exists()
    assert (cache.root / "markdown" / meta.cache_file).exists()
    assert meta.cache_file.endswith(".zip")


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
# 3. 3GPP URL + filename with no tdoc_id pattern: warning + no DB.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_direct_3gpp_url_no_pattern_in_filename_warns(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """A 3GPP URL whose filename has no tdoc_id pattern: warning + no rows."""
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
    assert result.exit_code == 0, result.output

    combined = (result.stdout or "") + (result.stderr or "")
    assert "does not match the 3GPP TDoc id" in combined
    assert "meeting_minutes.zip" in combined

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
def test_direct_from_local_file_emits_output_without_db(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """``--from-path`` produces output and never touches the cache or DB."""
    create_schema()

    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists()

    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(tmp_path / "cache"))
    from doc3gpp.settings.loader import get_settings
    get_settings.cache_clear()

    runner = CliRunner()
    result = runner.invoke(
        app, [
            "tdoc", "parse",
            "--from-path", str(fixture),
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

    assert "R5s260009" in (result.stdout or "")


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
    # Two zip cache files, one markdown file (same content but distinct
    # extract runs both write through).
    assert snapshot.zips == 2
    # Verify the keys are URL-derived (post-T3) — each ftp_url yields a
    # distinct <stem>-<md5>.zip key. The legacy filename is still the
    # leading component of the cached key.
    zip_names = sorted(p.name for p in (tmp_path / "cache" / "zips").iterdir())
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
