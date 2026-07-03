# TDoc Extraction Pipeline — Implementation Plan

> Status: **in progress** — Phases 1–3 implemented, 4–8 pending ·
> Owner: TBD · Source of truth for the new download → parse →
> extract TDoc flow.
>
> **Goal:** given a TDoc identifier (e.g. `R5s260009`), download the
> 3GPP zip → cache it → convert the inner `.docx`/`.doc` to markdown via
> `markitdown` → cache the markdown → extract structured CR cover-page
> fields → persist them. First cut covers CR-type TDocs; other TDoc
> types (e.g. `LS`, `DRAFT`) come later.
>
> This document is the single source of truth and the tracking
> checklist. Update checkboxes as work lands; do not maintain a
> separate plan in chat.

## Reference material

- `docs/ttcn_cr_cli_example.py` — the legacy, working CR parser + zip
  download flow (1839 lines, no integration with the layered
  `doc3gpp` package). Treat it as **reference, not source**: the
  markdown-extraction helpers (`parse_cr_cover_page`,
  `_parse_ttcn_cr_overview`, `_parse_ttcn_cr_corrections`,
  `_remove_markdown_formatting`, `convert_document_to_markdown`,
  `extract_docx_from_cr_zip`, `build_tdoc_zip_url`) will be ported
  into the layered architecture, not vendored.
- `tests/fixtures/tdoc_cr_doc/` — 7 CR zip fixtures already in tree
  (`C6-250028.zip`, `R5-227476.zip`, `R5-253079.zip`, `R5s260009.zip`,
  `R5s260051.zip`, `R5s260135.zip`, `R5s260176.zip`). Spans RAN5
  TTCN email (5 files) + RAN5 normal (1) + CT6 (1). They are
  the seed corpus for parser and pipeline tests.
- `docs/TODO.md` — design conventions to preserve (Protocol/impl
  signature parity, `db init` as the single schema boundary, no
  `create_schema()` in services, Protocol-typed services from the
  factory).

## Out of scope (called out for clarity)

- Other TDoc types beyond CR (LS, DRAFT, BB, etc.) — only CR in this
  pass; the parser will be type-dispatched from day one so the next
  type plugs in cleanly.
- Alembic / versioned migrations. The project still uses
  `Base.metadata.create_all` (see `docs/architecture.md` §"Current Known
  Constraints"). New tables added by this plan are picked up the next
  time `doc3gpp db init` runs on a fresh DB. Existing DBs that need
  the new tables must drop the `tdoc_cr_details` and `tdoc_extracts`
  tables and re-run `db init`, **or** call `create_schema()` (it's
  idempotent and adds only the new tables); we'll surface this in
  user-facing errors. Trade-off: not worth a full Alembic setup for a
  single addition.
- Online tests against live 3gpp.org + FTP — same as the rest of the
  project; `python -m pytest -m online` is opt-in.

## Design decisions (locked in unless overturned)

| Decision | Choice | Why |
|---|---|---|
| Cache location | `~/.cache/doc3gpp/tdocs/` by default; configurable via `[cache] dir` in TOML / `DOC3GPP_CACHE__DIR` | XDG default; project already has XDG config in `settings/config_source.py` |
| Cache layout | Two subtrees: `zips/<tdoc>.zip` (raw downloads) and `markdown/<sha256>.md` (markitdown output) | Keeps both layers independently evictable; markdown keyed by content hash so updated docx re-parses |
| Cache eviction | Insertion-time LRU — track `created_at` per file; when total size > `cache.size_limit_mb`, delete oldest until under | Oldest-first (FIFO on create time) matches the brief and is the cheapest to implement; the file's create time never changes so a single `os.stat().st_ctime` is enough |
| Explicit purge | `doc3gpp cache purge` CLI; one `--yes` flag to skip the prompt | Symmetric with the eviction; won't surprise users |
| Markdown converter | `markitdown[all]` as a soft dependency — wrapped with try/except ImportError; degrades to a clear `ImportError("markitdown is required for TDoc extraction")` at the call site | The reference implementation already does this. Document `pip install doc3gpp[extract]` as the install path |
| Extraction persistence | New `tdoc_cr_details` table keyed by `tdoc_id` (1:1) + a sibling `tdoc_extracts` table for raw markdown + parser diagnostics (1:1) | Avoids column-bloating `tdocs`; markdown sidecar is useful for debugging without rerunning markitdown |
| Service composition | New `TDocCrService` + `TDocCache`; built via `services/factory.py` (`build_tdoc_cr_service`); Protocol-typed in `repository/protocols.py` | Matches the existing factory + Protocol pattern (see TODO #11) |
| CLI surface | `doc3gpp tdoc extract --tdoc <id> [--tdoc-type cr] [--force]`; `doc3gpp tdoc show --tdoc <id>` extended to print extracted fields; `doc3gpp cache status`, `doc3gpp cache purge` | Reuses `tdoc` group; adds a small `cache` group |
| Parser source of truth | Port from `docs/ttcn_cr_cli_example.py`; tested against all 7 fixtures | The reference works; we just need it in the package |

## Architecture additions (target file map)

```
src/doc3gpp/
├── models/
│   └── tdoc_cr.py            # NEW: TDocCRDetails dataclass (parsed fields)
├── parsers/
│   ├── cr_parser.py          # NEW: port cover-page/overview/corrections parsers
│   └── markitdown_converter.py  # NEW: markitdown wrapper with fallback
├── repository/
│   └── protocols.py          # EXTEND: TDocCrDetailRepository, TDocCacheRepository
├── scraping/
│   ├── cache.py              # NEW: TDocCache — store, get, evict, purge
│   └── tdoc_zip_source.py    # NEW: build_tdoc_zip_url + download_to_cache
├── services/
│   ├── tdoc_cr_service.py    # NEW: end-to-end extract orchestration
│   ├── tdoc_cache_service.py # NEW: status + purge operations
│   └── factory.py            # EXTEND: build_tdoc_cr_service, build_tdoc_cache_service
├── settings/
│   ├── schema.py             # EXTEND: CacheSettings nested model
│   └── config_source.py      # (no change; existing TOML plumbing covers [cache])
├── storage/
│   ├── db/models.py          # EXTEND: TDocCROrm, TDocExtractOrm
│   ├── db/migrate.py         # (no change; create_all picks up the new tables)
│   └── repositories/
│       ├── tdoc_cr_sql.py    # NEW: SQLAlchemy impl of TDocCrDetailRepository
│       └── tdoc_extract_sql.py  # NEW: SQLAlchemy impl of TDocCacheRepository (metadata only)
└── cli.py                    # EXTEND: tdoc extract, tdoc show (extended), cache status, cache purge
```

## Phased plan

Each phase is independently shippable: every box ticked at the end
of a phase leaves the test suite green and adds a discrete, demoable
capability. Run `ruff check .` and `./scripts/test_sqlite.sh` after
each phase.

### Phase 1 — Settings & cache primitives

**Goal:** add cache configuration to `Settings` and a stand-alone
`TDocCache` module that knows nothing about the database or 3GPP —
just bytes in, bytes out, with size-based eviction and explicit
purge. This is the foundation everything else sits on.

**Add to `src/doc3gpp/settings/schema.py`:**
- New `CacheSettings(BaseModel)` with fields:
  - `dir: Path = Field(default_factory=lambda: Path.home() / ".cache" / "doc3gpp" / "tdocs")`
  - `size_limit_mb: int = Field(default=1024, ge=0)` (`0` = unlimited)
  - `purge_confirm: bool = Field(default=True)` (CLI guard for `cache purge`)
- Add `cache: CacheSettings = Field(default_factory=CacheSettings)` to
  `Settings`. Env vars via the existing `__` delimiter:
  `DOC3GPP_CACHE__DIR=...`, `DOC3GPP_CACHE__SIZE_LIMIT_MB=...`.

**Add to `doc3gpp.toml.example`:** a documented `[cache]` section.

**Add `src/doc3gpp/scraping/cache.py`:** `class TDocCache` with:
- `__init__(self, root: Path, size_limit_bytes: int)` — preconditions
  on `root` (created if missing, must be a directory).
- `put_bytes(self, key: str, payload: bytes, subdir: Literal["zips", "markdown"]) -> Path`
  — atomic write (`tempfile.NamedTemporaryFile` + `os.replace`),
  enforces size limit afterwards.
- `get_bytes(self, key: str, subdir: Literal["zips", "markdown"]) -> bytes | None`
  — returns `None` on miss, `bytes` on hit. **No** `mtime` touch on
  read — the brief says "create time when first downloaded" is the
  eviction key, and touching mtime would silently change it.
- `path_for(self, key: str, subdir: Literal["zips", "markdown"]) -> Path`
  — returns the expected path even if missing; useful for "are we
  about to download this?" checks.
- `enforce_size_limit(self) -> int` — scans `zips/` and `markdown/`,
  sorts by `st_ctime` ascending, deletes oldest until total size
  ≤ limit; returns count deleted. Idempotent.
- `purge(self) -> int` — recursive delete of `zips/` and `markdown/`
  under `root`, then `mkdir(parents=True, exist_ok=True)`; returns
  count deleted.
- `status(self) -> CacheStatus` — typed dataclass with
  `file_count: int`, `total_bytes: int`, `limit_bytes: int`,
  `zips: int`, `markdown: int`. **No filesystem writes.**

**Sanity invariants in `TDocCache.__init__` / `put_bytes`:**
- Reject negative `size_limit_bytes` (validation lives in
  `CacheSettings`, but the cache constructor asserts for safety).
- `key` is sanitised: must match `^[A-Za-z0-9._-]{1,128}$`; rejects
  `..`, path separators, and control characters so a malicious
  tdoc_id can never escape the cache root.
- Refuse to write zero-byte payloads (they're a sign of a
  truncated download).

**Tests (`tests/unit/test_tdoc_cache.py`):**
- put → get round-trip
- `enforce_size_limit` deletes oldest by `st_ctime` (mock or sleep
  the ctime)
- `purge` clears both subdirs and recreates them
- `status` is non-mutating
- path traversal rejected (key with `..`, `/`, etc.)
- atomic write: pre-existing file with the same name is overwritten,
  not appended
- `get_bytes` does **not** touch `st_ctime` (test by capturing
  `stat().st_ctime` before and after a get)

**Done when:** `pytest tests/unit/test_tdoc_cache.py` green,
`doc3gpp config show` includes the new `[cache]` block, ruff clean.

### Phase 2 — TDoc zip URL getter + downloader

**Goal:** resolve a TDoc id to a 3GPP URL, download the zip through
`ScraperClient`, and stage it in the cache. Pure network + cache
layer — no parsing yet.

**Add `src/doc3gpp/scraping/tdoc_zip_source.py`:**
- `get_tdoc_zip_url(tdoc: str) -> str | None` — search up tdoc id in db from tdocs table, find the url for download. 
  if tdocs not found, try call out sync function to get the tdocs list from 3gpp.org, based on the tsg and meeting year derived from the tdoc id 
  ({R5s260009} → R5, 2026), then find the tdoc and its url.  Return None if still no URL can be resolved.
- `download_tdoc_zip(tdoc: str, client: ScraperClient, cache: TDocCache) -> Path`
  — checks cache first; on miss, builds URL, GETs via `ScraperClient`,
  writes to `cache.put_bytes(tdoc.lower(), payload, subdir="zips")`,
  returns the resulting `Path`. Raises
  `TDocZipDownloadError(httpx.HTTPError, url)` on non-retryable
  failure so the caller can decide whether to skip.
- `tsg_meeting_year_for(tdoc: str) -> tuple[str, int | None]` — derive the tsg short name (`R5`, `C6`, etc.) and the
  two-digit year from the tdoc id (`R5s260009` → [`R5`, 2026]) so the Tdoc URL
  fetch, if needed, is data-driven.

**Tests (`tests/unit/test_tdoc_zip_source.py`):**
- URL builder matrix: `R5s260009`, `R5s260176`, `R5w260009`, `R5-227476`,
  `C6-250028`, lower-case variants, garbage input → `None`.
- `download_tdoc_zip` cache-hit path returns cached bytes without a
  network call (monkeypatch `ScraperClient.get_bytes` and assert it
  is **not** called).
- `download_tdoc_zip` cache-miss path calls `ScraperClient.get_bytes`
  once and writes the result through `TDocCache.put_bytes`.
- `ScraperClient.HTTPError` is wrapped into `TDocZipDownloadError`
  (use `monkeypatch` to raise a synthetic error).

**Done when:** unit tests green, `ruff check .` clean. The
unrecognised templates (`R5-`, `C6-`) can be returned as `None` for
now; we will harden them during Phase 8 against the live site if the
fixtures' sources are still online.

### Phase 3 — markitdown wrapper

**Goal:** turn the inner `.docx`/`.doc` into markdown with a clean
import boundary, so the rest of the pipeline never has to know about
`markitdown` directly. Must fail loud if `markitdown` is not
installed (rather than silently returning empty strings like the
reference does — the reference's empty-string fallback masks
missing-dep bugs; we want them to surface).

**Add `src/doc3gpp/parsers/markitdown_converter.py`:**
- `class MarkitdownNotInstalledError(ImportError)`: explicit subclass
  so callers can catch it.
- `convert_document_to_markdown(doc_bytes: bytes, filename: str) -> str`:
  - Resolve the file extension from `filename`.
  - Validate that it is `.docx` or `.doc` (raise `ValueError`
    otherwise — the caller is the only one who could feed us a
    different format, and silently doing nothing is worse).
  - `import markitdown` inside a `try`; raise
    `MarkitdownNotInstalledError` on `ImportError` with an actionable
    message pointing at `pip install doc3gpp[extract]`.
  - Write `doc_bytes` to a `tempfile.NamedTemporaryFile(suffix=ext)`,
    run `MarkItDown().convert(tmp_path)`, return `.text_content`
    (fallback to `.markdown` / `str(...)` to be tolerant of
    markitdown's version drift, mirroring the reference).
  - `try/finally` deletes the temp file.
- `is_docx_or_doc(filename: str) -> bool` — small helper used by the
  zip extractor.

**Add `pyproject.toml [project.optional-dependencies]`:**
- `extract = ["markitdown[all]>=0.0.1"]`

**Tests (`tests/unit/test_markitdown_converter.py`):**
- `MarkitdownNotInstalledError` raised when `import markitdown` fails
  (use `monkeypatch.setitem(sys.modules, "markitdown", None)` to
  force the `ImportError`).
- Reject non-`.docx`/`.doc` filenames with `ValueError`.
- End-to-end conversion using one of the 7 fixtures
  (`tests/fixtures/tdoc_cr_doc/R5s260009.zip` → extract
  `R5s260009.docx` → call `convert_document_to_markdown` → assert
  non-empty result, and assert that a known fixture substring like
  `"3GPP TSG-RAN5"` appears in the markdown).
- Mark the integration with a real fixture as `@pytest.mark.online`
  if markitdown is missing in CI; the install-required test stays in
  the default pool.

**Done when:** unit tests green, `doc3gpp` installs cleanly without
markitdown (i.e. the dependency is opt-in via the `[extract]` extra).

### Phase 4 — CR parser (the heart of the extraction)

**Goal:** port the markdown → structured fields logic from
`docs/ttcn_cr_cli_example.py` into the `parsers/` package, with the
existing test fixtures as the regression suite. **No persistence in
this phase** — pure `str → dict[str, str]`.

**Add `src/doc3gpp/parsers/cr_parser.py`:** port from reference
(`parse_cr_cover_page`, `_parse_ttcn_cr_overview`,
`_parse_ttcn_cr_corrections`, `_parse_ttcn_cr_single_correction`,
`_extract_change_from_table`, `_search_pattern_in_lines`,
`_remove_markdown_formatting`, `_collapse_whitespace`,
`parse_ttcn_cr_details`, `derive_tech_from_spec`).

Adaptations from the reference:
- Replace the module-level `re.search(r"3GPP\s+TSG-...")` header
  sniff with a structured check: extract the meeting family and
  decide parser branch (RAN5 TTCN email vs RAN5 plain vs CT6) — the
  fixtures show that the RAN5 plain (`R5-227476.zip`) and CT6
  (`C6-250028.zip`) variants differ slightly. The reference
  TTCN-specific cover-page expectations (`R5s\d{6}` in the header)
  need to be loosened; the branch should be keyed on the parsed
  cover-page fields, not the document's first line. Concretely:
  - The cover-page TDoc pattern regex
    (`r"\s+([RCS][1-8][-sw]\d{6})"` in the reference) needs to
    accept `R5-227476`-style ids too. Add `r"\s+([RCS][1-8][-]\d{6})"`
    as a second alternative.
  - The RAN5-TTCN `iwd-TTCN3-B\d{4}-\d{2}_D\d{2}wk\d{2}` regex
    must remain RAN5-specific; non-TTCN CRs simply don't have it,
    and the test will assert that `ats_version` is empty in that
    case (rather than the reference's "extract failed" return).
- `parse_ttcn_cr_details` is renamed to `parse_cr_details` and
  returns a typed dataclass (see Phase 5) instead of a
  `Dict[str, str]`. The `tdoc` key is **not** populated by the
  parser (caller passes it in).
- `corrections` is a `list[dict[str, str]]` in the return; the
  service layer (Phase 6) JSON-serialises it for storage.
- All regex strings compile once at module import (the reference
  compiles them per-call — cheap but noisy).

**Tests (`tests/unit/test_cr_parser.py`):** for each of the 7
fixtures:
- Extract the inner `.docx` (zipfile is small; this is an in-test
  helper, not a network call), call `convert_document_to_markdown`
  (mark as `@pytest.mark.skipif` if markitdown is unavailable), feed
  the markdown to `parse_cr_details`, assert the expected fields
  per fixture (snapshot table below). If markitdown is not
  installed, fall back to a tiny **hand-rolled markdown fixture**
  per parser test (each regex's `pattern` field already shows the
  expected input shape) so the test stays in the default pool.

**Snapshot table for fixture assertions** (this is the regression
contract — copy these into test cases):

| Fixture | spec | cr_num | rev | cr_cat | release | year (derived) |
|---|---|---|---|---|---|---|
| `C6-250028.zip` | `31.124` | `0797` | `0` (`-`) | (A,B,C,D, or F) | (Rel-?) | 2025 |
| `R5-227476.zip` | `38.508-1` | `2678` | `1` | (A,B,C,D, or F) | (Rel-?) | 2022 |
| `R5-253079.zip` | `38.523-1` | `4947` | `1` | (A,B,C,D, or F) | (Rel-?) | 2025 |
| `R5s260009.zip` | `38.523-3` | `3790` | `0` (`-`) | (A,B,C,D, or F) | (Rel-?) | 2026 |
| `R5s260051.zip` | `38.523-3` | `3806` | `0` (`-`) | (A,B,C,D, or F) | (Rel-?) | 2026 |
| `R5s260135.zip` | `38.523-3` | `3824` | `0` (`-`) | (A,B,C,D, or F) | (Rel-?) | 2026 |
| `R5s260176.zip` | `36.523-3` | `4971` | `0` (`-`) | (A,B,C,D, or F) | (Rel-?) | 2026 |

(Field values for the ones marked `(A,B,C,D, or F)` etc. must be filled in
by reading each fixture during implementation; update this table as
the test cases are written. `rev` shows `0` when the cover page
shows `-`, per the reference's normalisation rule.)

**Done when:** all 7 fixtures produce a non-empty `TDocCRDetails`
with `spec`, `cr_num`, `title`, `source` populated, and the
RAN5-TTCN fixtures additionally populate `ats_version` /
`ttcn_release`. Parser tests stay green in the default pool even
when markitdown is not installed (the hand-rolled markdown
fallback covers the regex logic).

### Phase 5 — Domain model + ORM

**Goal:** typed `TDocCRDetails` dataclass and a persistence shape
that survives schema evolution.

**Add `src/doc3gpp/models/tdoc_cr.py`:**
- `@dataclass(slots=True, frozen=True) class TDocCRDetails:` with
  fields mirroring the parser output, plus typing for the
  corrections list (`list[dict[str, str]]`). `__post_init__` validates
  non-emptiness of `tdoc_id` and coerces `corrections` to a JSON
  string on demand via a `to_persisted()` helper.
- `class TDocExtractOrm` (the metadata-only row): `tdoc_id: str
  (PK)`, `zip_path: str`, `markdown_path: str`, `doc_filename: str`,
  `extracted_at: datetime`, `parser_version: str`. This is the
  cheap row that records "we've already extracted this" without
  storing the full markdown again.
- `class TDocCrDetailOrm`: `tdoc_id: str (PK, FK tdocs.tdoc_id)`,
  one column per parser field (mostly `String(64-1000)` plus
  `corrections: Text` for the JSON blob), `extracted_at:
  DateTime(timezone=True)`.

**Add to `src/doc3gpp/storage/db/models.py`:**
- Both ORM classes. FK to `tdocs.tdoc_id` with `ondelete="CASCADE"`
  so a TDoc delete cleans the detail row.

**Update `src/doc3gpp/storage/db/migrate.py`:** no code changes (the
metadata-based `create_all` picks up the new tables automatically).

**Tests (`tests/unit/test_tdoc_cr_model.py`):**
- `TDocCRDetails.to_persisted()` JSON-serialises `corrections`.
- Empty / whitespace `tdoc_id` rejected by `__post_init__`.
- ORM models construct cleanly with a transient SQLAlchemy engine
  (in-memory sqlite, `create_all`, then `session.add` + `commit`).

**Done when:** model unit tests green, in-memory sqlite can
`create_all` and round-trip a `TDocCrDetailOrm` row.

### Phase 6 — Repository + service (the integration)

**Goal:** a `TDocCrService` that takes a TDoc id and returns
either the cached extract or a freshly computed one, persisting both
the raw bytes (via `TDocCache`) and the structured details (via
`TDocCrRepository`).

**Extend `src/doc3gpp/repository/protocols.py`:**
- `class TDocCrDetailRepository(Protocol)` with `get(tdoc_id)`,
  `upsert(details: TDocCRDetails, extract_meta: TDocExtractMeta)`,
  `get_extract_meta(tdoc_id)`.
- `class TDocExtractRepository(Protocol)` (or fold the metadata
  into the detail repo — see design call below). **Design call:**
  fold into one repository, `TDocCrRepository`, to keep the surface
  small. If the metadata write needs to be optional (e.g. for a
  future "parse-only" mode), split later.

**Add `src/doc3gpp/storage/repositories/tdoc_cr_sql.py`:**
- `class SQLAlchemyTDocCrRepository` implementing the protocol
  using the session factory; the `upsert` covers both
  `tdoc_cr_details` and `tdoc_extracts` in one transaction.

**Add `src/doc3gpp/services/tdoc_cr_service.py`:**
- `class TDocCrService` takes the cache, the scraper client
  factory, the CR detail repository, and the (read-only) TDoc
  repository.
- `extract(self, tdoc_id: str, *, force: bool = False) -> TDocCRDetails`:
  the main entry point. Sequence:
  1. Validate `tdoc_id` against `^[A-Za-z0-9-]{1,32}$` (no garbage
     ever reaches the cache or the network).
  2. Look up the TDoc in `tdocs` to confirm it exists and is a CR
     type (the TDoc list XLSX is the source of truth for
     `type == "CR"`; do **not** guess from the tdoc id shape alone —
     `R5-227476` and `R5s260009` are both CRs, but the user might
     pass a non-CR like `LS-123456`).
  3. If the detail row exists and `not force`, return it.
  4. Else `download_tdoc_zip` → `extract_docx_from_zip` (returns
     `(filename, bytes)`; small helper in this file or in
     `scraping/tdoc_zip_source.py`).
  5. `convert_document_to_markdown` →
     `cache.put_bytes(sha256_of_doc_bytes, markdown, "markdown")`
     so the markdown is keyed by content hash (re-runs after
     upstream edits re-parse cleanly).
  6. `parse_cr_details(markdown)` → `TDocCRDetails` → `repo.upsert`.
  7. Return the result.
- `extract_many(self, tdoc_ids: Iterable[str], *, force: bool = False) -> dict[str, TDocCRDetails]`:
  iterates, captures per-id exceptions, returns a dict of successes
  and logs (does not raise) failures — CLI surfaces the failures
  via a summary line.

**Extend `src/doc3gpp/services/factory.py`:** `build_tdoc_cr_service`
that wires `TDocCache` (from settings), `ScraperClient`, and the SQL
repos.

**Tests (`tests/integration/test_tdoc_cr_sqlite.py`):**
- `extract(tdoc_id)` happy path against a sqlite DB with a
  pre-seeded `tdocs` row and a mock `ScraperClient.get_bytes`
  returning the contents of one fixture.
- Cache hit: second call does not invoke `ScraperClient.get_bytes`
  (assert via `mock_calls`).
- Markdown cache hit (force=False but markdown already cached):
  even if the zip was purged, the markdown sidecar lets us skip
  markitdown.
- `--force` bypasses both caches.
- Non-CR tdoc id raises `TDocTypeUnsupportedError` (a new typed
  exception in `services/tdoc_cr_service.py`).
- Network failure on zip download raises `TDocZipDownloadError`
  (does not silently leave a half-written cache entry — assert the
  cache does not contain a 0-byte file).

**Done when:** integration test green, the service composes
through `factory.build_tdoc_cr_service()`.

### Phase 7 — CLI surface

**Goal:** user-facing commands for the new pipeline.

**Extend `src/doc3gpp/cli.py`:**
- `tdoc_app.command("extract")` with options:
  - `--tdoc TDOC` (single, repeatable for batch; mutually exclusive
    with `--tdoc-id`).
  - `--tdoc-id INT` (repeatable, integer pk of the `tdocs` row;
    resolved via `TDocRepository.list` or a new
    `get_by_id(tdoc_id_int)` Protocol method — see TODO #1..M2
    pattern).
  - `--force` (skip both caches).
  - `--full` (forwarded to parser; pulls in `before_change` /
    `after_change` content per correction).
- `tdoc_app.command("show")` extended to print a `[Extracted
  Details]` block when a `tdoc_cr_details` row exists; mirrors
  `ttcn_cr_cli_example.py:command_show` (the "Extracted Details"
  block).
- New `cache_app` Typer group with:
  - `cache status` — prints `file_count`, `total_bytes`, `limit_bytes`,
    `zips`, `markdown`. Output is a plain table; no `--format` for
    this initial cut.
  - `cache purge` — gated by `CacheSettings.purge_confirm`; CLI
    passes `--yes` to skip the prompt. Emits a single line per
    deleted subdir.

**Update `docs/cli.md`** to document the new commands and options.

**Tests (`tests/unit/test_tdoc_extract_cli.py`,
`tests/unit/test_cache_cli.py`):**
- `tdoc extract --tdoc R5s260009` happy path (uses a fake
  `TDocCrService` injected through `monkeypatch`; no network, no
  filesystem outside `tmp_path`).
- `--tdoc-id` resolves through the TDoc repository.
- `cache status` prints zeros on a fresh cache dir.
- `cache purge` deletes files and recreates subdirs.
- `cache purge` without `--yes` and with `purge_confirm=True`
  aborts; `--yes` overrides; env override
  `DOC3GPP_CACHE__PURGE_CONFIRM=false` skips the prompt.

**Done when:** new tests green, existing CLI tests still green
(no signature drift on `TDocRepository.list` etc. — see TODO #11
anti-pattern).

### Phase 8 — Integration test pass + live URL verification

**Goal:** prove the end-to-end flow works against the real
fixtures, and (optionally) the live 3GPP site.

- Extend `tests/integration/test_tdoc_cr_sqlite.py` with a full
  end-to-end test that takes the user through the actual
  `cli.main()` runner: `tdoc extract --tdoc R5s260009` → assert
  the row landed in `tdoc_cr_details`.
- Markitdown version drift: pin a minimum version, but the test
  also asserts the markdown contains the substring
  `3GPP TSG-RAN5 Meeting` — a smoke check that the converter isn't
  silently returning empty.
- Add a `tests/integration/test_online_tdoc_extract.py` (with
  `@pytest.mark.online`): a single happy-path test against
  `R5s260009` on the live site. Skip if the user has not installed
  `markitdown[all]`. This is the test that surfaces URL-template
  rot for the R5- / C6- branches.
- Update `docs/implementation-status.md` to flip the "Planned /
  Not Yet Implemented → TDoc Extraction Pipeline" section to
  "Implemented" and document the new commands in `docs/cli.md`.

**Done when:** sqlite test profile passes, online test
documented (skipped by default).

## Cross-phase guardrails (do not skip)

- **Ruff clean at every phase boundary.** Run `ruff check .` and
  `ruff format --check .` before ticking a phase.
- **No `as any` / `# type: ignore`** — see AGENTS.md conventions.
- **No `create_schema()` in services.** Phase 6's
  `TDocCrService.extract` should not call `create_schema()`. If
  the new tables are missing, the SQL repo will raise a
  `ProgrammingError`; convert it to a user-facing `typer.BadParameter`
  in the CLI ("run `doc3gpp db init` first") — this matches the
  TODO #24 direction.
- **No `cache_clear()` leakage.** Any test that mutates
  `DOC3GPP_*` env vars must call `get_settings.cache_clear()` in
  the teardown (per `tests/conftest.py::sqlite_env`).
- **Update `AGENTS.md`** when CLI surface or settings schema
  change (the project explicitly tracks this in the
  "Conventions" section).
- **Update `docs/TODO.md`** with any anti-patterns we discover
  during the port (e.g. if the reference's broad `except Exception`
  in `convert_document_to_markdown` shows up here, add a TODO to
  fix it).

## Tracking checklist (copy into GitHub Issues for finer-grained
progress, if desired)

- [x] **Phase 1** — `CacheSettings` added, `TDocCache` module + tests
- [x] **Phase 2** — `tdoc_zip_source.py` (URL builder + downloader) + tests
- [x] **Phase 3** — `markitdown_converter.py` + `pyproject.toml [extract]` + tests
- [ ] **Phase 4** — `cr_parser.py` ported + 7-fixture regression tests
- [ ] **Phase 5** — `TDocCRDetails` model + `TDocCrDetailOrm` / `TDocExtractOrm` + tests
- [ ] **Phase 6** — `TDocCrRepository` protocol + SQL impl + `TDocCrService` + factory wiring + integration tests
- [ ] **Phase 7** — CLI: `tdoc extract`, `tdoc show` (extended), `cache status`, `cache purge` + tests; `docs/cli.md` updated
- [ ] **Phase 8** — End-to-end CLI integration test + online test marker + `docs/implementation-status.md` flipped; live URL templates verified

### Implementation progress (Phases 1–3 landed 2026-07-03)

Verified via `pytest tests/unit -q` (345 passed, 2 skipped) and
`./scripts/test_sqlite.sh` (387 passed, 2 skipped, 5 deselected;
95% coverage). `ruff check src/doc3gpp tests` clean.

| Phase | Files added | Files extended | Tests |
|---|---|---|---|
| 1 | `src/doc3gpp/scraping/cache.py`, `tests/unit/test_tdoc_cache.py` | `src/doc3gpp/settings/schema.py`, `doc3gpp.toml.example` | 20 cases (14 functions + 7-case parametrise) |
| 2 | `src/doc3gpp/scraping/tdoc_zip_source.py`, `tests/unit/test_tdoc_zip_source.py` | — | 27 cases (7 functions) |
| 3 | `src/doc3gpp/parsers/markitdown_converter.py`, `tests/unit/test_markitdown_converter.py` | `pyproject.toml` | 22 cases (5 functions; 2 fixture e2e skip when `markitdown` not installed) |

**URL matrix verified:** `R5s260009` / `R5s260051` / `R5s260135` /
`R5s260176` → `…/TTCN_CRs/2026/Docs/<id>.zip`;
`R5w260009` → `…/Workshop/TSGR5_Workshop_2026/Docs/<id>.zip`;
`R5-227476` / `C6-250028` → `None` (Phase 8 work).
Lower-case ids (`r5s260009`) normalise to the upper-case URL.

**Deviations from the plan (small):**

1. **Phase 2 — `TDocCacheLike` Protocol.** Phases 1 and 2 ran in
   parallel; Phase 2 couldn't import the real `TDocCache` yet, so the
   source declares a structural Protocol with `put_bytes` /
   `get_bytes` / `path_for`. The real `TDocCache` (Phase 1) is a
   drop-in. No migration needed.
2. **Phase 2 — DB lookup in `get_tdoc_zip_url` deferred.** Marked
   `# TODO(phase-6): also check the tdocs table for an explicit URL
   stored from a prior tdoc sync run`. Phase 5/6 owns the
   `TDocRepository` Protocol; we don't introduce it here.
3. **Phase 1 — `CacheStatus` is a `@dataclass(slots=True, frozen=True)`.**
   Plan described it as a "typed dataclass"; dataclass adds
   immutable equality and a `__repr__` for free, so we used it. No
   user-facing change.
4. **Phase 1 — `Negative size_limit_bytes` raises `ValueError`**, not
   `AssertionError`. Chosen to keep the failure surface uniform with
   `CacheSettings.size_limit_mb`'s pydantic validation (which raises
   `ValidationError`, a `ValueError` subclass). Documented inline.
5. **Phase 3 — 2 fixture e2e tests skip when markitdown is missing.**
   Guarded by `@pytest.mark.skipif(not _markitdown_available(), ...)`
   so the suite stays in the default pool. Install with
   `pip install doc3gpp[extract]` to exercise them locally.

**Pre-existing ruff violations (not from this work):**
`docs/ttcn_cr_cli_example.py` has 2 unused-symbol errors that predate
this plan. Out of scope (the file is the reference script, not part
of the new pipeline).

## Open questions (resolve before Phase 4)

1. The `R5-` and `C6-` URL templates are placeholders until we hit
   the live site. Acceptable to ship Phase 2 with them returning
   `None` and address in Phase 8? → **Resolved (yes)** — Phase 2
   ships with these returning `None`; Phase 8 will harden against the
   live site.
2. Should `tdoc extract` require a prior `meeting sync` + `tdoc
   sync` (i.e. the TDoc must exist in `tdocs` to be extractable),
   or should it accept an arbitrary `R5s\d{6}` / `R5-\d{6}` and
   self-bootstrap? Lean toward the former (current plan) — keeps
   the type-check and meeting-resolve logic simple and consistent
   with `TDocSyncCoordinator`.
3. The `tdoc_extracts` sidecar row: keep it, or fold `markdown_path`
   into `tdoc_cr_details`? Lean toward keeping it separate — the
   markdown path is a *cache pointer*, the detail row is *parsed
   data*; they have different invalidation lifecycles.
