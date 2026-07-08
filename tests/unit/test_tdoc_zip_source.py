"""Unit tests for :mod:`doc3gpp.scraping.tdoc_zip_source`.

The tests target the Protocol-shaped ``TDocCacheLike`` contract defined
in this module, so they work against either the real ``TDocCache`` (in
``doc3gpp.scraping.cache``) or a small in-memory stub.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import httpx
import pytest

from doc3gpp.scraping.client import ScraperClient
from doc3gpp.scraping.tdoc_zip_source import (
    TDocZipDownloadError,
    canonicalise_tdoc_id,
    download_tdoc_zip,
    get_tdoc_zip_url,
    tsg_meeting_year_for,
)

CacheSubdir = Literal["zips", "markdown"]


# ---------------------------------------------------------------------------
# Stub TDocCache — used until Phase 1 ships src/doc3gpp/scraping/cache.py.
# Matches the Protocol documented on ``TDocCacheLike``.
# ---------------------------------------------------------------------------


class _StubTDocCache:
    """Minimal ``TDocCache`` replacement for the Phase 2 test suite.

    Keys are stored in a flat dict keyed by ``(key.lower(), subdir)`` and
    materialised on disk under ``root / subdir / <safe-key>.bin`` so the
    returned ``path_for`` points at a real file path.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._store: dict[tuple[str, str], bytes] = {}
        for sub in ("zips", "markdown"):
            (self._root / sub).mkdir(parents=True, exist_ok=True)

    def put_bytes(self, key: str, payload: bytes, subdir: CacheSubdir) -> Path:
        if not payload:
            raise ValueError("refusing to cache zero-byte payload")
        path = self.path_for(key, subdir)
        path.write_bytes(payload)
        self._store[(key.lower(), subdir)] = payload
        return path

    def get_bytes(self, key: str, subdir: CacheSubdir) -> bytes | None:
        return self._store.get((key.lower(), subdir))

    def path_for(self, key: str, subdir: CacheSubdir) -> Path:
        safe = key.lower()
        return self._root / subdir / f"{safe}.bin"


# ---------------------------------------------------------------------------
# Fake ScraperClient — exposes ``get_bytes`` as a plain callable slot.
# ---------------------------------------------------------------------------


class _FakeScraperClient:
    """Test double matching the ``ScraperClient.get_bytes(url) -> bytes`` shape.

    Supports two configuration modes:

    * Global: ``payload`` is returned for every URL, or ``error`` is
      raised for every URL. Used by the existing single-URL tests.
    * Per-URL: ``url_payloads`` / ``url_errors`` map specific URLs to
      their response. Used by the primary-url fallback tests so the
      same client can serve one URL and 404 on another.
    """

    def __init__(
        self,
        payload: bytes | None = None,
        error: Exception | None = None,
        url_payloads: dict[str, bytes] | None = None,
        url_errors: dict[str, Exception] | None = None,
    ) -> None:
        self.payload = payload
        self.error = error
        self.url_payloads = url_payloads or {}
        self.url_errors = url_errors or {}
        self.calls: list[str] = []

    def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        if url in self.url_errors:
            raise self.url_errors[url]
        if url in self.url_payloads:
            return self.url_payloads[url]
        if self.error is not None:
            raise self.error
        assert self.payload is not None, "fake client not configured"
        return self.payload


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cache(tmp_path: Path) -> _StubTDocCache:
    return _StubTDocCache(tmp_path)


@pytest.fixture
def fake_client() -> _FakeScraperClient:
    return _FakeScraperClient()


# ---------------------------------------------------------------------------
# 1. tsg_meeting_year_for matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tdoc", "expected"),
    [
        ("R5s260009", ("R5", 2026)),
        ("R5s260176", ("R5", 2026)),
        ("R5w260009", ("R5", 2026)),
        ("R5-227476", ("R5", 2022)),
        ("C6-250028", ("C6", 2025)),
        ("r5s260009", ("R5", 2026)),
        ("", ("", None)),
        ("bogus", ("", None)),
    ],
)
def test_tsg_meeting_year_for_matrix(tdoc: str, expected: tuple[str, int | None]) -> None:
    assert tsg_meeting_year_for(tdoc) == expected


# ---------------------------------------------------------------------------
# 2. get_tdoc_zip_url matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tdoc", "expected_url"),
    [
        # Known-good R5s shapes — TTCN email CRs.
        (
            "R5s260009",
            "https://www.3gpp.org/ftp/tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260009.zip",
        ),
        (
            "R5s260051",
            "https://www.3gpp.org/ftp/tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260051.zip",
        ),
        (
            "R5s260135",
            "https://www.3gpp.org/ftp/tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260135.zip",
        ),
        (
            "R5s260176",
            "https://www.3gpp.org/ftp/tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260176.zip",
        ),
        # R5w workshop shape — same TSG, different template.
        (
            "R5w260009",
            "https://www.3gpp.org/ftp/tsg_ran/WG5_Test_ex-T1/Workshop/TSGR5_Workshop_2026/Docs/R5w260009.zip",
        ),
        # Lower-case input must resolve to the same URL as the upper-case form.
        (
            "r5s260009",
            "https://www.3gpp.org/ftp/tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260009.zip",
        ),
        # R5- / C6- shapes deferred to Phase 8 — return None, do not raise.
        ("R5-227476", None),
        ("C6-250028", None),
    ],
)
def test_get_tdoc_zip_url_matrix(tdoc: str, expected_url: str | None) -> None:
    assert get_tdoc_zip_url(tdoc) == expected_url


# ---------------------------------------------------------------------------
# 3. download_tdoc_zip cache-hit path skips the network.
# ---------------------------------------------------------------------------


def test_download_tdoc_zip_cache_hit_skips_network(
    cache: _StubTDocCache, fake_client: _FakeScraperClient
) -> None:
    """Pre-populated cache must satisfy the request without touching the client."""
    payload = b"PK\x03\x04already-cached"
    cache.put_bytes("R5s260009", payload, "zips")

    # Wire a client whose get_bytes raises — if it is called, the test fails.
    fake_client.payload = None
    fake_client.error = AssertionError("network should not be called on a cache hit")

    result = download_tdoc_zip("R5s260009", fake_client, cache)

    assert result.path == cache.path_for("r5s260009", "zips")
    assert fake_client.calls == []
    assert cache.get_bytes("R5s260009", "zips") == payload


# ---------------------------------------------------------------------------
# 4. download_tdoc_zip cache-miss path fetches once and writes through.
# ---------------------------------------------------------------------------


def test_download_tdoc_zip_cache_miss_writes_through(
    cache: _StubTDocCache, fake_client: _FakeScraperClient
) -> None:
    """Empty cache triggers one ``get_bytes`` call and stages the payload."""
    payload = b"zip-bytes"
    fake_client.payload = payload

    result = download_tdoc_zip("R5s260009", fake_client, cache)

    expected_url = get_tdoc_zip_url("R5s260009")
    assert expected_url is not None
    assert fake_client.calls == [expected_url]
    assert result.path == cache.path_for("r5s260009", "zips")
    assert cache.get_bytes("R5s260009", "zips") == payload
    # File is actually on disk too — guards against future stubs that forget to write.
    assert result.path.read_bytes() == payload


# ---------------------------------------------------------------------------
# 5. download_tdoc_zip wraps httpx.HTTPError as TDocZipDownloadError.
# ---------------------------------------------------------------------------


def test_download_tdoc_zip_wraps_httpx_error(
    cache: _StubTDocCache, fake_client: _FakeScraperClient
) -> None:
    """Terminal HTTP errors must surface as TDocZipDownloadError, not bare httpx."""
    fake_client.payload = None
    fake_client.error = httpx.ConnectError("nope")

    with pytest.raises(TDocZipDownloadError) as excinfo:
        download_tdoc_zip("R5s260009", fake_client, cache)

    err = excinfo.value
    expected_url = get_tdoc_zip_url("R5s260009")
    assert err.url == expected_url
    assert isinstance(err.original, httpx.ConnectError)
    assert "nope" in str(err.original)
    # Network failure must not leave a half-written cache entry.
    assert cache.get_bytes("R5s260009", "zips") is None
    assert not cache.path_for("r5s260009", "zips").exists()


# ---------------------------------------------------------------------------
# 6. download_tdoc_zip rejects bad-shape ids without touching cache or network.
# ---------------------------------------------------------------------------


def test_download_tdoc_zip_rejects_bad_shape(
    cache: _StubTDocCache, fake_client: _FakeScraperClient
) -> None:
    """A garbage id must raise ``ValueError`` immediately."""
    fake_client.payload = None
    fake_client.error = AssertionError("network should not be called for a bad id")

    with pytest.raises(ValueError):
        download_tdoc_zip("not-a-tdoc", fake_client, cache)

    assert fake_client.calls == []
    # Nothing should have been written to the cache.
    assert not any(cache._root.glob("zips/*"))  # noqa: SLF001 — test-only access


# ---------------------------------------------------------------------------
# 7. Invalid TDoc format returns None from get_tdoc_zip_url.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tdoc", ["", "bogus", "R5", "R5s", "R5-12", "C6-99"])
def test_get_tdoc_zip_url_invalid_returns_none(tdoc: str) -> None:
    assert get_tdoc_zip_url(tdoc) is None


# ---------------------------------------------------------------------------
# Cross-check: the type annotation matches the real ScraperClient surface.
# ---------------------------------------------------------------------------


def test_scraper_client_get_bytes_signature_matches() -> None:
    """The annotation we declare must be a method on the real ``ScraperClient``."""
    assert hasattr(ScraperClient, "get_bytes")
    # Sanity check the import works — guards against a typo'd ``TYPE_CHECKING`` block.
    assert ScraperClient is not None


# ---------------------------------------------------------------------------
# 8. download_tdoc_zip primary_url: per-TDoc URL from the tdocs table is
# preferred over the template-based guess, with template fallback on error.
# ---------------------------------------------------------------------------


def test_download_tdoc_zip_uses_primary_url_when_present(
    cache: _StubTDocCache,
) -> None:
    """When ``primary_url`` is provided and the fetch succeeds, the template
    URL must never be tried."""
    payload = b"zip-bytes"
    primary = "https://www.3gpp.org/ftp/meeting/R5s260009.zip"
    client = _FakeScraperClient(url_payloads={primary: payload})

    result = download_tdoc_zip("R5s260009", client, cache, primary_url=primary)

    assert client.calls == [primary]
    assert result.path == cache.path_for("r5s260009", "zips")
    assert cache.get_bytes("R5s260009", "zips") == payload


def test_download_tdoc_zip_falls_back_to_template_on_primary_error(
    cache: _StubTDocCache,
) -> None:
    """A terminal HTTP error on ``primary_url`` must trigger the template
    fallback, not propagate the primary error."""
    primary = "https://www.3gpp.org/ftp/meeting/R5s260009.zip"
    template = get_tdoc_zip_url("R5s260009")
    assert template is not None
    assert primary != template  # dedup assertion only meaningful if they differ
    client = _FakeScraperClient(
        url_errors={primary: httpx.ConnectError("primary down")},
        url_payloads={template: b"template-zip"},
    )

    result = download_tdoc_zip("R5s260009", client, cache, primary_url=primary)

    assert client.calls == [primary, template]
    assert result.path == cache.path_for("r5s260009", "zips")
    assert cache.get_bytes("R5s260009", "zips") == b"template-zip"


def test_download_tdoc_zip_without_primary_url_uses_template(
    cache: _StubTDocCache,
) -> None:
    """Passing ``primary_url=None`` (or omitting it) must preserve the
    original template-only behaviour."""
    template = get_tdoc_zip_url("R5s260009")
    assert template is not None
    client = _FakeScraperClient(url_payloads={template: b"zip"})

    result = download_tdoc_zip("R5s260009", client, cache)

    assert client.calls == [template]
    assert result.path == cache.path_for("r5s260009", "zips")


def test_download_tdoc_zip_both_urls_fail_raises_last_error(
    cache: _StubTDocCache,
) -> None:
    """When every candidate URL fails, the last error (from the template)
    is what surfaces, wrapped in :class:`TDocZipDownloadError`."""
    primary = "https://www.3gpp.org/ftp/meeting/R5s260009.zip"
    template = get_tdoc_zip_url("R5s260009")
    assert template is not None
    client = _FakeScraperClient(
        url_errors={
            primary: httpx.ConnectError("primary down"),
            template: httpx.ConnectError("template down"),
        },
    )

    with pytest.raises(TDocZipDownloadError) as excinfo:
        download_tdoc_zip("R5s260009", client, cache, primary_url=primary)

    assert excinfo.value.url == template
    assert isinstance(excinfo.value.original, httpx.ConnectError)
    assert client.calls == [primary, template]
    # No half-written cache entry on full failure.
    assert cache.get_bytes("R5s260009", "zips") is None


def test_download_tdoc_zip_dedupes_when_primary_matches_template(
    cache: _StubTDocCache,
) -> None:
    """If the stored ``primary_url`` happens to equal the template URL
    (common for R5s/R5w rows whose XLSX hyperlink points at the same
    location the template would guess), only one fetch is made."""
    template = get_tdoc_zip_url("R5s260009")
    assert template is not None
    client = _FakeScraperClient(url_payloads={template: b"zip"})

    result = download_tdoc_zip("R5s260009", client, cache, primary_url=template)

    assert client.calls == [template]
    assert result.path == cache.path_for("r5s260009", "zips")


def test_download_tdoc_zip_primary_url_with_no_template_falls_back_to_error(
    cache: _StubTDocCache,
) -> None:
    """R5- / C6- shapes have no template (``get_tdoc_zip_url`` returns
    ``None``); when the stored ``primary_url`` also fails, the only
    available error is the primary one."""
    primary = "https://www.3gpp.org/ftp/meeting/R5-227476.zip"
    assert get_tdoc_zip_url("R5-227476") is None  # confirms no template
    client = _FakeScraperClient(
        url_errors={primary: httpx.HTTPError("404")},
    )

    with pytest.raises(TDocZipDownloadError) as excinfo:
        download_tdoc_zip("R5-227476", client, cache, primary_url=primary)

    assert excinfo.value.url == primary
    assert client.calls == [primary]


# ---------------------------------------------------------------------------
# 9. DownloadedZip: the URL field records the exact candidate that
# supplied the bytes (or None on a cache hit, when the originating URL
# is not tracked).
# ---------------------------------------------------------------------------


def test_download_tdoc_zip_returns_primary_url_on_fresh_download(
    cache: _StubTDocCache,
) -> None:
    """On a fresh download, the returned ``DownloadedZip.url`` must match
    the primary candidate that actually served the bytes."""
    primary = "https://www.3gpp.org/ftp/stored/R5s260009.zip"
    client = _FakeScraperClient(url_payloads={primary: b"zip"})

    result = download_tdoc_zip("R5s260009", client, cache, primary_url=primary)

    assert result.url == primary
    assert result.path == cache.path_for("r5s260009", "zips")


def test_download_tdoc_zip_returns_template_url_when_primary_fails(
    cache: _StubTDocCache,
) -> None:
    """When the primary URL fails, the returned ``url`` must be the
    template URL that actually served the bytes — not the failed primary."""
    primary = "https://www.3gpp.org/ftp/stored/R5s260009.zip"
    template = get_tdoc_zip_url("R5s260009")
    assert template is not None and primary != template
    client = _FakeScraperClient(
        url_errors={primary: httpx.HTTPError("404")},
        url_payloads={template: b"zip"},
    )

    result = download_tdoc_zip("R5s260009", client, cache, primary_url=primary)

    assert result.url == template
    assert result.path == cache.path_for("r5s260009", "zips")


def test_download_tdoc_zip_url_is_none_on_cache_hit(
    cache: _StubTDocCache,
) -> None:
    """A pre-populated cache must return ``url=None`` — the URL that
    populated the cache in an earlier call is not tracked here."""
    cache.put_bytes("R5s260009", b"cached", "zips")
    # Client would raise if invoked; the assertion proves the cache
    # short-circuit never touched the network.
    client = _FakeScraperClient(error=AssertionError("network on cache hit"))

    result = download_tdoc_zip("R5s260009", client, cache)

    assert result.url is None
    assert result.path == cache.path_for("r5s260009", "zips")
    assert client.calls == []


def test_download_tdoc_zip_url_is_none_without_primary_url(
    cache: _StubTDocCache,
) -> None:
    """Without a primary URL the template's URL is recorded on success."""
    template = get_tdoc_zip_url("R5s260009")
    assert template is not None
    client = _FakeScraperClient(url_payloads={template: b"zip"})

    result = download_tdoc_zip("R5s260009", client, cache)

    assert result.url == template
    assert result.path == cache.path_for("r5s260009", "zips")


# ---------------------------------------------------------------------------
# 10. canonicalise_tdoc_id — CLI uses this to map lowercase user input
# to the canonical DB-stored form. The canonical output is the TSG
# short name upper-cased; everything else is preserved.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Already-canonical input is returned unchanged.
        ("R5s260009", "R5s260009"),
        ("R5w260009", "R5w260009"),
        ("R5-227476", "R5-227476"),
        ("C6-250028", "C6-250028"),
        # Lowercase input is canonicalised to uppercase TSG prefix.
        ("r5s260009", "R5s260009"),
        ("r5w260009", "R5w260009"),
        ("r5-227476", "R5-227476"),
        ("c6-250028", "C6-250028"),
        # All-uppercase suffix is canonicalised to lowercase subtype.
        ("R5S260009", "R5s260009"),
        ("R5W260009", "R5w260009"),
        # Surrounding whitespace is stripped.
        ("  r5s260009  ", "R5s260009"),
        # Empty / non-CR / unrecognised inputs return None so the CLI
        # can fall back to the stripped raw input for non-CR shapes.
        ("", None),
        ("bogus", None),
        ("LS-260001", None),  # non-CR shape — narrow regex returns None
        ("R5", None),
        ("R5s", None),
    ],
)
def test_canonicalise_tdoc_id(raw: str, expected: str | None) -> None:
    assert canonicalise_tdoc_id(raw) == expected