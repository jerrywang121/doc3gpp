# doc3gpp

> Last reviewed: 2026-08-13

> Provide 3GPP TDoc information with full text search capability — a Python CLI/library, a local web UI, and an MCP server for AI clients.

[![License: MIT](https://img.shields.io/github/license/jerrywang121/doc3gpp)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![PyPI](https://img.shields.io/pypi/v/doc3gpp)](https://pypi.org/project/doc3gpp/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000)](https://docs.astral.sh/ruff/)

## Description

`doc3gpp` scrapes 3GPP meeting calendars, work items (WIs), TDocs,
and specifications (TS/TR) from
[3gpp.org](https://www.3gpp.org) and persists them to a relational database
for programmatic access. It ships in three forms:

- a **Python library (SDK)** for embedding the data in your own code,
- a **Typer-based CLI** (`doc3gpp`) for scripting and ad-hoc queries, and
- a **local web server** with a built-in browser UI (FastAPI + HTMX + Jinja2)
  plus a **Model Context Protocol** endpoint (`/mcp`) that exposes the same
  data to AI clients with byte-for-byte JSON parity against the HTTP
  `?format=json` routes.

SQLite is the sole storage backend.

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [CLI Usage](#cli-usage)
- [Database Configuration](#database-configuration)
- [Configuration File (TOML)](#configuration-file-toml)
- [Architecture](#architecture)
- [Testing](#testing)
- [Roadmap](#roadmap)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)
- [Acknowledgments](#acknowledgments)
- [Support](#support)

## Features

- **Meeting / TDoc / WI / Spec sync** — fetch the 3GPP Meetings, TDocs, Work Items List, and 3GPP specifications (TS/TR) with versions. Spec sync fans out across per-spec detail pages in a thread pool and caches the result for `spec list` / `spec show`.
- **TDoc CR extraction** — Download and parse TDoc CR into structured records.
- **Full-text search (FTS5 + BM25, with optional semantic rerank)** — SQLite FTS5 keyword search with BM25-ranked hits and highlighted snippets; optionally semantic reranked by a natural language string.
- **Hybrid semantic search (FTS5 + embeddings)** — vector KNN + FTS5 keyword search, merged via reciprocal-rank fusion.
- **Web server + MCP** — a single-port HTTP server (FastAPI + HTMX + Jinja2) for browsing and searching 3GPP data in a browser, plus a Streamable-HTTP Model Context Protocol endpoint (`/mcp`) exposing the same data to AI clients with byte-for-byte JSON parity with the HTTP `?format=json` routes. Background jobs (sync, parse, search rebuild, cache purge) run on a shared asyncio worker with live SSE progress.
- **SQLite storage backend** — via SQLAlchemy 2.0.

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
pip install "doc3gpp[extract]"    # TDoc CR extraction (python-docx)
pip install "doc3gpp[search]"     # FTS5 + BM25 full-text search
pip install "doc3gpp[semantic]"   # Hybrid FTS5 + embedding vector search (sentence-transformers, sqlite-vec)
pip install "doc3gpp[web]"        # Web server + MCP (FastAPI, uvicorn, Jinja2 + HTMX, markdown-it-py)
pip install "doc3gpp[all]"        # every runtime extra: CLI, extraction, search, semantic, web
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
doc3gpp spec sync --tsg r5                 # scrape spec list + parallel detail pages
doc3gpp spec sync --spec-id 36.579-5       # sync a single stored spec
doc3gpp spec list --type TS --limit 20
doc3gpp spec show 36.579-5                 # header + version rows
```

## CLI Usage

The CLI ships ten sub-apps. The most common
entry points are `meeting sync` (DynaReport calendar), `tdoc sync`
(TDoc-list XLSX + auxiliary file scan), `tdoc parse` (extract CR cover
pages), `tdoc show --format raw` (render the converted `.docx`
markdown), `spec sync` (3GPP specification records with versions), and
`search query` (FTS5 + BM25 full-text search).

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

### `spec` — 3GPP specifications (TS / TR)

```bash
# sync — fetch list page + parallel detail pages (TSGs / WI links / ETSI PDF / CR list)
doc3gpp spec sync --tsg r5                          # 24h skip rule via sync.spec_sync_interval
doc3gpp spec sync --tsg r5 --force                  # bypass the skip rule
doc3gpp spec sync --spec-id 36.579-5                # sync a single stored spec (no list page)
doc3gpp spec sync --spec-id 36.579-5 --force        # bypass the skip rule for one spec
doc3gpp spec sync --tsg r5 --per-version-details    # always re-fetch ETSI PDF + CR list per version (default OFF)

# list — 9 filter flags combine freely (rich-filter grammar: %, !pattern, null, not-null)
doc3gpp spec list --limit 20
doc3gpp spec list --tsg R5 --type TS --status "Under change control"
doc3gpp spec list --spec-id '36.579%' --title '%conformance%'
doc3gpp spec list --tsg R5 --format json -o r5_specs.json

# show — dotted spec id; renders header + version rows
doc3gpp spec show 36.579-5
doc3gpp spec show 36.579-5 --format json -o 36_579-5.json
```

`spec sync` honours `sync.spec_sync_interval` (default `24h`) on a
**per-spec** basis: each per-worker `_sync_one_spec` short-circuits
specs whose own `specs.last_synced_at` is within the interval, and
re-syncs the rest; `--force` bypasses the check. Each spec's
`last_synced_at` is stamped on a successful re-sync so a `spec list`
/ `spec show` always reads from cache. Per-version follow-ups
(ETSI PDF + CR list) are skipped by default; pass `--per-version-details`
to fetch them. The default preserves any previously-cached `pdf_url` /
`crs` values on existing rows.

### `search` — FTS5 + BM25 full-text search

Requires the `doc3gpp[search]` extra (ships FTS5 helpers; sqlite-only).
On builds without FTS5, `search` reports
unavailable with a one-liner and the rest of the CLI is unaffected.

```bash
# query — BM25-ranked hits with highlighted snippets
doc3gpp search query "NB-IoT scheduling" --tsg RAN1 --limit 10
doc3gpp search query "R5-1234567" --format json
doc3gpp search query "scheduling NR" --spec 38.300 --since 2026-01-01

# query --sem-query — rerank FTS5 hits by cosine similarity
doc3gpp search query "NB-IoT scheduling" --tsg RAN1 \
  --sem-query "power saving for NB-IoT UEs" --limit 10
doc3gpp search query "scheduling NR" --spec 38.300 \
  --sem-query "FR2 scheduler design" --quiet

# index — status, rebuild, resume, stale-only refresh
doc3gpp search index                                  # show SearchIndexStatus
doc3gpp search index --rebuild                        # drop + rebuild from scratch
doc3gpp search index --rebuild --resume --batch 1000  # resume a crashed rebuild
doc3gpp search index --rebuild --stale-only --quiet   # re-index only newer tdocs
```

The auto-index hook keeps the index fresh after every successful
`tdoc parse`; tune or disable via the `[search]` section in
`doc3gpp.toml` (`enabled`, `auto_index_on_parse`,
`rebuild_batch_size`, `snippet_tokens`, `search_fanout_factor`).

#### Search query syntax

`search query` uses the FTS5 rich-text search pattern. Plain text is
wrapped as a quoted expression (implicit `AND` between terms), so
`"NB-IoT scheduling"` matches documents containing both terms. Queries
that contain an FTS5 operator (`AND`, `OR`, `NOT`, `NEAR`, `*`, or a
`"`) pass through unchanged, letting you write full FTS5 expressions:

```bash
doc3gpp search query "scheduling AND (NR OR LTE)"
doc3gpp search query "R5-*"                       # prefix match on TDoc ids
doc3gpp search query "38.300*"                    # prefix match on spec ids
doc3gpp search query '"CSI report" NEAR/5 feedback'
```

Single-quoted phrases are rewritten to FTS5 double-quoted phrases
(`'CSI report'` → `"CSI report"`). FTS5 special characters (`(`, `)`,
`:`, `"`, `*`, `\`) are backslash-escaped to prevent injection, and
stopwords-only queries (e.g. `"the a"`) are rejected with a clear
error. TDoc ids and spec numbers are normalized on both the index and
query side so `R5-1234567r2` matches every revision and `38.300`
stays a single token.

#### `search query --sem-query`

Optional `--sem-query STR` reranks the BM25 hits by cosine similarity
to a natural-language string. The FTS5 path fetches
`limit * search_fanout_factor` candidates (default 4×) before the
reranker truncates back to `--limit`. Missing candidates receive a
`-inf` score and sink to the bottom; on a fully empty
`vec_tdoc_embeddings` the FTS5 order is preserved and a one-shot
`WARNING` is logged (suppress with `--quiet`). Empty `--sem-query ""`
is a no-op. Requires the `[semantic]` extra and a populated
`vec_tdoc_embeddings` index — build it with
`doc3gpp search index --rebuild-embeddings` first.

The legacy `--rerank` flag was removed; callers should switch to
`--sem-query`.

### `search sem` — hybrid FTS5 + embedding vector search

Requires the `doc3gpp[semantic]` extra (`sentence-transformers`,
`sqlite-vec`); sqlite-only. The `sentence-transformers` model is
pulled automatically by `pip install`, so the single install command
sets up the library and the model together. On
builds without sqlite-vec the command reports unavailable with a
one-liner; `search query` (FTS5-only) still works.

```bash
# sem — vector-only by default; opt into FTS5 via --fts5-query
doc3gpp search sem "what CRs touch NB-IoT power saving" --limit 10
doc3gpp search sem "scheduling NR for FR2" --spec 38.300 --format json
doc3gpp search sem "TTCN changes for R5-12345" \
  --fts5-query "R5-12345" --fts5-weight 0.5

# extend `search index` to manage the embedding index
doc3gpp search index --rebuild-embeddings           # drop + rebuild vec_tdoc_embeddings
doc3gpp search index --rebuild-embeddings --stale-only --quiet
doc3gpp search index --rebuild-all                   # both FTS5 + vector in sequence
```

The auto-embed hook keeps `vec_tdoc_embeddings` fresh after every
successful `tdoc parse` (skipped when `Settings.semantic_search.
auto_embed_on_parse = false`). Tune via the `[semantic_search]`
section in `doc3gpp.toml` (`enabled`, `embedding_model`, `chunk_size`,
`chunk_overlap`, `rrf_k`, `fts5_weight`, `fanout_multiplier`,
`max_chunks_per_tdoc`).

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
(`wi list` and `spec list` use their configured
`output.fields.wi` / `output.fields.spec` lists):

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

### `server` — web server + MCP

The optional `doc3gpp[web]` extra installs a single-port HTTP server that
serves both a browsable HTMX UI and a Model Context Protocol endpoint.
Enable it in the TOML config, install an OS service, then start it:

```bash
doc3gpp config set server.enabled true
doc3gpp server install systemd --no-start     # or `launchd` on macOS
doc3gpp server start                          # opens http://127.0.0.1:8765/
```

- **HTML UI** — browse meetings, TDocs, TSGs, WIs, and search results.
- **JSON API** — every read route accepts `?format=json`, byte-for-byte
  identical to the MCP tools.
- **MCP** — `http://127.0.0.1:8765/mcp` exposes 24 tools covering the
  same reads plus job lifecycle. The transport is set under `[mcp]` in the
  TOML config: `streamable_http` (default, single `POST /mcp`) or `sse`
  (legacy two-endpoint `GET /mcp/sse` + `POST /mcp/messages/`). Browser
  clients must have their origin in `[mcp] allowed_origins` (defaults to
  `http://127.0.0.1` and `http://localhost`).
- **Jobs** — sync, parse, search rebuild, and cache purge run on a shared
  asyncio worker; watch live progress over SSE at `/jobs/{id}/events`.

Point an MCP client at the endpoint. For example, in Claude Desktop's
`claude_desktop_config.json` (or any client that supports a
Streamable-HTTP MCP server):

```json
{
  "mcpServers": {
    "doc3gpp": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

For clients that only speak the legacy SSE protocol, switch the server
transport and point at the SSE endpoint instead:

```bash
doc3gpp config set mcp.transport sse
```

```json
{
  "mcpServers": {
    "doc3gpp": {
      "type": "sse",
      "url": "http://127.0.0.1:8765/mcp/sse"
    }
  }
}
```

Manage the process with `doc3gpp server start|stop|status|logs`,
`doc3gpp server install|uninstall systemd|launchd`. See
[`docs/web-server.md`](docs/web-server.md) for the full guide.

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
`output.*`, `db_auto_migrate`, `http_max_retries`,
`http_retry_backoff`, …) are TOML-only — see the example file.

Examples:

```bash
# sqlite (omit DOC3GPP_DATABASE_URL to use the pydantic default,
# which resolves to ~/.local/share/doc3gpp/doc3gpp.db)
DOC3GPP_DATABASE_URL=sqlite+pysqlite:////absolute/path/to/doc3gpp.db
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

[search]
enabled = true                       # sqlite-only FTS5 + BM25 search; off = no-op
auto_index_on_parse = true           # keep the index in sync after every parse
snippet_tokens = 8                   # FTS5 snippet() length; --snippet-tokens overrides
# Per-column BM25 weights — drive both ranking and snippet selection.
# Order matches the 8 indexed FTS5 columns: (title, ftp_url,
# meeting_title, meeting_location, wis, cover_text, change_text,
# ttcn_text). Weight 0 excludes a column from ranking AND from
# previews; weight > 0 gives it its own highlighted snippet (only
# when the snippet actually contains a match). Tune via
# `doc3gpp search --explain`.
bm25_weights = [5.0, 0.0, 0.0, 1.0, 5.0, 5.0, 5.0, 5.0]

# Multiplier for the candidate pool fed into semantic rerank via
# `search query --sem-query`. FTS5 fetches `limit * search_fanout_factor`
# rows; the reranker then truncates back to `--limit`. Only consulted
# when `--sem-query` is supplied. Default 4. Range 1..64.
search_fanout_factor = 4

# Hybrid search — only loaded when the `[semantic]` extra is installed.
# sqlite-only; on builds without sqlite-vec the vector path is a no-op.
[semantic_search]
enabled = true                       # master switch for `search sem` + auto-embed
auto_embed_on_parse = true           # upsert embeddings after every successful parse
embedding_model = "sentence-transformers/all-MiniLM-L6-v2"  # 384-dim
chunk_size = 200                     # whitespace tokens per chunk
chunk_overlap = 20                   # trailing tokens repeated at next chunk start
rrf_k = 60                           # RRF k constant
fts5_weight = 0.5                    # 0.0 = vector-only, 1.0 = FTS5-only (vector weight = 1 - fts5_weight)
fanout_multiplier = 4                # hybrid-path fanout: limit * fanout per side
max_chunks_per_tdoc = 8              # cap on chunks per TDoc

# Web server + MCP — both TOML-only (no env overrides); only loaded
# with the `doc3gpp[web]` extra installed.
[server]
enabled = false                      # master switch; gates every `server` subcommand
host = "127.0.0.1"
port = 8765

[mcp]
enabled = true                       # mount /mcp; no effect unless server.enabled
transport = "streamable_http"        # streamable_http (default) | sse
allowed_origins = ["http://127.0.0.1", "http://localhost"]  # browser origins allowed to call /mcp
sse_queue_size = 100                 # per-session event queue length
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

SQLite-only profile (excludes `online` markers):

```bash
python -m pytest -q --cov=src/doc3gpp --cov-report=term-missing -m "not online"
```

Equivalent helper script:

```bash
./scripts/test_sqlite.sh
```

Online tests (opt-in, hits live 3gpp.org and FTP):

```bash
python -m pytest -q -m online -rs
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
- [Web server + MCP](docs/web-server.md)
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