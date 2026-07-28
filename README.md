# doc3gpp

> Extract 3GPP TDoc information by scraping 3gpp.org — a Python CLI and library with pluggable SQL backends.

[![License: MIT](https://img.shields.io/github/license/jerrywang121/doc3gpp)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![PyPI](https://img.shields.io/pypi/v/doc3gpp)](https://pypi.org/project/doc3gpp/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000)](https://docs.astral.sh/ruff/)

## Description

`doc3gpp` scrapes 3GPP meeting calendars, work items (WIs), and TDocs from
[3gpp.org](https://www.3gpp.org) and persists them to a relational database
for programmatic access. It ships as both a Python library (SDK) and a
Typer-based CLI (`doc3gpp`), with SQLite as the default store and MySQL /
PostgreSQL available via configuration.

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Database Configuration](#database-configuration)
- [Configuration File (TOML)](#configuration-file-toml)
- [CLI Usage](#cli-usage)
- [Architecture](#architecture)
- [Testing](#testing)
- [Roadmap](#roadmap)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)
- [Acknowledgments](#acknowledgments)
- [Support](#support)

## Features

- **Meeting sync** — fetch the 3GPP DynaReport calendar (`meetings` table) and
  persist it to your store of choice. The `--tsg` flag is stamped onto every
  row as a foreign key into `tsgs.short_name`, powering the `meeting list
  --tsg` filter.
- **TDoc sync** — download a meeting's TDoc-list XLSX from the 3GPP portal
  (`GenerateDocumentList.aspx?meetingId={meeting_id}`) and persist the rows.
  Auxiliary TDoc files are still scanned from the meeting's FTP folders.
- **TDoc CR extraction** — optional `python-docx` pipeline that downloads,
  caches, and parses CR cover pages into structured records. The slim
  `tdoc_cr_cover_page` table holds cover-page fields only; the new
  `tdoc_cr_ttcn_details` sidecar persists the six TTCN overview fields
  (`testcase`, `ue`, `ss`, `ats_version`, `ttcn_release`, `test_suite`)
  plus a gzip-compressed `required_changes` JSON blob and a
  newline-delimited, SQL-searchable `changed_functions` aggregate
  (sorted + deduped `<module>.<function>` entries auto-derived from
  `required_changes`). When only the module basename is recoverable the
  entry is recorded as `'<module>.'` (trailing-dot sentinel); when only
  the function name is recoverable it is recorded as `'.<function>'`
  (leading-dot sentinel); when neither is recoverable the correction
  is dropped. Cache artefacts live in `tdoc_extracts`. `tdoc
  show` automatically appends a TTCN section (`[TTCN Details]` in table,
  `## TTCN Details` in markdown, a `ttcn` key in JSON) when the TDoc is
  a TTCN CR, and an auxiliary files section (`[Auxiliary Files]` / `##
  Auxiliary Files` / `files` key) listing every `tdoc_files` row whose
  `tdoc_id` matches.
- **Work Items (WIs)** — scrape the DynaReport WI list per TSG and list with
  SQL `LIKE` filters (`--tsg`, `--release`, `--acronym`).
- **TSG reference data** — seeded with the canonical 19 3GPP TSGs and used to
  validate `--tsg` flags across `meeting sync` and `wi sync`.
- **Multi-backend storage** — SQLite (default), MySQL, and PostgreSQL via
  SQLAlchemy 2.0.
- **Layered architecture** — strict separation between `scraping/`,
  `parsers/`, `services/`, `repository/`, and `storage/`.

## Installation

### SDK (library)

```bash
pip install doc3gpp
```

Use the SDK to access 3GPP data programmatically:

```python
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository

service = MeetingService(SQLAlchemyMeetingRepository())
meetings = service.list_recent(limit=10)
```

### CLI (command-line tool)

```bash
pip install "doc3gpp[cli]"
# or, for an isolated install:
pipx install "doc3gpp[cli]"
```

The `[cli]` extra adds the `doc3gpp` command (Typer-based subcommands).

### Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
doc3gpp db init
doc3gpp db check
```

The `[dev]` extra includes `[cli]`, `pytest`, `pytest-cov`, and `ruff`.

### Optional extras

```bash
pip install "doc3gpp[mysql]"      # MySQL driver (pymysql)
pip install "doc3gpp[postgres]"   # PostgreSQL driver (psycopg)
pip install "doc3gpp[extract]"    # TDoc CR extraction (python-docx)
```

## Quick Start

### SDK

```python
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.tdoc_service import TDocService
from doc3gpp.services.wi_service import WiService
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
from doc3gpp.storage.repositories.wi_sql import SQLAlchemyWiRepository

meetings = MeetingService(SQLAlchemyMeetingRepository())
tdocs = TDocService(SQLAlchemyTDocRepository())
wis = WiService(SQLAlchemyWiRepository())

recent = meetings.list_recent(limit=5)
for m in recent:
    print(m.meeting_id, m.name, m.end_date)
```

### CLI

```bash
doc3gpp db init                            # create schema + seed tsgs table
doc3gpp meeting sync --tsg r5              # scrape DynaReport, validate --tsg
doc3gpp meeting list --limit 5
doc3gpp meeting list --tdoc R5-260013       # find the meeting containing a TDoc
doc3gpp tdoc sync --meeting-id 85434       # requires a stored meeting row
doc3gpp tdoc sync                          # sync every tracked meeting_id in tdocs
doc3gpp tdoc list --tdoc 'R5%'
doc3gpp tdoc parse --meeting-id 85434      # extract CR cover pages; prompts before batch
doc3gpp tdoc parse --tdoc 'R5s26%' --yes   # pattern match, skip confirmation
doc3gpp wi sync --tsg r5                   # scrape WI DynaReport for R5
doc3gpp wi list --release "Rel-19" --limit 50
```

## Database Configuration

Configuration is read from a closed allowlist of environment variables
(see [`ALLOWED_ENV_VARS`](src/doc3gpp/settings/schema.py) for the
canonical list), the `.env` file (only the allowlisted vars are
honoured), and the TOML config file (everything else).

| Variable | Purpose |
| --- | --- |
| `DOC3GPP_DATABASE_URL` | SQLAlchemy URL (omit for default SQLite) |
| `DOC3GPP_DB_ECHO` | Echo SQL to stdout |
| `DOC3GPP_LOG_LEVEL` | Library log level |
| `DOC3GPP_HTTP_VERIFY` | TLS verification toggle |
| `DOC3GPP_CACHE__DIR` | TDoc extraction cache root |
| `DOC3GPP_SYNC__AUTO_SYNC` | When true, `meeting list` / `tdoc list` / `tdoc show` / DB-mode `tdoc parse` internally trigger the same sync paths used by explicit `meeting sync` / `tdoc sync` |

Plus the bootstrap var `DOC3GPP_CONFIG` (path to a TOML config file
or directory) — see the TOML section below. Any other `DOC3GPP_*`
env var is silently ignored; configure those values via TOML instead.

The remaining settings (`cache.size_limit_mb`, `cache.purge_confirm`,
`tdoc_parse.max_batch`, `tdoc_parse.max_ftp_depth`, `sync.*`,
`output.*`, `db_pool_size`, `db_auto_migrate`, `http_max_retries`,
`http_retry_backoff`, …) are TOML-only — see the example file.

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

## Configuration File (TOML)

For structured settings — DB URL plus fetch knobs and per-command output
defaults — drop a TOML file at one of these locations (first hit wins):

1. The path named by `DOC3GPP_CONFIG` (file or directory; absolute or
   relative). `DOC3GPP_CONFIG` is independent of the
   [`ALLOWED_ENV_VARS`](src/doc3gpp/settings/schema.py) allowlist and
   is the canonical way to pin a config file location from the shell.
2. `./doc3gpp.toml` (project-local — check into git for team defaults).
3. `~/.config/doc3gpp/config.toml` (user-wide; honors `$XDG_CONFIG_HOME`).

See [`doc3gpp.toml.example`](./doc3gpp.toml.example) for the full schema.
Highlights:

```toml
[output]
format = "json"         # default for every `* list --format`

[output.fields]
meeting = [
  "meeting_id", "name", "location", "start_date",
  "end_date", "ftp_url", "start_doc", "end_doc",
]
tdoc = [
  "tdoc_id", "meeting_name", "title", "source", "type",
  "status", "cr_cat", "spec", "version", "related_wis",
]
tsg = ["tsg_name", "short_name", "description"]
wi  = ["wi_id", "acronym", "release", "name"]

[cache]
dir = "~/.cache/doc3gpp/tdocs"
size_limit_mb = 1024
purge_confirm = true

[tdoc_parse]
max_batch = 100
max_ftp_depth = 2
```

Precedence (highest wins): **CLI flag > environment variable > config file >
built-in default**. Inspect what's in effect with:

```bash
doc3gpp config path   # which file is being read
doc3gpp config show   # the fully-resolved settings, as JSON
```

Edit values without hand-editing the TOML:

```bash
doc3gpp config init                       # bootstrap a config file with full defaults
doc3gpp config set sync.auto_sync true    # then edit individual keys
```

## CLI Usage

The CLI ships seven sub-apps and twenty commands. The most common
entry points are `meeting sync` (DynaReport calendar), `tdoc sync`
(TDoc-list XLSX + auxiliary file scan), `tdoc parse` (extract CR cover
pages), and `tdoc show --format raw` (render the converted `.docx`
markdown).

### `db` — database lifecycle

```bash
doc3gpp db init                # create schema + seed tsgs table
doc3gpp db check               # verify connectivity
doc3gpp db reset --yes         # destructive: wipe + recreate SQLite schema
```

### `tsg` — 3GPP TSG reference

```bash
doc3gpp tsg list               # show the canonical 3GPP TSG list
doc3gpp tsg show --tsg r5      # show a single TSG record
doc3gpp tsg seed               # re-seed the reference table
```

### `meeting` — 3GPP meeting calendar

```bash
doc3gpp meeting sync --tsg r5              # scrape DynaReport; --tsg validated against tsgs
doc3gpp meeting list --limit 20
doc3gpp meeting list --tdoc R5-260013      # find the meeting whose start_doc/end_doc brackets a TDoc
```

### `tdoc` — list, parse, show

```bash
# sync — every tracked meeting_id or one specific meeting
doc3gpp tdoc sync                                # sync every distinct meeting_id in tdocs
doc3gpp tdoc sync --meeting-id 85434
doc3gpp tdoc sync --meeting "R5--TTCN Workshop#74"

# list — 18 filter flags combine freely
doc3gpp tdoc list --limit 10
doc3gpp tdoc list --tdoc 'R5%'                   # LIKE pattern on tdoc_id
doc3gpp tdoc list --meeting-id 85434 --cr-cat F
doc3gpp tdoc list --tdoc 'R5%' --meeting "%RAN3%"
doc3gpp tdoc list --title '!%Sidelink%'          # NOT LIKE

# parse (DB mode) — every flag is a filter
doc3gpp tdoc parse --meeting-id 85434            # CR-type only; prompts before batch (pending only)
doc3gpp tdoc parse --tdoc 'R5s26%' --yes         # LIKE pattern; non-interactive
doc3gpp tdoc parse --meeting-id 85434 --meeting '%RAN5%' --cr-cat F
doc3gpp tdoc parse --meeting-id 85434 --release 'Rel-19' --cr-num not-null
doc3gpp tdoc parse --meeting-id 85434 --force    # re-extract everything (includes already-parsed)

# parse (direct mode) — bypasses DB filters
doc3gpp tdoc parse --from-path ~/Downloads/R5s260009.docx                # local .docx → stdout
doc3gpp tdoc parse --from-url https://www.3gpp.org/ftp/.../R5s260009.zip # 3GPP URL → cache + DB
doc3gpp tdoc parse --from-url https://example.com/some.zip --format json -o /tmp/out.json  # non-3GPP URL → in-memory only
doc3gpp tdoc parse --from-path ./tdocs --output ./parsed --recursive --format json         # local batch
doc3gpp tdoc parse --from-url https://www.3gpp.org/ftp/.../Docs/ --recursive --output ./parsed  # online batch

# show — --tdoc and --ftp-url are mutually exclusive
doc3gpp tdoc show --tdoc R5s260009 --format json -o r5s260009.json
doc3gpp tdoc show --tdoc R5s260009 --format raw  -o r5s260009.md    # converted .docx markdown
doc3gpp tdoc show --ftp-url tsg_ran/WG5/.../R5s260009.zip            # URL-keyed lookup
doc3gpp tdoc show --ftp-url https://www.3gpp.org/ftp/.../R5s260009.zip --format raw
```

### `wi` — Work items

```bash
doc3gpp wi sync --tsg r5                       # scrape the WI DynaReport page for R5
doc3gpp wi list --limit 10                     # default fields: wi_id, acronym, release, name
doc3gpp wi list --tsg R5 --release "Rel-19" --limit 100
```

### `config` — TOML config lifecycle

```bash
doc3gpp config init                              # bootstrap a TOML config file with full defaults
doc3gpp config path                              # which file is in effect (or "(no config file found)")
doc3gpp config show                              # fully-resolved Settings as JSON for diffing
doc3gpp config set sync.auto_sync true           # write one setting into the active TOML config
doc3gpp config set sync.auto_sync true --dry-run # preview the resulting TOML without writing
```

### `cache` — Local extraction cache

```bash
doc3gpp cache status                  # file count, total bytes, limit, per-subdir breakdown
doc3gpp cache purge --yes             # delete cached markdown sidecars (default scope)
doc3gpp cache purge --scope zips --yes # only the 3GPP-served zip blobs
doc3gpp cache purge --scope all --yes  # both subtrees
```

### Common output options

Every `* list` command accepts `--format {table,json,markdown}` and
`-o/--output PATH`. `meeting list`, `tdoc list`, and `tsg list`
additionally accept `--fields` to override the configured column set
(`wi list` uses the configured `output.fields.wi` list):

```bash
doc3gpp tdoc list --format json -o tdocs.json
doc3gpp meeting list --format markdown -o meetings.md
doc3gpp tsg list --format json
doc3gpp wi list --format markdown
```

`tdoc show` accepts the same `--format` + `-o/--output` pair plus
`--format raw` for the converted `.docx` markdown body. The direct-mode
`tdoc parse --from-path` / `--from-url` also accepts `--format raw` for
local-batch use.

- `--compact` — strip output formatting. JSON drops indent and operator
  space (single line, `separators=(",", ":")`); Markdown drops CommonMark
  decorators (bold, italic, headings, bullets, GFM tables, code fences)
  and emits `key: value` lines with blank-line section separators. No-op
  for `table` and `raw`. Default: `false`; opt in globally with
  `[output] compact = true` in `doc3gpp.toml`.

Full command reference: [`docs/cli.md`](docs/cli.md).

## Architecture

The codebase is split into strict layers to keep concerns separate:

| Layer       | Path                       | Responsibility                       |
| ----------- | -------------------------- | ------------------------------------ |
| `models/`   | domain dataclasses         | Pass between layers; no ORM leak     |
| `repository/` | `protocols.py`           | Abstract repo contracts              |
| `services/` | `*_service.py`             | Orchestration; injected with repos   |
| `scraping/` | `client.py`, `*_source.py` | HTTP/FTP transport only              |
| `parsers/`  | `*_parser.py`              | HTML/Excel → domain objects         |
| `storage/`  | `db/`, `repositories/`     | Persistence only                     |
| `settings/` | `schema.py`, `loader.py`   | Env-driven config                   |
| `cli.py`    | Typer commands             | Thin: build service, call, format    |

See [`docs/architecture.md`](docs/architecture.md) for the full design
document and module map.

## Testing

```bash
pytest
```

SQLite-only profile (excludes `mysql` and `online` markers):

```bash
python -m pytest -q --cov=src/doc3gpp --cov-report=term-missing -m "not mysql and not online"
```

Equivalent helper script:

```bash
./scripts/test_sqlite.sh
```

Online tests (opt-in, hits live 3gpp.org and FTP):

```bash
python -m pytest -q -m online -rs
```

MySQL tests (requires `DOC3GPP_TEST_MYSQL_URL`):

```bash
python -m pytest -m mysql
```

## Roadmap

Known constraints are documented in `AGENTS.md` §Known Constraints, and the
TDoc extraction pipeline's current state (the `R5s` / `R5w` URL templates
are verified; the `R5-` / `C6-` templates are intentionally unresolved)
and the calendar parser's coupling to the current DynaReport layout are
called out in `docs/architecture.md` §Out of scope (today).

## Documentation

- [Architecture](docs/architecture.md)
- [CLI reference](docs/cli.md)
- [3GPP knowledge base](docs/3gpp-knowledge.md)

## Contributing

Issues and pull requests are welcome. There is no formal `CONTRIBUTING.md`
yet — for now:

1. Open an issue describing the change before sending a non-trivial PR.
2. Match the existing style: Python 3.10+, ruff (`line-length = 100`),
   strict type hints, layered architecture.
3. Add or update tests in `tests/unit/` (mock external calls) and
   `tests/integration/` (sqlite).
4. Keep `README.md`, `AGENTS.md`, and `docs/*.md` in sync when CLI or
   public-API behavior changes.

## License

[MIT](LICENSE) — Copyright © 2026 jerry wang.

## Acknowledgments

- The [3GPP](https://www.3gpp.org) community for making meeting calendars,
  TDocs, and WI lists publicly available.
- The maintainers of [httpx](https://www.python-httpx.org/),
  [SQLAlchemy](https://www.sqlalchemy.org/),
  [Pydantic](https://docs.pydantic.dev/),
  [Typer](https://typer.tiangolo.com/),
  [BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/),
  [openpyxl](https://openpyxl.readthedocs.io/),
  [tomli_w](https://pypi.org/project/tomli_w/), and
  [python-docx](https://python-docx.readthedocs.io/) — the libraries this
  project stands on.

## Support

- Bug reports and feature requests: [GitHub Issues](https://github.com/jerrywang121/doc3gpp/issues)
- Source: [github.com/jerrywang121/doc3gpp](https://github.com/jerrywang121/doc3gpp)