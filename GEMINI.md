# doc3gpp Project Instructions

This project is a Python library and CLI tool designed to scrape 3GPP TDoc information from the 3GPP website (3gpp.org). It supports multiple database backends (SQLite, MySQL, PostgreSQL) and uses a layered architecture.

## Project Overview

- **Purpose:** Automate the extraction and storage of 3GPP meeting calendar and TDoc metadata.
- **Tech Stack:**
  - Python 3.10+
  - **Storage:** SQLAlchemy 2.0, Alembic (migrations pending full implementation), SQLite (default), MySQL, PostgreSQL.
  - **Parsing:** BeautifulSoup4, LXML, openpyxl (for Excel reports).
  - **API/Network:** HTTPX (async-ready client).
  - **CLI:** Typer.
  - **Validation:** Pydantic 2.8.
  - **Testing:** Pytest.

## Architecture Layers

The project follows a "library-first" approach under `src/doc3gpp`:

- `models/`: Domain objects (e.g., `Meeting`, `TDoc`).
- `settings/`: Configuration management using `pydantic-settings`.
- `scraping/`: Network-level retrieval from 3GPP servers.
- `parsers/`: Logic to convert HTML and Excel data into domain models.
- `services/`: Business logic orchestration (e.g., syncing meetings).
- `storage/`: Concrete implementations of persistence using SQLAlchemy.
  - `backends/`: Engine configurations for SQLite, MySQL, and Postgres.
  - `db/`: ORM models and session management.
  - `repositories/`: SQL implementations of storage protocols.
- `cli.py`: CLI entry point and command definitions.

## Getting Started

### Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

### Database Initialization

```bash
doc3gpp db init
doc3gpp db check
```

## Building and Running

### Common CLI Commands

- **Sync Meetings:** `doc3gpp meeting sync --tsg r5`
- **List Meetings:** `doc3gpp meeting list --limit 20`
- **Sync TDocs:** `doc3gpp tdoc sync --meeting-id <id>` or `doc3gpp tdoc sync --meeting "Meeting Name"`
- **List TDocs:** `doc3gpp tdoc list --tsg R5 --year 26`

### Testing

Run all tests:
```bash
pytest
```

Run only SQLite tests (skipping MySQL and online tests):
```bash
./scripts/test_sqlite.sh
```

Run online tests:
```bash
python -m pytest -m online
```

### Linting

```bash
ruff check .
```

## Development Conventions

- **Layered Responsibility:** Keep parsing logic in `parsers/`, network logic in `scraping/`, and persistence in `storage/`.
- **Domain Models:** Always use domain objects (from `models/`) when passing data between layers.
- **Environment Variables:** All configuration should be accessible via `DOC3GPP_*` environment variables (managed in `src/doc3gpp/settings/`).
- **Testing:** New features should include both unit tests (mocking external calls) and integration tests (against SQLite).
- **Type Hinting:** Use static type hints throughout the codebase.

## Key Files

- `pyproject.toml`: Build system and dependency definitions.
- `src/doc3gpp/cli.py`: Main CLI implementation.
- `src/doc3gpp/storage/db/models.py`: SQLAlchemy ORM definitions.
- `docs/architecture.md`: Detailed architectural overview.
- `docs/implementation-status.md`: Current progress and roadmap.

## Implementation Status Notes

- Schema creation currently uses `Base.metadata.create_all`. Full Alembic migrations are planned but not yet the primary way to manage schema changes.
- The `meetings` sync depends on the structure of the 3GPP DynaReport pages.
- TDoc extraction currently focuses on FTP-based Excel files; `GenerateDocumentList.aspx` support is planned.
