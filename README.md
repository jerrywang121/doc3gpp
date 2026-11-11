# doc3gpp

A Python library and CLI for collecting and searching 3GPP meeting calendars,
TDocs, work items, specifications, and RAN5 testcase status snapshots.
Store source metadata in SQL, extract Word and presentation content, and use
the same services through the CLI, web UI, HTTP API, or MCP tools.

## Install

Requires **Python 3.10+** and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install 'doc3gpp[cli,extract]'
# Or install all runtime extras: CLI, extraction, semantic search, and web/MCP.
uv tool install 'doc3gpp[all]'
```

If `doc3gpp` is not on your `PATH`, run `uv tool update-shell` and restart your shell.

`uv add doc3gpp` adds the core SDK to a Python project
([Python example](docs/README.md#python-sdk)); CLI dependencies require `[cli]`.
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
`uv tool install 'doc3gpp[cli,web]'`. If the CLI is already installed, use
`uv tool install --reinstall 'doc3gpp[all]'` to install all runtime extras.
Run `doc3gpp mcp` for stdio, or `doc3gpp server start` for the UI at
<http://127.0.0.1:13999/> and MCP at
`http://127.0.0.1:13999/mcp/`.
There is no built-in authentication: keep the HTTP server on a trusted local
machine. See [MCP connections](docs/mcp.md#connect) for client configuration.

## Agent Skill and MCP

Install the [doc3gpp skill](src/doc3gpp/data/skills/doc3gpp/SKILL.md) for
supported agents in the current project with Node.js (`npx`):

```bash
npx --yes skills add https://github.com/jerrywang121/doc3gpp/tree/main/src/doc3gpp/data/skills/doc3gpp --skill doc3gpp -y
```

Configure your MCP client to use either local stdio or HTTP. These examples use
`mcpServers` JSON; client configuration keys and file locations vary, so adapt
them to your client.

### stdio

```json
{
  "mcpServers": {
    "doc3gpp": {
      "command": "doc3gpp",
      "args": ["mcp"]
    }
  }
}
```

### HTTP (Streamable HTTP)

Start the server with `doc3gpp server start --no-open`, then configure an
HTTP-capable MCP client to connect to the default endpoint:

```json
{
  "mcpServers": {
    "doc3gpp": {
      "type": "http",
      "url": "http://127.0.0.1:13999/mcp/"
    }
  }
}
```

For transport details and security requirements, see
[MCP connections](docs/mcp.md#connect).

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
uv sync --extra dev --extra extract --extra semantic
ruff check .
./scripts/test_sqlite.sh
```

Tests are offline by default. See [testing conventions](docs/conventions.md#tests)
for optional dependencies, parallel execution, and opt-in online tests.

## License

[MIT](LICENSE).
