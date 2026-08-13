# 3GPP Spec Sync / List / Show — Design

**Status:** design draft (awaiting user review before writing-plans)
**Date:** 2026-08-10
**Author:** brainstorming session

## 1. Problem

The current `doc3gpp` codebase scrapes and persists meeting calendars,
TDoc lists, auxiliary TDoc files, CR cover pages, TTCN sidecars, and
Work Items (WIs). There is **no** model for the 3GPP **specifications**
themselves — the TSs / TRs (e.g. `TS 36.579-5`, `TR 38.760-1`) and the
versioned artefacts produced against them (one ZIP per release, each
with a known meeting, a `versionId`, a CR list, and an ETSI PDF
mirror).

This spec adds:

1. A `spec sync` command that scrapes the per-TSG spec list from the
   3GPP DynaReport page, then fetches each spec's detail page in
   parallel to extract header info, version rows, related WIs, ETSI
   PDF links, and CR lists.
2. A `spec list` command that lists stored specs with rich-filter
   parity against the other list commands.
3. A `spec show` command that renders one spec with its versions and
   per-version PDF / CR / meeting metadata.
4. The same data exposed through the web server (`/specs`,
   `/specs/{spec_id}`) and the MCP server (`list_specs`, `get_spec`).

Two storage tables are added (`specs`, `spec_versions`); the related
WIs are stored as a comma-joined string on the `specs` row (no
`spec_wis` join table). The user explicitly opted out of a
`spec_wis` join table and out of an automatic `wi sync --tsg {tsg}`
trigger at the start of `spec sync`.

## 2. Data model

### 2.1 `specs` (header)

| Column            | Type           | Notes |
|-------------------|----------------|-------|
| `spec_id`         | `String(32) PK` | Full dotted form, e.g. `36.579-5`. Same value as the anchor text in the DynaReport list page and as the path in the detail URL (`/DynaReport/36579-5.htm` minus the dots). |
| `type`            | `String(8)`    | `TS` or `TR`. |
| `title`           | `Text`         | Full spec title from the list page. |
| `status`          | `String(32)`   | From the detail page `#statusVal` (e.g. `Under change control`, `Draft`). |
| `radio_tech`      | `String(64)`   | Comma-joined ticked values from `#radioTechnologyVals` (e.g. `2G,3G,LTE,5G,6G`). Order preserved from the page. |
| `initial_release` | `String(16)`   | Normalised from `#initialPlannedReleaseVal` (e.g. `Release 20` → `Rel-20`, `Release 9` → `Rel-9`, `R99` → `R99`). |
| `tsg`             | `String(16) FK → tsgs.short_name` | Owning TSG (e.g. `R5`). Indexed. |
| `wis`             | `String(512)`  | Comma-joined related-WI acronyms from the detail page's related-WIs grid. Point-in-time snapshot (not a live join against `wis`). |
| `rapporteurs`     | `String(128)`  | Comma-joined company names from the detail page's rapporteurs grid. Nullable. |
| `last_synced_at`  | `DateTime(tz)` | UTC of last successful detail fetch (per spec row). |

`spec_id` is a 1:1 PK on the spec identity used in URLs and TDoc
metadata (`tdocs.spec`). The composite `spec_id_no_dot` (e.g.
`36579-5`) is the URL slug and is a pure function of `spec_id` — it
is never stored.

### 2.2 `spec_versions`

| Column         | Type           | Notes |
|----------------|----------------|-------|
| `spec_id`      | `String(32) FK → specs.spec_id ON DELETE CASCADE` | Indexed. |
| `version`      | `String(16)`   | e.g. `18.3.0`. |
| `ftp_url`      | `String(1024)` | `https://www.3gpp.org/ftp/Specs/archive/36_series/36.579-5/36579-5-i30.zip`. Stored as the absolute URL (matches the convention used for `tdocs.ftp_url` and `tdoc_files.ftp_url`). |
| `release`      | `String(16)`   | Source of truth: the 3GPP release marker as scraped from upstream. For version rows this is the leading-digit mapping of `version` (`0.x.y` → `draft`, `1.x.y` / `2.x.y` / `3.x.y` → `pre-release`, else `Rel-N`) — the `&release=` parameter on the `imgRelatedCRs` href is the 3GPP-internal release id (e.g. `193`) and is **not** mapped to this column. Normalised to the same shape as `specs.initial_release` (`Rel-N`). |
| `meeting_id`   | `Integer`      | From `?m_id=` in the row's `lnkMeetings` link. |
| `meeting_name` | `String(64)`   | From the link text (e.g. `RAN#108`). |
| `upload_date`  | `Date`         | From the row's `Upload date` cell. |
| `version_id`   | `Integer`      | From the `?versionId=` parameter in the row's `imgRelatedCRs` link. Used to build the CR list URL. |
| `pdf_url`      | `String(1024)` | From the ETSI fetch. Nullable. |
| `crs`          | `Text`         | Comma-joined `tdoc_id`s from the 3GPP CR list page (e.g. `R5-253030,R5-253031`). Nullable. |

PK: `(spec_id, version)`. One row per (spec, version) — re-syncing
the same pair is idempotent.

`ON DELETE CASCADE` mirrors the convention used for the other
spec-detail sidecars (`tdoc_cr_cover_page`, `tdoc_cr_ttcn_details`,
`tdoc_cr_change_details`).

### 2.3 `tsgs.spec_last_sync` (additive)

> **Superseded** — the per-TSG `tsgs.spec_last_sync` column and the
> `TsgRepository.update_spec_last_sync` method described in this
> sub-section were removed in the per-spec skip rule plan
> ([`docs/superpowers/plans/2026-08-13-per-spec-skip-rule.md`](../../plans/2026-08-13-per-spec-skip-rule.md)).
> The spec sync skip rule is now keyed on `specs.last_synced_at`
> and enforced per worker inside `SpecService.sync` (and per call
> inside `SpecService.sync_spec`); `tsgs` no longer carries a
> `spec_last_sync` column and `TsgRepository` no longer exposes
> `update_spec_last_sync`. The text below is preserved as the
> original design record — the column and method it describes no
> longer exist in the schema or the Protocol.

`tsgs` gains one new column:

| Column           | Type           | Notes |
|------------------|----------------|-------|
| `spec_last_sync` | `DateTime(tz)` | UTC of the last successful `spec sync --tsg <this_tsg>` run. Read by `SpecService.sync` to enforce `settings.sync.spec_sync_interval`; written on every successful sync. Nullable so existing rows in the seeded reference table are valid as-is. |

`SQLAlchemyTsgRepository` gains
`update_spec_last_sync(short_name, synced_at) -> bool`, mirroring
the existing `update_meeting_last_sync`. The Protocol gains the
same method. `create_schema` picks up the new column via the
existing `Base.metadata.create_all` call — the new column is
nullable, so the migration is in-place for existing rows.

## 3. Scraping & parsing

### 3.1 List page

`https://www.3gpp.org/dynareport?code=TSG-WG--{tsg}.htm`

- The relevant table has class `dsptab adynspec dsp-tsgwg ...`.
- Each data row has 3 cells: `Spec` (type + `<a>` to detail), `Title`
  (text), `Rapporteur` (text + optional link).
- `parse_spec_list(html, tsg)` returns `list[Spec]`. Rows missing the
  spec anchor or the type token are silently skipped (mirrors the
  existing `wi_parser` policy).

### 3.2 Detail page

`https://www.3gpp.org/DynaReport/{spec_id_no_dot}.htm`
(e.g. `36579-5`).

`parse_spec_detail(html, spec_id, tsg)` returns `(header_fields,
list[SpecVersion])`:

- `header_fields`:
  - `status` ← `#statusVal`
  - `initial_release` ← `#initialPlannedReleaseVal`, normalised
    (see §3.5)
  - `radio_tech` ← ticked checkboxes inside `#radioTechnologyVals`
  - `wis` ← `<span>` texts in the related-WI grid's acronym column
- `version_rows`:
  - Per `<a id=...lnkFtpDownload ...>VERSION</a>` + sibling
    `<a id=...lnkMeetings ...>MEETING</a>` + sibling
    `<a id=...imgRelatedCRs ...>` (carries `?versionId=` and a
    3GPP-internal `&release=` id which is **ignored** for the
    `release` column):
    - `version`, `ftp_url` (absolute), `meeting_id` (from `m_id`),
      `meeting_name`, `version_id`
    - `release` ← derived from `version` via the leading-digit
      mapping in §3.5 (the `&release=` parameter is the 3GPP-
      internal release id and is not surfaced).
  - `upload_date` ← next-cell `YYYY-MM-DD` text.

### 3.3 Per-version follow-up fetches

For each `SpecVersion` returned by `parse_spec_detail`, the service
issues the optional follow-up fetches **only when the gating
predicate is true**. The WKI and CR list links are siblings of the
`lnkFtpDownload` anchor in the version row; the parser extracts them
along with the version metadata so the service can fire the
follow-ups without re-parsing the page.

**ETSI PDF** (gated):

- Trigger: a per-row `<a id=...imgRelatedWI ... href="...WKI_ID={N}...">`
  is present in the source HTML for this version row **AND** the
  existing row's `pdf_url` is `NULL`.
- Fetch: `GET https://portal.etsi.org/webapp/workprogram/Report_WorkItem.asp?WKI_ID={N}`.
- Parse: first `<a href="...\.pdf">` in the response (the ETSI
  "download as PDF" link).
- On success, set `pdf_url` on the upserted row. On HTTP error /
  parse miss, leave `pdf_url` as `NULL` and log at `DEBUG`.

**3GPP CR list** (gated):

- Trigger: a per-row `imgRelatedCRs` anchor with a `?versionId=`
  parameter is present **AND** (`upload_date` is within 3 months of
  `now` **OR** the existing row's `crs` is `NULL` / empty).
- Fetch: `GET https://portal.3gpp.org/ChangeRequests.aspx?q=1&versionId={version_id}`.
  The `&release=` parameter that lives on the same href is a
  3GPP-internal release id and is **not** required — the bare
  `?q=1&versionId={N}` URL returns the same list (verified on
  3gpp.org: `?versionId=92276` and `?versionId=92276&release=193`
  both return the same change-request table).
- Parse: every `<a id="wgTdocDetailsLink" ...>TDOC_ID</a>` in the
  rendered table. The page is paginated; the default page size is
  200 and the parser does **not** page — per-version CR counts
  above 200 are rare and would be a separate follow-up.
- Store as comma-joined string. On HTTP error, leave `crs` as-is and
  log at `DEBUG`.

### 3.4 Concurrency

The per-spec detail fetch is parallelised with a `concurrent.futures.ThreadPoolExecutor`
backed by the existing `httpx.Client` connection pool. Worker count
defaults to `min(32, os.cpu_count() + 4)` (Python's
`ThreadPoolExecutor` default). The follow-up ETSI / CR fetches are
issued from inside the per-spec worker — i.e. nested parallelism is
serial inside each spec — to keep memory bounded and avoid hammering
`portal.3gpp.org` with thousands of concurrent connections.

The list page is fetched once, sequentially, at the start of `sync`.

### 3.5 Release normalisation

Upstream uses two different release-marker shapes:

- **The spec detail page** emits `Release 20`, `Release 9`, …
- **Pre-2000 releases** (only in `initial_release`) use `R99`.

Both `specs.initial_release` and `spec_versions.release` are stored
in a single canonical form so the CLI / web / MCP surfaces do not
have to special-case the upstream shape. The normaliser is a single
pure function in
`src/doc3gpp/parsers/spec_release.py`:

```python
def normalise_release(text: str) -> str:
    """Return the canonical release marker.

    - ``"Release 20"`` → ``"Rel-20"``
    - ``"Release 9"``  → ``"Rel-9"``
    - ``"R99"``        → ``"R99"`` (passed through; pre-Rel-4 marker)
    - ``"draft"`` / ``"pre-release"`` / already-canonical values
      pass through unchanged.
    - Empty / whitespace → empty string.
    """
```

The leading-digit mapping in `spec_versions.release` is also
expressed through the same helper:

- `0.x.y` → `draft`
- `1.x.y` / `2.x.y` / `3.x.y` → `pre-release`
- else → `Rel-{N}` (where `N` is the major digit of `version`)

So both columns share one source of truth for the canonical shape.

## 4. Service layer

```python
class SpecService:
    def __init__(
        self,
        repository: SpecRepository,
        tsg_repository: TsgRepository,
        sync_interval: timedelta = timedelta(hours=24),
    ) -> None: ...

    def sync(self, tsg: str, *, force: bool = False) -> SyncOutcome:
        """Fetch list page → parallel detail pages → upsert.

        - Resolves the TSG via the existing `TsgRepository` (raises
          if unknown).
        - ~~Reads ``tsgs.spec_last_sync`` for the TSG; if it is
          non-null and within ``sync_interval`` of ``now``, returns
          a ``SyncOutcome(status="skipped", ...)`` (mirrors
          ``MeetingService.sync``). ``force=True`` bypasses the
          check.~~ **Superseded** — the per-TSG skip was replaced by
          a per-spec `last_synced_at` check; see
          [`docs/superpowers/plans/2026-08-13-per-spec-skip-rule.md`](../../plans/2026-08-13-per-spec-skip-rule.md).
        - Fetches the per-TSG list once.
        - For each spec_id, fetches the detail page in a thread
          pool, parses header + versions, then runs the
          conditional ETSI / CR follow-up fetches.
        - Upserts the header row + version rows in a single
          transaction per spec.
        - On success, ~~stamps ``tsgs.spec_last_sync = now()``.~~
          **Superseded** — each per-worker `_sync_one_spec` now
          stamps the spec's own `specs.last_synced_at` instead; see
          the link above.
        - Returns a ``SyncOutcome(status, reason, synced_count,
          version_count)``.
        """

    def list_recent(
        self,
        limit: int = 50,
        offset: int = 0,
        tsg: str | None = None,
        type: str | None = None,
        spec_id: str | None = None,
        title: str | None = None,
        status: str | None = None,
        radio_tech: str | None = None,
        initial_release: str | None = None,
        wis: str | None = None,
    ) -> list[Spec]: ...

    def get(self, spec_id: str) -> Spec | None: ...

    def list_versions(
        self,
        spec_id: str,
        limit: int = 200,
        offset: int = 0,
    ) -> list[SpecVersion]: ...
```

`SpecService` does **not** automatically run `wi sync --tsg {tsg}`
before scraping. The user explicitly removed that dependency. WIs on
the `specs.wis` column are a point-in-time snapshot from the spec
detail page; the comma-joined string reflects what the spec page
listed at sync time.

## 5. Repository layer

```python
class SpecRepository(Protocol):
    def upsert(self, spec: Spec) -> None: ...
    def upsert_versions(self, versions: list[SpecVersion]) -> int: ...
    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        tsg: str | None = None,
        type: str | None = None,
        spec_id: str | None = None,
        title: str | None = None,
        status: str | None = None,
        radio_tech: str | None = None,
        initial_release: str | None = None,
        wis: str | None = None,
    ) -> list[Spec]: ...
    def get(self, spec_id: str) -> Spec | None: ...
    def list_versions(
        self,
        spec_id: str,
        limit: int = 200,
        offset: int = 0,
    ) -> list[SpecVersion]: ...
```

`SQLAlchemySpecRepository` lives at
`src/doc3gpp/storage/repositories/spec_sql.py`. `list_versions`
orders by `version DESC` (newest first) and supports `offset` for
pagination. `list` uses the existing `apply_text_filter` for the
rich-filter text columns (matches the `WiRepository` pattern).

`SQLAlchemyTsgRepository` ~~gains
`update_spec_last_sync(short_name: str, synced_at: datetime) -> bool`
(mirroring `update_meeting_last_sync`). The `TsgRepository`
Protocol and the in-memory test double both pick it up.~~
**Superseded** — `update_spec_last_sync` was removed in the
per-spec skip rule plan
([`docs/superpowers/plans/2026-08-13-per-spec-skip-rule.md`](../../plans/2026-08-13-per-spec-skip-rule.md));
the per-spec skip is enforced by `SpecService` against
`specs.last_synced_at`.

## 6. CLI surface

`doc3gpp spec sync --tsg {tsg}` plus two new subcommands
under a new `spec_app` typer (three total):

| Command | Notes |
|---|---|
| `doc3gpp spec sync --tsg R5 [--force]` | Triggers the full list+detail+follow-up fetch. ~~Honours `settings.sync.spec_sync_interval` (default 24h): a second sync within the interval is skipped with a `SyncOutcome(status="skipped", reason=...)` unless `--force` is passed.~~ **Superseded** — the skip rule is now **per-spec** and keyed on `specs.last_synced_at`; each per-worker `_sync_one_spec` short-circuits specs whose own `last_synced_at` is within the interval and stamps the spec's own `last_synced_at` on a successful re-sync. See [`docs/superpowers/plans/2026-08-13-per-spec-skip-rule.md`](../../plans/2026-08-13-per-spec-skip-rule.md). |
| `doc3gpp spec list [--tsg] [--type TS|TR] [--spec-id] [--title] [--status] [--radio-tech] [--initial-release] [--wis] [--limit N] [--offset N] [--format table\|json\|markdown] [--output FILE] [--compact]` | Mirrors `tdoc list` and `wi list`. All text columns use the rich-filter grammar (`null` / `not-null` / `!pattern` / plain `LIKE`). |
| `doc3gpp spec show --spec-id {spec_id} [--format table\|json\|markdown] [--output FILE] [--compact]` | Renders header + version table (per-version: version, release, ftp_url, meeting_id, meeting_name, upload_date, pdf_url, crs count). |

`spec_app` is registered in `cli.py` next to `wi_app`. The default
TSG constant `DEFAULT_TSG = "r5"` is reused.

## 7. Web & MCP surface

### Web

- `GET /specs` — list page mirroring `/wis` / `/tdocs`. Filter form
  with the same 5-column grid (tsg, type, spec_id, title, status +
  radio_tech). HTMX fragment `partials/spec_results.html` for the
  swap target.
- `GET /specs/{spec_id}` — detail page mirroring `/meetings/{id}`.
  Renders header + version table with per-row "open" links to the
  ftp_url / pdf_url / CR list / tdoc_id anchor pages.
- `JSONResponse` branch on both routes for `?format=json`. Default
  fields exposed: `spec_id, type, title, status, radio_tech,
   initial_release, tsg, wis` (header) + `version, release, ftp_url,
   meeting_id, meeting_name, upload_date, pdf_url, crs`
   (versions).

### MCP

Two new tools, byte-for-byte parity with the HTTP `?format=json`
output:

- `list_specs(tsg, type, spec_id, title, status, radio_tech,
  initial_release, wis, limit, offset)` — filters described in the
  same rich-filter grammar; mirrors `list_wis`.
- `get_spec(spec_id)` — returns `{spec: {...}, versions: [...]}`.

## 8. Settings & schema bootstrap

`SyncSettings` (the existing `[sync]` TOML block) gains one new
field:

| Field | Default | Description |
|---|---|---|
| `spec_sync_interval` | `timedelta(hours=24)` | Minimum time between re-syncs of the **same spec** (per-spec throttle). Mirrors `meeting_sync_interval` in shape but is now enforced per spec via `specs.last_synced_at` rather than per TSG. |

The human-string parser (`24h`, `30m`, `90d`, `PT1H`, …) is already
wired through `_validate_durations`, so the new field is
automatically readable from TOML. Following the convention used
for `meeting_sync_interval` (which is also TOML-only — not in
`ALLOWED_ENV_VARS`), `spec_sync_interval` is **not** added to the
env-var allowlist. The `doc3gpp.toml.example` block gets a
commented-out example next to `meeting_sync_interval`.

The follow-up fetch knobs (3-month CR recency gate, `min(32, cpu + 4)`
workers) remain constants on `SpecService`. If a future change
exposes them, the conventional `[spec_sync]` block follows the
existing `tdoc_parse.max_tdoc_size_kb` pattern.

`create_schema` already calls `Base.metadata.create_all(bind=engine)`,
so the new `specs` / `spec_versions` tables ~~and the new
`tsgs.spec_last_sync` column~~ are created on the next
`doc3gpp db init` / first command run. No alembic migration is
needed (the project is on the in-place `create_schema` bootstrap,
~~and the new column is nullable~~). **Superseded** — the
`tsgs.spec_last_sync` column was removed in the per-spec skip
rule plan
([`docs/superpowers/plans/2026-08-13-per-spec-skip-rule.md`](../../plans/2026-08-13-per-spec-skip-rule.md));
the per-spec skip rule is enforced at the service layer against
`specs.last_synced_at`.

## 9. Testing

- **Unit** (`tests/unit/test_spec_parser.py`): golden-file HTML
  fixtures for the list page, the detail page (with versions +
  related WIs + CR link), the ETSI page, and the CR list page. The
  parser is pure, so each fixture maps 1:1 to a parser assertion.
- **Unit** (`tests/unit/test_spec_service.py`): the service is fed a
  stub `ScraperClient` + stub `SpecRepository` and the gating
  predicates (ETSI `pdf_url is NULL`, CR `upload_date within 3m OR
  crs is empty`) are exercised with table-driven cases.
- **Integration** (`tests/integration/test_spec_sql.py`): a sqlite
  in-memory `SQLAlchemySpecRepository` round-trips `Spec` +
  `SpecVersion` and re-reads them; the rich-filter `list` and the
  `list_versions` ordering are asserted.
- **Integration** (`tests/integration/test_spec_cli.py`): the CLI
  surface (`spec sync --help`, `spec list`, `spec show`) is
  exercised via Typer's `CliRunner` with a stubbed service.
- **Online** (`-m online`): one end-to-end sync against `R5`,
  asserting that a small known spec (e.g. `36.579-5`) returns ≥ 1
  version with a populated `ftp_url`. The follow-up ETSI / CR
  fetches are not asserted in CI (they depend on the live state of
  external portals); they're tested manually in the design review.

## 10. Documentation sync

- `AGENTS.md` — extend the "Where to look" table with the spec
  row; the "Doc pointers" with the design / plan paths.
- `docs/cli.md` — add a `spec` section next to `wi` / `tsg` /
  `meeting` with every flag and example.
- `docs/code-map.md` — add the new symbols under
  `services/`, `parsers/`, `storage/`, `web/`.
- `docs/architecture.md` — add the two ORM tables to the schema
  diagram and the data-flow paragraph for the new commands.
- `docs/3gpp-knowledge.md` — note the new DynaReport list / detail
  URL patterns next to the existing `dynareport?code=…` examples.
- `README.md` — short mention in the feature list.
- `doc3gpp.toml.example` — add a commented-out
  `spec_sync_interval` row next to `meeting_sync_interval` in the
  `[sync]` block.

## 11. Open / non-goals

- **Live `wis` join.** The comma-joined `specs.wis` is a
  point-in-time snapshot. A future `spec refresh-wis` (or an
  `--refresh-wis` flag on `spec sync`) could re-parse the detail
  page and update the column without doing a full sync. **Out of
  scope for this spec.**
- **Pagination of the CR list page.** The default page size on
  `portal.3gpp.org` is 200. Per-version CR counts above 200 are
  rare; if needed, a follow-up can add a `?page=N` driver.
- **Backfill command.** A standalone `spec backfill` that walks
  every stored spec and re-fetches the detail page is unnecessary —
  re-running `spec sync --tsg R5` already updates every spec the
  page lists.
- **TSG → spec link validation.** `specs.tsg` is an FK to
  `tsgs.short_name`. The TSG reference table is auto-seeded by
  `_ensure_tsg_ready`; no extra wiring needed.
- **Status / draft propagation.** We do **not** propagate the spec's
  `status` (e.g. `Draft`) to any downstream TDoc field. The spec
  status is informational.
