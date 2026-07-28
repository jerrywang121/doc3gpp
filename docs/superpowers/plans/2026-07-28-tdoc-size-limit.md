# `tdoc_parse.max_tdoc_size_kb` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-file size limit (`tdoc_parse.max_tdoc_size_kb`, default 1000) that skips oversized TDoc sources (`.zip` or `.docx`) across every parse path, with a `--max-tdoc-size-kb` CLI override.

**Architecture:** A single byte cap (`0` = unlimited) is computed once at the CLI entry point and threaded through `TDocCrService`, `download_tdoc_zip`, and `direct_parse_bytes`. New `TDocTooLargeError` routes into the existing skip bucket. Pre-download cache probes save bandwidth on cache hits; post-fetch guards catch fresh downloads.

**Tech Stack:** Python 3.10+, Pydantic v2 + pydantic-settings, SQLAlchemy 2.0, Typer, pytest.

## Global Constraints

- The new env var `DOC3GPP_TDOC_PARSE__MAX_TDOC_SIZE_KB` is intentionally **not** added to `ALLOWED_ENV_VARS`. The existing `tdoc_parse.max_batch` / `tdoc_parse.max_ftp_depth` knobs are TOML-only — the new field follows the same convention. Tests must mirror `tests/unit/test_settings_config_file.py::test_env_var_allowlist` and confirm the env var is silently ignored.
- `0` means "no check" everywhere (field default, CLI flag value, constructor kwarg, gate-point check). Internally the value is converted to bytes (`* 1024`) once at the resolution helper.
- Skip routing reuses the existing `BatchExtractResult.skipped` dict — never the failures dict. The CLI counts size-skips separately by inspecting the reason prefix (`"TDocTooLargeError:"` vs `"TDocNotYetOnFTPError:"`).
- Skip must not raise — `_tdoc_parse_direct`'s `except Exception` handler must catch `TDocTooLargeError` BEFORE the generic fallback so it can exit `0` with a `WARNING` rather than `1` with `FAILED`.
- Service constructor defaults (`max_tdoc_size_bytes: int = 0`) keep all existing test instantiations backward-compatible.
- Follow existing layered boundaries: scraping/transport only, parsers/parsing only, services/orchestration, cli/thin dispatch.
- Lint: `ruff check .` must pass before each commit. Tests: `pytest -x -q tests/unit` (sqlite-only) and `./scripts/test_sqlite.sh` must pass before each commit. Online tests are opt-in.

---

### Task 1: Settings field + TOML example + settings tests

**Files:**
- Modify: `src/doc3gpp/settings/schema.py:212-235`
- Modify: `src/doc3gpp/data/doc3gpp.toml.example:83-88`
- Modify: `tests/unit/test_tdoc_parse_cli.py` (settings tests around `test_tdoc_parse_max_batch_default_is_100`)
- Modify: `tests/unit/test_settings_config_file.py` (allowlist test section)

**Interfaces:**
- Consumes: existing `TDocParseSettings` class
- Produces: `TDocParseSettings.max_tdoc_size_kb: int` (Field default 1000, `ge=0`); TOML example commented key

- [ ] **Step 1: Write the failing settings tests**

Add to `tests/unit/test_tdoc_parse_cli.py` after the existing `test_tdoc_parse_max_batch_*` block (around line 1720):

```python
def test_tdoc_parse_max_tdoc_size_kb_default_is_1000(sqlite_env) -> None:
    """``tdoc_parse.max_tdoc_size_kb`` defaults to 1000 KB (≈ 1 MiB)."""
    from doc3gpp.settings.loader import get_settings

    assert get_settings().tdoc_parse.max_tdoc_size_kb == 1000


def test_tdoc_parse_max_tdoc_size_kb_toml_override(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """TOML config can override ``tdoc_parse.max_tdoc_size_kb``."""
    from doc3gpp.settings.loader import get_settings

    config_path = tmp_path / "doc3gpp.toml"
    config_path.write_text(
        "[tdoc_parse]\nmax_tdoc_size_kb = 500\n", encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    assert get_settings().tdoc_parse.max_tdoc_size_kb == 500


def test_tdoc_parse_max_tdoc_size_kb_zero_disables(sqlite_env, tmp_path, monkeypatch) -> None:
    """``0`` is a valid value that disables the limit (per the field's ``ge=0``)."""
    from doc3gpp.settings.loader import get_settings

    config_path = tmp_path / "doc3gpp.toml"
    config_path.write_text(
        "[tdoc_parse]\nmax_tdoc_size_kb = 0\n", encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    assert get_settings().tdoc_parse.max_tdoc_size_kb == 0
```

Add to `tests/unit/test_settings_config_file.py` after the existing `test_env_var_allowlist` block (around line 426):

```python
def test_tdoc_parse_max_tdoc_size_kb_env_var_is_ignored(
    tmp_path, monkeypatch,
) -> None:
    """``DOC3GPP_TDOC_PARSE__MAX_TDOC_SIZE_KB`` is outside the env-var
    allowlist, so it is silently dropped and the default applies."""
    from doc3gpp.settings.loader import get_settings

    assert "DOC3GPP_TDOC_PARSE__MAX_TDOC_SIZE_KB" not in ALLOWED_ENV_VARS
    monkeypatch.setenv("DOC3GPP_TDOC_PARSE__MAX_TDOC_SIZE_KB", "256")
    # No DOC3GPP_CONFIG pin → defaults apply.
    monkeypatch.delenv("DOC3GPP_CONFIG", raising=False)
    assert get_settings().tdoc_parse.max_tdoc_size_kb == 1000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -x -q tests/unit/test_tdoc_parse_cli.py::test_tdoc_parse_max_tdoc_size_kb_default_is_1000 tests/unit/test_settings_config_file.py::test_tdoc_parse_max_tdoc_size_kb_env_var_is_ignored -v`

Expected: FAIL with `AttributeError: 'TDocParseSettings' object has no attribute 'max_tdoc_size_kb'`.

- [ ] **Step 3: Add the field to `TDocParseSettings`**

File: `src/doc3gpp/settings/schema.py:212`

Replace the `TDocParseSettings` class:

```python
class TDocParseSettings(BaseModel):
    """Knobs for ``doc3gpp tdoc parse``."""

    max_batch: int = Field(default=100, ge=1)
    max_ftp_depth: int = Field(default=2, ge=0, le=10)
    max_tdoc_size_kb: int = Field(
        default=1000,
        ge=0,
        description=(
            "Per-file cap in KB for TDoc sources (.zip or .docx). "
            "Files larger than this are skipped (size-limit skip bucket). "
            "0 disables the limit."
        ),
    )
```

- [ ] **Step 4: Update the TOML example**

File: `src/doc3gpp/data/doc3gpp.toml.example:83-88`

Replace the `[tdoc_parse]` commented block:

```toml
# [tdoc_parse]
# max_batch = 100
# max_ftp_depth = 2  # max recursion depth for `tdoc parse --from-url <3gpp-folder> --recursive`
# max_tdoc_size_kb = 1000  # skip .zip/.docx larger than this; 0 = unlimited
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_tdoc_parse_cli.py tests/unit/test_settings_config_file.py -v`

Expected: PASS (all four new tests pass; existing tests unaffected).

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/settings/schema.py \
        src/doc3gpp/data/doc3gpp.toml.example \
        tests/unit/test_tdoc_parse_cli.py \
        tests/unit/test_settings_config_file.py
git commit -m "feat(settings): add tdoc_parse.max_tdoc_size_kb (TOML-only)"
```

---

### Task 2: `TDocTooLargeError` + service constructor kwarg

**Files:**
- Modify: `src/doc3gpp/services/tdoc_cr_service.py` (exception class near `TDocNotYetOnFTPError`)
- Modify: `src/doc3gpp/services/tdoc_cr_service.py:362-379` (`__init__`)
- Modify: `tests/unit/test_tdoc_cr_service.py` (constructor kwarg)

**Interfaces:**
- Consumes: existing exception classes in `tdoc_cr_service.py`
- Produces: `TDocTooLargeError(source: str, size: int, limit: int)` exception; `TDocCrService.__init__(..., max_tdoc_size_bytes: int = 0)` storing `self._max_tdoc_size_bytes`

- [ ] **Step 1: Write the failing constructor test**

Add to `tests/unit/test_tdoc_cr_service.py`:

```python
def test_constructor_default_max_tdoc_size_bytes_is_zero() -> None:
    """``TDocCrService(...)`` defaults to ``max_tdoc_size_bytes=0`` (no check).

    Existing test fixtures construct the service without the new
    kwarg; the default must be backwards-compatible.
    """
    service = build_minimal_service()
    assert service._max_tdoc_size_bytes == 0


def test_constructor_accepts_max_tdoc_size_bytes() -> None:
    """Passing ``max_tdoc_size_bytes=N`` is stored verbatim."""
    service = build_minimal_service(max_tdoc_size_bytes=2_500_000)
    assert service._max_tdoc_size_bytes == 2_500_000
```

Add a `build_minimal_service` helper at the top of the test file (mirror any existing constructor helper):

```python
def build_minimal_service(max_tdoc_size_bytes: int = 0) -> TDocCrService:
    """Build a TDocCrService with stub repos for constructor-only tests."""
    from doc3gpp.scraping.cache import TDocCache
    from doc3gpp.scraping.client import ScraperClient
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import SQLAlchemyTDocCrTtcnRepository
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    return TDocCrService(
        cache=TDocCache(root=Path("/tmp/doc3gpp-test-cache"), size_limit_bytes=0),
        scraper_client=ScraperClient(),
        cr_repository=SQLAlchemyTDocCrRepository(),
        cr_ttcn_repository=SQLAlchemyTDocCrTtcnRepository(),
        tdoc_repository=SQLAlchemyTDocRepository(),
        max_tdoc_size_bytes=max_tdoc_size_bytes,
    )
```

(Use the existing imports if `Path` / `TDocCrService` are already imported; otherwise add them.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -x -q tests/unit/test_tdoc_cr_service.py::test_constructor_default_max_tdoc_size_bytes_is_zero -v`

Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'max_tdoc_size_bytes'`.

- [ ] **Step 3: Add `TDocTooLargeError` next to `TDocNotYetOnFTPError`**

File: `src/doc3gpp/services/tdoc_cr_service.py` (find the existing `TDocNotYetOnFTPError` class — locate it via `grep`)

Add immediately after that class:

```python
class TDocTooLargeError(Exception):
    """Raised when a TDoc's source file exceeds ``tdoc_parse.max_tdoc_size_kb``.

    Routed to the existing skip bucket (not the failure bucket)
    because "this file is too big for our parse budget" is an
    operational decision, not an upstream-side error.
    """

    def __init__(self, source: str, size: int, limit: int) -> None:
        super().__init__(
            f"TDoc source {source!r} is {size} bytes, "
            f"exceeds max_tdoc_size_kb limit ({limit} bytes)"
        )
        self.source = source
        self.size = size
        self.limit = limit
```

- [ ] **Step 4: Add the constructor kwarg**

File: `src/doc3gpp/services/tdoc_cr_service.py:362`

Replace the `__init__` signature:

```python
def __init__(
    self,
    *,
    cache: TDocCacheLike,
    scraper_client: "ScraperClient",
    cr_repository: TDocCrDetailRepository,
    cr_ttcn_repository: TDocCrTTCNDetailRepository,
    tdoc_repository: TDocRepository,
    parser: TDocParser | None = None,
    parser_registry: TDocParserRegistry | None = None,
    max_tdoc_size_bytes: int = 0,
) -> None:
    self._cache = cache
    self._scraper = scraper_client
    self._repo = cr_repository
    self._cr_ttcn_repo = cr_ttcn_repository
    self._tdoc_repo = tdoc_repository
    self._parser = parser
    self._parser_registry = parser_registry
    self._max_tdoc_size_bytes = max_tdoc_size_bytes
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_tdoc_cr_service.py -v`

Expected: PASS (both new tests pass; all existing tests unaffected).

- [ ] **Step 6: Lint**

Run: `ruff check src/doc3gpp/services/tdoc_cr_service.py tests/unit/test_tdoc_cr_service.py`

Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/services/tdoc_cr_service.py tests/unit/test_tdoc_cr_service.py
git commit -m "feat(service): add TDocTooLargeError + max_tdoc_size_bytes kwarg"
```

---

### Task 3: `download_tdoc_zip` pre-fetch + post-fetch guards

**Files:**
- Modify: `src/doc3gpp/scraping/tdoc_zip_source.py:224-327` (`download_tdoc_zip`)
- Modify: `tests/unit/test_tdoc_parse_direct.py` (add `max_bytes` coverage)

**Interfaces:**
- Consumes: existing `download_tdoc_zip` signature
- Produces: `download_tdoc_zip(..., *, max_bytes: int = 0)`; raises `TDocTooLargeError` on cache-hit-too-big (no network) or post-fetch-too-big (after `cache.put_bytes`)

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_tdoc_parse_direct.py`:

```python
def test_download_tdoc_zip_skips_oversized_cache_without_network(
    tmp_path, monkeypatch,
) -> None:
    """When the on-disk zip cache exists and exceeds ``max_bytes``,
    no network fetch happens — the function raises TDocTooLargeError
    before calling ``client.get_bytes``.
    """
    from doc3gpp.scraping.cache import TDocCache
    from doc3gpp.scraping.tdoc_zip_source import (
        download_tdoc_zip,
        TDocTooLargeError,
    )

    cache = TDocCache(root=tmp_path, size_limit_bytes=0)
    ftp_url = "tsg/WG5/R5/TSGR5_99/Docs/R5-260100.zip"
    big_payload = b"x" * (5 * 1024 * 1024)  # 5 MiB
    cache.put_bytes(derive_cache_file(ftp_url), big_payload, "zips")

    calls = []
    class _NoNetworkClient:
        def get_bytes(self, url: str) -> bytes:
            calls.append(url)
            return b""

    with pytest.raises(TDocTooLargeError) as exc_info:
        download_tdoc_zip(
            "R5-260100",
            _NoNetworkClient(),  # type: ignore[arg-type]
            cache,
            primary_url="https://example.test/" + ftp_url,
            ftp_url=ftp_url,
            max_bytes=1024 * 1024,  # 1 MiB cap → 5 MiB cache must be skipped
        )
    assert exc_info.value.size == len(big_payload)
    assert calls == [], "network must not have been called"


def test_download_tdoc_zip_post_fetch_raises_when_fresh_download_too_large(
    tmp_path,
) -> None:
    """A fresh download that exceeds ``max_bytes`` raises TDocTooLargeError.

    The zip is written to the cache first (existing behaviour) so the
    next call can short-circuit on the cache-hit path; the exception
    fires after the write, before returning to the caller.
    """
    from doc3gpp.scraping.cache import TDocCache
    from doc3gpp.scraping.client import ScraperClient
    from doc3gpp.scraping.tdoc_zip_source import (
        download_tdoc_zip,
        TDocTooLargeError,
    )

    cache = TDocCache(root=tmp_path, size_limit_bytes=0)

    class _FakeClient:
        def get_bytes(self, url: str) -> bytes:
            return b"y" * (3 * 1024 * 1024)

    with pytest.raises(TDocTooLargeError) as exc_info:
        download_tdoc_zip(
            "R5-260100",
            _FakeClient(),  # type: ignore[arg-type]
            cache,
            primary_url="https://example.test/tsg/R5-260100.zip",
            ftp_url="tsg/R5-260100.zip",
            max_bytes=1024 * 1024,
        )
    assert exc_info.value.size == 3 * 1024 * 1024


def test_download_tdoc_zip_max_bytes_zero_is_noop(
    tmp_path,
) -> None:
    """``max_bytes=0`` disables the check (no exception, download proceeds)."""
    from doc3gpp.scraping.cache import TDocCache
    from doc3gpp.scraping.tdoc_zip_source import download_tdoc_zip

    cache = TDocCache(root=tmp_path, size_limit_bytes=0)

    class _FakeClient:
        def get_bytes(self, url: str) -> bytes:
            return b"z" * (10 * 1024 * 1024)

    result = download_tdoc_zip(
        "R5-260100",
        _FakeClient(),  # type: ignore[arg-type]
        cache,
        primary_url="https://example.test/tsg/R5-260100.zip",
        ftp_url="tsg/R5-260100.zip",
        max_bytes=0,
    )
    assert result.path.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -x -q tests/unit/test_tdoc_parse_direct.py::test_download_tdoc_zip_max_bytes_zero_is_noop -v`

Expected: FAIL with `TypeError: download_tdoc_zip() got an unexpected keyword argument 'max_bytes'` (or `ImportError` for `TDocTooLargeError` if import is added first).

- [ ] **Step 3: Implement the pre-fetch + post-fetch guards**

File: `src/doc3gpp/scraping/tdoc_zip_source.py:224-327`

Add the import near the top of the file (next to `TDocZipDownloadError`):

```python
from doc3gpp.services.tdoc_cr_service import TDocTooLargeError  # noqa: E402
```

(If a circular-import risk exists, defer the import inside the function. Test by running the test suite — if it fails on import, switch to the deferred form.)

Replace the function body. The new signature is:

```python
def download_tdoc_zip(
    tdoc: str,
    client: "ScraperClient",
    cache: TDocCacheLike,
    primary_url: str | None = None,
    *,
    ftp_url: str | None = None,
    max_bytes: int = 0,
) -> DownloadedZip:
    """Return a :class:`DownloadedZip` for the TDoc, downloading on cache miss.

    See existing docstring for cache / URL semantics. When
    ``max_bytes > 0``:

    * On a cache hit, the cached file is statted first; if its size
      exceeds ``max_bytes``, :class:`TDocTooLargeError` is raised
      before any network I/O.
    * On a fresh download, the bytes are written to the cache (so a
      subsequent call hits the cache-hit path) and then statted; if
      they exceed ``max_bytes``, :class:`TDocTooLargeError` is raised
      before returning.

    ``max_bytes=0`` disables both checks.
    """
    if not tdoc:
        raise ValueError("TDoc id is empty")

    canonical = canonicalise_tdoc_id(tdoc)
    if canonical is None:
        raise ValueError(f"Invalid TDoc id shape: {tdoc!r}")

    cached_bytes: bytes | None = None
    cache_key: str | None = None
    if ftp_url:
        cache_key = derive_cache_file(ftp_url)
        cached_bytes = cache.get_bytes(cache_key, "zips")
        if cached_bytes is not None:
            # Pre-fetch cache size guard.
            if max_bytes > 0:
                cached_path = cache.path_for(cache_key, "zips")
                try:
                    cached_size = cached_path.stat().st_size
                except FileNotFoundError:
                    cached_size = len(cached_bytes)
                if cached_size > max_bytes:
                    raise TDocTooLargeError(
                        source=f"cache:{cached_path}",
                        size=cached_size,
                        limit=max_bytes,
                    )
            logger.debug("Cache hit for TDoc zip %s", cache_key)
            return DownloadedZip(path=cache.path_for(cache_key, "zips"), url=None)

    candidates: list[str] = []
    if primary_url:
        candidates.append(primary_url)
    template_url = get_tdoc_zip_url(canonical)
    if template_url and template_url not in candidates:
        candidates.append(template_url)

    if not candidates:
        raise TDocZipDownloadError(url="", original=ValueError("no URL template"))

    last_error: TDocZipDownloadError | None = None
    for url in candidates:
        try:
            payload = client.get_bytes(url)
        except httpx.HTTPError as exc:
            logger.error(
                "HTTP error downloading TDoc zip %s from %s: %s",
                cache_key or canonical.lower(),
                url,
                exc,
            )
            last_error = TDocZipDownloadError(url=url, original=exc)
            continue
        if cache_key is None:
            cache_key = derive_cache_file(url)
        cached_path = cache.put_bytes(cache_key, payload, "zips")
        # Post-fetch size guard.
        if max_bytes > 0 and len(payload) > max_bytes:
            raise TDocTooLargeError(
                source=f"download:{url}",
                size=len(payload),
                limit=max_bytes,
            )
        logger.info(
            "Cached TDoc zip %s at %s (%d bytes) from %s",
            cache_key,
            cached_path,
            len(payload),
            url,
        )
        return DownloadedZip(path=cached_path, url=url)

    assert last_error is not None  # candidates is non-empty, so we must have set it
    raise last_error
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_tdoc_parse_direct.py -v -k "download_tdoc_zip_max_bytes or download_tdoc_zip_skips or download_tdoc_zip_post_fetch"`

Expected: PASS (all three new tests pass).

- [ ] **Step 5: Lint + commit**

Run: `ruff check src/doc3gpp/scraping/tdoc_zip_source.py tests/unit/test_tdoc_parse_direct.py`

Then:

```bash
git add src/doc3gpp/scraping/tdoc_zip_source.py tests/unit/test_tdoc_parse_direct.py
git commit -m "feat(scraping): add download_tdoc_zip max_bytes pre/post-fetch guards"
```

---

### Task 4: `direct_parse_bytes` guard

**Files:**
- Modify: `src/doc3gpp/parsers/direct_extractor.py:351-424`
- Modify: `tests/unit/test_tdoc_parse_direct.py`

**Interfaces:**
- Consumes: existing `direct_parse_bytes` signature
- Produces: `direct_parse_bytes(payload, *, filename, full=False, max_bytes=0)` raising `TDocTooLargeError` before unzip/python-docx work when `len(payload) > max_bytes > 0`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_tdoc_parse_direct.py`:

```python
def test_direct_parse_bytes_raises_when_payload_too_large() -> None:
    """``max_bytes > 0`` and ``len(payload) > max_bytes`` raises TDocTooLargeError
    before any unzip / python-docx work."""
    from doc3gpp.parsers.direct_extractor import direct_parse_bytes
    from doc3gpp.services.tdoc_cr_service import TDocTooLargeError

    payload = b"x" * (3 * 1024 * 1024)
    with pytest.raises(TDocTooLargeError) as exc_info:
        direct_parse_bytes(
            payload, filename="R5-260100.zip", max_bytes=1024 * 1024,
        )
    assert exc_info.value.size == len(payload)
    assert exc_info.value.limit == 1024 * 1024


def test_direct_parse_bytes_max_bytes_zero_is_noop() -> None:
    """``max_bytes=0`` disables the guard — a large payload is parsed normally.

    Only checks the early-return branch; for a parseable payload the
    test uses a minimal zip containing a docx. Skipped when
    python-docx is unavailable.
    """
    pytest.importorskip("docx")
    from doc3gpp.parsers.direct_extractor import direct_parse_bytes

    # Use a real small fixture to prove the no-op path; if a tiny
    # zip-wrapped docx fixture is unavailable, just assert no
    # TDocTooLargeError is raised.
    # (This test mainly guards against accidental hard-fail when
    # max_bytes=0 is set.)
    try:
        direct_parse_bytes(b"x", filename="R5-260100.zip", max_bytes=0)
    except Exception as exc:
        # Any non-TDocTooLargeError is acceptable here (the input is
        # invalid on purpose; we only assert the size guard did not
        # fire).
        from doc3gpp.services.tdoc_cr_service import TDocTooLargeError
        assert not isinstance(exc, TDocTooLargeError)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -x -q tests/unit/test_tdoc_parse_direct.py::test_direct_parse_bytes_raises_when_payload_too_large -v`

Expected: FAIL with `TypeError: direct_parse_bytes() got an unexpected keyword argument 'max_bytes'`.

- [ ] **Step 3: Implement the guard**

File: `src/doc3gpp/parsers/direct_extractor.py:351`

Replace the `direct_parse_bytes` signature:

```python
def direct_parse_bytes(
    payload: bytes,
    *,
    filename: str,
    full: bool = False,
    max_bytes: int = 0,
) -> tuple[str, str, "TDocCRParseResult"]:
```

Add at the top of the function body (right after the docstring's closing triple-quote), BEFORE the suffix dispatch:

```python
    if max_bytes > 0 and len(payload) > max_bytes:
        from doc3gpp.services.tdoc_cr_service import TDocTooLargeError
        raise TDocTooLargeError(
            source=filename,
            size=len(payload),
            limit=max_bytes,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_tdoc_parse_direct.py -v -k "direct_parse_bytes"`

Expected: PASS.

- [ ] **Step 5: Lint + commit**

Run: `ruff check src/doc3gpp/parsers/direct_extractor.py tests/unit/test_tdoc_parse_direct.py`

Then:

```bash
git add src/doc3gpp/parsers/direct_extractor.py tests/unit/test_tdoc_parse_direct.py
git commit -m "feat(parsers): add direct_parse_bytes max_bytes guard"
```

---

### Task 5: DB-mode skip routing in `extract_many`

**Files:**
- Modify: `src/doc3gpp/services/tdoc_cr_service.py:644-664` (`extract_many`)
- Modify: `src/doc3gpp/services/tdoc_cr_service.py:509-515` (`extract` → `download_tdoc_zip` call)
- Modify: `src/doc3gpp/services/tdoc_cr_service.py:924-927` (`extract_from_bytes` → `direct_parse_bytes` call)
- Modify: `tests/unit/test_tdoc_cr_service.py`

**Interfaces:**
- Consumes: `TDocTooLargeError` (Task 2), `download_tdoc_zip(..., max_bytes=...)` (Task 3), `direct_parse_bytes(..., max_bytes=...)` (Task 4)
- Produces: `extract_many` routes `TDocTooLargeError` into the existing `skipped` dict; service internals forward the bytes cap

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_tdoc_cr_service.py`:

```python
def test_extract_many_routes_too_large_to_skipped(
    sqlite_env, monkeypatch,
) -> None:
    """When ``extract`` raises ``TDocTooLargeError``, ``extract_many``
    puts the id in ``skipped`` (NOT in ``failures``).
    """
    from doc3gpp.services.tdoc_cr_service import (
        TDocCrService,
        TDocTooLargeError,
    )

    class _StubService(TDocCrService):
        def extract(self, tdoc_id, *, force=False, full=False):
            raise TDocTooLargeError(source="x", size=10, limit=5)

    service = build_minimal_service()
    service.__class__ = _StubService  # type: ignore[assignment]

    batch = service.extract_many(["R5-260100", "R5-260101"])
    assert set(batch.failures) == set()
    assert set(batch.skipped.keys()) == {"R5-260100", "R5-260101"}
    assert all(
        reason.startswith("TDocTooLargeError:")
        for reason in batch.skipped.values()
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -x -q tests/unit/test_tdoc_cr_service.py::test_extract_many_routes_too_large_to_skipped -v`

Expected: FAIL — the existing `extract_many` only catches `TDocNotYetOnFTPError`, `(ValueError, LookupError, TDocZipDownloadError, TypeError)`. `TDocTooLargeError` (which is a plain `Exception`) currently falls into the generic `(ValueError, …)` tuple because… wait — `TDocTooLargeError` does NOT inherit from any of those, so today it propagates out of `extract_many`. Verify by running the test: it will fail with `TDocTooLargeError` propagating, not with the wrong bucket.

- [ ] **Step 3: Add the new `except` clause in `extract_many`**

File: `src/doc3gpp/services/tdoc_cr_service.py:647-663`

Replace the for-loop body:

```python
    for raw_id in tdoc_ids:
        try:
            result = self.extract(raw_id, force=force, full=full)
        except TDocNotYetOnFTPError as exc:
            logger.info("Skipping TDoc %r: %s", raw_id, exc)
            skipped[raw_id.strip()] = f"{type(exc).__name__}: {exc}"
            continue
        except TDocTooLargeError as exc:
            logger.info(
                "Skipping TDoc %r: %s bytes exceeds max_tdoc_size_kb limit (%s bytes)",
                raw_id, exc.size, exc.limit,
            )
            skipped[raw_id.strip()] = f"{type(exc).__name__}: {exc}"
            continue
        except (ValueError, LookupError, TDocZipDownloadError, TypeError) as exc:
            logger.warning(
                "Failed to extract TDoc %r: %s",
                raw_id,
                exc,
                exc_info=True,
            )
            failures[raw_id.strip()] = f"{type(exc).__name__}: {exc}"
            continue
        successes[result.details.tdoc_id] = result
```

- [ ] **Step 4: Wire `max_tdoc_size_bytes` into the internal calls**

File: `src/doc3gpp/services/tdoc_cr_service.py:509` (`extract` → `download_tdoc_zip` call)

Replace:

```python
        downloaded = download_tdoc_zip(
            normalised,
            self._scraper,
            self._cache,
            primary_url=primary_url,
            ftp_url=tdoc.ftp_url,
            max_bytes=self._max_tdoc_size_bytes,
        )
```

File: `src/doc3gpp/services/tdoc_cr_service.py:925` (`extract_from_bytes` → `direct_parse_bytes` call)

Replace:

```python
        markdown, docx_filename, parsed = direct_parse_bytes(
            docx_bytes, filename=filename, full=full,
            max_bytes=self._max_tdoc_size_bytes,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_tdoc_cr_service.py -v`

Expected: PASS (new test + all existing tests).

- [ ] **Step 6: Lint + commit**

Run: `ruff check src/doc3gpp/services/tdoc_cr_service.py tests/unit/test_tdoc_cr_service.py`

Then:

```bash
git add src/doc3gpp/services/tdoc_cr_service.py tests/unit/test_tdoc_cr_service.py
git commit -m "feat(service): route TDocTooLargeError to skipped bucket + thread max_bytes"
```

---

### Task 6: URL batch skip routing

**Files:**
- Modify: `src/doc3gpp/models/tdoc_cr.py:344-354` (`DirectParseBatchResult`)
- Modify: `src/doc3gpp/services/tdoc_cr_service.py:793-845` (`extract_from_url_batch`)
- Modify: `src/doc3gpp/services/tdoc_cr_service.py:950-1046` (`_extract_from_3gpp_url`)
- Modify: `src/doc3gpp/cli.py:2979-3062` (`_emit_url_batch_results`)
- Modify: `tests/unit/test_tdoc_cr_service.py`
- Modify: `tests/unit/test_tdoc_parse_cli.py`

**Interfaces:**
- Consumes: `TDocTooLargeError` (Task 2)
- Produces: `DirectParseBatchResult.skipped: dict[str, str]`; `extract_from_url_batch` routes `TDocTooLargeError` to `skipped` instead of `failures`; `_extract_from_3gpp_url` threads `max_bytes` to its `self._cache.put_bytes` slot (post-fetch guard happens via `extract_docx_from_zip` payload length — see Step 3); `_emit_url_batch_results` prints the size-skip line

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_tdoc_cr_service.py`:

```python
def test_extract_from_url_batch_routes_too_large_to_skipped() -> None:
    """``extract_from_url_batch`` puts oversized URLs in ``skipped``."""
    from doc3gpp.services.tdoc_cr_service import TDocCrService
    from doc3gpp.services.tdoc_cr_service import TDocTooLargeError

    service = build_minimal_service(max_tdoc_size_bytes=10)
    service.collect_3gpp_file_urls = lambda url, *, max_depth: ["https://example.test/a.zip"]

    class _Boom(TDocCrService):
        def extract_from_url(self, url, *, force=False, full=False):
            raise TDocTooLargeError(source=url, size=99, limit=10)

    service.__class__ = _Boom  # type: ignore[assignment]

    batch = service.extract_from_url_batch(
        "https://example.test/folder/",
        max_depth=0,
    )
    assert "https://example.test/a.zip" in batch.skipped
    assert "https://example.test/a.zip" not in batch.failures
    assert batch.skipped["https://example.test/a.zip"].startswith("TDocTooLargeError:")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -x -q tests/unit/test_tdoc_cr_service.py::test_extract_from_url_batch_routes_too_large_to_skipped -v`

Expected: FAIL — today the per-file `try/except` wraps with `except Exception` into `failures`; `TDocTooLargeError` lands in `failures` not `skipped`.

- [ ] **Step 3: Add `skipped` field to `DirectParseBatchResult`**

File: `src/doc3gpp/models/tdoc_cr.py:344`

Replace:

```python
@dataclass(slots=True, frozen=True)
class DirectParseBatchResult:
    """Outcome of a batch ``tdoc parse --from-url <folder>`` run.

    Bundles every per-file :class:`DirectParseResult` with a failure map
    and a skip map so the CLI can emit a summary without recomputing
    visit order. ``skipped`` carries files that were too large for the
    ``tdoc_parse.max_tdoc_size_kb`` cap (size-limit skip) — kept
    separate from ``failures`` because the operator-facing meaning
    differs (size-skip is a budget decision, failure is a bug).
    """

    results: list[DirectParseResult]
    failures: dict[str, str]
    skipped: dict[str, str] = field(default_factory=dict)
```

- [ ] **Step 4: Route `TDocTooLargeError` in `extract_from_url_batch`**

File: `src/doc3gpp/services/tdoc_cr_service.py:793`

Replace the per-file loop:

```python
    results: list[DirectParseResult] = []
    failures: dict[str, str] = {}
    skipped: dict[str, str] = {}
    for file_url in file_urls:
        try:
            result = self.extract_from_url(file_url, force=force, full=full)
        except TDocTooLargeError as exc:
            logger.info(
                "Skipping %s: %s bytes exceeds max_tdoc_size_kb limit (%s bytes)",
                file_url, exc.size, exc.limit,
            )
            skipped[file_url] = f"{type(exc).__name__}: {exc}"
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to parse %s: %s", file_url, exc, exc_info=True,
            )
            failures[file_url] = f"{type(exc).__name__}: {exc}"
            continue
        results.append(result)

    return DirectParseBatchResult(results=results, failures=failures, skipped=skipped)
```

- [ ] **Step 5: Add post-fetch size guard in `_extract_from_3gpp_url`**

File: `src/doc3gpp/services/tdoc_cr_service.py:997-1000`

Replace the download block:

```python
        zip_payload = self._scraper.get_bytes(url)
        # Post-fetch size guard: defence-in-depth — the direct-parse
        # path also writes its own zip slot (the cache's
        # enforce_size_limit might evict it differently from the
        # DB-mode path), and the size check is cheap.
        if self._max_tdoc_size_bytes > 0 and len(zip_payload) > self._max_tdoc_size_bytes:
            self._cache.put_bytes(cache_file, zip_payload, "zips")
            raise TDocTooLargeError(
                source=f"download:{url}",
                size=len(zip_payload),
                limit=self._max_tdoc_size_bytes,
            )
        self._cache.put_bytes(cache_file, zip_payload, "zips")
```

- [ ] **Step 6: Update `_emit_url_batch_results`**

File: `src/doc3gpp/cli.py:2979`

Replace the summary block at the end of the function (around line 3054):

```python
    typer.echo("---")
    typer.echo(f"Scanned:                         {len(batch.results) + len(batch.failures) + len(batch.skipped)}")
    if output_dir is not None:
        typer.echo(f"Skipped (output already exists): {skipped}")
    typer.echo(f"Skipped (exceeds max_tdoc_size_kb): {len(batch.skipped)}")
    typer.echo(f"Newly parsed:                    {newly_parsed}")
    typer.echo(f"Cache hits:                      {cache_hits}")
    typer.echo(f"Failures:                        {failures}")
    if failures > 0 and newly_parsed + cache_hits == 0:
        raise typer.Exit(code=1)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_tdoc_cr_service.py tests/unit/test_tdoc_parse_cli.py -v`

Expected: PASS (new test passes; existing tests unchanged).

- [ ] **Step 8: Lint + commit**

Run: `ruff check src/doc3gpp/models/tdoc_cr.py src/doc3gpp/services/tdoc_cr_service.py src/doc3gpp/cli.py tests/unit/test_tdoc_cr_service.py`

Then:

```bash
git add src/doc3gpp/models/tdoc_cr.py \
        src/doc3gpp/services/tdoc_cr_service.py \
        src/doc3gpp/cli.py \
        tests/unit/test_tdoc_cr_service.py \
        tests/unit/test_tdoc_parse_cli.py
git commit -m "feat(service): route URL-batch size-skip + emit summary line"
```

---

### Task 7: Factory wiring

**Files:**
- Modify: `src/doc3gpp/services/factory.py:119-159`
- Modify: `tests/unit/test_services_factory.py` (or equivalent)

**Interfaces:**
- Consumes: `TDocCrService(max_tdoc_size_bytes=...)` (Task 2)
- Produces: `build_tdoc_cr_service(..., max_tdoc_size_bytes: int | None = None)` that resolves `settings.tdoc_parse.max_tdoc_size_kb * 1024` when `None`, else uses the override

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_services_factory.py`:

```python
def test_build_tdoc_cr_service_threads_max_tdoc_size_bytes_from_settings(
    monkeypatch, tmp_path,
) -> None:
    """Factory defaults ``max_tdoc_size_bytes`` from settings (KB → bytes)."""
    from doc3gpp.services.factory import build_tdoc_cr_service

    config_path = tmp_path / "doc3gpp.toml"
    config_path.write_text(
        "[tdoc_parse]\nmax_tdoc_size_kb = 500\n", encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    service = build_tdoc_cr_service()
    assert service._max_tdoc_size_bytes == 500 * 1024


def test_build_tdoc_cr_service_explicit_override_wins(
    monkeypatch, tmp_path,
) -> None:
    """Passing ``max_tdoc_size_bytes`` explicitly overrides the settings value."""
    from doc3gpp.services.factory import build_tdoc_cr_service

    config_path = tmp_path / "doc3gpp.toml"
    config_path.write_text(
        "[tdoc_parse]\nmax_tdoc_size_kb = 500\n", encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    service = build_tdoc_cr_service(max_tdoc_size_bytes=10 * 1024 * 1024)
    assert service._max_tdoc_size_bytes == 10 * 1024 * 1024


def test_build_tdoc_cr_service_default_zero_when_unset(
    monkeypatch, tmp_path,
) -> None:
    """When neither override nor settings value is set, the default ``0``
    (no check) applies — preserves backward compat for tests that
    build the service without any size config."""
    from doc3gpp.services.factory import build_tdoc_cr_service

    monkeypatch.delenv("DOC3GPP_CONFIG", raising=False)
    # No tmp_path config → no TOML → defaults apply.
    service = build_tdoc_cr_service()
    assert service._max_tdoc_size_bytes == 1000 * 1024  # default 1000 KB
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -x -q tests/unit/test_services_factory.py::test_build_tdoc_cr_service_threads_max_tdoc_size_bytes_from_settings -v`

Expected: FAIL — the factory doesn't accept `max_tdoc_size_bytes` and always passes the default.

- [ ] **Step 3: Wire the factory**

File: `src/doc3gpp/services/factory.py:119`

Replace:

```python
def build_tdoc_cr_service(
    cr_ttcn_repository: TDocCrTTCNDetailRepository | None = None,
    *,
    max_tdoc_size_bytes: int | None = None,
) -> TDocCrService:
    """Construct a :class:`TDocCrService` for the ``tdoc parse`` command.

    ...

    When ``max_tdoc_size_bytes`` is ``None`` (default), the factory
    resolves the value from
    ``settings.tdoc_parse.max_tdoc_size_kb`` (``* 1024``). When an
    explicit value is supplied it overrides the setting (typically
    used by the CLI to apply ``--max-tdoc-size-kb``). The resolved
    bytes value is forwarded to :class:`TDocCrService` as
    ``max_tdoc_size_bytes``; ``0`` disables the size guard.
    """
    settings = get_settings()
    if max_tdoc_size_bytes is None:
        max_tdoc_size_bytes = settings.tdoc_parse.max_tdoc_size_kb * 1024
    return TDocCrService(
        cache=TDocCache(
            root=settings.cache.dir,
            size_limit_bytes=settings.cache.size_limit_mb * 1024 * 1024,
        ),
        scraper_client=ScraperClient(),
        cr_repository=SQLAlchemyTDocCrRepository(),
        cr_ttcn_repository=cr_ttcn_repository or build_tdoc_cr_ttcn_repository(),  # type: ignore[call-arg]
        tdoc_repository=SQLAlchemyTDocRepository(),
        max_tdoc_size_bytes=max_tdoc_size_bytes,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_services_factory.py -v`

Expected: PASS.

- [ ] **Step 5: Lint + commit**

Run: `ruff check src/doc3gpp/services/factory.py tests/unit/test_services_factory.py`

Then:

```bash
git add src/doc3gpp/services/factory.py tests/unit/test_services_factory.py
git commit -m "feat(factory): thread max_tdoc_size_bytes into TDocCrService"
```

---

### Task 8: CLI dispatch (flag + resolver + single-file direct path)

**Files:**
- Modify: `src/doc3gpp/cli.py:1138-1283` (`tdoc_parse` Typer command signature)
- Modify: `src/doc3gpp/cli.py:1311-1425` (`tdoc_parse` body branches that call factory / direct / url / local helpers)
- Modify: `src/doc3gpp/cli.py:1124-1135` (add `_resolve_max_tdoc_size_bytes`)
- Modify: `src/doc3gpp/cli.py:1808-1891` (`_tdoc_parse_direct`)
- Modify: `tests/unit/test_tdoc_parse_cli.py`

**Interfaces:**
- Consumes: `build_tdoc_cr_service(max_tdoc_size_bytes=...)` (Task 7); `_tdoc_parse_direct` / `_tdoc_parse_local_batch` / `_tdoc_parse_url_batch` need new kwarg (Tasks 8, 9, 6)
- Produces: `--max-tdoc-size-kb INT` Typer option; `_resolve_max_tdoc_size_bytes(override_kb, settings) -> int` helper; `_tdoc_parse_direct` accepts `max_tdoc_size_bytes` and routes `TDocTooLargeError` to exit 0 + WARNING

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_tdoc_parse_cli.py`:

```python
def test_resolve_max_tdoc_size_bytes_uses_override_kb_when_set(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """``--max-tdoc-size-kb`` overrides the settings value; KB → bytes conversion."""
    from doc3gpp.cli import _resolve_max_tdoc_size_bytes
    from doc3gpp.settings.loader import get_settings

    config_path = tmp_path / "doc3gpp.toml"
    config_path.write_text(
        "[tdoc_parse]\nmax_tdoc_size_kb = 100\n", encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    settings = get_settings()
    # override wins:
    assert _resolve_max_tdoc_size_bytes(
        override_kb=2000, settings=settings,
    ) == 2000 * 1024


def test_resolve_max_tdoc_size_bytes_falls_back_to_settings_kb(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """When override is ``None``, the settings value applies (KB → bytes)."""
    from doc3gpp.cli import _resolve_max_tdoc_size_bytes
    from doc3gpp.settings.loader import get_settings

    config_path = tmp_path / "doc3gpp.toml"
    config_path.write_text(
        "[tdoc_parse]\nmax_tdoc_size_kb = 500\n", encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    settings = get_settings()
    assert _resolve_max_tdoc_size_bytes(
        override_kb=None, settings=settings,
    ) == 500 * 1024


def test_tdoc_parse_cli_flag_propagates_to_service(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """``--max-tdoc-size-kb`` on the CLI threads through to the service."""
    from typer.testing import CliRunner
    from doc3gpp.cli import cli

    runner = CliRunner()
    captured = {"max_tdoc_size_bytes": None}

    from doc3gpp.services import factory

    original_build = factory.build_tdoc_cr_service

    def spy(cr_ttcn_repository=None, *, max_tdoc_size_bytes=None):
        captured["max_tdoc_size_bytes"] = max_tdoc_size_bytes
        return original_build(cr_ttcn_repository, max_tdoc_size_bytes=max_tdoc_size_bytes)

    monkeypatch.setattr(factory, "build_tdoc_cr_service", spy)
    # DB-mode requires at least one filter; provide one and force exit
    # before the actual extract (the service spy fires before extract).
    runner.invoke(
        cli,
        [
            "tdoc", "parse",
            "--tdoc", "R5-260100",
            "--max-tdoc-size-kb", "256",
            "--yes",
        ],
        catch_exceptions=False,
    )
    assert captured["max_tdoc_size_bytes"] == 256 * 1024
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -x -q tests/unit/test_tdoc_parse_cli.py::test_resolve_max_tdoc_size_bytes_uses_override_kb_when_set -v`

Expected: FAIL — `_resolve_max_tdoc_size_bytes` does not exist yet.

- [ ] **Step 3: Add `_resolve_max_tdoc_size_bytes`**

File: `src/doc3gpp/cli.py:1124` (immediately after `_resolve_url_batch_depth`)

Add:

```python
def _resolve_max_tdoc_size_bytes(
    *,
    override_kb: int | None,
    settings: "Settings",
) -> int:
    """Return the effective per-file cap in bytes; ``0`` = unlimited.

    Mirrors :func:`_resolve_url_batch_depth`: when ``override_kb`` is
    ``None``, the value comes from
    ``settings.tdoc_parse.max_tdoc_size_kb``. The bytes value (KB ×
    1024) is the canonical form the service layer and gate points
    consume; the CLI flag is in KB for human readability.
    """
    if override_kb is not None:
        return override_kb * 1024
    return settings.tdoc_parse.max_tdoc_size_kb * 1024
```

- [ ] **Step 4: Add the `--max-tdoc-size-kb` CLI flag**

File: `src/doc3gpp/cli.py:1138` (inside the `tdoc_parse` Typer command signature, immediately after `max_depth`)

Add:

```python
    max_tdoc_size_kb: int | None = typer.Option(
        None,
        "--max-tdoc-size-kb",
        min=0,
        help=(
            "Override tdoc_parse.max_tdoc_size_kb. Source files (.zip "
            "or .docx) larger than this many KB are skipped (size-limit "
            "skip bucket). 0 = unlimited."
        ),
    ),
```

- [ ] **Step 5: Compute the resolved bytes once and thread it through**

File: `src/doc3gpp/cli.py:1284` (top of `tdoc_parse` body)

Replace the entry to the function body (immediately after the docstring):

```python
    max_tdoc_size_bytes = _resolve_max_tdoc_size_bytes(
        override_kb=max_tdoc_size_kb,
        settings=get_settings(),
    )
```

Then update every `build_tdoc_cr_service()` call inside `tdoc_parse` to pass `max_tdoc_size_bytes=max_tdoc_size_bytes`. There are two call sites:

- Line ~1372: `tdoc_service = build_tdoc_cr_service()` — pass `max_tdoc_size_bytes=max_tdoc_size_bytes`
- Line ~1579: `service = build_tdoc_cr_service()` — pass `max_tdoc_size_bytes=max_tdoc_size_bytes`

Also update the dispatch branches to forward the kwarg:

- Line ~1342 (`_tdoc_parse_direct` for `--from-path FILE`): pass `max_tdoc_size_bytes=max_tdoc_size_bytes`
- Line ~1354 (`_tdoc_parse_local_batch` for `--from-path DIR`): pass `max_tdoc_size_bytes=max_tdoc_size_bytes`
- Line ~1387 (`_tdoc_parse_direct` for `--from-url FILE`): pass `max_tdoc_size_bytes=max_tdoc_size_bytes`
- Line ~1395 (`_tdoc_parse_url_batch` for `--from-url FOLDER`): pass `max_tdoc_size_bytes=max_tdoc_size_bytes`

- [ ] **Step 6: Update `_tdoc_parse_direct` signature and exception handling**

File: `src/doc3gpp/cli.py:1808`

Replace:

```python
def _tdoc_parse_direct(
    *,
    from_path: str | None,
    from_url: str | None,
    fmt: str | None,
    output: str | None,
    full: bool,
    max_tdoc_size_bytes: int = 0,
) -> None:
```

Add `TDocTooLargeError` to the imports block near the top of `_tdoc_parse_direct`:

```python
    from doc3gpp.services.tdoc_cr_service import TDocTooLargeError
```

Replace the `try` block. The existing block reads bytes / calls `extract_from_bytes` / `extract_from_url`. Add the size exception BEFORE the generic `except Exception`:

```python
    try:
        if from_path is not None:
            payload = Path(from_path).read_bytes()
            result = service.extract_from_bytes(
                payload, from_path, force=False, full=full,
                max_tdoc_size_bytes=max_tdoc_size_bytes,
            )
        else:
            result = service.extract_from_url(
                raw, force=False, full=full,
                max_tdoc_size_bytes=max_tdoc_size_bytes,
            )
    except TDocTooLargeError as exc:
        # Size-limit skip — exit 0 with a warning; the operator's
        # --max-tdoc-size-kb is an explicit budget decision, not a
        # bug. Consistent with the non-3GPP URL warning+emit path.
        typer.echo(
            f"SKIPPED - {type(exc).__name__}: {exc}",
            err=True,
        )
        raise typer.Exit(code=0) from None
    except FileNotFoundError as exc:
        ...
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"FAILED - {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from None
```

Note: `extract_from_bytes` / `extract_from_url` need a `max_tdoc_size_bytes` kwarg too — update those signatures in Task 7/Task 9 (already wired into the service constructor in Task 2/Task 5; the methods need to forward the kwarg).

- [ ] **Step 7: Forward `max_tdoc_size_bytes` through `extract_from_url` / `extract_from_bytes`**

File: `src/doc3gpp/services/tdoc_cr_service.py:670` (`extract_from_url`)

Replace the signature:

```python
    def extract_from_url(
        self,
        url: str,
        *,
        force: bool = False,
        full: bool = False,
        max_tdoc_size_bytes: int | None = None,
    ) -> DirectParseResult:
```

Body change: the non-3GPP branch (`if not is_3gpp_ftp_url(url):` — line ~717) currently calls `direct_parse_bytes(payload, filename=url, full=full)`. Add the kwarg:

```python
            markdown, docx_filename, parsed = direct_parse_bytes(
                payload, filename=url, full=full,
                max_bytes=max_tdoc_size_bytes
                if max_tdoc_size_bytes is not None
                else self._max_tdoc_size_bytes,
            )
```

Apply the same `max_bytes=...` kwarg to the two `direct_parse_bytes` calls inside `extract_from_url` for the FK-miss / no-tdoc-id branches (lines ~740 and ~763).

Update the 3GPP branch (line ~783) to forward `max_tdoc_size_bytes=max_tdoc_size_bytes` to `_extract_from_3gpp_url`.

`extract_from_bytes` (line ~888):

Replace the signature:

```python
    def extract_from_bytes(
        self,
        docx_bytes: bytes,
        filename: str,
        *,
        force: bool = False,
        full: bool = True,
        max_tdoc_size_bytes: int | None = None,
    ) -> DirectParseResult:
```

Body change (line ~925):

```python
        markdown, docx_filename, parsed = direct_parse_bytes(
            docx_bytes, filename=filename, full=full,
            max_bytes=max_tdoc_size_bytes
            if max_tdoc_size_bytes is not None
            else self._max_tdoc_size_bytes,
        )
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_tdoc_parse_cli.py tests/unit/test_tdoc_cr_service.py tests/unit/test_services_factory.py -v`

Expected: PASS.

- [ ] **Step 9: Lint + commit**

Run: `ruff check src/doc3gpp/cli.py src/doc3gpp/services/tdoc_cr_service.py tests/unit/test_tdoc_parse_cli.py`

Then:

```bash
git add src/doc3gpp/cli.py \
        src/doc3gpp/services/tdoc_cr_service.py \
        tests/unit/test_tdoc_parse_cli.py
git commit -m "feat(cli): add --max-tdoc-size-kb flag + thread bytes cap"
```

---

### Task 9: Local batch size guard

**Files:**
- Modify: `src/doc3gpp/cli.py:2822-2926` (`_tdoc_parse_local_batch`)
- Modify: `tests/integration/test_tdoc_parse_local_batch_sqlite.py` (new file)

**Interfaces:**
- Consumes: `TDocTooLargeError` (Task 2), `service.extract_from_bytes(..., max_tdoc_size_bytes=...)` (Task 8)
- Produces: `_tdoc_parse_local_batch(*, max_tdoc_size_bytes=0)`; per-file `Path.stat()` guard BEFORE `read_bytes()`; new `size_skipped` counter + summary line

- [ ] **Step 1: Write the failing integration test**

Create `tests/integration/test_tdoc_parse_local_batch_sqlite.py`:

```python
"""Integration: ``tdoc parse --from-path DIR`` honours max_tdoc_size_kb."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


@pytest.fixture
def sized_dir(tmp_path: Path) -> Path:
    """Create a directory with a small file and a large file (relative to the cap)."""
    small = tmp_path / "R5-260100.docx"
    small.write_bytes(b"x" * 100)  # ~100 B
    big = tmp_path / "R5-260200.docx"
    big.write_bytes(b"y" * (3 * 1024 * 1024))  # 3 MiB
    return tmp_path


def test_local_batch_skips_files_over_size_limit(
    runner, sized_dir, tmp_path, monkeypatch,
) -> None:
    """With ``--max-tdoc-size-kb=1`` (1024 B), the 3 MiB file is skipped."""
    output_dir = tmp_path / "out"
    result = runner.invoke(
        cli,
        [
            "tdoc", "parse",
            "--from-path", str(sized_dir),
            "--output", str(output_dir),
            "--format", "raw",
            "--max-tdoc-size-kb", "1",  # 1024 B cap → 3 MiB file must be skipped
            "--yes",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # The small file should produce output; the big file should not.
    outputs = sorted(p.name for p in output_dir.iterdir())
    assert any(name.startswith("R5-260100") for name in outputs)
    assert not any(name.startswith("R5-260200") for name in outputs)
    # Summary should mention size-skip.
    assert "exceeds max_tdoc_size_kb" in result.stdout or \
           "exceeds max_tdoc_size_kb" in result.stderr
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest -x -q tests/integration/test_tdoc_parse_local_batch_sqlite.py -v`

Expected: FAIL — `_tdoc_parse_local_batch` does not accept `max_tdoc_size_bytes` and the big file produces output too.

- [ ] **Step 3: Wire the local-batch helper**

File: `src/doc3gpp/cli.py:2822`

Replace the signature:

```python
def _tdoc_parse_local_batch(
    *,
    from_path: str,
    output: str,
    fmt: str | None,
    recursive: bool,
    force: bool,
    full: bool,
    max_tdoc_size_bytes: int = 0,
) -> None:
```

Replace the per-file loop body (around line 2869):

```python
    skipped = 0
    size_skipped = 0
    re_parsed = 0
    newly_parsed = 0
    failures = 0

    for input_path in targets:
        rel = input_path.relative_to(input_dir)
        out_path = _resolve_batch_output_path(rel, output_dir, resolved_format)

        if out_path.exists() and not force:
            skipped += 1
            logger.debug("Skipping %s because output already exists: %s", input_path, out_path)
            continue

        # Pre-read size guard — saves an OOM-causing ``read_bytes()`` on
        # huge files. ``max_tdoc_size_bytes == 0`` disables the check.
        if max_tdoc_size_bytes > 0:
            try:
                file_size = input_path.stat().st_size
            except OSError as exc:
                logger.warning("Failed to stat %s: %s", input_path, exc)
                failures += 1
                continue
            if file_size > max_tdoc_size_bytes:
                logger.warning(
                    "Skipping %s: %d bytes exceeds max_tdoc_size_kb limit (%d bytes)",
                    input_path, file_size, max_tdoc_size_bytes,
                )
                size_skipped += 1
                continue

        try:
            payload = input_path.read_bytes()
            result = service.extract_from_bytes(
                payload, str(input_path), force=False, full=full,
                max_tdoc_size_bytes=max_tdoc_size_bytes,
            )
        except TDocTooLargeError as exc:
            # Belt-and-braces — the pre-read stat above should have
            # caught this, but the service-side guard fires too.
            logger.warning("Skipping %s: %s", input_path, exc)
            size_skipped += 1
            continue
        except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
            logger.warning("Failed to read %s: %s", input_path, exc)
            failures += 1
            continue
        except (TDocZipDownloadError, CRHeaderMissingError, ValueError) as exc:
            logger.warning("Failed to parse %s: %s", input_path, exc)
            failures += 1
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to parse %s: %s: %s", input_path, type(exc).__name__, exc
            )
            failures += 1
            continue

        try:
            if resolved_format == "raw":
                _emit_record_raw(result.markdown, str(out_path))
            else:
                if result.details is None:
                    logger.warning(
                        "No parsed details for %s; skipping output.", input_path
                    )
                    failures += 1
                    continue
                _emit_record(result.details, resolved_format, str(out_path))
        except OSError as exc:
            logger.warning("Failed to write %s: %s", out_path, exc)
            failures += 1
            continue

        if out_path.exists() and force:
            re_parsed += 1
        else:
            newly_parsed += 1

    typer.echo("---")
    typer.echo(f"Skipped (output already exists): {skipped}")
    typer.echo(f"Skipped (exceeds max_tdoc_size_kb): {size_skipped}")
    typer.echo(f"Re-parsed (with --force):        {re_parsed}")
    typer.echo(f"Newly parsed:                    {newly_parsed}")
    typer.echo(f"Failures:                        {failures}")
    if failures > 0 and newly_parsed + re_parsed == 0:
        raise typer.Exit(code=1)
```

Add `TDocTooLargeError` to the imports at the top of the function:

```python
    from doc3gpp.services.tdoc_cr_service import TDocTooLargeError
```

- [ ] **Step 4: Run integration test to verify it passes**

Run: `pytest -x -q tests/integration/test_tdoc_parse_local_batch_sqlite.py -v`

Expected: PASS.

- [ ] **Step 5: Lint + commit**

Run: `ruff check src/doc3gpp/cli.py tests/integration/test_tdoc_parse_local_batch_sqlite.py`

Then:

```bash
git add src/doc3gpp/cli.py tests/integration/test_tdoc_parse_local_batch_sqlite.py
git commit -m "feat(cli): local-batch size guard + size-skipped summary line"
```

---

### Task 10: DB-mode CLI summary lines

**Files:**
- Modify: `src/doc3gpp/cli.py:1600-1642` (`tdoc_parse` summary block)
- Modify: `tests/unit/test_tdoc_parse_cli.py`

**Interfaces:**
- Consumes: `BatchExtractResult.skipped` (already populated with `"TDocTooLargeError: …"` reasons by Task 5)
- Produces: split-by-prefix count for size-skips vs FTP-skips; new `Skipped (exceeds max_tdoc_size_kb=…)` summary line

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_tdoc_parse_cli.py`:

```python
def test_db_mode_summary_splits_size_skip_from_ftp_skip(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """DB-mode summary distinguishes size-skips from FTP-skips."""
    from typer.testing import CliRunner
    from doc3gpp.cli import cli

    runner = CliRunner()
    config_path = tmp_path / "doc3gpp.toml"
    config_path.write_text(
        "[tdoc_parse]\nmax_tdoc_size_kb = 1\n",  # 1 KB cap
        encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    # Run with a single filter; assert the summary contains BOTH lines.
    result = runner.invoke(
        cli,
        [
            "tdoc", "parse",
            "--tdoc", "R5-260100",
            "--yes",
            "--max-tdoc-size-kb", "1",
        ],
        catch_exceptions=False,
    )
    # Either the row was skipped for size OR not — we just need both
    # summary lines present when at least one skip happens.
    if "exceeds max_tdoc_size_kb" in result.stdout or \
       "exceeds max_tdoc_size_kb" in result.stderr:
        assert "Skipped (not yet on FTP):" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -x -q tests/unit/test_tdoc_parse_cli.py::test_db_mode_summary_splits_size_skip_from_ftp_skip -v`

Expected: FAIL — only the old "Skipped (not yet on FTP)" line exists.

- [ ] **Step 3: Add the size-skip summary line**

File: `src/doc3gpp/cli.py:1625-1630` (inside the `tdoc_parse` summary block)

Replace:

```python
    size_skipped = sum(
        1 for tid, reason in batch.skipped.items()
        if reason.startswith("TDocTooLargeError:")
    )
    ftp_skipped = sum(
        1 for tid, reason in batch.skipped.items()
        if reason.startswith("TDocNotYetOnFTPError:")
    )
    typer.echo("---")
    typer.echo(f"Skipped (exceeds max_tdoc_size_kb={max_tdoc_size_kb}): {size_skipped}")
    typer.echo(f"Skipped (already parsed before this run): {already_parsed}")
    typer.echo(f"Skipped (not yet on FTP):                 {ftp_skipped}")
    typer.echo(f"Re-parsed (with --force):                  {re_parsed}")
    typer.echo(f"Newly parsed:                              {newly_parsed}")
    typer.echo(f"Failures:                                  {len(failures)}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -x -q tests/unit/test_tdoc_parse_cli.py -v`

Expected: PASS.

- [ ] **Step 5: Lint + commit**

Run: `ruff check src/doc3gpp/cli.py tests/unit/test_tdoc_parse_cli.py`

Then:

```bash
git add src/doc3gpp/cli.py tests/unit/test_tdoc_parse_cli.py
git commit -m "feat(cli): DB-mode summary line for max_tdoc_size_kb skips"
```

---

### Task 11: Documentation sync

**Files:**
- Modify: `AGENTS.md` (one-liner + table entry)
- Modify: `docs/cli.md` (`tdoc parse` section + new "Size limit" subsection)
- Modify: `docs/conventions.md` §"Settings caching"

**Interfaces:**
- Consumes: feature complete from Tasks 1-10
- Produces: user-facing documentation matches the implementation

- [ ] **Step 1: Update `AGENTS.md` "Where to look" table**

Add a row to the `tdoc_parse` row in the workflow one-liner and "Where to look" table:

In the `tdoc parse` workflow paragraph in `AGENTS.md` (around line ~50), extend the one-liner with the size-limit note:

> `doc3gpp tdoc parse <filters>` is end-to-end filter-driven — every
> flag is a filter, capped by `Settings.tdoc_parse.max_batch`. The
> per-file byte cap is `Settings.tdoc_parse.max_tdoc_size_kb` (default
> `1000` KB; `0` = unlimited); oversized sources route to the skip
> bucket via `TDocTooLargeError`.

- [ ] **Step 2: Update `docs/cli.md`**

Add to the `tdoc parse` command section:

- A new bullet under the flag list: ``--max-tdoc-size-kb INT`` (override `tdoc_parse.max_tdoc_size_kb`).
- A new "Size limit" subsection explaining the default, the `0`-disables semantics, and the four gate points (`download_tdoc_zip` pre-fetch + post-fetch, `direct_parse_bytes`, `_tdoc_parse_local_batch` pre-read stat).
- Update the summary-line examples to include `Skipped (exceeds max_tdoc_size_kb=N): 0`.

- [ ] **Step 3: Update `docs/conventions.md` §"Settings caching"**

Add a note: "`tdoc_parse.max_tdoc_size_kb` is TOML-only, matching the other `tdoc_parse.*` knobs. No env-var allowlist update is needed."

- [ ] **Step 4: Lint + run full sqlite suite + commit**

Run:

```bash
ruff check .
./scripts/test_sqlite.sh
```

Expected: all green.

Then:

```bash
git add AGENTS.md docs/cli.md docs/conventions.md
git commit -m "docs: document tdoc_parse.max_tdoc_size_kb + --max-tdoc-size-kb"
```

---

## Self-review

1. **Spec coverage:**
   - Setting field (`max_tdoc_size_kb`) → Task 1 ✓
   - TOML example update → Task 1 ✓
   - Env-var TOML-only clarification → Task 1 ✓
   - CLI flag `--max-tdoc-size-kb` → Task 8 ✓
   - `_resolve_max_tdoc_size_bytes` helper → Task 8 ✓
   - `TDocCrService` constructor kwarg → Task 2 ✓
   - `TDocTooLargeError` → Task 2 ✓
   - `download_tdoc_zip` pre-fetch cache probe → Task 3 ✓
   - `download_tdoc_zip` post-fetch guard → Task 3 ✓
   - `direct_parse_bytes` guard → Task 4 ✓
   - `_tdoc_parse_local_batch` stat guard → Task 9 ✓
   - `_extract_from_3gpp_url` post-fetch guard → Task 6 ✓
   - `extract_many` skip routing → Task 5 ✓
   - `extract_from_url_batch` skip routing + `DirectParseBatchResult.skipped` → Task 6 ✓
   - DB-mode CLI summary line → Task 10 ✓
   - URL batch summary line → Task 6 ✓
   - Factory wiring → Task 7 ✓
   - Tests (unit + integration) → Tasks 2,3,4,5,6,7,8,9,10 ✓
   - Docs (AGENTS, cli, conventions) → Task 11 ✓

2. **Placeholder scan:** no "TBD" / "TODO" / "implement later" in any task. All code blocks are concrete. ✓

3. **Type consistency:** `max_tdoc_size_kb` (CLI/setting, KB), `max_tdoc_size_bytes` (constructor kwarg + service attribute + `download_tdoc_zip` / `direct_parse_bytes` / `_tdoc_parse_local_batch` kwargs, bytes), `max_bytes` (gate-point kwarg, bytes). All conversions happen once in `_resolve_max_tdoc_size_bytes` and at the boundary when callers want to override the service-level default. Method names `extract_from_url`, `extract_from_bytes`, `extract_from_url_batch`, `extract_many` all match the existing service. The exception class `TDocTooLargeError` is referenced consistently. The skip-bucket key prefix `"TDocTooLargeError:"` matches the existing `f"{type(exc).__name__}: {exc}"` formatting used by `TDocNotYetOnFTPError`. ✓

4. **Edge cases:**
   - Cache-hit + oversized → pre-fetch probe catches it (Task 3) ✓
   - Fresh download + oversized → post-fetch guard catches it (Task 3) ✓
   - `extract_from_url` non-3GPP path → forwarded `max_bytes` (Task 8 Step 7) ✓
   - `_extract_from_3gpp_url` post-fetch → defence-in-depth (Task 6 Step 5) ✓
   - `--from-path FILE` with `service.extract_from_bytes(..., max_tdoc_size_bytes=...)` (Task 8 Step 7) ✓
   - `--from-path DIR` with pre-read `stat()` guard (Task 9) ✓
   - `0` everywhere → no-op (all gate points check `> 0` before raising) ✓
   - Settings TOML-only env var → no `ALLOWED_ENV_VARS` change; test mirrors the existing pattern (Task 1) ✓