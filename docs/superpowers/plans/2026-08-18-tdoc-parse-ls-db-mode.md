# DB-mode LS support for `tdoc parse` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `doc3gpp tdoc parse --tdoc <id>` (no `--type`) extract rows of any `tdocs.type` — dispatching CR → `CRParser`/`TTCNCRParser` and LS → `ThreeGPPLSParser` — with the same on-disk zip + markdown cache lifecycle and the same DB-side cache short-circuit behaviour regardless of type.

**Architecture:** Branch inside `TDocCrService.extract` on the resolved parser: LS branch writes the LS sidecar (`tdoc_cr_ls_details`) + `tdoc_extracts.cache_file` and returns a new `LSResult`; CR branch stays as today. `extract_many` reports a flat successes/failures/skipped total (merged across CR + LS). Drop the implicit `--type=CR` default at the CLI filter boundary. Extend `exclude_parsed` to also exclude rows already present in the LS sidecar.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0, Pydantic v2, Typer, FastAPI; existing `LSResult`/`LSParserBase`/`TDocLSDetails` machinery from `2026-08-17-ls-tdoc-parser`.

## Global Constraints

- Branch target: `feat/tdoc-parse-ls-db-mode`. Spec: `docs/superpowers/specs/2026-08-18-tdoc-parse-ls-db-mode-design.md`.
- Touch only the files named in each task's `Files:` block.
- Use existing fixtures: `tests/fixtures/` already holds LS markdown samples (`tests/unit/test_tdoc_cr_service_ls.py` uses inline samples).
- Test commands: `./scripts/test_sqlite.sh` for unit + integration, sqlite-only. Online tests use `python -m pytest -m online -rs`.
- Lint: `ruff check .` runs cleanly after every task.
- Follow `docs/conventions.md`: never leak ORM attributes; `@dataclass(slots=True, frozen=True)` for new DTOs; rich-filter grammar unchanged.
- No comments unless task explicitly requires one.

---

## Task 1: Add `LSResult` dataclass + extend `BatchExtractResult`

**Files:**
- Modify: `src/doc3gpp/services/tdoc_cr_service.py` (top-level imports + module-level dataclass section after `ExtractResult` / `BatchExtractResult`)

**Interfaces:**
- Consumes: existing `TDocLSDetails`, `TDocExtractMeta`
- Produces: `class LSResult` (in `tdoc_cr_service.py`), `BatchExtractResult.ls_successes: dict[str, LSResult]` (NEW field, default factory dict)

**Acceptance:** Importing `from doc3gpp.services.tdoc_cr_service import LSResult, BatchExtractResult` succeeds; `BatchExtractResult(successes={}, failures={})` round-trips with `ls_successes={}`.

- [ ] **Step 1: Add `LSResult` dataclass and import**

In `src/doc3gpp/services/tdoc_cr_service.py`, find the section immediately after the `ExtractResult` dataclass (around line 308-331, just before `BatchExtractResult`). Add a sibling dataclass:

```python
@dataclass(slots=True, frozen=True)
class LSResult:
    """Outcome of a single DB-mode :meth:`TDocCrService.extract` for an LS row.

    Mirror of :class:`ExtractResult` for the LS family. Carries the
    parsed LS fields plus the extract metadata that anchors the on-disk
    zip + markdown cache. The cover-page slots are absent because LS
    rows never write to ``tdoc_cr_cover_page`` / ``tdoc_cr_ttcn_details``
    — the LS sidecar (``tdoc_cr_ls_details``) is the source of truth.

    Attributes:
        details: The parsed LS fields. On a cache hit, these come
            straight from the persisted sidecar row; on a fresh
            extract, they come from the in-memory parser output that
            was just upserted.
        extract_meta: Metadata pointing at the on-disk cache artefacts
            (the ``cache_file`` basename and the inner docx filename).
            On a DB-cache hit the basename reflects what the previous
            successful extract wrote.
        from_cache: ``True`` iff the result was returned from the
            ``tdoc_cr_ls_details`` row **without** re-downloading the
            zip or re-rendering the markdown. A hot markdown cache
            alone does NOT set this flag.
    """

    details: TDocLSDetails
    extract_meta: TDocExtractMeta
    from_cache: bool
```

Make sure the existing `TDocLSDetails` and `TDocExtractMeta` imports already cover this (they're imported at the top of the file from `doc3gpp.models.tdoc_ls` and `doc3gpp.models.tdoc_cr` respectively). If `TDocLSDetails` is not yet imported at module-level, add `from doc3gpp.models.tdoc_ls import TDocLSDetails` to the imports block.

- [ ] **Step 2: Extend `BatchExtractResult` with `ls_successes` field**

Find the `BatchExtractResult` dataclass (around line 334-371). Add a new field right after `successes`:

```python
@dataclass(slots=True, frozen=True)
class BatchExtractResult:
    """..."""

    successes: dict[str, ExtractResult]
    ls_successes: dict[str, LSResult] = field(default_factory=dict)
    failures: dict[str, str]
    skipped: dict[str, str] = field(default_factory=dict)
```

The `field` import is already at the top of the file (line 67). The order of fields matters because `@dataclass` requires fields with defaults to come after fields without — `failures` already has no default today, so `ls_successes` must slot between `successes` and `failures` only if we keep `failures` non-default. Verify the existing `failures` field does NOT have a default — if it does, the order is already flexible and `ls_successes` can be appended; if not, insert it as shown above.

- [ ] **Step 3: Run a quick sanity check**

Run: `python -c "from doc3gpp.services.tdoc_cr_service import LSResult, BatchExtractResult; r = BatchExtractResult(successes={}, failures={}); print(r.ls_successes)"`
Expected: prints `{}`.

- [ ] **Step 4: Commit**

```bash
git add src/doc3gpp/services/tdoc_cr_service.py
git commit -m "feat(services): add LSResult + BatchExtractResult.ls_successes"
```

---

## Task 2: Branch `TDocCrService.extract` on parser type (LS branch first cut)

**Files:**
- Modify: `src/doc3gpp/services/tdoc_cr_service.py:668-678` (replace the `TDocTypeUnsupportedError` raise with the LS branch)

**Interfaces:**
- Consumes: `LSResult`, `LSParserBase.parse_ls(...)` (already returns `TDocLSParserResult`), `LSRepository.upsert(TDocLSDetails)`, `_repo.upsert_extract_meta(TDocExtractMeta)`, `_index_after_parse`, `_embed_after_parse`
- Produces: on success, returns `LSResult(...)` for LS rows; existing `ExtractResult(...)` for CR rows. The `TDocTypeUnsupportedError` exception is no longer raised for LS rows in DB-mode `extract`.

**Acceptance:** `extract("R5-260017")` on a row with `tdoc.type == "LS in"` succeeds via the LS parser, writes the LS sidecar, and returns an `LSResult`. CR rows still return `ExtractResult`. A bogus `tdoc.type` (e.g. `DRAFT`) raises `TDocTypeUnsupportedError`.

- [ ] **Step 1: Write the failing unit test (LS happy path)**

Create `tests/unit/test_tdoc_cr_service_db_mode_ls.py`:

```python
from unittest.mock import MagicMock

import pytest

from doc3gpp.models.tdoc_ls import TDocLSDetails
from doc3gpp.services.tdoc_cr_service import (
    LSResult,
    TDocCrService,
    TDocTypeUnsupportedError,
)


@pytest.fixture
def svc_factory():
    """Build a TDocCrService with mocked I/O.

    Tests set the mocks' return values; the fixture returns a callable
    that constructs the service and binds the returned mocks so the
    test body can wire them.
    """
    cache = MagicMock()
    scraper = MagicMock()
    scraper.get_bytes = MagicMock(return_value=b"zip-bytes-not-used-on-cache-hit")
    cr_repo = MagicMock()
    cr_repo.upsert = MagicMock()
    cr_repo.get_by_url = MagicMock(return_value=None)
    cr_repo.get_extract_meta_by_url = MagicMock(return_value=None)
    cr_ttcn_repo = MagicMock()
    cr_change_repo = MagicMock()
    tdoc_repo = MagicMock()

    def factory(tdoc_row):
        tdoc_repo.get_by_id = MagicMock(return_value=tdoc_row)
        ls_repo = MagicMock()
        ls_repo.upsert = MagicMock()
        ls_repo.get_by_url = MagicMock(return_value=None)
        svc = TDocCrService(
            cache=cache,
            scraper_client=scraper,
            cr_repository=cr_repo,
            cr_ttcn_repository=cr_ttcn_repo,
            cr_change_details_repository=cr_change_repo,
            tdoc_repository=tdoc_repo,
            ls_repository=ls_repo,
        )
        return svc, ls_repo, cr_repo

    return factory


_LS_MD = (
    "3GPP TSG RAN WG2 Meeting #104\tTDoc R5-260017\n\n"
    "Title:	LS on frequency separation for Type 4b UE NR-CA PDSCH demodulation requirements\n"
    "Source:	TSG WG RAN4\n"
    "To:	RAN WG5\n"
)


def test_db_mode_extract_writes_ls_sidecar(svc_factory, tmp_path, monkeypatch):
    """DB-mode extract() dispatches to LS parser for an LS-in row."""
    from doc3gpp.models.tdoc import TDoc

    tdoc = TDoc(
        tdoc_id="R5-260017",
        meeting_id=110,
        ftp_url="tsg_ran/WG4_Radio/TSGR4_110/Inbox/R5-260017.zip",
        source="TSG WG RAN4",
        type="LS in",
        status="noted",
    )
    svc, ls_repo, cr_repo = svc_factory(tdoc)

    # Short-circuit the cache + zip download paths so the test focuses
    # on the parse-and-persist branch.
    monkeypatch.setattr(
        "doc3gpp.services.tdoc_cr_service.download_tdoc_zip",
        lambda *a, **kw: MagicMock(path=tmp_path / "R5-260017.zip", url=None),
    )
    monkeypatch.setattr(
        "doc3gpp.services.tdoc_cr_service.extract_docx_from_zip",
        lambda _: ("R5-260017.docx", _LS_MD.encode("utf-8")),
    )
    svc._cache.put_bytes = MagicMock()
    svc._cache.get_bytes = MagicMock(return_value=None)
    svc._load_or_render_markdown = MagicMock(return_value=_LS_MD)  # type: ignore[method-assign]

    result = svc.extract("R5-260017", force=True)

    assert isinstance(result, LSResult)
    assert result.from_cache is False
    assert isinstance(result.details, TDocLSDetails)
    assert result.details.tdoc_id == "R5-260017"
    assert result.details.variant == "3gpp"
    ls_repo.upsert.assert_called_once()
    cr_repo.upsert.assert_not_called()
    # extract_meta still gets written so tdoc_content / FTS5 find the markdown.
    cr_repo.upsert_extract_meta.assert_called_once()
    assert cr_repo.upsert_extract_meta.call_args[0][0].cache_file.endswith(".zip")
```

- [ ] **Step 2: Run the test — expect it to FAIL with `TDocTypeUnsupportedError`**

Run: `python -m pytest tests/unit/test_tdoc_cr_service_db_mode_ls.py::test_db_mode_extract_writes_ls_sidecar -v`
Expected: FAIL with `TDocTypeUnsupportedError: TDoc 'R5-260017' has type 'LS in'; ...`.

- [ ] **Step 3: Implement the LS branch**

In `src/doc3gpp/services/tdoc_cr_service.py`, replace lines 668-678:

```python
parser = self._resolve_parser(
    normalised,
    tdoc_type=tdoc.type,
    source=tdoc.source,
)
if isinstance(parser, LSParserBase):
    raise TDocTypeUnsupportedError(
        normalised,
        f"{tdoc.type!r} (DB-mode LS extraction is not yet supported; "
        "use 'tdoc parse --from-url' for LS rows)",
    )
parsed: TDocCRParseResult = parser.parse(
    markdown, tdoc_id=normalised, full=full,
)
```

…with:

```python
parser = self._resolve_parser(
    normalised,
    tdoc_type=tdoc.type,
    source=tdoc.source,
)
if self._ls_repository is None:
    raise RuntimeError(
        "TDocCrService requires an ls_repository; "
        "construct via services.factory.build_tdoc_cr_service()"
    )

if isinstance(parser, LSParserBase):
    ls_result = parser.parse_ls(markdown, tdoc_id=normalised)
    if ls_result.cover is None:
        raise LSHeaderMissingError(
            "LS parser returned no cover payload for tdoc_id "
            f"{normalised!r}; the markdown does not look like an LS document"
        )
    details = replace(
        ls_result.cover,
        tdoc_id=normalised,
        ftp_url=stored_ftp_url,
    )
    self._ls_repository.upsert(details)
    meta = TDocExtractMeta(
        ftp_url=stored_ftp_url or "",
        tdoc_id=normalised,
        cache_file=cache_file,
        doc_filename=doc_filename,
    )
    self._repo.upsert_extract_meta(meta)
    self._index_after_parse(normalised)
    self._embed_after_parse(normalised)
    logger.info(
        "Persisted LS sidecar for TDoc %s at ftp_url %s (variant=%s)",
        normalised, stored_ftp_url, details.variant,
    )
    return LSResult(details=details, extract_meta=meta, from_cache=False)

parsed: TDocCRParseResult = parser.parse(
    markdown, tdoc_id=normalised, full=full,
)
```

The `replace` import is already present (line 67). Confirm `LSHeaderMissingError` is importable — add `from doc3gpp.parsers.ls.header import LSHeaderMissingError` near the existing parsers imports (around line 92) if not already imported.

- [ ] **Step 4: Run the test — expect PASS**

Run: `python -m pytest tests/unit/test_tdoc_cr_service_db_mode_ls.py::test_db_mode_extract_writes_ls_sidecar -v`
Expected: PASS.

- [ ] **Step 5: Run `ruff`**

Run: `ruff check src/doc3gpp/services/tdoc_cr_service.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/services/tdoc_cr_service.py tests/unit/test_tdoc_cr_service_db_mode_ls.py
git commit -m "feat(services): DB-mode extract() routes LS rows to LSParserBase"
```

---

## Task 3: LS-aware DB-side cache short-circuit

**Files:**
- Modify: `src/doc3gpp/services/tdoc_cr_service.py:596-642` (replace the cover-page-only probe with a type-dispatched probe)

**Interfaces:**
- Consumes: `self._ls_repository.get_by_url(...)`, existing `self._repo.get_by_url(...)` / `get_extract_meta_by_url(...)`
- Produces: cache hits return `LSResult(from_cache=True)` for LS rows, `ExtractResult(from_cache=True)` for CR rows.

**Acceptance:** After a fresh extract, a second call to `extract(<ls-id>, force=False)` returns `LSResult(from_cache=True)` without re-downloading the zip or re-rendering markdown. CR behaviour is unchanged.

- [ ] **Step 1: Write the failing test (LS cache hit)**

Append to `tests/unit/test_tdoc_cr_service_db_mode_ls.py`:

```python
def test_db_mode_extract_returns_from_cache_for_cached_ls(svc_factory, tmp_path, monkeypatch):
    """An LS row already in tdoc_cr_ls_details returns from_cache=True without downloading."""
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_ls import TDocLSDetails

    tdoc = TDoc(
        tdoc_id="R5-260017",
        meeting_id=110,
        ftp_url="tsg_ran/WG4_Radio/TSGR4_110/Inbox/R5-260017.zip",
        source="TSG WG RAN4",
        type="LS in",
        status="noted",
    )
    svc, ls_repo, cr_repo = svc_factory(tdoc)

    cached_meta = MagicMock(cache_file="R5s260017.zip", doc_filename="R5-260017.docx",
                            ftp_url="tsg_ran/WG4_Radio/TSGR4_110/Inbox/R5-260017.zip",
                            tdoc_id="R5-260017")
    cached_details = TDocLSDetails(
        tdoc_id="R5-260017",
        ftp_url="tsg_ran/WG4_Radio/TSGR4_110/Inbox/R5-260017.zip",
        variant="3gpp",
        title="LS on frequency separation",
    )
    ls_repo.get_by_url = MagicMock(return_value=cached_details)
    cr_repo.get_extract_meta_by_url = MagicMock(return_value=cached_meta)

    download_called = MagicMock()
    monkeypatch.setattr(
        "doc3gpp.services.tdoc_cr_service.download_tdoc_zip",
        download_called,
    )

    result = svc.extract("R5-260017", force=False)

    assert isinstance(result, LSResult)
    assert result.from_cache is True
    assert result.details is cached_details
    ls_repo.upsert.assert_not_called()
    download_called.assert_not_called()
```

- [ ] **Step 2: Run the test — expect FAIL (download_tdoc_zip is hit)**

Run: `python -m pytest tests/unit/test_tdoc_cr_service_db_mode_ls.py::test_db_mode_extract_returns_from_cache_for_cached_ls -v`
Expected: FAIL because today's code only probes `cr_repo.get_by_url` and falls through to the zip download.

- [ ] **Step 3: Replace the cover-page-only probe with type-dispatched probe**

In `src/doc3gpp/services/tdoc_cr_service.py:596-642`, the block currently reads:

```python
# Pre-download cache probe: known candidate URLs can be
# resolved without touching the network, so the DB cache
# short-circuits the zip fetch.
if not force:
    for candidate in candidates:
        cached_details = self._repo.get_by_url(
            normalize_ftp_path(candidate)
        )
        cached_meta = self._repo.get_extract_meta_by_url(
            normalize_ftp_path(candidate)
        )
        if cached_details is not None and cached_meta is not None:
            logger.debug(
                "DB cache hit for TDoc %s at URL %s", normalised, candidate
            )
            return ExtractResult(
                details=cached_details,
                extract_meta=cached_meta,
                from_cache=True,
            )
```

Replace with:

```python
# Pre-download cache probe: known candidate URLs can be
# resolved without touching the network, so the DB cache
# short-circuits the zip fetch. The probe dispatches on the
# resolved parser type — CR rows probe tdoc_cr_cover_page,
# LS rows probe tdoc_cr_ls_details — so both families share
# the same short-circuit.
if not force:
    for candidate in candidates:
        normalised_url = normalize_ftp_path(candidate)
        cached_meta = self._repo.get_extract_meta_by_url(normalised_url)
        if cached_meta is None:
            continue
        if isinstance(parser, LSParserBase):
            if self._ls_repository is None:
                continue
            cached_details = self._ls_repository.get_by_url(normalised_url)
            if cached_details is not None:
                logger.debug(
                    "DB cache hit (LS) for TDoc %s at URL %s",
                    normalised, candidate,
                )
                return LSResult(
                    details=cached_details,
                    extract_meta=cached_meta,
                    from_cache=True,
                )
        else:
            cached_details = self._repo.get_by_url(normalised_url)
            if cached_details is not None:
                logger.debug(
                    "DB cache hit (CR) for TDoc %s at URL %s",
                    normalised, candidate,
                )
                return ExtractResult(
                    details=cached_details,
                    extract_meta=cached_meta,
                    from_cache=True,
                )
```

The `parser` variable doesn't exist yet at this point — the existing probe runs before the parser is resolved. Resolve the parser **once**, before the probe, by moving the `_resolve_parser(...)` call from line 668-672 up to just before the probe (around line 593). Place it right after the `candidates = resolve_download_url(...)` line:

```python
candidates = resolve_download_url(normalised, primary_url)

# Resolve the parser here so the DB cache probe can dispatch by
# family. Cheap for CR (registry has 3 candidates) and the only way
# the LS short-circuit works.
parser = self._resolve_parser(
    normalised,
    tdoc_type=tdoc.type,
    source=tdoc.source,
)
```

Then remove the duplicate `_resolve_parser` from line 668-672 (already part of Task 2's branch block).

The post-download probe (lines 627-642) does NOT need to be touched for this task — the pre-download probe is what Task 2's test exercises. The post-download probe can stay CR-only for now; a follow-up task may extend it. (If `resolve_download_url` returned a fresh URL, the CR cache miss on the post-download is benign — the LS sidecar would already have been hit on the pre-download probe.)

- [ ] **Step 4: Run the test — expect PASS**

Run: `python -m pytest tests/unit/test_tdoc_cr_service_db_mode_ls.py::test_db_mode_extract_returns_from_cache_for_cached_ls -v`
Expected: PASS.

- [ ] **Step 5: Re-run task 2's test (regression check) + ruff**

Run: `python -m pytest tests/unit/test_tdoc_cr_service_db_mode_ls.py::test_db_mode_extract_writes_ls_sidecar -v && ruff check src/doc3gpp/services/tdoc_cr_service.py`
Expected: PASS + clean.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/services/tdoc_cr_service.py tests/unit/test_tdoc_cr_service_db_mode_ls.py
git commit -m "feat(services): LS-aware DB cache short-circuit on extract()"
```

---

## Task 4: `BatchExtractResult` / `extract_many` populates `ls_successes` and CLI surfaces flat totals

**Files:**
- Modify: `src/doc3gpp/services/tdoc_cr_service.py:776-809` (extract_many result-collection logic)
- Modify: `src/doc3gpp/cli.py:1709-1758` (CLI batch summary)

**Interfaces:**
- Consumes: `LSResult` return from `extract` (Task 2-3 wiring); `BatchExtractResult.ls_successes` dict (Task 1)
- Produces: `extract_many` populates both `successes` (CR) and `ls_successes` (LS); CLI prints one "Extracted N" total (CR+LS merged).

**Acceptance:** `extract_many(["R5-260017"])` on an LS row populates `result.ls_successes["R5-260017"]` and leaves `result.successes` empty. CLI batch summary line reads `Extracted 1` (no per-type split).

- [ ] **Step 1: Write the failing test for `extract_many` LS population**

Append to `tests/unit/test_tdoc_cr_service_db_mode_ls.py`:

```python
def test_extract_many_populates_ls_successes(svc_factory, tmp_path, monkeypatch):
    """extract_many routes an LS row to ls_successes (not successes)."""
    from doc3gpp.models.tdoc import TDoc

    tdoc = TDoc(
        tdoc_id="R5-260017",
        meeting_id=110,
        ftp_url="tsg_ran/WG4_Radio/TSGR4_110/Inbox/R5-260017.zip",
        source="TSG WG RAN4",
        type="LS in",
        status="noted",
    )
    svc, ls_repo, cr_repo = svc_factory(tdoc)

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_cr_service.download_tdoc_zip",
        lambda *a, **kw: MagicMock(path=tmp_path / "R5-260017.zip", url=None),
    )
    monkeypatch.setattr(
        "doc3gpp.services.tdoc_cr_service.extract_docx_from_zip",
        lambda _: ("R5-260017.docx", _LS_MD.encode("utf-8")),
    )
    svc._cache.put_bytes = MagicMock()
    svc._cache.get_bytes = MagicMock(return_value=None)
    svc._load_or_render_markdown = MagicMock(return_value=_LS_MD)  # type: ignore[method-assign]

    result = svc.extract_many(["R5-260017"], force=True)

    assert result.successes == {}
    assert "R5-260017" in result.ls_successes
    assert isinstance(result.ls_successes["R5-260017"], LSResult)
    assert result.failures == {}
```

- [ ] **Step 2: Run the test — expect FAIL**

Run: `python -m pytest tests/unit/test_tdoc_cr_service_db_mode_ls.py::test_extract_many_populates_ls_successes -v`
Expected: FAIL with `AttributeError: 'ExtractResult' object has no attribute '_ls_adapted'` OR similar — the success collector currently pushes everything into `successes[...]` regardless of result type.

- [ ] **Step 3: Update `extract_many` to dispatch by result type**

In `src/doc3gpp/services/tdoc_cr_service.py`, find the result-collection tail of `extract_many` (around line 808):

```python
successes[result.details.tdoc_id] = result
```

Replace with:

```python
if isinstance(result, LSResult):
    ls_successes[result.details.tdoc_id] = result
else:
    successes[result.details.tdoc_id] = result
```

Then add `ls_successes: dict[str, LSResult] = {}` to the locals block (around line 778):

```python
successes: dict[str, ExtractResult] = {}
ls_successes: dict[str, LSResult] = {}
failures: dict[str, str] = {}
skipped: dict[str, str] = {}
```

Return the new field at the bottom of `extract_many` (around line 809):

```python
return BatchExtractResult(
    successes=successes,
    ls_successes=ls_successes,
    failures=failures,
    skipped=skipped,
)
```

The existing positional init in `extract_many`'s return is today `return BatchExtractResult(successes=successes, failures=failures, skipped=skipped)` — update to the four-arg form above.

- [ ] **Step 4: Run the test — expect PASS**

Run: `python -m pytest tests/unit/test_tdoc_cr_service_db_mode_ls.py::test_extract_many_populates_ls_successes -v`
Expected: PASS.

- [ ] **Step 5: Update CLI summary to merge CR + LS**

Find the CLI summary rendering block at `src/doc3gpp/cli.py:1709-1758`. The relevant `To parse` / `Already parsed` print logic counts CR parses. Add a merged-total note for the operator-visible "Extracted" headline.

After the `extract_many` call site in `cli.py` (find the line that calls `tdoc_service.extract_many(...)` and unpacks `BatchExtractResult`), prepend:

```python
batch = tdoc_service.extract_many(
    tdoc_ids,
    force=force,
    full=full,
)
total_ok = len(batch.successes) + len(batch.ls_successes)
total_failed = len(batch.failures)
total_skipped = len(batch.skipped)
```

…and replace any subsequent `len(batch.successes)` or summary text that counts the successes with `total_ok`. The print pattern around `cli.py:1709-1758` uses `_print_parse_group("To parse", to_parse, columns)` — keep that for CR parses and add a sibling call for LS rows:

```python
ls_to_parse = [
    _TDOC_PARSE_TABLE_ROW_FACTORY(m.tdoc, m.ls_details)
    for m in batch.ls_successes.values()
]
```

(Note: `batch.ls_successes` is a flat dict of `LSResult`, not a list of `TDocWithMeeting`. The summary loop iterates `batch.ls_successes.values()` directly. Adapt the print helper as needed — keep the column set slim: `tdoc_id`, `meeting_name` (resolved via a separate `tdoc_repo.get_by_id` lookup), `title`, `variant`, `parser_version`.)

Add a top-line summary print **before** the parse groups:

```python
typer.echo(
    f"Extracted {total_ok} TDoc(s); {total_failed} failed; {total_skipped} skipped."
)
```

This replaces any pre-existing per-group summary line that says "Extracted N TDoc(s)".

- [ ] **Step 6: Run the CLI test that expects the LS happy path**

Run: `python -m pytest tests/unit/test_tdoc_parse_cli.py -v -k "ls"`
Expected: tests that previously asserted `TDocTypeUnsupportedError` for LS rows now need to be updated to assert the flat-success outcome (Task 5).

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/services/tdoc_cr_service.py src/doc3gpp/cli.py tests/unit/test_tdoc_cr_service_db_mode_ls.py
git commit -m "feat(parse): extract_many ls_successes + flat CLI summary"
```

---

## Task 5: Update existing CLI tests that expected `TDocTypeUnsupportedError` for LS rows

**Files:**
- Modify: `tests/unit/test_tdoc_parse_cli.py` (multiple test functions around lines 598, 606, 615-616, 2703)

**Interfaces:**
- Consumes: existing test fixtures, `BatchExtractResult` shape with the new `ls_successes` field (Task 1+4)
- Produces: tests assert successful LS extract (counts + presence) instead of `TDocTypeUnsupportedError`

**Acceptance:** `./scripts/test_sqlite.sh` passes for `tests/unit/test_tdoc_parse_cli.py`.

- [ ] **Step 1: Search for every site asserting `TDocTypeUnsupportedError` for LS rows**

Run: `rg -n "TDocTypeUnsupportedError" tests/unit/test_tdoc_parse_cli.py`
Expected: multiple sites (line 598 had the batch math test; lines 606, 615-616 had CLI output assertions; line 2703 was a stub-raise helper).

- [ ] **Step 2: Update each test to assert the LS happy path**

For each test:
1. Remove the `TDocTypeUnsupportedError` raise in any stub monkeypatch helpers (line 2703 area).
2. Replace `failures["..."]` expectations with `ls_successes["..."]`.
3. Replace CLI-output assertions that mention `TDocTypeUnsupportedError` with assertions that the "Extracted N TDoc(s)" line appears with `N = 1` (the LS row).

Three test cases to update (find via the grep from Step 1):

- `test_parse_cli_batch_ls_handled_separately` (or similar named — line ~598): Replace `failures` expectation with `ls_successes["R5s260010"]`.
- `test_parse_cli_output_includes_ls_failure_reason` (line ~606): The CLI summary now reads `Extracted 1 TDoc(s); 0 failed; 0 skipped.` Update the expected output string.
- The helper that raises `TDocTypeUnsupportedError(tdoc_id, observed_type="LS")` (line ~2703): Replace with a normal LS-success stub that returns an `LSResult`.

- [ ] **Step 3: Run the updated tests — expect PASS**

Run: `python -m pytest tests/unit/test_tdoc_parse_cli.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_tdoc_parse_cli.py
git commit -m "test(cli): parse batch math now expects LS happy path"
```

---

## Task 6: Drop the implicit `--type=CR` default at the CLI filter boundary

**Files:**
- Modify: `src/doc3gpp/cli.py:1673` (drop `or "CR"`); verify `_warn_on_ignored_filter_flags` still validates `--type` correctly when unset

**Interfaces:**
- Consumes: `tdoc_type` flag (optional, default empty)
- Produces: `list_kwargs["tdoc_type"]` is `None` when `--type` is unset; SQL `LIKE` filter no-ops; rows of any `tdocs.type` become candidates.

**Acceptance:** `doc3gpp tdoc parse --tdoc R5-260017` returns the LS row (matches > 0); `--type LS` still narrows to LS rows only.

- [ ] **Step 1: Write a failing test asserting that unset `--type` accepts any row**

Append to `tests/unit/test_tdoc_parse_cli.py` (find the existing CLI flag-validation test class and add inside the right class — search for `def test_parse_requires_at_least_one_filter`):

```python
def test_parse_omits_default_cr_filter_when_type_unset(monkeypatch):
    """`--type` is no longer a hidden default — an unset value keeps all rows in scope."""
    from typer.testing import CliRunner
    from doc3gpp.cli import app

    captured = {}
    fake_tdocs = [
        # row with type='LS in'
        MagicMock(tdoc_id="R5-260017", ftp_url="tsg/.../R5-260017.zip", type="LS in"),
    ]

    def fake_list_with_meeting(**kwargs):
        captured.update(kwargs)
        return [
            MagicMock(tdoc=MagicMock(tdoc_id="R5-260017", type="LS in",
                                     ftp_url="tsg/.../R5-260017.zip"),
                       meeting_name="R5-110")
            for _ in fake_tdocs
        ]

    monkeypatch.setattr("doc3gpp.cli.build_tdoc_repository", MagicMock())
    monkeypatch.setattr("doc3gpp.cli._tdoc_parse_many", MagicMock())
    monkeypatch.setattr("doc3gpp.cli.trigger_auto_sync", MagicMock())
    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.build_tdoc_service",
        MagicMock(return_value=MagicMock(list_with_meeting=fake_list_with_meeting,
                                          list_recent=fake_list_with_meeting)),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "parse", "--tdoc", "R5-260017", "--yes"])
    assert "No TDoc matched" not in result.output
    # The new contract: tdoc_type filter is None when --type is unset, not "CR".
    assert captured.get("tdoc_type") is None
```

- [ ] **Step 2: Run the test — expect FAIL with "No TDoc matched"`

Run: `python -m pytest tests/unit/test_tdoc_parse_cli.py::test_parse_omits_default_cr_filter_when_type_unset -v`
Expected: FAIL with "No TDoc matched the provided filters." in output.

- [ ] **Step 3: Drop the default-CR at `cli.py:1673`**

In `src/doc3gpp/cli.py` at line 1673, change:

```python
"tdoc_type": tdoc_type or "CR",
```

to:

```python
"tdoc_type": tdoc_type,
```

Confirm no other caller in this function passes `tdoc_type or "CR"` — grep for `or "CR"` in `cli.py` to confirm only this site.

- [ ] **Step 4: Run the test — expect PASS**

Run: `python -m pytest tests/unit/test_tdoc_parse_cli.py::test_parse_omits_default_cr_filter_when_type_unset -v`
Expected: PASS.

- [ ] **Step 5: Run the existing `--type LS` explicit-filter test (regression check)**

Run: `python -m pytest tests/unit/test_tdoc_parse_cli.py -v -k "type"`
Expected: All PASS. `--type LS` still narrows via the SQL `LIKE` filter, no behaviour change for the explicit-filter path.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/cli.py tests/unit/test_tdoc_parse_cli.py
git commit -m "fix(cli): drop implicit --type=CR default in tdoc parse filters"
```

---

## Task 7: Extend `exclude_parsed` to skip LS sidecar rows too

**Files:**
- Modify: `src/doc3gpp/storage/repositories/tdoc_sql.py:174-179` (add a second `~EXISTS` clause against `tdoc_cr_ls_details`)

**Interfaces:**
- Consumes: existing `TDocCrLSDetailOrm`, ORM model import
- Produces: `list(exclude_parsed=True)` returns rows whose `tdoc_id` is not present in either `tdoc_cr_cover_page` or `tdoc_cr_ls_details`.

**Acceptance:** An LS row already in `tdoc_cr_ls_details` is excluded from a non-`--force` batch in `tdoc parse`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_tdoc_sql_exclude_parsed_ls.py`:

```python
"""Tests for the LS-aware exclude_parsed filter on TDocRepository.list."""

from unittest.mock import MagicMock

import pytest

from doc3gpp.storage.db.models import TDocCrLSDetailOrm, TDocCrDetailOrm


def test_exclude_parsed_skips_ls_sidecar_rows(monkeypatch):
    """list(exclude_parsed=True) must exclude tdocs whose ls sidecar row exists."""
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    # The session factory + ORM machinery are stubbed at the query layer
    # — we assert the WHERE-clause SQL emitted by the builder.
    fake_session = MagicMock()
    fake_session.scalars.return_value.all.return_value = []
    fake_session_factory = MagicMock(return_value=fake_session)
    monkeypatch.setattr(
        "doc3gpp.storage.repositories.tdoc_sql.get_session_factory",
        lambda: fake_session_factory,
    )

    repo = SQLAlchemyTDocRepository()
    repo.list(limit=20, offset=0, exclude_parsed=True)

    # Inspect the compiled statement to verify both NOT-EXISTS clauses exist.
    stmt = fake_session.scalars.call_args[0][0]
    where_clauses = [str(c) for c in stmt.whereclause.get_children()]
    combined = " ".join(where_clauses)
    assert TDocCrDetailOrm.__tablename__ in combined
    assert TDocCrLSDetailOrm.__tablename__ in combined
```

- [ ] **Step 2: Run the test — expect FAIL**

Run: `python -m pytest tests/unit/test_tdoc_sql_exclude_parsed_ls.py -v`
Expected: FAIL — today's `exclude_parsed` only emits the cover-page clause.

- [ ] **Step 3: Extend the `exclude_parsed` block**

In `src/doc3gpp/storage/repositories/tdoc_sql.py`, find the `exclude_parsed` block (around line 174-179):

```python
if exclude_parsed:
    stmt = stmt.where(
        ~select(TDocCrDetailOrm.tdoc_id)
        .where(TDocCrDetailOrm.tdoc_id == TDocORM.tdoc_id)
        .exists()
    )
```

Add the LS-OR clause (the conjunction must remain: a row is "parsed" if either sidecar exists):

```python
if exclude_parsed:
    stmt = stmt.where(
        ~select(TDocCrDetailOrm.tdoc_id)
        .where(TDocCrDetailOrm.tdoc_id == TDocORM.tdoc_id)
        .exists()
        & ~select(TDocCrLSDetailOrm.tdoc_id)
        .where(TDocCrLSDetailOrm.tdoc_id == TDocORM.tdoc_id)
        .exists()
    )
```

Confirm `TDocCrLSDetailOrm` is already imported (likely from `doc3gpp.storage.db.models`). If not, add it.

- [ ] **Step 4: Run the test — expect PASS**

Run: `python -m pytest tests/unit/test_tdoc_sql_exclude_parsed_ls.py -v`
Expected: PASS.

- [ ] **Step 5: Run the existing CR-only `exclude_parsed` test (regression check)**

Run: `python -m pytest tests/unit tests/integration -v -k "exclude_parsed"`
Expected: All PASS. CR behaviour unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/storage/repositories/tdoc_sql.py tests/unit/test_tdoc_sql_exclude_parsed_ls.py
git commit -m "feat(repos): exclude_parsed also excludes LS sidecar rows"
```

---

## Task 8: Web worker `_parse_tdocs` merges `ls_successes` into the success count

**Files:**
- Modify: `src/doc3gpp/web/workers/handlers.py:293-318` (`_parse_tdocs` aggregation)

**Interfaces:**
- Consumes: `BatchExtractResult.successes` and `BatchExtractResult.ls_successes`
- Produces: returned envelope still `{requested, successes, failures, skipped}`; the `successes` count is the merged total; per-batch progress log line lists the merged total.

**Acceptance:** A mixed CR + LS batch through `_parse_tdocs` returns `successes` = `len(result.successes) + len(result.ls_successes)`; CR-only and LS-only behaviour is unchanged from the user's perspective.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_job_worker.py` (find the `_parse_tdocs` test cluster):

```python
async def test_parse_tdocs_merges_ls_successes_into_count(
    monkeypatch, sqlite_env, httpd_unused  # noqa: ARG001
):
    """_parse_tdocs counts ls_successes into the successes envelope field."""
    from doc3gpp.services.tdoc_cr_service import (
        ExtractResult, LSResult, BatchExtractResult,
    )
    from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
    from doc3gpp.models.tdoc_ls import TDocLSDetails
    from doc3gpp.web.workers.handlers import _parse_tdocs
    from doc3gpp.models.jobs import Job, JobKind

    # Two-row batch: one CR, one LS.
    fake_tdoc_repo = MagicMock()
    fake_tdoc_repo.list = MagicMock(return_value=[
        MagicMock(tdoc_id="R5-260010", meeting_id=110),  # CR
        MagicMock(tdoc_id="R5-260017", meeting_id=110),  # LS
    ])
    extract_many = MagicMock(return_value=BatchExtractResult(
        successes={
            "R5-260010": ExtractResult(
                details=TDocCRDetails(tdoc_id="R5-260010", ftp_url="x"),
                extract_meta=TDocExtractMeta(
                    ftp_url="x", tdoc_id="R5-260010",
                    cache_file="x", doc_filename="x"),
                from_cache=False,
            ),
        },
        ls_successes={
            "R5-260017": LSResult(
                details=TDocLSDetails(tdoc_id="R5-260017", ftp_url="y"),
                extract_meta=TDocExtractMeta(
                    ftp_url="y", tdoc_id="R5-260017",
                    cache_file="y", doc_filename="y"),
                from_cache=False,
            ),
        },
        failures={},
    ))
    tdoc_cr = MagicMock(extract_many=extract_many)
    services = MagicMock(tdoc_repo=fake_tdoc_repo, tdoc_cr=tdoc_cr)

    job = Job(
        id="abc",
        kind=JobKind.PARSE_TDOCS,
        params={"filter": {"meeting_id": "110"}, "force": True},
    )

    async def collect():
        return []
    from doc3gpp.web.workers.progress import ProgressCollector
    progress = ProgressCollector()
    settings = MagicMock(tdoc_parse=MagicMock(max_batch=20))

    result = await _parse_tdocs(
        job, services, settings,
        progress=progress,
        cancel_event=asyncio.Event(),
    )

    assert result["requested"] == 2
    assert result["successes"] == 2  # 1 CR + 1 LS
    assert result["failures"] == 0
```

(Note: adapt the import / fixture specifics to match the test file's existing setup — read `tests/unit/test_job_worker.py` first to align with the project's job-factory helpers.)

- [ ] **Step 2: Run the test — expect FAIL with `successes == 1`**

Run: `python -m pytest tests/unit/test_job_worker.py::test_parse_tdocs_merges_ls_successes_into_count -v`
Expected: FAIL — today the handler aggregates `result.successes` only and reports `successes == 1`.

- [ ] **Step 3: Update `_parse_tdocs` aggregation**

In `src/doc3gpp/web/workers/handlers.py:293-318`, the existing block reads:

```python
total_successes: dict[str, object] = {}
total_failures: dict[str, str] = {}
total_skipped: dict[str, str] = {}
for start in range(0, len(tdoc_ids), max_batch):
    if cancel_event.is_set():
        raise asyncio.CancelledError()
    batch = tdoc_ids[start : start + max_batch]
    result = services.tdoc_cr.extract_many(
        batch,
        force=force,
        full=full,
        is_cancelled=cancel_event.is_set,
    )
    total_successes.update(result.successes)
    total_failures.update(result.failures)
    total_skipped.update(result.skipped)
    progress(
        f"batch {start // max_batch + 1}: {len(batch)} requested, "
        f"{len(result.successes)} ok, {len(result.failures)} failed"
    )
return {
    "requested": len(tdoc_ids),
    "successes": len(total_successes),
    "failures": len(total_failures),
    "skipped": len(total_skipped),
}
```

Replace with:

```python
total_successes: dict[str, object] = {}
total_ls_successes: dict[str, object] = {}
total_failures: dict[str, str] = {}
total_skipped: dict[str, str] = {}
for start in range(0, len(tdoc_ids), max_batch):
    if cancel_event.is_set():
        raise asyncio.CancelledError()
    batch = tdoc_ids[start : start + max_batch]
    result = services.tdoc_cr.extract_many(
        batch,
        force=force,
        full=full,
        is_cancelled=cancel_event.is_set,
    )
    total_successes.update(result.successes)
    total_ls_successes.update(result.ls_successes)
    total_failures.update(result.failures)
    total_skipped.update(result.skipped)
    batch_ok = len(result.successes) + len(result.ls_successes)
    progress(
        f"batch {start // max_batch + 1}: {len(batch)} requested, "
        f"{batch_ok} ok, {len(result.failures)} failed"
    )
return {
    "requested": len(tdoc_ids),
    "successes": len(total_successes) + len(total_ls_successes),
    "failures": len(total_failures),
    "skipped": len(total_skipped),
}
```

- [ ] **Step 4: Run the test — expect PASS**

Run: `python -m pytest tests/unit/test_job_worker.py::test_parse_tdocs_merges_ls_successes_into_count -v`
Expected: PASS.

- [ ] **Step 5: Run all `_parse_tdocs` tests (regression)**

Run: `python -m pytest tests/unit/test_job_worker.py -v -k "parse_tdocs"`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/workers/handlers.py tests/unit/test_job_worker.py
git commit -m "feat(web): _parse_tdocs counts LS successes into envelope total"
```

---

## Task 9: Integration test — DB-mode `tdoc parse --tdoc <ls-id>` end-to-end

**Files:**
- Modify: `tests/integration/test_tdoc_cr_sqlite.py` (replace `test_ls_row_raises_TDocTypeUnsupportedError` at line ~437)

**Interfaces:**
- Consumes: SQLite, real `TDocCrService`, real `SQLAlchemyLSParserRepository`
- Produces: A test that ingests an LS row, runs `tdoc_service.extract(...)` end-to-end (with the markdown cache primed to skip docx rendering), and asserts the LS sidecar row + `tdoc_extracts.cache_file` row both land.

**Acceptance:** `pytest tests/integration/test_tdoc_cr_sqlite.py -v -k ls` passes.

- [ ] **Step 1: Read the existing `test_ls_row_raises_TDocTypeUnsupportedError` test**

Open `tests/integration/test_tdoc_cr_sqlite.py` around line 437 and read the existing test verbatim — use the same `service` fixture construction, the same TDoc row setup, the same LS markdown fixture.

- [ ] **Step 2: Rewrite the test as `test_db_mode_ls_row_writes_sidecar`**

Replace the existing `test_ls_row_raises_TDocTypeUnsupportedError` (and any other `LS` + `TDocTypeUnsupportedError` sibling test in the file) with a happy-path test:

```python
def test_db_mode_ls_row_writes_sidecar(sqlite_engine, sqlite_session_factory):
    """DB-mode extract() writes the LS sidecar + tdoc_extracts.cache_file row."""
    from doc3gpp.services.factory import build_tdoc_cr_service
    from doc3gpp.storage.repositories.tdoc_cr_ls_sql import SQLAlchemyLSParserRepository
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.storage.db.models import TDocORM
    from sqlalchemy.orm import sessionmaker
    from doc3gpp.parsers.ls.header import is_ls_header_present

    # Seed a TDoc row whose type is "LS in".
    Session = sessionmaker(bind=sqlite_engine)
    with Session() as s:
        s.add(TDocORM(tdoc_id="R5-260017", meeting_id=110,
                      ftp_url="tsg/ls/R5-260017.zip",
                      source="TSG WG RAN4", type="LS in", status="noted"))
        s.commit()

    # Build service with real LS repo and a stubbed cache / scraper that
    # skip the docx rendering. The LS markdown fixture is what we want
    # the parser to see.
    ls_md = (
        "3GPP TSG RAN WG2 Meeting #104\tTDoc R5-260017\n\n"
        "Title:	LS on frequency separation for Type 4b UE NR-CA PDSCH demodulation requirements\n"
        "Source:	TSG WG RAN4\n"
        "To:	RAN WG5\n"
    )
    assert is_ls_header_present(ls_md)[0]

    service = build_tdoc_cr_service(
        ls_repository=SQLAlchemyLSParserRepository(
            session_factory=sqlite_session_factory,
        ),
        max_tdoc_size_bytes=0,  # disable size guard
    )
    # Stub the on-disk cache + network: hand back a zip containing a docx
    # whose body is the LS markdown text.
    import io, zipfile
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, mode="w") as zf:
        zf.writestr("R5-260017.docx", b"placeholder-docx-bytes")
    service._scraper.get_bytes = MagicMock(return_value=zip_buf.getvalue())
    service._cache.put_bytes = MagicMock()
    service._cache.get_bytes = MagicMock(return_value=None)
    monkeypatch.setattr(
        "doc3gpp.services.tdoc_cr_service.download_tdoc_zip",
        lambda *a, **kw: MagicMock(
            path=MagicMock(read_bytes=lambda: zip_buf.getvalue()),
            url="https://www.3gpp.org/ftp/tsg/ls/R5-260017.zip",
        ),
    )
    monkeypatch.setattr(
        "doc3gpp.services.tdoc_cr_service.extract_docx_from_zip",
        lambda _: ("R5-260017.docx", ls_md.encode("utf-8")),
    )

    result = service.extract("R5-260017", force=True)

    assert isinstance(result, LSResult)
    assert result.details.tdoc_id == "R5-260017"
    assert result.details.variant == "3gpp"
    assert result.from_cache is False

    # Sidecar row landed.
    repo = SQLAlchemyLSParserRepository(session_factory=sqlite_session_factory)
    sidecar = repo.get_by_url("tsg/ls/R5-260017.zip")
    assert sidecar is not None
    assert sidecar.tdoc_id == "R5-260017"
```

(Adapt the existing `sqlite_engine` / `sqlite_session_factory` fixture names to match the project's established pattern in `tests/integration/conftest.py` or wherever they live.)

- [ ] **Step 3: Run the test — expect PASS**

Run: `python -m pytest tests/integration/test_tdoc_cr_sqlite.py -v -k db_mode_ls_row_writes_sidecar`
Expected: PASS.

- [ ] **Step 4: Run the full sqlite integration suite — regression**

Run: `./scripts/test_sqlite.sh`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_tdoc_cr_sqlite.py
git commit -m "test(integration): DB-mode LS row writes LS sidecar"
```

---

## Task 10: Documentation sync

**Files:**
- Modify: `docs/cli.md`, `docs/architecture.md`, `docs/code-map.md`, `docs/web-server.md`, `AGENTS.md` (remove stale "DB-mode LS extraction is not yet supported" wording; update summary tables)

**Interfaces:**
- Consumes: the new spec's contract (DB-mode LS support enabled; CLI summary is a flat count)
- Produces: docstrings that match the implemented behaviour

**Acceptance:** `rg -n "DB-mode LS extraction is not yet supported" docs/ src/ AGENTS.md README.md` returns no matches.

- [ ] **Step 1: Find every stale sentence**

Run: `rg -n "DB-mode LS|not yet supported|--from-url.*LS|extracts.*LS" docs/ AGENTS.md README.md`
Expected: several hits across `docs/cli.md`, `docs/architecture.md`, `docs/code-map.md`, `docs/web-server.md`, `AGENTS.md`.

- [ ] **Step 2: Update each stale sentence**

For each hit:
1. **CLI doc** (`docs/cli.md`): Update the `tdoc parse` section to remove the paragraph that says "DB-mode parse jobs on LS rows fail with `TDocTypeUnsupportedError`" and replace with "DB-mode parses an LS row through the LS parser; runs `parse_ls`, writes `tdoc_cr_ls_details`."
2. **Architecture doc** (`docs/architecture.md`): In §"LS TDoc header extraction" (around line 361-384), drop the assertion that `TDocCrService.extract` raises `TDocTypeUnsupportedError` and explain that the DB-mode `extract` now routes LS rows through `LSParserBase` exactly like the direct-mode path. Cross-link to `tdoc_cr_service.py::extract`.
3. **Code-map doc** (`docs/code-map.md`): Update the `TDocCrService` row (line ~60) to say "DB-mode `extract` dispatches LS via `LSParserBase`; LS branch of `extract()` writes LS sidecar + `tdoc_extracts.cache_file`."
4. **Web-server doc** (`docs/web-server.md`): Update the LS-Cover paragraph that mentions `TDocTypeUnsupportedError` (line ~292) to say "DB-mode `tdoc parse --tdoc <id>` populates the LS sidecar; the LS cover card shows after a parse."
5. **AGENTS.md**: Update the auto-sync / LS dispatch section (line ~88) to drop the "DB-mode parse jobs on LS rows fail with `TDocTypeUnsupportedError`, so LS extraction runs via `tdoc parse --from-url`" paragraph. Replace with "DB-mode `tdoc parse --tdoc <id>` dispatches LS rows to `ThreeGPPLSParser`; the on-disk zip + markdown caches are populated identically to CR rows."

- [ ] **Step 3: Verify no stale references remain**

Run: `rg -n "DB-mode LS extraction is not yet supported" docs/ AGENTS.md README.md src/`
Expected: no matches.

- [ ] **Step 4: Commit**

```bash
git add docs/ AGENTS.md README.md
git commit -m "docs: remove stale 'DB-mode LS unsupported' notes"
```

---

## Task 11: Final verification — full test suite + ruff

**Files:** none (verification-only)

**Acceptance:** `./scripts/test_sqlite.sh` is green; `ruff check .` is clean; the doc-rg from Task 10 step 3 returns no hits.

- [ ] **Step 1: Run the full sqlite test suite**

Run: `./scripts/test_sqlite.sh`
Expected: All PASS (unit + integration, sqlite-only). If `xdist` is installed, this runs in parallel.

- [ ] **Step 2: Run ruff**

Run: `ruff check .`
Expected: Clean (no warnings).

- [ ] **Step 3: Confirm no stale references**

Run: `rg -n "DB-mode LS extraction is not yet supported" docs/ AGENTS.md README.md src/`
Expected: no matches.

- [ ] **Step 4: End-to-end smoke (optional, requires live 3GPP FTP)**

Run: `python -c "
import sys
sys.path.insert(0, 'src')
from doc3gpp.services.factory import build_tdoc_cr_service
from doc3gpp.models.tdoc import TDoc
svc = build_tdoc_cr_service()
result = svc.extract('R5-260017', force=False, full=False)
print('success:', type(result).__name__, 'tdoc_id:', result.details.tdoc_id)
"`
Expected: prints `success: LSResult tdoc_id: R5-260017` (only when FTP is reachable; skip if `OPERATIONAL_ERROR`).

- [ ] **Step 5: Final commit (only if step 1-3 surfaced fixes)**

```bash
# Only run if any fix commit is required; otherwise skip.
git status
# If clean:
# nothing to commit, working tree clean
```

---

## Done criteria

- `tdoc parse --tdoc R5-260017` (no `--type`) extracts the LS row and writes `tdoc_cr_ls_details` + `tdoc_extracts.cache_file`.
- `tdoc parse --tdoc R5-260017 --force` re-downloads, re-renders, replaces both caches.
- `tdoc parse --meeting-id 110` works the same for CR + LS rows.
- `tdoc parse --from-url <3gpp-url>` continues to auto-sync and parse; LS still works (unchanged).
- CLI summary is flat: `Extracted N TDoc(s); M failed; K skipped.`
- Web `POST /jobs/parse/tdocs` envelope is unchanged in shape; `successes` includes both CR + LS counts.
- `tdoc show --tdoc R5-260017` renders the LS Cover card from the sidecar.
- `tdoc content` / `tdoc download` HTTP routes read `markdown/<cache_file>` and `zips/<cache_file>` regardless of type.
- Tests: unit + integration green; ruff clean; no stale docs.
