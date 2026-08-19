# Web server + MCP

> Last reviewed: 2026-08-13

The `doc3gpp` web server serves a browsable HTML interface over the same
services the CLI uses, plus an MCP (Model Context Protocol) endpoint for
AI tooling. It runs as a single process on one HTTP port
(`127.0.0.1:8765` by default).

The web layer is a thin adapter over the service + repository layer. Every
HTTP route calls the same services the CLI calls; the HTTP JSON output and
the MCP tool output are **byte-for-byte identical** for the same query (the
only exception is the job-enqueue envelope, which carries an extra `message`
key — see [MCP reference](#mcp-reference)).

## Prerequisites

The web layer is an extra on top of the core SDK:

```bash
pip install "doc3gpp[web]"
```

This installs FastAPI, uvicorn, the vendored HTMX/Jinja2 templates, and the
MCP package.

## Quick start

```bash
# 1. Initialise a config file (auto-detects project root vs user home)
doc3gpp config init

# 2. Enable the server (it is disabled by default)
doc3gpp config set server.enabled true

# 3. Install a service unit (optional; systemd or launchd)
doc3gpp server install systemd --no-start    # Linux
doc3gpp server install launchd --no-start    # macOS

# 4. Start the server
doc3gpp server start        # opens your browser when ready
# or, for a supervised service:
doc3gpp server start --no-open

# 5. Browse
#    HTML:  http://127.0.0.1:8765/
#    MCP:   http://127.0.0.1:8765/mcp
```

Check it is up:

```bash
doc3gpp server status
```

Stop it:

```bash
doc3gpp server stop
```

## Config

The web server is configured under `[server]` and `[mcp]` in `doc3gpp.toml`
(TOML-only — there are no `DOC3GPP_SERVER__*` env vars).

```toml
[server]
enabled = false            # master switch; all `server` commands refuse when false
host = "127.0.0.1"
port = 8765
max_concurrent_jobs = 1    # how many background jobs run at once
poll_interval_seconds = 1  # how often the worker checks for new QUEUED jobs
progress_interval_seconds = 10  # min gap between periodic progress log lines for long-running jobs
cleanup_interval_seconds = 300   # how often the worker purges terminal rows
log_retention = "7d"       # keep this much of the server log
cache_subdir = "web"       # optional subdir under the tdoc cache for server content (default None)

[mcp]
enabled = true             # mount /mcp; no effect unless server.enabled
transport = "streamable_http"   # streamable_http (default) | sse
sse_queue_size = 100
allowed_origins = ["http://127.0.0.1", "http://localhost"]  # browser origins allowed to call /mcp
```

The MCP mount is active only when **both** `server.enabled` and `mcp.enabled`
are true.

`poll_interval_seconds` (default `1.0`) governs pickup latency for freshly
enqueued `POST /jobs/...` requests; raise it to reduce DB load on idle
installs. `progress_interval_seconds` (default `10.0`) throttles the
worker's periodic progress lines (at most one per interval) so
long-running sync/parse jobs show live status without flooding the job
log / SSE stream; intermediate lines under the throttle are dropped, not
buffered, and terminal-summary emissions bypass the throttle via
`progress(message, force=True)`. `cleanup_interval_seconds` (default `300`)
is unrelated and controls retention cleanup cadence only — flipping it
does not change pickup speed.

## CLI reference

`doc3gpp server` groups the server commands. Every subcommand starts with a
guard that refuses to run while `[server] enabled = false`.

### `doc3gpp server start [flags]`

Start the HTTP server. Foreground with `--reload`, otherwise backgrounded.

| Flag | Description |
| --- | --- |
| `--host HOST` | Override `server.host`. |
| `--port PORT` | Override `server.port`. |
| `--open / --no-open` | Open the bound URL in a browser when ready (default open). |
| `--reload` | Run uvicorn in auto-reload mode (development only; blocks). |
| `--force` | Overwrite an existing pid file even when it points to a live process. |

Background mode writes a PID file to `server.pid_file` (or
`{cache.dir}/server.pid`), appends logs to `server.log_file` (or
`{cache.dir}/server.log`), and polls `/healthz` until healthy.

**Failure modes the operator can hit:**

- **Port already in use** — if the chosen port is bound by a *different*
  process (including an older `doc3gpp server start` that survived),
  the new uvicorn child exits with `[Errno 98]` before it can answer
  `/healthz`. The CLI polls the child for up to 3s after launch; if it
  exits inside that window the start command fails fast, surfaces the
  log tail, and does **not** write a pid file pointing at the dead
  child. Without this probe the user would see `server running at
  http://...` against a port still owned by the *previous* process.
- **Live pid file** — if a pid file is already present and points at a
  live process, `start` refuses with a `ClickException` and names the
  live pid. Pass `--force` to overwrite.

### `doc3gpp server stop`

Stop a running background server: sends `SIGTERM`, waits up to 10s, then
`SIGKILL` if still alive, and removes the PID file.

### `doc3gpp server status`

Report whether the server is running, its PID, uptime, the OS service state
(systemd/launchd, if installed), the HTTP/MCP URLs, and the last job.

### `doc3gpp server logs [flags]`

Print recent server logs.

| Flag | Description |
| --- | --- |
| `--job JOB_ID` | Print a specific job's log lines (from the DB). |
| `-f, --follow/--no-follow` | Follow the log file (`tail -f`). Conflicts with `--job`. |

### `doc3gpp server install systemd|launchd [flags]`

Install a service unit so the server is supervised.

| Flag | Description |
| --- | --- |
| `--user / --system` | Install scope (default user). |
| `--no-start` | Install only — do not start the service. |
| `--dry-run` | Print the unit file instead of installing. |

### `doc3gpp server uninstall systemd|launchd [flags]`

Remove a managed service unit. Refuses (via `InstallNotManagedError`) when
the target is missing or not `X-Doc3gpp-Managed` by doc3gpp.

## HTTP routes

| Method | Route | Description |
| --- | --- | --- |
| GET | `/healthz` | Liveness probe → `{"ok": true}`. |
| GET | `/` | Landing page. |
| GET | `/meetings` | List meetings (`?format=json`); shows `start_doc`/`end_doc` and a coloured, clickable sync symbol (`↻`) per meeting. |
| GET | `/meetings/{id}` | Meeting detail (HTML or JSON). |
| GET | `/tdocs` | List TDocs (`?format=json`; repeatable `fields` selects the HTML columns, see below). |
| GET | `/tdocs/{id}` | TDoc show (HTML or JSON). |
| GET | `/tdocs/{id}/content` | Parsed markdown/HTML content (`?format=markdown\|html`). 404 `cache_miss` with a hint when unparsed. |
| GET | `/tdocs/{id}/download` | Download the cached source zip (404 `cache_miss` with a hint when unparsed). |
| GET | `/tdocs/by-url` | URL-anchored TDoc show (`?ftp_url=<url>`; HTML or JSON). 404 when the URL matches no row in any of the six URL-keyed tables. |
| GET | `/tsgs` | List TSGs. |
| GET | `/tsgs/{short_name}` | TSG detail. |
| GET | `/wis` | List WIs. |
| GET | `/search` | FTS5 search (`?format=json`). Accepts an optional `sem` query param — when present, the FTS5 hits are reordered by cosine similarity to that text (CLI `--sem-query` parity; empty/absent = pure FTS5). |
| GET | `/jobs`, `/jobs/{id}` | List / show jobs. |
| GET | `/jobs/{id}/events` | SSE stream for a job. |
| POST | `/jobs/sync/meetings` | Enqueue `sync_meetings`. A `tsg` not present in the `tsgs` reference table is rejected (the job fails fast before any network work). |
| POST | `/jobs/sync/tdocs` | Enqueue `sync_tdocs` (by meeting id or name). |
| POST | `/jobs/sync/tdocs/all` | Enqueue `sync_all_tdocs`. |
| POST | `/jobs/sync/specs` | Enqueue `sync_specs` (exactly one of `tsg` / `spec_id`). A `tsg` not present in the `tsgs` reference table is rejected (the job fails fast before any network work). |
| POST | `/jobs/parse/tdocs` | Enqueue `parse_tdocs`. |
| POST | `/jobs/search/rebuild` | Enqueue `rebuild_search`. |
| POST | `/jobs/cache/purge` | Enqueue `cache_purge` (requires `yes: true`). |
| POST | `/jobs/parse/tdoc-url` | Enqueue `parse_tdoc_url` (a single 3GPP FTP URL or folder; `url` must be `https://www.3gpp.org/ftp/...`, `recursive` XOR `max_depth`). |
| POST | `/jobs/{id}/cancel` | Cancel a queued/running job. Accepts `?format=html` to return the refreshed job row as an `outerHTML` swap target for the list page's per-row Cancel button. JSON otherwise. |
| POST | `/jobs/sync_tdocs` | Flat alias for `sync_tdocs` (form or JSON). |
| GET | `/sync` | Sync hub page: nine enqueue panels (meetings, tdocs, all-tdocs, specs-by-tsg, specs-by-id, parse-tdocs, parse-tdoc-url, search-rebuild, cache-purge) + a "Recent sync jobs" table. |
| GET | `/sync?format=fragment` | Recent-jobs table fragment (wrapped in `<div id="recent-jobs">`) for HTMX `outerHTML` swap. |

Append `?format=json` to any list/detail route to get the CLI-equivalent
JSON. Append `?format=html` (or omit) for the browsable HTML view.

The meeting list shows a `↻` sync symbol per meeting coloured by TDoc-list
sync freshness — green (`≤ 24h` ago), orange (`> 24h` ago), grey (never
synced). Clicking it enqueues the TDoc-list sync for that meeting (same job
as the detail page's sync button) and flashes a brief "queued" indication
next to the symbol. Meeting names link to the 3GPP portal
(`https://portal.3gpp.org/Home.aspx#/meeting?MtgId={id}`). The meeting
detail page shows `start_doc`/`end_doc`, the last-sync timestamp
(`YYYY-MM-DD HH:MM UTC`), a link to the meeting's TDocs, and an FTP URL
field linking to `https://www.3gpp.org/ftp/{ftp_url}`; its sync button
flashes a "Sync job queued" indication after enqueueing.

The filter form supports TSG, name, year, location, and a TDoc id
selector, all with the same rich-filter grammar as the CLI.

The spec detail page shows a Sync card with a Force sync checkbox that
enqueues a single-spec sync job for that spec; the page auto-refreshes
when the job completes. A "Per-version details" checkbox alongside
"Force sync" forwards `per_version_details=true` to the job so the
worker always re-fetches the ETSI PDF + CR-list follow-ups for every
version (default OFF — without it, cached rows are preserved).

The sync hub (`/sync`) is a single page for enqueueing every sync-shaped job. Each panel submits a JSON body to the matching `/jobs/...` route via the shared `bindJobPolling` helper; when the job reaches a terminal state the bottom "Recent sync jobs" table is refreshed in place via HTMX (`GET /sync?format=fragment`) rather than a full page reload, so the user keeps their scroll position.

The header nav is ordered Home, TSGs, Meetings, TDocs, WIs, Search, Jobs.
The Jobs link shows a badge with the number of queued jobs (e.g. `Jobs (2)`)
when any are pending. The TSG list links each TSG name to the TSG's own URL
and its `show` link jumps to the meetings list pre-filtered to that TSG
(`/meetings?tsg={short_name}`).

The TDoc detail page links the FTP URL field to the cached source zip
(`/tdocs/{id}/download`) when a cached copy exists, otherwise to
`https://www.3gpp.org/ftp/{ftp_url}`. The TTCN section lists the
`changed_functions` aggregate when present, and auxiliary files link to
their FTP locations.

The same template (`tdoc_show.html`) renders in URL-anchored mode
when invoked by `GET /tdocs/by-url?ftp_url=<url>`: the TDoc card is
replaced with a "no parent tdocs row" placeholder when no parent
TDoc matches, the Parse card is omitted (no parent TDoc to filter
on), and the FTP URL field links directly to the 3GPP FTP site.
Cover / TTCN / auxiliary-files cards render identically to the
`tdoc_id`-anchored view.

The TDoc detail page renders an additional **XLSX metadata** panel
when any of the six new `tdocs` columns (`tdoc_for` / `abstract` /
`secretary_remarks` / `ls_to` / `ls_cc` / `original_ls`) is
non-`NULL`. The panel is omitted for legacy rows that were synced
before this feature landed — they stay `NULL` until the next
`doc3gpp tdoc sync` re-reads the meeting XLSX.

The TDoc detail page shows a Parse card (only when the TDoc has an FTP
URL) with "Force re-parse" and "Full extraction" checkboxes. Submitting
enqueues a `parse_tdocs` job filtered to that single TDoc id; the job
status partial polls inline until the job finishes, then the page
reloads so the server-rendered cover page / TTCN / extracted-at
sections pick up the freshly-written DB rows. The job detail page
itself renders each section (Params, optional error, log tail) as a
`card`, with the htmx poll sentinel at the bottom while the job is
in flight. A successful parse
auto-indexes the FTS5 row and the embedding chunks when
`[search].auto_index_on_parse` / `[semantic_search].auto_embed_on_parse`
are enabled — the same hooks the CLI parse path uses.

The filter form (submitted via HTMX to `GET /meetings`, swapping the
`#results` partial) supports `tsg`, `year`, `location`, `tdoc`, and
`limit`. The `tdoc` field is a text input accepting a CR-shape TDoc id
(e.g. `R5-260013`); the list is narrowed to meetings whose `start_doc` /
`end_doc` range brackets it. An empty value is ignored; a malformed value
returns a 400 `invalid_filter` response.

Both search modes (`GET /search` and `GET /search/sem`) accept a
`tdoc-id` query param — an exact-match tdoc filter identical in
semantics to the CLI's `search query --tdoc-id`. The search form
carries a TDoc text input in both the FTS5 and the semantic branch
(empty input → no filter).

The search form uses a 5-column grid: on `/search` the Query box spans
2 columns and an optional Semantic box (the `sem` param) spans the
remaining 3; on `/search/sem` the Query box spans 3 columns and the
FTS5 query box spans 2. Both forms share the full filter set (TSG,
Meeting, TDoc, Release, Spec, Since, Until, Limit; `/search/sem` also
keeps FTS5 weight). Each page links to the other at the top right
(`/search` → "Hybrid search", `/search/sem` → "FTS5 search").
The `/search/sem` form always submits an `fts5_query` field; a blank or
whitespace-only value is normalised to `None` server-side so the default
is pure-vector (matching `doc3gpp search sem`), rather than running FTS5
with an empty query and returning zero hits.

Search results render one collapsible "Matching fields" block per hit
(a single `<details>` element replacing the previous per-column
folding), with an "Expand all matching fields" toggle above the table.
The toggle's state persists in `localStorage` (key `doc3gpp-search-expand`)
and is re-applied on every HTMX swap (`htmx:afterSwap`), so the
preference survives re-queries; the script lives at
`/static/js/search.js`.

The TDoc detail page links the FTP URL field to the cached source zip
(`/tdocs/{id}/download`) when a cached copy exists, otherwise to
`https://www.3gpp.org/ftp/{ftp_url}`. The TDoc section now also shows
`Related WIs` (from the XLSX-derived `tdoc.related_wis`); the Cover
page section keeps its own separate Related WIs field (parsed from the
docx). The TTCN section lists the `changed_functions` aggregate when
present, and auxiliary files link to their FTP locations.

When a parsed TDoc has structured sidecar data, the page also surfaces a 'Required changes' card (TTCN CRs) and an 'Extracted changes' card (non-TTCN CRs); both are mutually exclusive and omitted when their respective sidecar is absent.

The TDoc list page accepts repeated `fields` query params selecting the
visible columns; values are validated against the column catalogue and
an unknown field returns a 400 `invalid_filter` response. Default
columns are TDoc ID, Title, Meeting, Type, Spec, Release, Status
(Uploaded was replaced by Status). A dropdown of checkboxes in the
filter form drives the `fields` param — toggling a checkbox re-queries
immediately (the form auto-submits on change). The tdoc list page uses
a wider layout (max-width 1430px) and the Meeting column is fixed at
180px. Row background colors derive from the status
value (case-insensitive substring, first match wins): conditionally /
partially → light green, agreed / approved → green, revised / reissued
/ merged → vanilla, rejected → red, withdrawn → grey, postponed →
pink, noted / treated / endorsed → light blue; no match → no
background. The color class applies to the whole row so it shows even
when the Status column is hidden. `?format=json` and the MCP
`list_tdocs` tool keep their fixed 10-field output regardless of the
HTML column selection.

## Jobs

Long-running operations (meeting sync, TDoc sync, parse, search rebuild,
cache purge) run as background jobs. A job is a row in the SQLite `jobs`
table, claimed by a single asyncio worker (one job at a time by default).

A job has a lifecycle: `queued → running → succeeded | failed | cancelled`.
Each enqueue returns a slim envelope with a `job_id` and self/events links;
poll `GET /jobs/{id}` or stream `GET /jobs/{id}/events` (Server-Sent
Events) for progress. Log lines carry a `[{iso}]` timestamp prefix.

Cancelling a terminal job is idempotent — the response returns the job's
final envelope with `200` so callers can inspect the result without a
separate `GET /jobs/{id}` round-trip. See `cancel_job` in the MCP/Job
tools section below. The `jobs` table is created automatically via schema
bootstrap — there is no migration step.

## MCP reference

The MCP endpoint is mounted at `/mcp` whenever `server.enabled` and
`mcp.enabled` are both true. The transport is selected by `[mcp] transport`:

- `streamable_http` (default) — a single POST endpoint at `/mcp`.
- `sse` — the legacy two-endpoint protocol: `GET /mcp/sse` (event stream)
  and `POST /mcp/messages/` (client→server messages).

Browser-based MCP clients (e.g. a Chrome extension at `http://127.0.0.1`)
must have their origin listed in `[mcp] allowed_origins`; the MCP SDK's
transport-security layer otherwise rejects cross-origin requests with a
403 "Invalid Origin header". The default allows `http://127.0.0.1` and
`http://localhost`. Set `allowed_origins = []` to disable the origin check.

The tool set and the JSON parity guarantees are identical across both
transports; `sse` exists for clients that only speak the legacy protocol.
It exposes 24 tools:
**Read tools** — `list_meetings`, `get_meeting`, `list_tdocs`, `get_tdoc`,
`get_tdoc_content`, `list_tsgs`, `get_tsg`, `list_wis`, `list_specs`,
`get_spec`, `search_tdocs`, `semantic_search_tdocs`.

`get_tdoc` accepts `tdoc_id` (canonical id, e.g. `R5-260013`) and/or
`ftp_url` (a 3GPP FTP URL or relative path); when both are supplied
`ftp_url` wins and `tdoc_id` is ignored, and an invalid-params error
is raised only when neither is supplied. The URL mode surfaces every
row across the six URL-keyed tables whose `ftp_url` matches; auto-sync
is never triggered (no parent TDoc / meeting to anchor on). A
`tdoc_url_not_found` error is raised when the URL resolves to no rows.

**Job tools** — `sync_meetings`, `sync_tdocs`, `sync_tdocs_by_meeting`,
`sync_all_tdocs`, `sync_specs`, `parse_tdocs`, `parse_tdoc_url`,
`rebuild_search_index`, `purge_cache`, `get_job`, `cancel_job`, `list_jobs`.
`cancel_job` is idempotent on terminal jobs: cancelling a job that has
already reached SUCCEEDED / FAILED / CANCELLED returns the envelope
instead of erroring, so callers can inspect the result without a
separate `get_job` call.

Every read tool returns exactly the bytes of the equivalent
`?format=json` HTTP route. `search_tdocs` normalises the query into a
valid FTS5 `MATCH` expression exactly like the CLI and the `/search`
route (a stopwords-only or empty query raises an MCP invalid-params
error, `-32602`). `search_tdocs` also accepts an optional `sem_query` argument — when
provided, the FTS5 hits are reordered by cosine similarity to that
text, mirroring the `/search?sem=` route and the CLI's
`search query --sem-query`. The job tools enqueue the same work as the
HTTP `POST /jobs/...` routes, but the enqueue envelope adds a `message`
key (the only parity exception).

The MCP `serverInfo` block returned on every `initialize` handshake carries `name` ("doc3gpp"), `version` (from `importlib.metadata.version("doc3gpp")`, falling back to `doc3gpp.__version__`), `title`, `description`, and `website_url`. Clients do not need a tool call to read the version.

### Example MCP client (Python)

```python
import json
import urllib.request

# Streamable HTTP: POST a JSON-RPC message to /mcp
payload = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "list_meetings", "arguments": {"limit": 5}},
}
req = urllib.request.Request(
    "http://127.0.0.1:8765/mcp",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
)
with urllib.request.urlopen(req) as resp:
    body = json.loads(resp.read().decode())
print(body["result"]["content"][0]["text"])
```

## Logs, retention and cleanup

- The server appends to the log file (default `{cache.dir}/server.log`);
  `server.log_retention` (default `7d`) bounds how much is kept.
- `server.poll_interval_seconds` (default `1.0`, range `0.05..60.0`)
  governs how often the job worker checks the `jobs` table for new
  `QUEUED` rows. Lower values mean faster pickup after a
  `POST /jobs/...` lands; the 1-second default keeps the nav badge in
  lockstep with the SSE stream.
- `server.progress_interval_seconds` (default `10.0`, range `0.1..60.0`)
  is the minimum gap between the worker's periodic progress log lines
  for long-running jobs. It is independent of `poll_interval_seconds` —
  it only throttles progress emission, not job pickup.
- `server.cleanup_interval_seconds` (default `300`, minimum `10`)
  controls how often the worker prunes terminal rows older than
  `log_retention`. The two cadences are independent — flipping
  `cleanup_interval_seconds` does not change how quickly new jobs are
  picked up. (Earlier v1 conflated the two and produced 5-minute
  pickup delays for parse / sync / cache-purge requests; the bug was
  split into the dedicated `poll_interval_seconds` knob in
  [server settings](#config).)
- On startup the worker sweeps any `RUNNING` rows left behind by a
  crashed prior process and marks them `FAILED` with
  `error="orphaned_after_restart"` so the nav badge can't get stuck
  on a job the new process never claimed.
- The `/mcp` purge_cache job removes cached content under the cache
  subdirectory; it requires an explicit `yes: true` argument.

## Uninstallation

```bash
doc3gpp server uninstall systemd      # stop + disable + remove the unit
doc3gpp server stop                   # stop any background server
```

To disable the server entirely, set `[server] enabled = false` in
`doc3gpp.toml` — every `server` subcommand will then refuse to run.
