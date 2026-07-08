# Implementation Status

This document tracks what is implemented today versus planned next work.

## Implemented

### Configuration and Environment

- Environment-driven settings via DOC3GPP_* variables.
- Defaults in src/doc3gpp/settings/schema.py.
- Settings loader with caching in src/doc3gpp/settings/loader.py.

### Database Backends

- sqlite default backend.
- mysql backend options.
- postgres backend options.
- Engine/session factory in src/doc3gpp/storage/db/session.py.

### ORM and Persistence

- tdocs table and SQL repository.
- tdoc_files table and SQL repository (auxiliary files: revisions, reviews, support docs).
- meetings table and SQL repository.
- Schema bootstrap with create_all.

### Scraping and Parsing

- ScraperClient for HTTP retrieval, with retry/backoff for transient errors.
- Calendar source adapter for DynaReport.
- Calendar parser:
  - meeting id extraction from MtgId link.
  - ftp path extraction from docs/files links.
  - doc range extraction start_doc/end_doc.
  - cancelled meeting filtering.
- TDoc parser:
  - exact-match header detection (rejects title-only rows).
  - retry-and-fallback lookup of TDoc list XLSX across `docs/` and `tdoc/` subfolders.
  - reservation/uploaded date parsing into `date` objects.
  - WARNING emitted when rows are skipped because the TDoc ID regex misses.
- TDoc CR extraction pipeline (zip download → on-disk cache →
  `python-docx` render → markdown cache → cover-page parser → persist):
  - URL builders for the `R5s` (TTCN) and `R5w` (Workshop) branches.
  - `python-docx` is an opt-in extra (`pip install doc3gpp[extract]`);
    the conversion step degrades with a clear `PythonDocxNotInstalledError`.
  - Tables: `tdoc_cr_details` (parsed cover-page fields) and
    `tdoc_extracts` (cache-pointer sidecar).

### Services

- MeetingService sync and list.
- TDocService save and list.
- TDocFileService: scans meeting FTP subfolders (`Inbox/`, `Docs/`,
  `Tdocs/`, `Review/`) and upserts auxiliary TDoc files
  (revisions, reviews, support docs) into `tdoc_files`.
- TDocSyncCoordinator: cross-service orchestration for `tdoc sync` —
  resolves the meeting, runs the TDoc sync, then the TDocFile sync
  with the freshly-persisted TDoc IDs.
- TsgService seed/list/validate for the 3GPP TSG reference table.
- Meeting-based TDoc sync now resolves FTP URL from stored meeting records.
- Service composition via `services.factory.build_*` helpers (CLI depends on
  Protocol-typed services, not on concrete SQLAlchemy repositories).

### CLI

- db check, db init (also seeds the `tsgs` reference table), db reset
  (destructive: wipe + recreate the SQLite file; refuses MySQL/Postgres).
- meeting sync (validates `--tsg` against the reference table).
- meeting list.
- tdoc sync, tdoc list.
- tdoc show (full TDoc + extracted CR cover-page fields).
- tdoc extract (download zip → cache → python-docx → parse → persist).
- cache status, cache purge (on-disk cache footprint for the extraction pipeline).
- tsg list, tsg show, tsg seed.
- Logging is configured via `DOC3GPP_LOG_LEVEL` and available at runtime for debugging.

### Tests

- Unit: settings defaults, calendar parser fixture.
- Integration: sqlite connectivity, meeting service persistence.
- Optional integration: mysql connectivity via DOC3GPP_TEST_MYSQL_URL.
- End-to-end extraction coverage:
  - `tests/integration/test_tdoc_cr_sqlite.py` exercises
    `TDocCrService` end-to-end against sqlite with a mocked
    `ScraperClient` and the 7 zip fixtures under
    `tests/fixtures/tdoc_cr_doc/`; the final test
    (`test_extract_end_to_end_via_cli_runner`) drives the production
    `tdoc extract` + `tdoc show` CLI via Typer's `CliRunner`.
  - `tests/integration/test_online_tdoc_extract.py` (opt-in,
    `-m online`) hits the live 3GPP FTP for `R5s260009` and
    `R5w260009` to surface URL-template rot.
- Pytest markers are defined for profile-based execution:
  - online
  - mysql
- SQLite-only command profile:
  - python -m pytest -q --cov=src/doc3gpp --cov-report=term-missing -m "not mysql and not online"

## Planned / Not Yet Implemented

### Additional Data Sources

- Workplan extraction.
- Spec status extraction.

### Migration Tooling

- Full Alembic workflow and versioned migrations.

### Operational Hardening

- Optional throttling and robots policy enforcement.
- Structured sync logs and metrics.

## Current Known Constraints

### Correctness / operational

- Schema bootstrap uses `create_all` rather than versioned migrations.
  Existing deployments must drop and recreate `tdoc_cr_details` /
  `tdoc_extracts` after the URL-PK change (`doc3gpp db init` will
  recreate them; previous rows are not preserved because no Alembic
  migration exists yet). For a non-destructive migration, run:

  ```sql
  ALTER TABLE tdoc_cr_details RENAME TO tdoc_cr_details_old;
  ALTER TABLE tdoc_extracts  RENAME TO tdoc_extracts_old;
  -- then re-run `doc3gpp db init` to create the new schema
  -- then ``INSERT INTO new SELECT * FROM old`` per row mapping
  -- the new PK (the immutable URL from the prior row's `url`
  -- column on `tdoc_cr_details`; `tdoc_extracts` was URL-free so
  -- the only available identity is `tdoc_id` + a synthetic suffix).
  ```
- Calendar parser depends on current DynaReport table structure; the
  `M5` log-warning surface catches pages that no longer carry a
  `<table class="meetings">` but a layout-only change may still break
  field extraction silently.
- TDoc CR extraction covers the `R5s` (TTCN) and `R5w` (Workshop) URL
  templates verified against offline fixtures; the `R5-` and `C6-`
  templates are deliberately unresolved until exercised against the
  live site.
- Full end-to-end online tests are not included in the default test
  suite; opt in with `python -m pytest -m online -rs` to exercise live
  3gpp.org + FTP code paths.
- `https://www.3gpp.org/ftp/` is hardcoded in
  `src/doc3gpp/scraping/ftp_source.py:72`; if 3GPP moves the assets to
  a CDN, `meeting sync` and `tdoc sync` silently return empty without
  a clear failure surface. *(carried from TODO #19.)*
- `ScraperClient.get_text` uses a broad `except Exception` block
  (`src/doc3gpp/scraping/client.py:35-37, 46-48`); programming errors
  (e.g. `httpx.InvalidURL`) look identical to network errors in logs.
  The retry path narrows to `httpx.HTTPError` but the text-fetch path
  has not been migrated. *(carried from TODO #20.)*
- `get_settings`' `@lru_cache` plus `ScraperClient.__init__` reading
  settings once means env changes mid-process do not propagate
  (e.g. flipping `DOC3GPP_HTTP_VERIFY` after a CLI constructed the
  client). Tests that mutate `DOC3GPP_*` must `cache_clear()` both
  `get_settings` and `get_engine` in teardown
  (`tests/conftest.py::sqlite_env` is the canonical pattern).
  *(carried from TODO #21.)*

### Code-style test coverage

The unit suite covers every concrete `SQLAlchemy*Repository`,
every service, every parser, every CLI subcommand, and the four
TDoc-CR pipeline components (cache, zip source, python-docx wrapper,
parser, ORM, service, CLI). Gaps to flag for future work:

- `TDocService.sync_from_meeting_ftp` end-to-end via CLI — covered by
  `tests/unit/test_tdoc_sync_cli.py` for the CLI selector paths, but a
  full FTP walk is integration-only
  (`tests/integration/test_tdoc_sqlite.py::test_cli_tdoc_sync_*`).
  Pre-existing failures in those tests come from upstream 3gpp.org
  DynaReport layout drift, not from the extraction pipeline.
