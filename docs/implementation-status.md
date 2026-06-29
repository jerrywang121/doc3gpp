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

- ScraperClient for HTTP retrieval.
- Calendar source adapter for DynaReport.
- Calendar parser:
  - meeting id extraction from MtgId link.
  - ftp path extraction from docs/files links.
  - doc range extraction start_doc/end_doc.
  - cancelled meeting filtering.

### Services

- MeetingService sync and list.
- TDocService save and list.

### CLI

- db check, db init.
- meetings sync, meetings list.
- tdoc add, tdoc list.

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
- Fallback lookup of tdoc_list files in ftp subfolders.
- Persist expanded tdoc metadata columns from legacy implementation.

### Additional Data Sources

- Workplan extraction.
- Spec status extraction.

### Migration Tooling

- Full Alembic workflow and versioned migrations.

### Operational Hardening

- Retry/backoff policy in HTTP client.
- Optional throttling and robots policy enforcement.
- Structured sync logs and metrics.

## Current Known Constraints

- Schema bootstrap uses create_all rather than versioned migrations.
- Calendar parser depends on current DynaReport table structure.
- Full end-to-end online tests are not included in default test suite.
