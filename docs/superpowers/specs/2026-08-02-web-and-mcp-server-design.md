# doc3gpp Web + MCP Server — Design Spec

**Status:** Approved (pending user review of the written spec)
**Date:** 2026-08-02
**Branch:** `web-and-mcp-server`
**Author:** brainstorming session

## Goal

Add a single-process, single-port HTTP server to doc3gpp that:

1. Serves a read-only web UI (HTMX + Jinja2) over meetings, TDocs, TSGs,
   WIs, and FTS5 / semantic search — including the full markdown body of
   any TDoc.
2. Exposes the same read capabilities (plus background sync / parse
   jobs) as MCP tools over Streamable HTTP at `/mcp`, so an AI client can
   browse and drive doc3gpp with structured tool calls.

The server is a thin adapter over the existing `services/` layer; no
business logic is duplicated.

## Non-goals

- No multi-user / multi-tenant design. Localhost only; one OS user.
- No authentication or authorization in v1. Documented as a known gap.
- No new storage backend, no new parser, no FTS5 / vector changes.
- No new CLI subcommands beyond the `server` group (start/stop/status/
  logs/install/uninstall).
- No live `meeting sync` / `tdoc sync` `Settings.sync.auto_sync`
  integration with the new server path (the server's job endpoints are
  the explicit way to drive syncs).
- No client-side JS frameworks (React, Vue, Svelte) — HTMX only.
- No WebSocket transport; SSE only for live job progress.

## Audience & deployment model

| Decision | Value |
| --- | --- |
| Audience | Personal, single user |
| Bind address | `127.0.0.1` (default; configurable via `[server].host`) |
| Port | `8765` (default; configurable) |
| Auth | None in v1 |
| Process model | Single uvicorn process; one asyncio loop owns HTTP, MCP, and the job worker |
| Persistence | New `jobs` SQLite table (same engine as the rest of the app) |

## Architecture overview

```
                    ┌───────────────────────────────────────────────┐
                    │        uvicorn (single process)                │
                    │                                                │
   curl / browser ──┼──▶  FastAPI app  ── / ── /meetings ── /tdocs  │
                    │       │            │              │            │
                    │       │            └── /tdocs/{id}/content      │
                    │       │                       │                │
                    │       │            ┌──────────┴──────────┐     │
                    │       │            │                     │     │
                    │       │       Jinja2 templates       markdown-it │
                    │       │       (HTMX partials)        (Pygments) │
                    │       │                                         │
                    │       │       ┌─── /mcp ─── FastMCP sub-app ──┐│
   AI client ───────┼───────┼──────▶│  stateless_http=True         ││
                    │       │       │  tools: list_meetings,       ││
                    │       │       │         get_tdoc, ...        ││
                    │       │       └──────────────────────────────┘│
                    │       │                                         │
                    │       │       ┌─── /jobs/* ───────────────────┐│
                    │       └──────▶│  POST: enqueue (returns 202)  ││
                    │               │  GET:  status                 ││
                    │               │  /events: SSE stream          ││
                    │               └─────────┬────────────────────┘│
                    │                         │                       │
                    │                         ▼                       │
                    │       ┌─ asyncio job worker (1 task) ─────────┐│
                    │       │  polls `jobs` table; runs handlers    ││
                    │       │  via services/factory.build_*         ││
                    │       └────────────────┬──────────────────────┘│
                    │                        │                       │
                    └────────────────────────┼───────────────────────┘
                                             ▼
                          services/* (existing) → repository/*
                                              → SQLAlchemy ORM (existing)
                                              → {cache.dir}/{zips,markdown}/
```

**Layer rule:** `src/doc3gpp/web/` is a new thin adapter layer. It calls
`services/factory.build_*` for domain operations and never touches the
repository layer directly. No business logic is duplicated — the HTTP
read paths call into `MeetingService`, `TDocService`, `SearchService`,
etc., exactly the same way the CLI does.

## Tech stack

| Concern | Choice | Reason |
| --- | --- | --- |
| ASGI framework | **FastAPI** | Async, Pydantic v2 native, SSE, OpenAPI for the JSON surface |
| ASGI server | **uvicorn** | Standard FastAPI runtime; `--reload` for dev |
| MCP | **`mcp` PyPI package** (official Python SDK) | Streamable HTTP transport (2025-03-26 spec); `FastMCP` adapter |
| Templating | **Jinja2** | Standard, integrates cleanly with FastAPI |
| Frontend interactivity | **HTMX 2.x** | Server-rendered partials; no JS build step |
| Markdown rendering | **markdown-it-py** + **Pygments** | Pure Python; emits safe HTML |
| Job queue | **`asyncio` task + `jobs` SQLite table** | Survives restarts; matches existing DB |
| Config | Existing pydantic-settings + TOML | New `[server]` + `[mcp]` blocks |
| Tests | `pytest`, `httpx.AsyncClient(ASGITransport)`, `mcp.ClientSession` | Mirrors existing test approach |

## HTTP API surface

### Read endpoints

```
GET  /                                              landing page
GET  /meetings                          ?tsg=&name=&location=&year=&tdoc=&limit=&offset=
GET  /meetings/{meeting_id}
GET  /tdocs                             ?tdoc=&meeting=&meeting-id=&source=&spec=&wi=
                                          &title=&cr-cat=&status=&type=&revision-of=
                                          &revised-to=&ftp-url=&release=&version=
                                          &cr-num=&cr-pack=&uploaded-date=
                                          &limit=&offset=
GET  /tdocs/{tdoc_id}                            slim cover + TTCN + auxiliary files
GET  /tdocs/{tdoc_id}/content                    full markdown body, rendered as HTML
GET  /tdocs/{tdoc_id}/content?format=raw         raw markdown text
GET  /tsgs
GET  /tsgs/{short_name}
GET  /wis                               ?tsg=&name=&acronym=&release=&limit=&offset=
GET  /search?q=...&...same-filters-as-tdocs
GET  /search/sem?q=...&fts5-query=...&fts5-weight=...&limit=&offset=
```

**Content negotiation:** every read path returns `text/html` by default.
Passing `?format=json` (or `Accept: application/json`) returns the same
payload the CLI would print in `--format json`, byte-for-byte. No
separate `/api/*` prefix.

**Filter grammar:** identical to the CLI. `null` / `not-null` /
`!<pattern>` for text filters; `OP'YYYY-MM-DD'` for dates. The same
`_resolve_text_filter` helper from `src/doc3gpp/cli_filters.py` is
imported by the HTTP query-param parser — one source of truth.

### Write endpoints (background jobs)

```
POST /jobs/sync/meetings                  body: {"tsg": "SA2"}
POST /jobs/sync/tdocs                     body: {"meeting_id": "..."} | {"meeting": "..."}
POST /jobs/sync/tdocs/all
POST /jobs/parse/tdocs                    body: filter dict (same as GET /tdocs)
                                          + {"force": bool, "full": bool, "max_batch": int?}
POST /jobs/search/rebuild                 body: {"stale_only": bool, "resume": bool}
POST /jobs/cache/purge                    body: {"scope": "markdown|zips|all", "yes": true}

GET  /jobs                                recent jobs (last 50, paginated by query)
GET  /jobs/{job_id}                       status + summary + log preview (last ~50 lines)
GET  /jobs/{job_id}/events                SSE stream: log lines + final status
POST /jobs/{job_id}/cancel                best-effort cancel (cooperative)
```

**Job contract:**

```jsonc
// POST → 202 Accepted
{
  "job_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "status": "queued",
  "links": {
    "self": "/jobs/f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "events": "/jobs/f47ac10b-58cc-4372-a567-0e02b2c3d479/events"
  }
}

// GET /jobs/{id}
{
  "job_id": "...",
  "kind": "sync_meetings",
  "status": "running",         // queued | running | succeeded | failed | cancelled
  "params": {"tsg": "SA2"},
  "result": null,              // populated on success
  "error": null,               // populated on failure
  "summary": {"meetings": 14},
  "log_tail": ["..."],         // last ~50 lines from jobs.log_lines
  "created_at": "2026-08-02T12:00:00Z",
  "started_at": "2026-08-02T12:00:01Z",
  "completed_at": null,
  "links": { ... }
}
```

**SSE event format** (`GET /jobs/{id}/events`):

```
event: status
data: {"status": "running"}

event: log
data: {"line": "[2026-08-02 12:00:01] fetched meeting SA2#156"}

event: status
data: {"status": "succeeded", "summary": {"tdocs": 200, "elapsed_s": 124.3}}
```

## MCP tool surface

```
# Meetings
list_meetings(tsg?, name?, location?, year?, tdoc?, limit?, offset?)
get_meeting(meeting_id)

# TDocs
list_tdocs(
  tdoc?, meeting?, meeting_id?, source?, spec?, wi?, title?,
  cr_cat?, status?, type?, revision_of?, revised_to?, ftp_url?,
  release?, version?, cr_num?, cr_pack?, uploaded_date?,
  limit?, offset?,
)
get_tdoc(tdoc_id)                       → cover + ttcn + auxiliary files
get_tdoc_content(tdoc_id, format?)      → markdown body (format: "markdown"|"html"; default "markdown")

# TSGs
list_tsgs()
get_tsg(short_name)

# WIs
list_wis(tsg?, name?, acronym?, release?, limit?, offset?)

# Search
search_tdocs(query, ...same-tdoc-filters, limit?, offset?, snippet_tokens?)
semantic_search_tdocs(query, fts5_query?, fts5_weight?, limit?, offset?)   # only if [semantic_search] enabled

# Jobs
sync_meetings(tsg)
sync_tdocs(meeting_id?)
sync_tdocs_by_meeting(meeting?)
sync_all_tdocs()
parse_tdocs(...same-filters-as-list, force?, full?, max_batch?)
rebuild_search_index(stale_only?, resume?)
purge_cache(scope?, yes?)
get_job(job_id)
cancel_job(job_id)
list_jobs(limit?)
```

**Tool result schema:** every tool returns a JSON-serializable dict
matching the HTTP JSON surface byte-for-byte. Job tools return
`{job_id, status, message, links: {self, events}}`.

**Filter shape difference from HTTP:** MCP tools expose filter params
as structured Pydantic fields (one per filter dimension). Date filters
split into `{op: ">=", value: "2026-01-01"}` instead of the CLI string
syntax — friendlier for AI clients.

**Mount:** `mcp.FastMCP("doc3gpp", stateless_http=True)` registered as
an ASGI sub-app at `/mcp`:

```python
app.mount("/mcp", mcp.streamable_http_app())
```

`stateless_http=True` because we don't need per-session server state —
the DB *is* the state.

**Resources & prompts:** not in v1. The skill explicitly de-prioritises
resources (most AI clients prefer tool calls) and prompts (AI can
compose tools itself).

**Invariant:** every MCP tool has a 1:1 HTTP read or job equivalent so
the AI can always cite a URL the user can paste in a browser.

## Background jobs

### Job lifecycle

```
POST /jobs/*              worker reads         handler runs
   │                          │                     │
   ▼                          ▼                     ▼
queued ───▶ running ───▶ succeeded
                │
                ├──▶ failed
                │
                └──▶ cancelled  ◀── POST /jobs/{id}/cancel
```

### Storage

New `jobs` table (added to `Base.metadata.create_all` — no migration
story needed):

| Column | Type | Notes |
| --- | --- | --- |
| `id` | TEXT PK | UUIDv4 |
| `kind` | TEXT NOT NULL | `sync_meetings` / `sync_tdocs` / `parse_tdocs` / `search_rebuild` / `cache_purge` |
| `status` | TEXT NOT NULL | `queued` / `running` / `succeeded` / `failed` / `cancelled` |
| `params` | TEXT NOT NULL | JSON-encoded request body |
| `result` | TEXT NULL | JSON-encoded handler output on success |
| `error` | TEXT NULL | Populated on `failed` |
| `summary` | TEXT NULL | JSON-encoded `{tdocs: N, elapsed_s: N, ...}` |
| `log_lines` | TEXT NOT NULL DEFAULT `''` | `\n`-delimited; trimmed to last 10 000 chars on each write |
| `created_at` | DATETIME NOT NULL | UTC |
| `started_at` | DATETIME NULL | Set when worker picks the job |
| `completed_at` | DATETIME NULL | Set on terminal status |
| `cancelled` | BOOLEAN NOT NULL DEFAULT 0 | Set by `cancel_job`; worker checks before each iteration |

### Worker

- One asyncio task, owned by the FastAPI lifespan.
- Polls `SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1`
  every `Settings.server.poll_interval_seconds` (default `1.0s`,
  range `0.05..60.0`) when idle — short cadence to keep the nav
  badge in lockstep with the SSE stream. `asyncio.wait(..., timeout=poll_interval, FIRST_COMPLETED)`
  yields the event loop on every tick so a slow handler does not
  delay pickup of newly enqueued jobs.
- `mark_running` issues `UPDATE ... WHERE status = 'queued'` and
  treats `rowcount == 0` as a no-op, returning a `(claimed, job)`
  pair. The guard makes a duplicate claim safe: two workers ticking
  the same row cannot both overwrite `started_at` / `log_lines`,
  and the losing worker skips the handler (no double execution).
- On startup the worker sweeps any rows stuck at `RUNNING` (a prior
  process died mid-flight) and marks them `FAILED` with
  `error="orphaned_after_restart"` so the badge can never get stuck
  on a row the new process never claimed.
- Marks `running`, dispatches via `JOB_HANDLERS[job.kind]`.
- Handler is an `async def run(params, ctx) -> result_dict` where `ctx`
  exposes `ctx.log(line)`, `ctx.set_summary(dict)`, `ctx.is_cancelled()`.
- Worker tees log lines into both `jobs.log_lines` (persisted) and
  `ctx.subscribers` (live SSE queues, one per `GET /jobs/{id}/events`).
- On completion, sets terminal status + `summary` + `completed_at`.

### Concurrency

`max_concurrent_jobs = 1` by default. doc3gpp sync is bandwidth-bound
against 3gpp.org; SQLite under write contention is the second
bottleneck. Sequential avoids both. Configurable via
`[server].max_concurrent_jobs`; the worker loop uses a semaphore so
this knob is non-breaking to add later.

### Cancellation

Cooperative. `POST /jobs/{id}/cancel` sets `jobs.cancelled = TRUE`.
Handlers check `ctx.is_cancelled()` between sync iterations, between
TDocs in `parse_many`, between meetings in bulk sync. Mid-httpx
downloads cannot be preempted; the worker finishes the current unit
and stops on the next checkpoint. Documented in API docs.

### Cleanup

A small `asyncio` task fires every `Settings.server.cleanup_interval_seconds`
(default `300`s, minimum `10`s) and deletes:

- terminal jobs (`succeeded` / `failed` / `cancelled`) older than
  `Settings.server.log_retention` (default `7d`).

Durations parsed with `humanfriendly.parse_timespan`. The cleanup
cadence is **independent** of the worker's pickup cadence
(`poll_interval_seconds`, default `1.0`s) so a fast pickup loop
does not multiply the SQL load on the cleanup query and vice-versa.
Earlier v1 conflated the two and the 5-minute cleanup cadence became
a 5-minute pickup delay for every freshly enqueued parse / sync /
cache-purge request — the split into the dedicated `poll_interval_seconds`
knob is what unblocks immediate pickup.

### Progress streaming

Per-job `asyncio.Queue[str]`. SSE handler subscribes; worker pushes a
line after every log entry. The queue is bounded (size 1000) and drops
the oldest entry if a slow consumer falls behind — keeps memory under
control without back-pressuring the worker.

## Cache strategy

No new cache path. Both subtrees below are already written and read by
`TDocCrService`:

```
{settings.cache.dir}/
├── zips/{cache_file}              # original zip from 3gpp FTP
└── markdown/{cache_file}          # zipfile.ZipFile wrapper, single *.md entry
```

The web layer calls `service.get_markdown(tdoc_id)` /
`service.get_zip(tdoc_id)` and serves the result. `[server].cache_subdir`
controls isolation:

- `null` (default): share the CLI's `cache.dir`.
- `"web"`: use `{cache.dir}/web/{zips,markdown}/` — server-only cache,
  doesn't pollute the CLI's cache, lets a user run server + CLI in
  parallel without contention.

**Cache miss behavior:**

| Path | Behavior |
| --- | --- |
| `GET /tdocs/{id}` (cover summary) | Always served from DB; no cache lookup |
| `GET /tdocs/{id}/content` (markdown body) | Serves from `cache.dir[/web]/markdown/{cache_file}`. **404 with actionable hint** on miss: `{"error": "cache_miss", "hint": "run: doc3gpp tdoc parse --tdoc <id>"}` |
| `POST /jobs/parse/tdocs` | Fills the cache as a side-effect of `TDocCrService.parse_many` |

## Settings schema

`src/doc3gpp/settings/schema.py` adds:

```python
class ServerSettings(BaseModel):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    max_concurrent_jobs: int = 1          # bounded 1..16
    poll_interval_seconds: float = 1.0     # ge=0.05, le=60.0; pickup cadence
    cleanup_interval_seconds: int = 300   # ge=10; retention-only cadence
    log_retention: str = "7d"             # humanfriendly duration for completed-job logs
    cache_subdir: str | None = None
    pid_file: str | None = None           # None -> {cache.dir}/server.pid at startup
    log_file: str | None = None           # None -> {cache.dir}/server.log at startup

class MCPSettings(BaseModel):
    enabled: bool = True
    transport: Literal["streamable_http"] = "streamable_http"
    sse_queue_size: int = 100             # ge=10; per-session event queue
```

> **Implementation note (shipped schema):** the spec originally named
> `job_retention_succeeded` / `job_retention_failed` (per-status retention)
> and `auto_open_browser`, plus MCP `mount_path` / `include_resources` /
> `include_prompts`. The shipped code consolidates retention into a single
> `log_retention` window (the worker recomputes the cutoff on every pass),
> resolves `pid_file` / `log_file` at startup, and locks MCP to the
> `streamable_http` transport. `auto_open_browser` was dropped in favour of
> the explicit `--open` CLI flag. The mount path is fixed at `/mcp` and
> resources / prompts are simply never registered (out of v1 scope), so no
> `mount_path` / `include_*` knobs are needed. This file supersedes the
> schema sketch below; `doc3gpp.toml.example`, `docs/web-server.md`, and the
> implementation plan all describe the shipped schema.

`Settings` exposes both via `server: ServerSettings` and
`mcp: MCPSettings`. `[server].enabled = false` (default) keeps the
existing CLI experience untouched. `[mcp].enabled` only takes effect
when `[server].enabled = true`; the MCP mount can't run standalone.

`get_settings()` caching and CLI-flag-overrides-env-overrides-file
precedence rules are unchanged — `--host` / `--port` on `server start`
override the TOML values for that invocation. Both `[server]` and
`[mcp]` fields are TOML-only (not in the env allowlist).

### `doc3gpp.toml` additions

```toml
[server]
enabled = true
host = "127.0.0.1"
port = 8765
max_concurrent_jobs = 1
poll_interval_seconds = 1.0      # pickup cadence for new QUEUED rows
cleanup_interval_seconds = 300   # retention-only cadence
log_retention = "7d"
cache_subdir = "web"
# pid_file = "/var/run/doc3gpp/server.pid"   # defaults to {cache.dir}/server.pid
# log_file = "/var/log/doc3gpp/server.log"   # defaults to {cache.dir}/server.log

[mcp]
enabled = true
transport = "streamable_http"
sse_queue_size = 100
```

## CLI commands (new `server` group)

```
doc3gpp server start     [--host 127.0.0.1] [--port 8765] [--open] [--reload]
doc3gpp server stop                                       # SIGTERM via PID file
doc3gpp server status                                     # OS-managed + in-process status
doc3gpp server logs       [--job <id>] [--follow]
doc3gpp server install systemd       [--user | --system] [--no-start] [--dry-run]
doc3gpp server install launchd                            [--no-start] [--dry-run]
doc3gpp server uninstall systemd     [--user | --system]
doc3gpp server uninstall launchd
```

A new Typer sub-app `server_app` joins the existing 7 groups.

- `start` — validates `[server].enabled`, binds the host/port, starts
  uvicorn, opens the browser if `--open`. `--reload` watches
  `src/doc3gpp/web/` only (not the whole tree, to avoid re-running
  sync on unrelated file touches).
- `stop` — reads PID from `{cache.dir}/server.pid`, sends SIGTERM,
  waits up to 10s for graceful shutdown.
- `status` — combines OS service state (systemd / launchd query) with
  in-process state (PID, uptime, last job).

### Install helper

`doc3gpp server install systemd --user --dry-run` renders:

```ini
[Unit]
Description=doc3gpp web + MCP server
X-Doc3gpp-Managed=true
After=network-online.target

[Service]
Type=simple
ExecStart=/home/jerry/personal/doc3gpp/.venv/bin/doc3gpp server start --no-open
Restart=on-failure
RestartSec=5
Environment=DOC3GPP_CONFIG=/home/jerry/personal/doc3gpp/doc3gpp.toml

[Install]
WantedBy=default.target
```

`launchd` renders a `~/Library/LaunchAgents/org.doc3gpp.server.plist`
with `RunAtLoad=true`, `KeepAlive=true`, and log paths under
`{cache.dir}/server.log`.

The helper:

1. Resolves runtime paths: `sys.executable`, `shutil.which("doc3gpp")`,
   active config path (`get_settings().config_path`).
2. Renders the unit / plist content with the resolved paths baked in.
3. Writes the file to the right path (unless `--dry-run`).
4. Runs `systemctl --user daemon-reload && enable --now doc3gpp`
   (or `launchctl load -w`) — unless `--no-start`.
5. `X-Doc3gpp-Managed=true` header guards `uninstall` so it only
   removes doc3gpp-managed files.

`status` for the OS-managed side:

```
doc3gpp server status
  OS service:  active (running) since 2026-08-02 12:34:56
  PID:         12345
  HTTP:        http://127.0.0.1:8765
  MCP:         http://127.0.0.1:8765/mcp
  Last job:    job-abc123 (succeeded, 14 tdocs synced, 124s ago)
```

## Error mapping

`src/doc3gpp/web/errors.py` is the single source of truth for both
transports:

- `map_domain_error(exc)` → a `JSONResponse` envelope `{error, detail, ...}`
  (used by the FastAPI exception handlers).
- `map_mcp_error(exc)` → `(code, message, data) | None` (used by the MCP
  tool guard to raise `mcp.shared.exceptions.MCPError`).

MCP codes: `-32000..-32099` are MCP-application-defined; `-32602` /
`-32603` are the standard JSON-RPC codes. The MCP surface is realised by a
`@_mcp_error_guard` decorator applied beneath every `@server.tool(...)`
registration; the SDK's `Tool.run` re-raises an `MCPError` as a JSON-RPC
protocol error (with a real `code`), whereas any other exception becomes a
bare `ToolError` (`isError`, no code).

| Domain exception | HTTP status | HTTP slug | MCP code | MCP `data.resource` |
| --- | --- | --- | --- | --- |
| `MeetingNotFoundError` | 404 | `meeting_not_found` | -32004 | `meeting` |
| `TDocNotFoundError` | 404 | `tdoc_not_found` | -32004 | `tdoc` |
| `TSGNotFoundError` | 404 | `tsg_not_found` | -32004 | `tsg` |
| `WINotFoundError` | 404 | `wi_not_found` | -32004 | `wi` |
| `JobNotFoundError` | 404 | `job_not_found` | -32004 | `job` |
| `CacheMissError` | 404 | `cache_miss` | -32005 | `tdoc_content` (+`hint`) |
| `InvalidFilterError` | 400 | `invalid_filter` | -32602 | `filter` |
| `JobAlreadyTerminalError` | 409 | `job_already_terminal` | -32603 | — |
| `SettingsDisabledError` | 503 | `settings_disabled` | -32603 | — |
| `httpx.HTTPError` (subclasses) | 502 | `upstream_unavailable` | -32603 | — |

> **Implementation note (shipped mapping):** the spec originally named
> `TDocTooLargeError` (413 / -32006) and `FilterParseError` (400 / -32602).
> The shipped code has no `TDocTooLargeError` on the web path — oversized
> TDocs are routed to the skip bucket inside the parser, so the 413 case is
> unused. `InvalidFilterError` is the single filter-error type (there is no
> separate `FilterParseError`), and `TSGNotFoundError` / `WINotFoundError` /
> `JobAlreadyTerminalError` / `SettingsDisabledError` were added to the
> table during implementation. The `-32006` code remains defined but
> unmapped.

All other exceptions bubble up as `500` (`-32603` on MCP via the guard's
fallback to `ToolError`, or as a bare protocol error from the SDK). FastAPI
registers a handler per mapped class; the MCP guard wraps the same tuple
into `mcp.shared.exceptions.MCPError`.

## Testing

### Layout

```
tests/
├── unit/
│   ├── web/
│   │   ├── test_routes_tdocs.py
│   │   ├── test_routes_meetings.py
│   │   ├── test_routes_jobs.py          # POST → poll → SSE; cancel flow
│   │   ├── test_mcp_tools.py            # mcp.ClientSession over ASGI app
│   │   ├── test_filter_grammar.py
│   │   ├── test_error_mapping.py
│   │   ├── test_settings_server.py
│   │   └── test_install_helper.py
│   └── ...
├── integration/
│   ├── test_web_end_to_end.py
│   ├── test_mcp_end_to_end.py
│   └── ...
```

### Key invariants (asserted by tests)

1. **HTTP JSON shape == MCP tool result shape** (byte-for-byte) for every
   shared read. Parametrized cross-checks call both paths with the same
   fixtures and diff the results.
2. **CLI filter grammar == HTTP `?filter=...` semantics** — the same
   `_resolve_text_filter` helper backs both.
3. **Job lifecycle** — POST creates `queued`, worker flips to
   `running`, completion lands in `succeeded`/`failed`/`cancelled`; SSE
   delivers the same transitions.
4. **Error mapping** — every domain exception subclass has a test that
   asserts both the HTTP status and the MCP code.
5. **MCP server boots with `[mcp].enabled = false`** without mounting
   anything (regression guard).
6. **Install helper `--dry-run`** outputs the rendered unit + the exact
   shell commands without writing anything (gated by `tmp_path`).

### Harness

`httpx.AsyncClient(transport=ASGITransport(app=app))` for HTTP,
`mcp.ClientSession` over the same ASGI app for MCP. SQLite engine uses
`:memory:` shared via `StaticPool` so multiple connections see the same
schema. Same fixtures the CLI integration tests already use.

### Verification commands

Added to `scripts/test_sqlite.sh`:

```bash
pytest tests/unit/web/ tests/integration/test_web_end_to_end.py tests/integration/test_mcp_end_to_end.py
```

Plus the manual smoke test in `docs/web-server.md`:

```bash
doc3gpp server install systemd --dry-run
doc3gpp server start --open
curl -s http://127.0.0.1:8765/tdocs?limit=3 | head
curl -s -X POST http://127.0.0.1:8765/jobs/sync/meetings \
     -H 'content-type: application/json' -d '{"tsg":"SA2"}'
```

## Files to add / modify

| File | Change |
| --- | --- |
| `pyproject.toml` | Add `[web]` extra: `fastapi`, `uvicorn[standard]`, `jinja2`, `markdown-it-py`, `pygments`, `mcp`, `humanfriendly`. HTMX is vendored as a static asset under `src/doc3gpp/web/static/htmx.min.js` (no Python dep). Also add `doc3gpp[web]` to the `all` extra. |
| `src/doc3gpp/web/__init__.py` | New: package marker |
| `src/doc3gpp/web/app.py` | New: FastAPI app factory, lifespan, route mounting |
| `src/doc3gpp/web/routes/` | New: `meetings.py`, `tdocs.py`, `tsgs.py`, `wis.py`, `search.py`, `jobs.py`, `landing.py` |
| `src/doc3gpp/web/templates/` | New: Jinja2 templates (`base.html`, `meeting_list.html`, `tdoc_show.html`, `tdoc_content.html`, `job_status.html`, `partials/*.html` for HTMX) |
| `src/doc3gpp/web/static/` | New: `style.css`, `htmx.min.js` (vendored, not CDN) |
| `src/doc3gpp/web/mcp_server.py` | New: `FastMCP` instance, tool registration, sub-app mount |
| `src/doc3gpp/web/workers/job_worker.py` | New: asyncio worker + handler dispatch + SSE queue mgmt |
| `src/doc3gpp/web/workers/handlers.py` | New: per-`kind` handler implementations (wrap `services/*`) |
| `src/doc3gpp/web/errors.py` | New: `map_domain_error()`, FastAPI + MCP exception handlers |
| `src/doc3gpp/web/install.py` | New: `render_systemd_unit()`, `render_launchd_plist()`, `install_systemd()`, `install_launchd()`, `uninstall_*()` |
| `src/doc3gpp/cli.py` | Add `server_app` Typer sub-app + 7 sub-commands |
| `src/doc3gpp/settings/schema.py` | Add `ServerSettings` + `MCPSettings` |
| `src/doc3gpp/storage/db/models.py` | Add `Job` ORM model |
| `src/doc3gpp/storage/repositories/jobs_sql.py` | New: `SQLAlchemyJobRepository` implementing `JobRepository` Protocol |
| `src/doc3gpp/repository/protocols.py` | Add `JobRepository` Protocol |
| `src/doc3gpp/services/factory.py` | Add `build_job_repository()` |
| `src/doc3gpp/models/jobs.py` | New: `Job` domain dataclass |
| `doc3gpp.toml.example` | Append `[server]` + `[mcp]` blocks with full comments |
| `docs/web-server.md` | New: route table, MCP tool table, install helper walkthrough, troubleshooting |
| `docs/cli.md` | Add `server` subcommand section |
| `README.md` | Add a "Web & MCP server" section near the top, link to `docs/web-server.md` |
| `AGENTS.md` | Update "Where to look" with new rows: web route handler, MCP server mount, install helper; add `[server]` + `[mcp]` to the Settings reference |
| `docs/code-map.md` | Add web + jobs symbols |
| `scripts/test_sqlite.sh` | Add the new test invocations |
| `tests/unit/web/`, `tests/integration/test_web_end_to_end.py`, `tests/integration/test_mcp_end_to_end.py` | New test files |

## Docs updates

| Doc | Change |
| --- | --- |
| `docs/web-server.md` (new) | Route table, MCP tool table, install helper walkthrough, troubleshooting (cache miss → parse, jobs stuck → status), local-only security note |
| `docs/cli.md` | Add `server` subcommand section |
| `doc3gpp.toml.example` | Append the `[server]` + `[mcp]` blocks with full comments |
| `README.md` | Add a "Web & MCP server" section near the top, link to `docs/web-server.md` |
| `AGENTS.md` | Update "Where to look" with new rows; add `[server]` + `[mcp]` to the Settings reference |
| `docs/code-map.md` | Add web + jobs symbols |

## Schema migration

`Base.metadata.create_all` in `src/doc3gpp/storage/db/migrate.py`
provisions new tables automatically. Adding the `Job` model is
zero-touch for existing users — `doc3gpp db init` creates the table on
the next run.

The first server start after upgrade prints `[info] created table: jobs`
once, then is silent.

## Known gaps (documented, deferred)

- No authentication / authorization. Localhost-only is the entire
  security model.
- No rate limiting. Single-user assumption.
- No remote-access story. Binding to `0.0.0.0` is supported but
  documented as unsupported and unmitigated.
- No MCP resources / prompts. Tool surface is complete; the rest is
  follow-up.
- No multi-process worker pool. `max_concurrent_jobs = 1` by design.

## Acceptance criteria

1. `pip install "doc3gpp[all]"` resolves cleanly with the new `web`
   extra.
2. `doc3gpp server start` boots, serves `/`, `/tdocs`, `/tdocs/{id}`,
   `/tdocs/{id}/content`, `/meetings`, `/tsgs`, `/wis`, `/search`.
3. `GET /mcp` (via `mcp.ClientSession`) exposes every tool listed above
   with descriptions auto-generated from the function signatures.
4. `doc3gpp server install systemd --dry-run` prints the rendered unit
   without writing.
5. `doc3gpp server install systemd --user` writes the unit, runs
   `daemon-reload`, enables and starts the service.
6. `doc3gpp server uninstall systemd --user` removes the unit, stops
   and disables the service, refuses if the unit isn't doc3gpp-managed.
7. `POST /jobs/sync/meetings {"tsg":"SA2"}` returns `202` with a job
   id; `GET /jobs/{id}` polls; `GET /jobs/{id}/events` streams logs.
8. `POST /jobs/{id}/cancel` flips the job to `cancelled` on the next
   checkpoint.
9. The HTTP JSON shape for every read endpoint equals the MCP tool
   result shape byte-for-byte (asserted by tests).
10. The CLI filter grammar used in `?filter=...` parses identically to
    the CLI's `--filter` (asserted by tests).
11. `GET /tdocs/{id}/content` returns 404 with a parse-hint payload on
    cache miss.
12. `doc3gpp db init` provisions the `jobs` table on a fresh DB without
    error.
13. `./scripts/test_sqlite.sh` passes.
14. `ruff check .` passes.

## Out of scope (explicit)

- Authentication, authorization, rate limiting
- WebSocket transport (SSE only)
- React / Vue / Svelte frontend (HTMX only)
- Multi-process worker pool
- MCP resources / prompts (tool surface only in v1)
- Per-user / per-tenant scoping
- Remote deployment story (always localhost)
- Migration framework (alembic) — keeps using `create_all`
- New TDoc parsers / extractors (reuses existing `TDocCrService`)
