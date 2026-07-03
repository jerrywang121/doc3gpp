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
# default sqlite (omit DOC3GPP_DATABASE_URL to use the pydantic default,
# which resolves to ~/.local/share/doc3gpp/doc3gpp.db)
DOC3GPP_DATABASE_URL=sqlite+pysqlite:////absolute/path/to/doc3gpp.db

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

## Configuration File (TOML)

For structured settings — DB URL plus fetch knobs and per-command output
defaults — drop a TOML file at one of these locations (first hit wins):

1. The path named by `DOC3GPP_CONFIG` (file or directory; absolute or
   relative).
2. `./doc3gpp.toml` (project-local — check into git for team defaults).
3. `~/.config/doc3gpp/config.toml` (user-wide; honors `$XDG_CONFIG_HOME`).

See [`doc3gpp.toml.example`](./doc3gpp.toml.example) for the full
schema. Highlights:

```toml
[meeting_sync]
closed_years = 5        # default for `doc3gpp meeting sync --closed-years`
future_years = 2        # default for `doc3gpp meeting sync --future-years`

[output]
format = "json"         # default for every `* list --format`

[output.fields]
meeting = ["meeting_id", "name", "end_date"]  # default columns
tdoc    = ["tdoc_id", "meeting_name", "title", "spec"]
wi      = ["wi_id", "name"]
```

Precedence (highest wins): **CLI flag > environment variable > config
file > built-in default**. Inspect what's in effect with:

```bash
doc3gpp config path   # which file is being read
doc3gpp config show   # the fully-resolved settings, as JSON
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

# Every `* list` command also accepts `--format table|json|markdown`
# and `-o/--output PATH` to redirect the result to a file:
doc3gpp tdoc list --format json -o tdocs.json
doc3gpp meeting list --format markdown -o meetings.md
doc3gpp tsg list --format json
doc3gpp wi list --format markdown
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
