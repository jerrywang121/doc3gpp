# doc3gpp

Extract 3GPP TDoc information by scraping 3gpp.org.

This project is scaffolded as both a Python library and a CLI, with configurable SQL backends.
Default storage uses local SQLite, while MySQL and PostgreSQL are available via configuration.

## Features

- Library-first package layout (`src/` based).
- CLI entrypoint: `doc3gpp`.
- Database backend selection from config.
- Default local SQLite database.
- Optional MySQL and PostgreSQL support.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
doc3gpp db init
doc3gpp db check
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
doc3gpp meetings sync --tsg r5
doc3gpp meetings list --limit 20
doc3gpp tdoc sync --meeting-id 85434
doc3gpp tdoc sync --meeting "R5--TTCN Workshop#74"
doc3gpp tdoc list --limit 10
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
