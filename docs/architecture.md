# Architecture

The project is implemented as a layered Python package under `src/doc3gpp/`,
shipped both as a library (SDK) and a CLI. Each layer depends only on the
layer below it; cross-layer imports flow strictly downward.

Current scope:

- Configurable SQL backends (sqlite default, mysql, postgres).
- Calendar scraping from the 3GPP DynaReport meetings pages.
- TDoc list scraping from per-meeting FTP folders.
- Work-item (WI) scraping from the per-TSG DynaReport WI pages.
- TDoc CR extraction pipeline (download zip → on-disk cache → python-docx
  render → markdown cache → cover-page parser → persist).
- Calendar / TDoc / WI / TDoc-CR persistence in SQLAlchemy.

## Layers

The seven layers sit between the CLI entry point and the database driver.
Each layer owns one concern; everything above depends only on the layer
immediately below it (with `services` reaching down into `storage` via
the `repository/` Protocols rather than touching the concrete ORM).

```
                  ┌──────────────────────────────────────┐
                  │            cli.py  (Typer)           │
                  └──────────────────┬───────────────────┘
                                     │
                  ┌──────────────────▼───────────────────┐
                  │      services/  (orchestration)      │
                  │    Protocol-typed repos in / out      │
                  └──────────────────┬───────────────────┘
                                     │
                  ┌──────────────────▼───────────────────┐
                  │   repository/  (abstract contracts)  │
                  └──────────────────┬───────────────────┘
                                     │
   ┌────────────┐ ┌─────────────────▼─┐ ┌──────────────────┐
   │ settings/  │ │    storage/        │ │   models/        │
   │ (config)   │ │   (ORM + repos +  │ │  (domain DTOs)   │
   │            │ │    engine)        │ │                  │
   └────────────┘ └─────────┬─────────┘ └──────────────────┘
                           │
                  ┌────────▼─────────┐
                  │    parsers/       │  ←─┐
                  │  (HTML/Excel →    │    │ shared inputs:
                  │   domain objects) │    │ bytes / str
                  └────────┬──────────┘    │
                           │               │
                  ┌────────▼──────────┐    │
                  │  scraping/        │ ───┘
                  │ (network I/O)     │
                  └───────────────────┘
```

Per-layer modules:

- `settings/` — schema and loader for environment-driven and TOML config;
  exposes `get_settings()` (cached) with `Settings` (root) +
  `MeetingSyncSettings` / `OutputSettings` / `OutputFieldsSettings` /
  `CacheSettings` / `TDocParseSettings` sub-models.
    - `src/doc3gpp/settings/schema.py`
    - `src/doc3gpp/settings/loader.py`
    - `src/doc3gpp/settings/config_source.py` (TOML discovery)
- `scraping/` — HTTP/FTP transport. Knows about URLs and bytes; never
  parses content.
    - `scraping/client.py` — `ScraperClient` (retry/backoff, UA, `httpx`)
    - `scraping/calendar_source.py` — DynaReport meetings HTML
    - `scraping/ftp_source.py` — FTP-directory listings + TDoc-list XLSX
    - `scraping/wi_source.py` — DynaReport WI list HTML per TSG
    - `scraping/tdoc_zip_source.py` — TDoc zip URL builder + downloader
      (`R5s` TTCN + `R5w` Workshop branches)
    - `scraping/cache.py` — `TDocCache` (two-subtree on-disk cache for
      zip + markdown, size-based FIFO eviction)
- `parsers/` — `bytes|str` → domain objects. No network I/O.
    - `parsers/calendar_parser.py`, `parsers/html_parsers.py`,
      `parsers/normalizers.py` — meetings HTML → `Meeting`
    - `parsers/tdoc_parser.py`, `parsers/tdoc_file_parser.py` — TDoc
      list XLSX → `TDoc` / `TDocFile`
    - `parsers/wi_parser.py` — DynaReport HTML → `Wi`
    - `parsers/docx_converter.py` — `.docx` → markdown via the
      optional `python-docx` extra (raises
      `PythonDocxNotInstalledError` when missing). The legacy `.doc`
      binary format is rejected at the wrapper boundary because
      python-docx only supports the OOXML container.
    - `parsers/cr_parser.py` — markdown → `TDocCRDetails` (cover-page,
      optional TTCN overview, optional TTCN corrections)
- `models/` — pure domain dataclasses (`@dataclass(slots=True)`),
  passed between layers; never leak ORM attributes.
    - `models/meeting.py`, `models/tdoc.py`, `models/tsg.py`,
      `models/wi.py`, `models/tdoc_file.py`, `models/tdoc_cr.py`
      (`TDocCRDetails` + `TDocExtractMeta`)
- `repository/` — abstract `Protocol` contracts used by services.
    - `repository/protocols.py` — `MeetingRepository`,
      `TDocRepository` (+ `get_by_id`), `TsgRepository`,
      `WiRepository`, `TDocFileRepository`, `TDocCrDetailRepository`
- `services/` — orchestration. Constructed via `services/factory.py`
  (`build_*` helpers); the CLI never imports a concrete SQL repository
  directly.
    - `services/meetings_service.py`, `services/tdoc_service.py`,
      `services/tsg_service.py`, `services/wi_service.py`
    - `services/tdoc_file_service.py` — auxiliary FTP files
    - `services/tdoc_sync_coordinator.py` — cross-service orchestration
      for `tdoc sync`
    - `services/tdoc_cr_service.py` — end-to-end CR extraction pipeline
    - `services/factory.py` — `build_meeting_service`,
      `build_tdoc_service`, `build_tdoc_file_service`,
      `build_tdoc_sync_coordinator`, `build_tdoc_cr_service`,
      `build_tsg_service`, `build_wi_service`,
      `build_tdoc_repository`, `build_tdoc_cr_repository`
- `storage/` — SQLAlchemy ORM models, engine / session factory,
  backend-specific options, concrete Protocol implementations.
    - `storage/db/models.py` — ORM classes
    - `storage/db/session.py` — `get_engine`, `get_session_factory`
      (cached)
    - `storage/db/base.py` — declarative `Base`
    - `storage/db/migrate.py` — `create_schema` (calls
      `Base.metadata.create_all`)
    - `storage/db/migrations/` — placeholder for future Alembic
    - `storage/backends/{sqlite,mysql,postgres}.py` — engine kwargs
    - `storage/repositories/{meeting,tdoc,tsg,wi,tdoc_file,tdoc_cr}_sql.py`
      — concrete `SQLAlchemy*Repository` classes

## Runtime Data Flow

The CLI composes a service via the factory, the service drives the
scrapers + parsers + repos through the Protocols, and the repos own the
SQLAlchemy session. There are four primary end-to-end flows; the
"meeting-based TDoc sync" flow is itself composed of two sub-flows,
and the TDoc CR extraction is the deepest.

### Meetings sync\n\n1. `doc3gpp meeting sync --tsg <short>` validates `<short>` against\n   the `tsgs` table (auto-seeded if empty).\n2. `MeetingService.sync` checks `tsgs.meeting_last_sync` against\n   `Settings.sync.meeting_sync_interval` (default `24h`) and skips\n   the upstream fetch when the last sync is still fresh. `--force`\n   bypasses this check.\n3. On a non-skipped run: `fetch_calendar` (DynaReport HTML) →\n   `parse_3gpp_calendar` (HTML → `Meeting` list). Every parsed\n   `Meeting` is then stamped with `Meeting.tsg = <short>` (canonicalised\n   to upper case) before being handed to\n   `SQLAlchemyMeetingRepository.upsert_many`. The FK constraint\n   requires the parent row to exist in `tsgs`, so the auto-seed in\n   step 1 is a hard prerequisite.\n4. `SQLAlchemyMeetingRepository.upsert_many` writes the rows; a final\n   `delete_with_end_before(cutoff)` pass trims out-of-window rows.\n5. `doc3gpp meeting list --tsg <pattern>` is a SQL ``LIKE`` lookup on\n     the indexed `meetings.tsg` column (case-insensitive on input). Rows\n     without an owning TSG are excluded.

### TDoc list sync (per meeting)

1. `doc3gpp tdoc sync --meeting-id <id>` (or `--meeting <name>`)
   resolves the meeting and reads its stored `ftp_url`.
2. `TDocSyncCoordinator.sync_for_meeting_id` applies three skip rules
   in order: closed window (`meetings.end_date` older than
   `Settings.sync.tdoc_list_closed_window`, default `90d`), recent local
   sync (`meetings.tdoc_list_last_sync` newer than
   `Settings.sync.tdoc_list_sync_interval`, default `30m`), and
   stale upstream XLSX (`Last-Modified` not newer than the local
   `tdoc_list_last_sync`). `--force` bypasses all three rules.
3. On a non-skipped run, the coordinator orchestrates:
     - `TDocService.sync_from_meeting_ftp` →
       `fetch_tdocs_from_meeting_ftp` →
       `read_tdoc_sheet` (XLSX → `TDoc` list) →
       `SQLAlchemyTDocRepository.upsert_many`.
     - `TDocFileService.sync_from_meeting_ftp` uses the freshly-persisted
       TDoc IDs as the prefix list to recognise attachments under
       `Inbox/`, `Docs/`, `Tdocs/`, `Review/`.
4. `SQLAlchemyTDocFileRepository.upsert_many` persists revision / review
   / support files keyed by the unique `ftp_url`.

### TDoc list sync (bulk / no selector)

1. `doc3gpp tdoc sync` (no `--meeting-id` and no `--meeting`) calls
   `TDocSyncCoordinator.sync_all_tracked_meetings`.
2. The coordinator reads the distinct non-null `meeting_id` values from
   the `tdocs` table via
   `SQLAlchemyTDocRepository.list_distinct_meeting_ids` (sorted ascending,
   orphaned TDocs excluded).
3. For each meeting ID, it resolves the record via
   `MeetingService.get_by_id` and runs the same per-meeting sync path as
   the single-meeting flow above (closed window, sync interval, and
   upstream XLSX mtime checks all apply individually). `--force`
   bypasses all three checks for every meeting in the run.
4. A single meeting failure (`MeetingNotFoundError` /
   `MeetingMissingFtpUrlError`) is recorded in `BulkSyncOutcome.failures`
   and does not abort the sweep; iteration continues so a partial sweep
   still completes.
5. The CLI prints a single summary block (no per-meeting lines):
   `TDoc bulk sync: N meeting(s) processed / Synced / Skipped / Failed`
   plus the per-failure detail. Exit code is `1` only when every meeting
   failed; otherwise `0`.

### TDoc CR extraction

1. `doc3gpp tdoc parse` is filter-driven. At least one filter must be
   supplied (`--tdoc` as a LIKE pattern, `--meeting-id`, `--meeting`, or
   any text/date filter); the CLI validates `--meeting-id` when present,
   applies `type == "CR"` by default when no explicit `--type` is
   supplied, and partitions matches into already-parsed vs to-parse
   groups before prompting.
2. `TDocCrService.extract(tdoc_id, *, force=False)`:
    - Pre-resolves the candidate download URL(s) via
      `resolve_download_url(tdoc_id, build_ftp_url(tdocs.ftp_url))`
      (combining the stored `tdocs.ftp_url` rebuilt to an absolute URL
      via `build_ftp_url`, and the template URL), then probes
      `TDocCrRepository.get_by_url` (normalised via `normalize_ftp_path`)
      per candidate. A hit short-circuits with
      `ExtractResult.from_cache = True` and skips the network.
    - Else, `download_tdoc_zip` resolves the TDoc id to its 3GPP URL
      via the template table (`R5s` → TTCN email CR,
      `R5w` → workshop CR), hits `ScraperClient.get_bytes`, and stages
      the zip in `TDocCache.put_bytes(key, payload, "zips")`. On cache
      miss the function tries the stored `tdocs.ftp_url` (rebuilt to
      an absolute URL), falling back to the template on a terminal
      HTTP error.
    - `extract_docx_from_zip` returns `(filename, docx_bytes)`.
    - The markdown for that exact `docx_bytes` is looked up by
      `sha256(docx_bytes)` in `TDocCache.get_bytes(sha, "markdown")`;
      on miss, `convert_document_to_markdown` runs (raises
      `PythonDocxNotInstalledError` if `python-docx` is not installed)
      and the result is written to `markdown/<sha>.md`.
    - `parse_cr_details(markdown, tdoc_id=...)` returns a typed
      `TDocCRDetails` (cover-page; TTCN overview + corrections only
      when `tdoc_id` matches `R5s\d{6}`).
    - `TDocCrRepository.upsert(details, extract_meta)` writes both the
      detail row and the extract-metadata row (both keyed by the
      relative `ftp_url`) in one transaction. The URL is the immutable
      identity — multiple extracts at distinct URLs for the same
      `tdoc_id` write distinct rows, one per revision.
    - Returns `ExtractResult(details, extract_meta, from_cache=False)`.
3. `doc3gpp tdoc show --tdoc <id>` reads `tdocs` (via
   `TDocRepository.get_by_id`) and every matching `tdoc_cr_details`
   row (one per URL/revision), printing each under its own
   `[Extracted Details]` block with the URL as a header. The
   `corrections` list of every block is JSON-dumped for full fidelity.

### Cache + CLI

- `doc3gpp cache status` → `TDocCache.status()` (file count, total
  bytes, limit, per-subdir breakdown; non-mutating).
- `doc3gpp cache purge [--yes]` (with `DOC3GPP_CACHE__PURGE_CONFIRM`
  gating the interactive prompt) → `TDocCache.purge()` clears both
  subtrees and recreates them.

## Database Schema

Tables live in `src/doc3gpp/storage/db/models.py`. Schema bootstrap is
`Base.metadata.create_all` via `doc3gpp db init`.

- `tdocs`:
    - `tdoc_id` (PK), `title`, `meeting_id` (FK → `meetings.meeting_id`),
      `ftp_url`, `source`, `type`, `status`, `reservation_date`,
      `uploaded_date`, `cr_cat`, `is_revision_of`, `revised_to`,
      `release`, `spec`, `version`, `related_wis`, `cr_num`,
      `cr_pack`.
- `tdoc_files`:
    - `id` (PK), `tdoc_id` (FK → `tdocs.tdoc_id`, no cascade),
      `type` (`revision` / `review` / `support`), `file`, `ftp_url`
      (unique, the upsert key; stored as a path relative to the
      canonical 3GPP FTP root), `uploaded_date`.
- `tdoc_cr_details`:
    - `ftp_url` (PK, immutable download URL stored relative to the
      3GPP FTP root) + `tdoc_id` (non-PK FK → `tdocs.tdoc_id` with
      `ondelete="CASCADE"`, indexed for the per-tdoc lookup), one
      column per parsed cover-page / overview / corrections field
      (`spec`, `cr_num`, `rev`, `version`, `title`, `source`, `tsg`,
      `related_wis`, `date`, `cr_cat`, `release`,
      `reason_for_change`, `consequences_if_not_approved`,
      `clauses_affected`, `other_comments`, `revision_history`,
      `ats_version`, `ttcn_release`, `test_case`, `test_suite`,
      `ue`, `ss`, `corrections` JSON blob, `year`, `tech`,
      `extracted_tdoc_id`), `parser_version`, `extracted_at`.
      Identity is the URL because 3GPP assets are
      byte-for-byte identical for the lifetime of the URL while a
      single `tdoc_id` may map to multiple URLs across revisions —
      every revision's parsed record is preserved.
- `tdoc_extracts`:
    - `ftp_url` (PK, matches `tdoc_cr_details.ftp_url`) + `tdoc_id`
      (non-PK FK → `tdocs.tdoc_id` with `ondelete="CASCADE"`, indexed
      for the per-tdoc lookup), `zip_path`, `markdown_path`,
      `doc_filename`, `extracted_at`, `parser_version`. Cache-pointer
      sidecar — the two child tables share the URL as their identity
      but have **no FK between themselves**: the on-disk cache can be
      purged (deleting every `tdoc_extracts` row) without dropping the
      parsed `tdoc_cr_details` history, and the parsed record can be
      rebuilt (deleting `tdoc_cr_details`) without invalidating the
      cached zip/markdown.
- `meetings`:
    - `meeting_id` (PK), `name`, `title`, `location`, `start_date`,
      `end_date`, `ftp_url`, `start_doc`, `end_doc`, `tsg` (nullable
      FK → `tsgs.short_name`, indexed for the `meeting list --tsg` filter).
- `tsgs`:
    - `short_name` (PK), `tsg_name` (unique), `description`, `url`.
      Seeded on `db init`; validates `--tsg` in `meeting sync` and
      `wi sync`.
- `wis`:
    - `(wi_id, tsg_short)` composite PK, `acronym`, `release`, `name`.
      `tsg_short` FK → `tsgs.short_name`; composite PK keeps the natural
      identifier stable across multi-TSG ownership.

Cascading FK deletes are deliberately inconsistent across the schema:
`tdoc_cr_details` / `tdoc_extracts` cascade on `tdocs.tdoc_id`
deletion (they are derived artefacts of the parent TDoc and are safe
to wipe with it), while `tdoc_files` does not (revision files
survive a TDoc re-sync). The `tdoc_cr_details` and
`tdoc_extracts` tables have **no FK between each other**: the cache
sidecar can be purged without dropping parsed detail history, and
the parsed detail can be rebuilt without invalidating the cached
zip/markdown. The `test_cascade_delete_via_fk` ORM test exercises
the cascade end-to-end via a `PRAGMA foreign_keys=ON` connect
listener (SQLite default is OFF).

## Backend Selection

The active backend is selected from `DOC3GPP_DATABASE_URL`:

- sqlite default: `sqlite+pysqlite:///~/.local/share/doc3gpp/doc3gpp.db`
- mysql example: `mysql+pymysql://user:pass@localhost:3306/doc3gpp`
- postgres example: `postgresql+psycopg://user:pass@localhost:5432/doc3gpp`

Backend-specific engine kwargs are applied in
`src/doc3gpp/storage/db/session.py` via:

- `src/doc3gpp/storage/backends/sqlite.py`
- `src/doc3gpp/storage/backends/mysql.py`
- `src/doc3gpp/storage/backends/postgres.py`

## CLI Surface

Implemented command groups in `src/doc3gpp/cli.py` (seven groups,
eighteen commands):

- `db`:
    - `check`
    - `init` — creates the schema and seeds the `tsgs` reference table
    - `reset` — SQLite-only destructive reset; deletes the DB file and
      sidecars, clears the engine cache, recreates the schema, and re-seeds
      `tsgs`
- `meeting`:
    - `sync` — validates `--tsg` against the reference table
    - `list` — filters by `--tsg`, `--name`, `--location`, `--year`,
      `--tdoc`, `--limit`, `--offset`. `--name` / `--location` are raw
      SQL LIKE patterns (use `%` / `_` explicitly). `--tdoc` accepts a
      9-character CR-shape id (e.g. `R5-260013`, `R5s260009`,
      `R5w260013`) and resolves to the meeting whose `start_doc` /
      `end_doc` range brackets the TDoc; prefix match is case-insensitive.
- `tdoc`:
    - `sync` — exactly one of `--meeting-id` or `--meeting`; delegates to
      `TDocSyncCoordinator`
    - `list` — filters by `--tdoc`, `--meeting`, `--meeting-id`,
      `--source`, `--spec`, `--wi`, `--title`, `--cr-cat`, `--status`,
      `--type`, `--revision-of`, `--revised-to`, `--ftp-url`, `--release`,
      `--version`, `--cr-num`, `--cr-pack`, `--uploaded-date`.
      Text-column filters accept the rich grammar from
      `src/doc3gpp/cli_filters.py` (`null` / `not-null` / `!<pattern>` for
      `NOT LIKE` / SQL `LIKE`); `--uploaded-date` additionally accepts
      `OP 'YYYY-MM-DD'` parameterised comparisons — the same surface
      `tdoc parse` exposes.
    - `parse` — `--tdoc` (LIKE pattern on `tdoc_id`), `--meeting-id`,
      `--meeting`, plus every text-column filter and `--uploaded-date`;
      `--force` re-extracts already-parsed rows, `--full` is reserved
      for the parser's `full=True` mode. End-to-end filter-driven:
      candidates are the intersection of every supplied predicate, with
      CR-type as the implicit default and a `max_batch` cap.
    - `show` — `--tdoc`; renders the matching TDoc and, when present,
      a `[Extracted Details]` block from `tdoc_cr_details`
- `tsg`:
    - `list`, `show`, `seed`
- `wi`:
    - `sync` — `--tsg`
    - `list` — filters by `--tsg`, `--name`, `--acronym`, `--release`
- `config`:
    - `path` — which TOML file is in effect (or
      `"(no config file found)"`)
    - `show` — fully-resolved `Settings` as JSON for diffing against
      `doc3gpp.toml.example`
- `cache`:
    - `status` — file count, total bytes, limit, per-subdir breakdown
    - `purge` — `[--yes]` to skip the interactive confirm; gated by
      `CacheSettings.purge_confirm` and overridable via
      `DOC3GPP_CACHE__PURGE_CONFIRM=false`

Every `* list` command also accepts `--format table|json|markdown`
and `-o/--output PATH`. `meeting list`, `tdoc list`, and `tsg list` also
accept `--fields`; `wi list` uses the configured `output.fields.wi` list
without a per-command `--fields` override.

## Composition

The CLI layer never instantiates a concrete `SQLAlchemy*Repository`
directly; everything goes through `services/factory.py::build_*`. The
factory wires:

- `get_settings()` (cached; `cache_clear()` in tests that mutate
  `DOC3GPP_*` env vars)
- `get_engine()` / `get_session_factory()` (cached; same clear
  contract)
- `ScraperClient()` — single instance per CLI invocation

`_build_cache` in the CLI constructs `TDocCache(settings.cache.dir,
size_limit_bytes=settings.cache.size_limit_mb * 1024 * 1024)` directly
for the `cache status` / `cache purge` commands, which don't need the
service stack.

## Testing Layout

- `tests/unit/` — pure-Python unit tests that mock external calls. Coverage
  is concentrated in:
    - parser fixtures (`test_calendar_parser.py`,
      `test_cr_parser.py`, `test_tdoc_parser.py`,
      `test_tdoc_file_parser.py`, `test_wi_parser.py`,
      `test_docx_converter.py`)
    - scraping + cache contracts (`test_tdoc_cache.py`,
      `test_tdoc_zip_source.py`, `test_ftp_source.py`,
      `test_scraper_client.py`)
    - repositories (CRUD + filter combinations for concrete
      `SQLAlchemy*Repository` classes)
    - services (`test_meetings_service_sync.py`,
      `test_tdoc_service_sync.py`, `test_tdoc_sync_coordinator.py`)
    - CLI (`test_meeting_cli*`, `test_tdoc_cli_fields.py`,
      `test_tdoc_sync_cli.py`, `test_tdoc_parse_cli.py`,
      `test_cache_cli.py`, `test_wi_cli.py`, `test_tsg_cli.py`,
      `test_db_reset_cli.py`)
- `tests/integration/` — sqlite-only by default; online + mysql
  opt-in. Coverage includes:
    - `test_sqlite_backend.py`, `test_sdk_integration.py`,
      `test_cli_sqlite.py`, `test_db_reset_sqlite.py`
    - `test_meeting_service_sqlite.py`,
      `test_tdoc_sqlite.py`, `test_tdoc_file_sqlite.py`,
      `test_tdoc_cr_sqlite.py`, `test_tsg_sqlite.py`, `test_wi_sqlite.py`
    - `test_online_3gpp_calendar.py`, `test_online_tdoc_parse.py`,
      `test_online_tdoc_fetch_r5.py` (live 3GPP endpoints,
      `@pytest.mark.online`)
    - `test_mysql_backend.py` (gated on
      `DOC3GPP_TEST_MYSQL_URL`)
- `tests/fixtures/tdoc_cr_doc/` — 7 CR zip fixtures
  (`C6-250028.zip`, `R5-227476.zip`, `R5-253079.zip`,
  `R5s260009.zip`, `R5s260051.zip`, `R5s260135.zip`,
  `R5s260176.zip`). Regression corpus for `cr_parser` and
  `tdoc_cr_service`.
- Pytest markers: `online`, `mysql`. The sqlite profile is
  `pytest -m "not mysql and not online"`; `./scripts/test_sqlite.sh`
  is the canonical wrapper.

## Cross-cutting design rules

These are enforced by code review (see `AGENTS.md` §Conventions for the
original convention list) and re-stated here for the architecture
readers:

- **Ruff clean at every phase boundary** — `ruff check src/doc3gpp
  tests` before merging.
- **No `as any` / `# type: ignore`** — use typed code paths instead.
- **Schema bootstrap is create-all, not versioned migrations.** `db init`
  is the intended schema boundary for normal use, while `meeting sync`,
  `wi sync`, and `tsg seed` still call `create_schema()` idempotently for
  fresh-database ergonomics. `tdoc sync` and `tdoc parse` assume the schema
  already exists. Existing installs need `doc3gpp db reset --yes` (SQLite)
  or a backend-native migration/reset after ORM shape changes.
- **Protocol ↔ impl signature parity** — when changing a filter
  signature on any repo, update both the Protocol and the impl.
- **CLI depends on `services/factory.py` only** — never instantiate a
  concrete `SQLAlchemy*Repository` from `cli.py`.
- **Settings caching** — `get_settings` and `get_engine` are
  `@lru_cache(maxsize=1)`; any test that mutates `DOC3GPP_*` must call
  `cache_clear()` on both in teardown (the `sqlite_env` fixture is the
  canonical pattern).

## Out of scope (today)

The full list of open constraints — schema bootstrap policy, settings
caching, hardcoded FTP root, calendar-parser coupling, TDoc source
coverage, R5-/C6- URL-template status, `python-docx` opt-in, and the
test-surface limits — lives in
[`docs/known-constraints.md`](known-constraints.md). That file is the
single source of truth; update it in the same change set when a
constraint is lifted.

Out-of-scope features that have not been implemented yet:

- TDoc types other than CR (LS, DRAFT, BB, etc.).
- Workplan / spec status extraction.
- Alembic / versioned migrations (the schema bootstrap is
  `Base.metadata.create_all` via `db init`).
