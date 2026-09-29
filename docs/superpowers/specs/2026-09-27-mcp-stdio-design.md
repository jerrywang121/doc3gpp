# MCP Stdio Support Design

> Status: approved in chat on 2026-09-27

## Goal

Add a foreground `doc3gpp mcp` command that serves the existing doc3gpp MCP
tool set over the Model Context Protocol stdio transport. The command must be
usable as a local subprocess by MCP harnesses, including coding agents such as
OpenCode, without requiring the HTTP server to be enabled or already running.

## Scope

### In scope

- Add the top-level `doc3gpp mcp` CLI command.
- Run the existing `build_mcp_server()` tool registry over MCP stdio.
- Reuse the existing service composition and repository wiring through
  `build_state()`.
- Start the existing background job worker so MCP tools that enqueue jobs keep
  their current behavior.
- Shut down the worker and dispose all configured database engines on normal
  stdio EOF, cancellation, or startup failure.
- Gate the command on `[mcp].enabled` only.
- Keep stdout exclusively for MCP protocol traffic; diagnostics and setup
  errors go to stderr through the CLI/logging machinery.
- Document installation, invocation, configuration, and harness integration.
- Add focused unit and integration coverage without changing HTTP transport
  behavior.

### Out of scope

- Adding `stdio` as a value of `[mcp].transport`.
- Changing the existing `/mcp` HTTP mount or its
  `streamable_http`/`sse` selection.
- Adding a second set of MCP tools or changing tool names and payloads.
- Adding a daemon, PID file, service-manager integration, or background mode
  for stdio.
- Making stdio available when the MCP optional dependency is not installed.

## Existing System

The MCP tools are registered by `src/doc3gpp/web/mcp_server.py::build_mcp_server`.
The HTTP application currently creates a `WebState` with `build_state()`, starts
`JobWorker`, mounts either `streamable_http_app()` or `sse_app()`, and disposes
the main, testcase, and specdata engines during lifespan teardown.

The HTTP mount is intentionally gated by both `server.enabled` and
`mcp.enabled`. That gate is appropriate for a web server, but it would prevent
the requested local subprocess use case when the user does not want an HTTP
listener. Stdio therefore has a separate command-level lifecycle and only
checks `mcp.enabled`.

## Architecture

### Command surface

Register a lazy top-level Typer command:

```text
doc3gpp mcp
```

The command is foreground and blocking. It reads MCP JSON-RPC messages from
stdin and writes MCP responses to stdout until the client closes stdin or the
process receives termination/cancellation.

The CLI module that registers the command must not import the MCP SDK, FastAPI,
or the stdio runtime at module import time. The command imports the runtime
only after settings validation so the core CLI remains usable without the
`[web]` extra. If the optional dependency is unavailable, the command exits
with a concise stderr error directing the user to install
`doc3gpp[web]`.

### Runtime module

Add a small runtime module at `src/doc3gpp/web/mcp_stdio.py`. Its public seam
is an async runner that accepts resolved `Settings` and owns the complete
stdio process lifecycle:

1. Compose `WebState` with `build_state(settings)`.
2. Create a `JobWorker` and `JobWorkerHandle` using the configured concurrency.
3. Attach the handle to the state and start the worker task.
4. Build the MCP server with `build_mcp_server(state)`.
5. Await the MCP SDK's stdio runner (`run_stdio_async()`), keeping the worker
   alive while tools are served.
6. In a `finally` block, shut down the worker and dispose the main, testcase,
   and optional specdata engines.

The synchronous Typer command calls this runner with `asyncio.run()`. The
runtime uses the SDK's direct stdio lifecycle rather than mounting an ASGI
application, so the SDK can own stdin EOF handling and protocol framing.

The cleanup path must cover failures while building the MCP server as well as
failures during protocol handling. If state composition succeeds but server
construction fails, engines are still disposed. If worker startup succeeds,
its task is always passed through `JobWorkerHandle.shutdown()`.

### Configuration

`MCPSettings.transport` remains an HTTP setting with the existing values:

- `streamable_http` (default)
- `sse`

Stdio is selected by the command and is not represented in that setting. This
avoids ambiguity between a foreground process and the transport used by the
HTTP server.

`[mcp].enabled = false` makes `doc3gpp mcp` fail before composing state or
importing the runtime, with a user-facing error that names the setting. The
value of `[server].enabled` does not affect `doc3gpp mcp`; it continues to gate
only the HTTP server and mounted `/mcp` endpoint.

No new environment-variable override is added. Existing TOML-only MCP
settings and precedence rules remain unchanged.

### Protocol and output behavior

The command must never write banners, status lines, progress messages, or
tracebacks to stdout because stdout is the MCP transport. CLI validation and
optional-dependency errors are rendered to stderr. Runtime logging continues
to use the project's logging configuration, whose output must also remain off
the protocol stream.

The tool registry is exactly the one returned by `build_mcp_server(state)`.
Therefore tool names, error mapping, JSON serialization, job envelopes, and
CLI/HTTP/MCP parity remain shared rather than duplicated.

## Error Handling

- Disabled MCP: fail immediately with a clear CLI error; do not open databases
  or start a worker.
- Missing `[web]` dependencies: fail immediately with an installation hint;
  do not emit a Python import traceback to stdout.
- Invalid settings: preserve the existing settings/CLI validation behavior.
- Runtime/build failure: log or report the failure on stderr and run cleanup
  before exiting non-zero.
- Client EOF: treat it as normal shutdown and return after cleanup.
- Worker handler failures: retain the existing job worker behavior; they are
  represented in job state and do not corrupt MCP framing.

## Testing Strategy

### Unit tests

Add focused tests for the command/runtime seam:

- `doc3gpp mcp` is registered as a top-level command.
- `mcp.enabled = false` rejects the command before runtime construction.
- `server.enabled = false` does not reject an enabled stdio command.
- The optional-dependency failure produces an actionable stderr message.
- A fake MCP server runner and fake state verify that the worker is started,
  stdio is awaited, and all cleanup paths run.
- Runtime cleanup occurs when server construction or protocol execution raises.

### Integration coverage

Add an MCP stdio smoke test using the installed SDK's in-memory/test stream
seam where available. It should send an initialization/request sequence and
verify that the existing tool registry responds over stdio. The test must
also verify that non-protocol output does not appear on stdout.

Run the existing MCP end-to-end and web suites to confirm that the default
Streamable HTTP mount, SSE mount, JSON parity, and job behavior are unchanged.

## Documentation Changes

Update the user-facing and contributor references in the same change set:

- `README.md`: mention the local stdio MCP command and `[web]` installation.
- `docs/cli.md`: add the `doc3gpp mcp` command contract and failure modes.
- `docs/web-server.md`: distinguish HTTP MCP transports from the standalone
  stdio command and add a harness configuration example.
- `doc3gpp.toml.example` and the packaged TOML template: document that
  `mcp.enabled` gates both MCP surfaces while `transport` applies only to
  HTTP.
- `AGENTS.md`: update the MCP route/tool guidance and command inventory.

The example harness configuration should invoke the installed executable
directly, for example:

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

## Expected Files

Implementation is expected to touch only the focused runtime/CLI/settings test
surface plus the documentation listed above:

- `src/doc3gpp/cli.py` or a small CLI module imported by it
- `src/doc3gpp/web/mcp_stdio.py`
- focused unit/integration test files
- `README.md`
- `docs/cli.md`
- `docs/web-server.md`
- `doc3gpp.toml.example`
- `src/doc3gpp/data/doc3gpp.toml.example`
- `AGENTS.md`

No database schema or existing MCP tool implementation should change.
