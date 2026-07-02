# doc3gpp

Extract 3GPP TDoc information by scraping 3gpp.org.

Default storage uses local SQLite, while MySQL and PostgreSQL are available via configuration.

## Installation

### SDK (library)

```bash
pip install doc3gpp
```

Use the SDK to access 3GPP data programmatically:

```python
from doc3gpp.settings import get_settings
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository

service = MeetingService(SQLAlchemyMeetingRepository())
meetings = service.list_recent(limit=10)
```

### CLI (command-line tool)

```bash
pip install "doc3gpp[cli]"
pipx install "doc3gpp[cli]"
```

The `[cli]` extra adds the `doc3gpp` CLI command with Typer-based subcommands.

### Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
doc3gpp db init
doc3gpp db check
```

The `[dev]` extra includes both `[cli]` and test/lint tooling.

### Database Backends

```bash
pip install "doc3gpp[mysql]"     # MySQL (pymysql)
pip install "doc3gpp[postgres]"  # PostgreSQL (psycopg)
```

## Database Configuration

Configuration is read from environment variables and `.env`.

- `DOC3GPP_DATABASE_URL`
- `DOC3GPP_DB_ECHO`
- `DOC3GPP_DB_POOL_SIZE`
- `DOC3GPP_DB_AUTO_MIGRATE`
- `DOC3GPP_LOG_LEVEL`

Examples:

```bash
# default sqlite
DOC3GPP_DATABASE_URL=sqlite+pysqlite:///~/.local/share/doc3gpp/doc3gpp.db

# mysql
DOC3GPP_DATABASE_URL=mysql+pymysql://user:pass@localhost:3306/doc3gpp

# postgresql
DOC3GPP_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/doc3gpp
```

Install backend drivers as needed:

```bash
pip install -e ".[mysql]"
pip install -e ".[postgres]"
```

## CLI Usage

```bash
doc3gpp db init
doc3gpp db check
doc3gpp tsg list                # show the canonical 3GPP TSG reference table
doc3gpp tsg show --tsg r5       # show a single TSG record
doc3gpp tsg seed                # re-seed the TSG reference table
doc3gpp meeting sync --tsg r5  # --tsg is validated against the tsg table
doc3gpp meeting list --limit 20
doc3gpp tdoc sync --meeting-id 85434
doc3gpp tdoc sync --meeting "R5--TTCN Workshop#74"
doc3gpp tdoc list --limit 10
# Filtered list examples
doc3gpp tdoc list --tsg R5 --year 26 --meeting "%RAN3%"
doc3gpp wi sync --tsg r5                       # scrape the WI DynaReport page for R5
doc3gpp wi list --limit 10                     # default fields: wi_id, acronym, release, name
doc3gpp wi list --tsg R5 --release "Rel-19" --limit 100
```

## Testing

```bash
pytest
```

SQLite-only profile (excludes mysql and online tests):

```bash
python -m pytest -q --cov=src/doc3gpp --cov-report=term-missing -m "not mysql and not online"
```

Equivalent helper script:

```bash
./scripts/test_sqlite.sh
```

Run online tests explicitly:

```bash
python -m pytest -q -m online -rs
```

## Documentation

- Architecture: docs/architecture.md
- CLI reference: docs/cli.md
- Implementation status: docs/implementation-status.md
