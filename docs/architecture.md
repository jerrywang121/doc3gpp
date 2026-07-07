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
  `CacheSettings` sub-models.
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

### Meetings sync

1. `doc3gpp meeting sync --tsg <short>` validates `<short>` against
   the `tsgs` table (auto-seeded if empty).
2. `MeetingService.sync` → `fetch_calendar` (DynaReport HTML) →
   `parse_3gpp_calendar` (HTML → `Meeting` list) →
   `SQLAlchemyMeetingRepository.upsert_many`, then
   `delete_with_end_before(cutoff)` to trim out-of-window rows.

### TDoc list sync (per meeting)

1. `doc3gpp tdoc sync --meeting-id <id>` (or `--meeting <name>`)
   resolves the meeting and reads its stored `ftp_url`.
2. `TDocSyncCoordinator.sync_for_meeting_id` orchestrates:
    - `TDocService.sync_from_meeting_ftp` →
      `fetch_tdocs_from_meeting_ftp` →
      `read_tdoc_sheet` (XLSX → `TDoc` list) →
      `SQLAlchemyTDocRepository.upsert_many`.
    - `TDocFileService.sync_from_meeting_ftp` uses the freshly-persisted
      TDoc IDs as the prefix list to recognise attachments under
      `Inbox/`, `Docs/`, `Tdocs/`, `Review/`.
3. `SQLAlchemyTDocFileRepository.upsert_many` persists revision / review
   / support files keyed by the unique `ftp_url`.

### TDoc CR extraction

1. `doc3gpp tdoc extract --tdoc <id>` (or `--tdoc-id N`) resolves the
   id (via `TDocRepository.get_by_id`) and validates the row exists with
   `type == "CR"` (raises `TDocTypeUnsupportedError` for non-CR ids).
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
      `cr_pack`, `created_at`, `updated_at`.
- `tdoc_files`:
    - `id` (PK), `tdoc_id` (FK → `tdocs.tdoc_id`, no cascade),
      `type` (`revision` / `review` / `support`), `file`, `ftp_url`
      (unique, the upsert key; stored as a path relative to the
      canonical 3GPP FTP root), `created_at`, `updated_at`.
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
      `extracted_tdoc_id`), `parser_version`, `extracted_at`,
      `updated_at`. Identity is the URL because 3GPP assets are
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
      `end_date`, `ftp_url`, `start_doc`, `end_doc`, `updated_at`.
- `tsgs`:
    - `short_name` (PK), `tsg_name` (unique), `description`, `url`.
      Seeded on `db init`; validates `--tsg` in `meeting sync`.
- `wis`:
    - `(wi_id, tsg_short)` composite PK, `acronym`, `release`, `name`,
      `updated_at`. `tsg_short` FK → `tsgs.short_name`; composite PK
      keeps the natural identifier stable across multi-TSG ownership.

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
seventeen commands):

- `db`:
    - `check`
    - `init` — also seeds the `tsgs` reference table
- `meeting`:
    - `sync` — validates `--tsg` against the reference table
    - `list` — filters by `--tsg`, `--meeting`, `--location`,
      `--start-date`, `--end-date`, `--limit`, `--offset`; auto-wraps
      like patterns
- `tdoc`:
    - `sync` — `--meeting-id` or `--meeting`; delegates to
      `TDocSyncCoordinator`
    - `list` — filters by `--tsg`, `--meeting`, `--year`, `--source`,
      `--spec`, `--wi`, `--title`, `--cat`, `--status`, `--type`
    - `extract` — `--tdoc` / `--tdoc-id` (repeatable), `--force`,
      `--full`; batch extraction with per-id failure isolation
    - `show` — `--tdoc`; renders the matching TDoc and, when present,
      a `[Extracted Details]` block from `tdoc_cr_details`
- `tsg`:
    - `list`, `show`, `seed`
- `wi`:
    - `sync` — `--tsg`
    - `list` — filters by `--tsg`, `--release`
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
and `-o/--output PATH`.

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

- `tests/unit/` — 53 files, 433 tests. Pure-Python unit tests; mock
  external calls. Coverage is concentrated in:
    - parser fixtures (`test_calendar_parser.py`,
      `test_cr_parser.py`, `test_tdoc_parser.py`,
      `test_tdoc_file_parser.py`, `test_wi_parser.py`,
      `test_docx_converter.py`)
    - scraping + cache contracts (`test_tdoc_cache.py`,
      `test_tdoc_zip_source.py`, `test_ftp_source.py`,
      `test_scraper_client.py`)
    - repositories (CRUD + filter combinations; for each concrete
      `SQLAlchemy*Repository`)
    - services (`test_meetings_service_sync.py`,
      `test_tdoc_service_sync.py`, `test_tdoc_sync_coordinator.py`)
    - CLI (`test_meeting_cli*`, `test_tdoc_cli_fields.py`,
      `test_tdoc_sync_cli.py`, `test_tdoc_extract_cli.py`,
      `test_cache_cli.py`, `test_wi_cli.py`, `test_tsg_cli.py`)
- `tests/integration/` — sqlite-only by default; online + mysql
  opt-in. 10 files, 53 tests:
    - `test_sqlite_backend.py`, `test_sdk_integration.py`
    - `test_meeting_service_sqlite.py`,
      `test_tdoc_sqlite.py`, `test_tdoc_file_sqlite.py`,
      `test_tdoc_cr_sqlite.py` (12 tests + 1 e2e Typer CLI test),
      `test_tsg_sqlite.py`, `test_wi_sqlite.py`
    - `test_online_3gpp_calendar.py`,
      `test_online_tdoc_extract.py` (live `R5s260009` /
      `R5w260009`, `@pytest.mark.online`)
    - `test_mysql_backend.py` (gated on
      `DOC3GPP_TEST_MYSQL_URL`)
- `tests/fixtures/tdoc_cr_doc/` — 7 CR zip fixtures
  (`C6-250028.zip`, `R5-227476.zip`, `R5-253079.zip`,
  `R5s260009.zip`, `R5s260051.zip`, `R5s260135.zip`,
  `R5s260176.zip`). Regression corpus for `cr_parser` and
  `tdoc_cr_service`.
- Pytest markers: `online`, `mysql`. The default profile is
  `pytest -m "not mysql and not online"`; `./scripts/test_sqlite.sh`
  is the canonical wrapper.

## Cross-cutting design rules

These are enforced by code review (see `AGENTS.md` §Conventions for the
original convention list) and re-stated here for the architecture
readers:

- **Ruff clean at every phase boundary** — `ruff check src/doc3gpp
  tests` before merging.
- **No `as any` / `# type: ignore`** — use typed code paths instead.
- **`db init` is the single schema boundary** — services never call
  `create_schema()`; if a table is missing, the SQL repo raises
  `OperationalError` and the CLI translates to a friendly
  `typer.BadParameter` ("run `doc3gpp db init` first").
- **Protocol ↔ impl signature parity** — when changing a filter
  signature on any repo, update both the Protocol and the impl.
- **CLI depends on `services/factory.py` only** — never instantiate a
  concrete `SQLAlchemy*Repository` from `cli.py`.
- **Settings caching** — `get_settings` and `get_engine` are
  `@lru_cache(maxsize=1)`; any test that mutates `DOC3GPP_*` must call
  `cache_clear()` on both in teardown (the `sqlite_env` fixture is the
  canonical pattern).

## Open issues carried over

These are actively tracked; see `docs/implementation-status.md`
§Current Known Constraints for the full list and severity. Summary
of the open items:

- `ScraperClient.get_text` uses a broad `except Exception`; programming
  errors look identical to network errors in logs.
- `get_settings`' `@lru_cache` plus `ScraperClient.__init__` reading
  settings once means env changes mid-process don't propagate.
- `https://www.3gpp.org/ftp/` is hardcoded; if 3GPP moves the assets
  to a CDN, scraping silently returns empty for `meeting sync`.
- `R5-` and `C6-` URL templates return `None` until exercised against
  the live site (only `R5s` and `R5w` are locked in).

## Out of scope (today)

- TDoc types other than CR (LS, DRAFT, BB, etc.).
- Workplan / spec status extraction.
- Alembic / versioned migrations (the schema bootstrap is
  `Base.metadata.create_all` via `db init`).
