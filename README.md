# doc3gpp

A Python library and CLI for collecting and searching 3GPP meeting calendars,
TDocs, work items, specifications, and RAN5 testcase status snapshots.
Store source metadata in SQL, extract Word and presentation content, and use
the same services through the CLI, web UI, HTTP API, or MCP tools.

## Install

Requires **Python 3.10+**.

```bash
pip install "doc3gpp[cli,extract]"
# Or install all runtime extras: CLI, extraction, semantic search, and web/MCP.
pip install "doc3gpp[all]"
```

`pip install doc3gpp` installs the core SDK ([Python example](docs/README.md#python-sdk));
CLI dependencies require `[cli]`.
Use `[extract]` for `.docx`/`.pptx` extraction and `[semantic]` for SQLite vector
search. Legacy `.doc`/`.ppt` conversion also requires LibreOffice.
See [configuration and dependencies](docs/config.md#optional-dependencies).

## Quick start

```bash
doc3gpp config init --target user
doc3gpp db init --scope all
doc3gpp db check --scope all

doc3gpp meeting sync --tsg R5
doc3gpp meeting list --tsg R5 --limit 10
# Choose a meeting_id from the list above.
doc3gpp tdoc sync --meeting-id <meeting_id>
doc3gpp tdoc list --meeting-id <meeting_id> --limit 10
doc3gpp tdoc parse --meeting-id <meeting_id> --yes
doc3gpp tdoc search --text 'handover' --limit 10
```

Sync and parse commands contact upstream sources. Some CLI reads also perform
best-effort [auto-sync](docs/processes.md#auto-sync); disable it for offline use:

```bash
doc3gpp config set sync.auto_sync false
```

For local MCP or the web server on a fresh installation, run
`pip install "doc3gpp[cli,web]"`. Run `doc3gpp mcp` for stdio, or
`doc3gpp server start` for the UI at <http://127.0.0.1:13999/> and MCP at
`http://127.0.0.1:13999/mcp/`.
There is no built-in authentication: keep the HTTP server on a trusted local
machine. See [MCP connections](docs/mcp.md#connect) for client configuration.

## Documentation

| Guide | Purpose |
| --- | --- |
| [Documentation index](docs/README.md) | Find the right guide; current vs historical documentation |
| [CLI reference](docs/cli.md) | Commands, flags, filters, and output contracts |
| [Configuration](docs/config.md) | Discovery, precedence, settings, dependencies, and secrets |
| [Processes](docs/processes.md) | Sync, parse, cache, chunk, index, query, pools, and jobs |
| [Web server](docs/web-server.md) | Running the server, UI, HTTP routes, and job monitoring |
| [MCP](docs/mcp.md) | Client connection, transports, tools, and troubleshooting |
| [Architecture](docs/architecture.md) | Layers, composition, databases, and content ownership |
| [3GPP knowledge](docs/3gpp-knowledge.md) | Upstream URLs, naming, and field semantics |
| [Code map](docs/code-map.md) | Implementation entry points for SDK users and contributors |
| [Conventions](docs/conventions.md) | Development, testing, and documentation rules |
| [Known constraints](docs/known-constraints.md) / [TODO](docs/TODO.md) | Current limitations and engineering backlog |

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,extract,semantic]"
ruff check .
./scripts/test_sqlite.sh
```

Tests are offline by default. See [testing conventions](docs/conventions.md#tests)
for optional dependencies, parallel execution, and opt-in online tests.

## License

[MIT](LICENSE).
