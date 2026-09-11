# RAN5 Testcases Sync / List / Show — Design

**Status:** design draft (awaiting user review before writing-plans)
**Date:** 2026-09-11
**Branch:** `feature/ran5-testcases`
**Author:** brainstorming session

## 1. Problem

`doc3gpp` scrapes meetings, TDocs, CR details, WIs and Specs, but has no
model for the RAN5 conformance **testcases** (the GCF/PTCRB + TTCN status
per TC published in the `TTCN CR Agreement Status` workbooks). Operators
currently track `Approved` / `Not approved` state by hand in the xlsx.

This spec adds:

1. A `testcase sync` command that discovers the latest immutable status
   zip in the 3GPP `History/` folder, downloads it once, unzips the
   single workbook, parses the six group sheets, and upserts
   `testcases` + `testcase_status` rows. Re-running sync is a no-op
   until a new file (later `yyyy-wk` or new `r`/`rev` revision) appears.
2. A `testcase list` command with pagination, rich-filter field filters,
   a cross-path `--status` filter on `ttcn_status`, and a composed
   `statuses` JSON dict (`path → ttcn_status`) per row.
3. A `testcase show` command rendering one testcase with all
   `testcases` fields plus every `testcase_status` row (both
   `gcf_ptcrb` and `ttcn_status`).
4. The same data on REST (`/testcases`, `/testcases/{id}`), the web UI
   (list + detail pages with HTMX fragments), background jobs
   (`POST /jobs/sync/testcases` + sync-hub panel), and MCP
   (`list_testcases`, `get_testcase`, `sync_testcases`), byte-consistent
   with the CLI JSON.

Decisions already locked in brainstorming:

- `testcase_status` stores **both** `gcf_ptcrb` and `ttcn_status` per
  `(testcase_id, path)` (the extraction always sees the pair).
- Sync resolves the **single latest** file only; no history backfill,
  no `--file` override in v1.
- `--status` matches **`ttcn_status` only** (any path); GCF/PTCRB gets
  its own `--gcf-status` flag with the same semantics.
- Sync state lives in a dedicated **`testcase_sources`** table
  (`filename` PK + `downloaded_at` / `parsed_at`), not as columns on
  data rows.
- Full job parity: `JobKind.SYNC_TESTCASES` + jobs route + sync-hub
  panel + MCP enqueue tool, mirroring `SYNC_SPECS`.

## 2. Data model

### 2.1 `testcases` (header, one row per TC)

| Column         | Type            | Notes |
|----------------|-----------------|-------|
| `testcase_id`  | `String(64) PK` | From the `TC` column, stripped. Global PK across all six sheets (a TC never repeats across groups upstream; a re-occurrence in a later file upserts in place). |
| `title`        | `Text`          | From `Title`. Nullable (empty cell → `None`). |
| `ats`          | `String(128)`   | From `ATS`. Nullable. |
| `feature`      | `String(256)`   | From `Feature`. Nullable. |
| `release`      | `String(32)`    | From `Release` (verbatim, e.g. `Rel-17`). Nullable. |
| `wis`          | `String(512)`   | From `RAN WIC` (verbatim snapshot, comma-joined upstream string kept as-is). Nullable. |
| `spec`         | `String(32)`    | Fixed `38.523-1` for group `5G`, fixed `36.523-1` for group `LTE`, else verbatim from the `part of` column. Nullable (missing `part of` cell → `None`). |
| `group`        | `String(8)`     | Sheet origin: `5G`, `LTE`, `IMS`, `UTRA`, `POS` (`Positioning_TC_Status`), `MCX`. Indexed. |

No per-row sync timestamp — file-level freshness lives in
`testcase_sources` (§2.3). Re-syncing the same file is a no-op; syncing
a newer file upserts every parsed TC in place (a TC that changed groups
upstream moves groups — last writer wins, matching the `Spec` upsert
convention).

### 2.2 `testcase_status` (one row per TC × path)

| Column         | Type | Notes |
|----------------|------|-------|
| `testcase_id`  | `String(64) FK → testcases.testcase_id ON DELETE CASCADE` | Part of composite PK. Indexed. |
| `path`         | `String(16) NOT NULL` | Part of composite PK. See path vocab below. Single-path groups store the literal `'default'` (see §2.2.1 rationale — `NULL` can be neither a PK member nor a JSON key). |
| `gcf_ptcrb`    | `Text` | Verbatim GCF/PTCRB cell, stripped; empty → `None`. Nullable. |
| `ttcn_status`  | `Text` | Verbatim TTCN-Status cell, stripped; empty → `None`. Nullable. Indexed (the `--status` EXISTS filter hits this column). |

PK: `(testcase_id, path)`.

Path vocab (normalised, `MCX-` prefix stripped):

| Group | Paths |
|-------|-------|
| `5G`  | `FR1`, `FR2`, `FR1+FR2` |
| `LTE` | `FDD`, `TDD` |
| `IMS` / `UTRA` / `POS` | `'default'` (single pair; surfaced as `"default"` key in list JSON and as `path: default` in show) |
| `MCX` | `IPCAN-4G`, `EUTRA`, `IPCAN-5G`, `NR5GC` |

Note on the request text: it lists MCX paths as `IPCAN-4G, EUTRA,
IOCAN-5Gm, NR5GC` — `IOCAN-5Gm` is read as a typo for the sheet's
`MCX-IPCAN-5G` column and normalised to `IPCAN-5G`. Flag in review if
upstream truly uses a different label.

#### 2.2.1 Why `'default'` instead of `NULL`

The request says `null` path for `IMS`/`UTRA`/`Positioning`. Three
reasons to store the literal `'default'` instead:

1. `path` is part of the composite PK — `NULL` PK members are
   rejected / behave inconsistently across dialects.
2. `UNIQUE(testcase_id, path)` does not deduplicate `NULL` paths
   (`NULL != NULL`), so re-sync could insert duplicate rows.
3. The list JSON is a dict keyed by path — JSON has no `null` key;
   `"default"` serialises cleanly on every surface (CLI/web/MCP).

The domain dataclass keeps `path: str` (never `None`); renderers emit
the stored value verbatim.

### 2.3 `testcase_sources` (sync ledger, one row per status file)

| Column           | Type | Notes |
|------------------|------|-------|
| `filename`       | `String(256) PK` | Upstream basename, e.g. `TTCN CR Agreement Status 2024-wk32.zip` (URL-decoded). |
| `year`           | `Integer` | Parsed `YYYY`. |
| `week`           | `Integer` | Parsed `wk##`. |
| `revision`       | `Integer` | Parsed `rN` / `revN`, `0` when absent. Together `(year, week, revision)` is the total order for latest-selection. |
| `downloaded_at`  | `DateTime(tz)` | UTC when the zip bytes were fetched. Set before parsing so a parse crash still records the attempt. |
| `parsed_at`      | `DateTime(tz)` | UTC when parse + upserts completed. `NULL` means "downloaded but never successfully parsed" → next sync retries the same file. Nullable. |
| `testcase_count` | `Integer` | Rows upserted into `testcases` by this file. |
| `status_count`   | `Integer` | Rows written into `testcase_status` by this file. |

Skip rule: `sync` resolves the latest upstream filename; when a
`testcase_sources` row exists for it with `parsed_at NOT NULL` and
`--force` is absent, sync returns `SyncOutcome(status="skipped", …)`
without downloading. Any new filename (greater `(year, week,
revision)` **or** merely different — comparison is by filename
identity, ordering only matters for latest-selection) triggers a full
download + parse + upsert. `--force` re-downloads and re-parses the
latest file even when already recorded.

## 3. Scraping & parsing

New modules (layering: network in `scraping/`, parsing in `parsers/`):

- `src/doc3gpp/scraping/testcase_source.py` — transport only.
- `src/doc3gpp/parsers/testcase_parser.py` — pure functions only.

`openpyxl` is already a hard dependency (`pyproject.toml`), so no new
packages. `ScraperClient.get_bytes` (exists, retry-backed) fetches the
zip; `zipfile` + `openpyxl.load_workbook(read_only=True,
data_only=True)` parse in memory — no on-disk cache in v1.

### 3.1 History listing & latest selection

```
HISTORY_URL = "https://www.3gpp.org/ftp/tsg_ran/WG5_Test_ex-T1/TTCN/Reporting/TTCN_status/History/"
```

- `list_history_files(client) -> list[str]`: `GET` the folder URL as
  text, collect `<a href="…​.zip">` basenames (URL-decoded,
  case-insensitive `.zip`), keep names matching the status-file
  grammar, ignore everything else (non-status zips, parent/sort links).
- Filename grammar (case-insensitive, spaces significant):
  `TTCN CR Agreement Status {YYYY}-wk{##}[{_|-}r{N}|rev{N}].zip`,
  e.g. `TTCN CR Agreement Status 2019-wk15_rev1.zip`. Regex:
  `r"TTCN CR Agreement Status (\d{4})-wk(\d{2})(?:[_-]?(?:r|rev)(\d+))?\.zip$"`
  with `re.IGNORECASE`. Unmatched zips are ignored (logged at `DEBUG`).
- `select_latest(filenames) -> str`: max by `(year, week, revision)`
  with missing revision as `0`. Raises a typed
  `TestcaseSourceNotFoundError` when no name matches (surfaced as
  `typer.BadParameter` on CLI, `InvalidFilterError` on HTTP/MCP).
- `fetch_testcase_zip(filename, client) -> bytes`: `GET` (bytes)
  `urljoin(HISTORY_URL, quote(filename))`. Filename quoting matters —
  upstream names contain spaces.

### 3.2 Workbook extraction

- `extract_workbook(zip_bytes) -> bytes`: open the zip, collect
  `*.xlsx` members (case-insensitive). Zero members →
  `TestcaseWorkbookNotFoundError`. More than one → pick the
  lexically-first name and log a warning (upstream ships one workbook;
  determinism beats guessing).
- Sheet map (exact names; unknown sheets ignored):
  `5G_TC_Status → 5G`, `LTE_TC_Status → LTE`,
  `IMS_TC_Status → IMS`, `UTRA_TC_Status → UTRA`,
  `Positioning_TC_Status → POS`, `MCX_TC_Status → MCX`.
  A missing sheet is skipped with a warning (layout drift must not fail
  the whole sync); the outcome `reason` lists skipped sheets.

### 3.3 Row parsing (fixed columns + header validation)

Row 1 of every sheet is the header. Common-field lookup is by
**normalised header name** (strip + casefold; `RAN WIC` matches with
any internal spacing): `tc`, `title`, `ats`, `feature`, `release`,
`ran wic`, `part of`. Rows with an empty `TC` cell are skipped.

Status columns are **fixed positions** (0-indexed; letters are the
1-indexed Excel labels from the request) with **header-substring
validation** — the cell in row 1 at each expected position must contain
`gcf / ptcrb` (normalised: strip non-alphanumerics, casefold) for a GCF
column and `ttcn-status`/`ttcn status` for a TTCN column. On mismatch
the pair is skipped for that sheet with a warning naming the sheet,
expected letter, and actual header text (fail-soft per sheet, not per
sync — upstream occasionally relabels one group).

| Group | Pairs (GCF col → TTCN col) |
|-------|----------------------------|
| `5G`  | shared GCF `C(2)` → `FR1:D(3)`, `FR2:P(15)`, `FR1+FR2:W(22)` |
| `LTE` | `FDD:C(2)→D(3)`, `TDD:P(15)→Q(16)` |
| `IMS` / `UTRA` / `POS` | single `C(2)→D(3)`, path `'default'` |
| `MCX` | `IPCAN-4G:C(2)→D(3)`, `EUTRA:R(17)→S(18)`, `IPCAN-5G:AA(26)→AB(27)`, `NR5GC:AL(37)→AM(38)` |

`spec` resolution: `5G → 38.523-1`, `LTE → 36.523-1`, otherwise the
row's `part of` cell verbatim (stripped, empty → `None`).

Cell normalisation: `None` / whitespace-only → `None`, else
`str(cell).strip()` verbatim (no case folding — status strings like
`Approved` keep upstream casing; the `--status` filter uses `LIKE`,
which is ASCII case-insensitive in SQLite).

`parse_testcase_workbook(xlsx_bytes) ->
tuple[list[TestCase], list[TestCaseStatus], list[str]]` returns the
header rows, status rows, and skipped-sheet notes. Pure — all I/O
stays in the service.

### 3.4 Upsert semantics

- `testcases`: `upsert_many` keyed by `testcase_id` (update in place,
  same pattern as `SQLAlchemyTDocRepository.upsert_many`).
- `testcase_status`: per-`testcase_id` **delete-then-insert** inside one
  transaction (removes stale paths when a TC loses a path between
  files, e.g. `5G` FR2 pair dropped upstream). Implemented as
  `replace_statuses(testcase_id, rows)` batched per sync; empty status
  set for a TC deletes its rows (a TC with no status cells is still
  listed, with an empty `statuses` dict).
- `testcase_sources`: insert `downloaded_at` before parsing; stamp
  `parsed_at` + counts after both upserts commit (mirrors the
  `SpecService` "stamp only on success" convention so a mid-flight
  crash retries the file).

## 4. Service layer

```python
class TestCaseService:
    def __init__(self, repository: TestCaseRepository) -> None: ...

    def sync(self, *, force: bool = False,
             on_progress: TestCaseProgressFn | None = None) -> SyncOutcome:
        """Resolve latest → skip-check → download → parse → upsert.

        Progress events (for CLI tqdm + job SSE): "listing" (History
        page fetched), "downloaded" {"filename": …}, "parsed"
        {"testcases": N, "statuses": M}. Returns SyncOutcome with
        status synced|skipped and counts in reason/synced_count.
        """

    def list_recent(self, limit: int = 50, offset: int = 0,
                    testcase_id: str | None = None, title: str | None = None,
                    ats: str | None = None, feature: str | None = None,
                    release: str | None = None, wis: str | None = None,
                    spec: str | None = None, group: str | None = None,
                    status: str | None = None,
                    gcf_status: str | None = None) -> list[TestCaseWithStatuses]: ...
    def get(self, testcase_id: str) -> TestCaseWithStatuses | None: ...
```

- `list_recent` returns `TestCaseWithStatuses(testcase, statuses)`
  where `statuses: dict[str, str | None]` maps `path → ttcn_status`
  (the list-view projection; GCF values omitted here by design —
  full pairs live in `show`/`get_testcase`). Ordered by
  `testcase_id ASC` for determinism.
- `status` / `gcf_status` accept the rich-filter grammar (`null` /
  `not-null` / `!pattern` / plain `LIKE`) applied as an `EXISTS`
  subquery on `testcase_status` (`ttcn_status` / `gcf_ptcrb`
  respectively, any path). Both combinable with every field filter
  (AND).
- `group` is an exact-match (upper-cased) filter on the six known
  values, validated at the CLI boundary (`typer.BadParameter` on
  unknown group, mirroring `_validate_tsg_short_name`).
- No auto-sync triggers: `testcase list`/`show` never hit the network
  (unlike meeting/tdoc reads) — the status workbooks are decoupled
  from the meeting calendar.

## 5. Repository layer

```python
class TestCaseRepository(Protocol):
    def upsert_many(self, cases: list[TestCase]) -> int: ...
    def replace_statuses(self, testcase_id: str,
                         rows: list[TestCaseStatus]) -> None: ...
    def list(self, limit: int = 50, offset: int = 0,
             testcase_id: str | None = None, ...,
             status: str | None = None,
             gcf_status: str | None = None) -> list[TestCase]: ...
    def get(self, testcase_id: str) -> TestCase | None: ...
    def list_statuses(self, testcase_id: str) -> list[TestCaseStatus]: ...
    def get_source(self, filename: str) -> TestCaseSource | None: ...
    def record_download(self, source: TestCaseSource) -> None: ...
    def record_parsed(self, filename: str, parsed_at: datetime,
                      testcase_count: int, status_count: int) -> None: ...
```

`SQLAlchemyTestCaseRepository` at
`src/doc3gpp/storage/repositories/testcase_sql.py` reuses
`apply_text_filter` for every text column (the `SpecRepository`
pattern). The `status`/`gcf_status` filters compile to
`EXISTS (SELECT 1 FROM testcase_status s WHERE
s.testcase_id = testcases.testcase_id AND <rich-filter on s.ttcn_status
/ s.gcf_ptcrb>)`. Status-row reads order by a fixed path rank
(`FR1, FR2, FR1+FR2, FDD, TDD, IPCAN-4G, EUTRA, IPCAN-5G, NR5GC,
default`) so `show` output is deterministic.

Domain models in `src/doc3gpp/models/testcase.py`
(`@dataclass(slots=True)`):

```python
class TestCase: testcase_id, title, ats, feature, release, wis, spec, group
class TestCaseStatus: testcase_id, path, gcf_ptcrb, ttcn_status
class TestCaseWithStatuses: testcase, statuses: dict[str, str | None]
class TestCaseDetail: testcase, statuses: list[TestCaseStatus]
class TestCaseSource: filename, year, week, revision, downloaded_at,
                      parsed_at, testcase_count, status_count
```

## 6. CLI surface

New `testcase_app` Typer in `cli.py` (registered as `testcase`,
singular, matching `spec`/`wi`/`meeting`):

| Command | Flags / notes |
|---|---|
| `doc3gpp testcase sync [--force]` | Latest-only sync (§2.3 skip rule). `--force` re-downloads + re-parses. Emits `outcome.reason`. Progress via tqdm (`listing → downloaded → parsed`), mirroring `spec sync`. |
| `doc3gpp testcase list [--testcase TC] [--title] [--ats] [--feature] [--release] [--wis] [--spec] [--group 5G\|LTE\|IMS\|UTRA\|POS\|MCX] [--status PAT] [--gcf-status PAT] [--limit N] [--offset N] [--fields F\|all] [--format table\|json\|markdown] [--output FILE] [--compact]` | All text filters use the rich grammar. `--status` filters on `ttcn_status` (any path); `--gcf-status` on `gcf_ptcrb`. Each row = testcase fields + `statuses` dict (`path → ttcn_status`). Default fields: `testcase_id, title, spec, group, release, statuses`. |
| `doc3gpp testcase show --testcase ID [--format …] [--output …] [--compact]` | Exact-PK lookup; missing → `typer.BadParameter` (`Testcase … not found`). Renders header fields + full status table (`path, gcf_ptcrb, ttcn_status` per row). |

`--fields` selection covers `testcase_id, title, ats, feature,
release, wis, spec, group, statuses` for list. `table` renders
`statuses` as compact `k=v;…` pairs; `json`/`markdown` share the
`_emit_records` path with `--compact` semantics unchanged.

## 7. Web & MCP surface

### REST (`src/doc3gpp/web/routes/testcases.py`)

- `GET /testcases` — query params mirror the CLI filters
  (`testcase, title, ats, feature, release, wis, spec, group, status,
  gcf_status, limit, offset, format`). Default renders
  `testcase_list.html`; `HX-Request: true` renders
  `partials/testcase_results.html` (`#results` swap target, per the
  AGENTS.md HTMX rule); `?format=json` returns the bare row array,
  byte-identical to `testcase list --format json` via shared
  `render.testcase_rows`.
- `GET /testcases/{testcase_id}` — detail page
  (`testcase_show.html`: header card + status-pairs card) or
  `?format=json` → `{"testcase": {…}, "statuses": [{path,
  gcf_ptcrb, ttcn_status}, …]}`. Unknown id → 404 (same error
  envelope as `get_spec`).
- `POST /jobs/sync/testcases` (`routes/jobs.py`, body `{"force":
  bool}`) → enqueues `JobKind.SYNC_TESTCASES`, `202` + job envelope.
  Handler `_sync_testcases` in `workers/handlers.py` calls
  `services.testcase.sync(force=…, on_progress=…)` on a worker thread
  and returns `{status, reason, synced_count, …}` — same shape as
  `_sync_specs`.
- Sync-hub: tenth `<form id="testcase-form">` in `sync.html` +
  `bindJobPolling` wiring in `sync_hub.js`, following the existing
  nine-form pattern.

### MCP (`web/mcp_server.py`, all via `_to_json` compact separators)

- `list_testcases(testcase, title, ats, feature, release, wis, spec,
  group, status, gcf_status, limit, offset)` — same payload as HTTP
  `?format=json`.
- `get_testcase(testcase_id)` — same payload as detail
  `?format=json`; unknown id raises `TestcaseNotFoundError`
  (new error type in `web/errors.py`, mirroring
  `SpecNotFoundError`).
- `sync_testcases(force=False)` — enqueues `SYNC_TESTCASES` via the
  shared `_enqueue` helper, returns `{job_id, status, message,
  links{self, events}}` like the other sync tools.

New enum member `JobKind.SYNC_TESTCASES` (`models/jobs.py`) +
`KIND_TO_HANDLER` entry. `ServiceContainer`/`state.py` gain a
`testcase` service built by `factory.build_testcase_service()`;
`deps.get_testcase_service` injects it.

## 8. Settings & schema bootstrap

- No new `[sync]` interval: freshness is file-identity-based (§2.3),
  not time-based. No `ALLOWED_ENV_VARS` change.
- `HISTORY_URL` is a module constant in `scraping/testcase_source.py`
  (not a setting) — one upstream location, no per-deployment variance
  expected. If a mirror is ever needed, promote to
  `Settings.sync.testcase_history_url` following the
  `tdoc_list_url_template` pattern.
- `create_schema` picks up `testcases` / `testcase_status` /
  `testcase_sources` via `Base.metadata.create_all` (all columns
  nullable except PKs — in-place safe, no alembic, no data migration
  helpers needed for a fresh table family).

## 9. Testing

- **Unit / parser** (`tests/unit/test_testcase_parser.py`): synthetic
  workbook fixture (built with `openpyxl` in-test, all six sheets +
  header-validation cases: relabelled TTCN column → pair skipped with
  warning; empty-TC rows skipped; `part of` → spec for IMS/UTRA/POS/MCX;
  fixed specs for 5G/LTE; MCX prefix stripping; revision-suffix
  filename ordering). Pure functions — no network.
- **Unit / service** (`tests/unit/test_testcase_service.py`): stub
  `ScraperClient` (History HTML + zip bytes) + stub repository;
  skip-rule matrix (already-parsed → skipped; new file → synced;
  `--force` → re-synced; `parsed_at IS NULL` → retried).
- **Integration / SQL** (`tests/integration/test_testcase_sql.py`):
  sqlite round-trip of header + statuses + sources; rich-filter `list`;
  `status`/`gcf_status` EXISTS semantics incl. `null`/`!pattern`;
  per-TC `replace_statuses` stale-path cleanup; `ON DELETE CASCADE`.
- **Integration / CLI** (`tests/integration/test_testcase_cli.py`):
  `testcase sync/list/show --help` + `CliRunner` with stubbed service
  (mirrors `test_spec_cli.py`).
- **Web/MCP** (`tests/unit/test_web_routes.py` additions +
  `test_mcp_end_to_end.py` additions): `GET /testcases?format=json`
  ↔ CLI JSON byte-parity; `GET /testcases/{id}` 404; MCP
  `list_testcases`/`get_testcase` parity; `sync_testcases` enqueue
  shape.
- **Online** (`-m online`, opt-in): one `testcase sync` against the
  live `History/` folder asserting ≥1 testcase row and ≥1 source row;
  no exact-count assertions (upstream grows over time).

## 10. Documentation sync (same change set)

- `AGENTS.md` — `testcase` row in "Where to look"; sync/list/show
  one-liners in the workflow list.
- `docs/cli.md` — full `testcase` section (every flag, default,
  example), incl. the `--status`/`--gcf-status` EXISTS semantics and
  the `statuses`-dict projection.
- `docs/code-map.md` — new symbols under `models/`, `scraping/`,
  `parsers/`, `services/`, `storage/`, `web/`.
- `docs/architecture.md` — three ORM tables in the schema diagram +
  data-flow paragraph.
- `docs/3gpp-knowledge.md` — `History/` URL + filename grammar +
  sheet/column map + fixed-spec rules.
- `docs/web-server.md` — `/testcases` routes + three MCP tools +
  sync-hub panel.
- `README.md` — one-line feature mention.

## 11. Open / non-goals

- **No history backfill.** Only the latest file is ever parsed; older
  files are never downloaded even if unrecorded. A `--file/--url`
  backfill flag is a possible follow-up, not v1.
- **No on-disk zip/xlsx cache.** Zip bytes live in memory during sync
  (workbooks are a few MB). If operators want offline re-parse, a later
  change can stage zips under the existing `TDocCache`.
- **No FTS5/semantic indexing** over testcases in v1 (`search query`
  / `search sem` unchanged). Rerank-free `LIKE` filters are enough at
  this row count (~10k TCs).
- **No WI/spec cross-linking.** `testcases.wis` / `testcases.spec` are
  verbatim snapshots, not FKs — same convention as `specs.wis`.
- **No per-path `--status` scoping** (e.g. `--path FR1 --status
  Approved`) in v1; `--status` is intentionally path-agnostic per the
  request. Path-scoped filtering is a small follow-up on the same
  `EXISTS` pattern.
