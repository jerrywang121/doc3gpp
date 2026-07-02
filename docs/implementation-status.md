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

### Services

- MeetingService sync and list.
- TDocService save and list.
- TDocSyncCoordinator: cross-service orchestration for `tdoc sync`.
- TsgService seed/list/validate for the 3GPP TSG reference table.
- Meeting-based TDoc sync now resolves FTP URL from stored meeting records.
- Service composition via `services.factory.build_*` helpers (CLI depends on
  Protocol-typed services, not on concrete SQLAlchemy repositories).

### CLI

- db check, db init (also seeds the `tsgs` reference table).
- meetings sync (validates `--tsg` against the reference table).
- meetings list.
- tdoc sync, tdoc list.
- tsg list, tsg show, tsg seed.
- Logging is configured via `DOC3GPP_LOG_LEVEL` and available at runtime for debugging.

### Tests

- Unit: settings defaults, calendar parser fixture.
- Integration: sqlite connectivity, meeting service persistence.
- Optional integration: mysql connectivity via DOC3GPP_TEST_MYSQL_URL.
- Pytest markers are defined for profile-based execution:
  - online
  - mysql
- SQLite-only command profile:
  - python -m pytest -q --cov=src/doc3gpp --cov-report=term-missing -m "not mysql and not online"

## Planned / Not Yet Implemented

### TDoc Extraction Pipeline

- Download and parse meeting tdoc list from GenerateDocumentList.aspx.
- Persist expanded tdoc metadata columns from legacy implementation.

### Additional Data Sources

- Workplan extraction.
- Spec status extraction.

### Migration Tooling

- Full Alembic workflow and versioned migrations.

### Operational Hardening

- Optional throttling and robots policy enforcement.
- Structured sync logs and metrics.

## Current Known Constraints

- Schema bootstrap uses create_all rather than versioned migrations.
- Calendar parser depends on current DynaReport table structure.
- Full end-to-end online tests are not included in default test suite.
