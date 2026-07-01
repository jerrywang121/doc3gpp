# Architecture

The project is implemented with a layered Python package under src/doc3gpp and supports both library usage and CLI usage.

Current scope includes:

- Configurable SQL backends (sqlite default, mysql, postgres).
- Calendar scraping from 3gpp DynaReport meetings pages.
- Calendar persistence in SQLAlchemy.
- Basic TDoc persistence and listing.

## Layers

- settings:
	- schema and loading for environment-driven config.
	- log level configuration via `DOC3GPP_LOG_LEVEL`.
	- modules: src/doc3gpp/settings/schema.py, src/doc3gpp/settings/loader.py.
- scraping:
	- HTTP retrieval from 3gpp.org.
	- modules: src/doc3gpp/scraping/client.py, src/doc3gpp/scraping/calendar_source.py.
- parsers:
	- HTML parsing and normalization.
	- modules: src/doc3gpp/parsers/calendar_parser.py, src/doc3gpp/parsers/html_parsers.py, src/doc3gpp/parsers/normalizers.py.
- models:
	- domain models for meeting, tdoc, and tsg records.
	- modules: src/doc3gpp/models/meeting.py, src/doc3gpp/models/tdoc.py, src/doc3gpp/models/tsg.py.
- repository:
	- protocol interfaces used by services.
	- module: src/doc3gpp/repository/protocols.py.
- services:
	- orchestration and sync use-cases.
	- modules: src/doc3gpp/services/meetings_service.py, src/doc3gpp/services/tdoc_service.py, src/doc3gpp/services/tsg_service.py.
- storage:
	- SQLAlchemy ORM models, session factory, backend-specific engine options, concrete repositories.
	- modules: src/doc3gpp/storage/db/models.py, src/doc3gpp/storage/db/session.py, src/doc3gpp/storage/backends/*.py, src/doc3gpp/storage/repositories/*.py.

## Runtime Data Flow

Meetings sync flow:

1. CLI command calls MeetingService.sync.
2. MeetingService uses scraping.calendar_source.fetch_calendar.
3. fetch_calendar loads HTML via ScraperClient.
4. parsers.calendar_parser.parse_3gpp_calendar converts HTML rows into Meeting domain objects.
5. SQLAlchemyMeetingRepository.upsert_many persists meetings into meetings table.

TDoc list flow:

1. CLI command calls TDocService.
2. TDocService delegates to SQLAlchemyTDocRepository.
3. Repository persists/reads rows in tdocs table.

Meeting-based TDoc sync flow:

1. `doc3gpp tdoc sync --meeting-id` loads meeting metadata from storage.
2. The meeting's stored FTP URL is used to discover the TDoc list XLSX file.
3. `fetch_tdocs_from_meeting_ftp` parses the XLSX file and returns TDoc records.
4. TDocService persists the discovered TDocs.

## Database Schema (Current)

Current tables defined in src/doc3gpp/storage/db/models.py:

- tdocs:
	- id, tdoc_id, title, meeting_id, url, source, type, status, reservation_date, uploaded_date, cr_cat, is_revision_of, revised_to, release, spec, version, related_wis, cr_num, cr_pack, created_at.
- meetings:
	- meeting_id, name, title, location, start_date, end_date, ftp_url, start_doc, end_doc, updated_at.
- tsgs:
	- id, tsg_name (unique), short_name (unique), description, url.
	- canonical 3GPP TSG list, seeded on `db init`; used to validate `--tsg` in `meetings sync`.

Schema creation currently uses Base.metadata.create_all through src/doc3gpp/storage/db/migrate.py.

## Backend Selection

The active backend is selected from DOC3GPP_DATABASE_URL.

- sqlite default: sqlite+pysqlite:///~/.local/share/doc3gpp/doc3gpp.db
- mysql example: mysql+pymysql://user:pass@localhost:3306/doc3gpp
- postgres example: postgresql+psycopg://user:pass@localhost:5432/doc3gpp

Backend-specific engine kwargs are applied in src/doc3gpp/storage/db/session.py via:

- src/doc3gpp/storage/backends/sqlite.py
- src/doc3gpp/storage/backends/mysql.py
- src/doc3gpp/storage/backends/postgres.py

## CLI Surface (Current)

Implemented command groups in src/doc3gpp/cli.py:

- db:
	- check
	- init (also seeds the `tsgs` reference table)
- meetings:
	- sync
	- list
- tdoc:
	- sync
	- add
	- list
- tsg:
	- list
	- show
	- seed

## Testing Layout

- unit:
	- tests/unit/test_settings.py
	- tests/unit/test_calendar_parser.py
- integration:
	- tests/integration/test_sqlite_backend.py
	- tests/integration/test_mysql_backend.py
	- tests/integration/test_calendar_service_sqlite.py

## Not Yet Implemented

The following are planned but not complete in current code:

- TDoc list sync from GenerateDocumentList.aspx.
- FTP tdoc_list discovery fallback logic.
- Workplan/spec extraction modules.
- Alembic migration pipeline beyond create_all bootstrap.

