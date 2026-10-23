# Web server + MCP

> Last reviewed: 2026-08-13

The `doc3gpp` web server serves a browsable HTML interface over the same
services the CLI uses, plus an MCP (Model Context Protocol) endpoint for
AI tooling. It runs as a single process on one HTTP port
(`127.0.0.1:13999` by default).

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

# 2. Install a service unit (optional; systemd or launchd)
doc3gpp server install systemd --no-start    # Linux
doc3gpp server install launchd --no-start    # macOS

# 3. Start the server
doc3gpp server start        # opens your browser when ready
# or, for a supervised service:
doc3gpp server start --no-open

# 4. Browse
#    HTML:  http://127.0.0.1:13999/
#    MCP:   http://127.0.0.1:13999/mcp
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
enabled = true             # master switch; set false to disable server commands
host = "127.0.0.1"
port = 13999
max_concurrent_jobs = 3    # how many background jobs run at once
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

### Parse and sync concurrency

The web process owns one `PipelineRuntime` sized by `[pipeline]
prepare_workers` (default `min(32, cpu_count + 4)`, range 1..32). All queued
TDoc jobs (`parse_tdocs`, `parse_tdoc_url` files and FTP folders) run through
the same service runner: bounded parallel preparation plus one serialized
writer. `server.max_concurrent_jobs` only limits active jobs. HTTP and MCP
enqueue contracts and job envelopes are unchanged; folder results keep
deterministic discovery order.

Queued TDoc and spec parse/sync jobs use one shared process-wide pipeline.
Downloads, DOCX conversion, parsing, extraction, and chunking run in a
bounded preparation pool, while all database mutations and post-parse search
index updates run through one serialized persistence worker. The preparation
pool is bounded to avoid unbounded queued work, and the persistence lane is
shared across jobs rather than one writer per job.

Preparation does not write repositories. Cancelling a job prevents additional
preparation submissions; work already submitted to the writer is allowed to
finish before shutdown. CLI calls retain their synchronous behavior by
calling preparation and persistence consecutively. Search is independent of
the pipeline: SQLite uses WAL plus a busy timeout, and HTTP search routes run
blocking facade calls off the asyncio event loop, so search remains available
while a queued parse or sync is writing.

## CLI reference

`doc3gpp server` groups the server commands. Every subcommand starts with a
guard that refuses to run when `[server] enabled = false` (the server defaults
to enabled on loopback at `127.0.0.1:13999`).

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
| GET | `/tdocs` | List TDocs (`?format=json`; repeatable `fields` selects the HTML columns, see below; `parsed=true|false` keeps complete or incomplete extractions for each TDoc's type). |
| GET | `/tdocs/{id}` | TDoc show (HTML or JSON). |
| GET | `/tdocs/by-url/content` | Exact ZIP-URL content snapshot (`?ftp_url=<url>`, optional `source_file`, `format=html\|markdown\|json`; default `html`). Registered before parameterized content routes; no sync, download, or extraction. |
| GET | `/tdocs/{id}/content` | Stored TDoc content (`?format=html\|markdown\|json`, default `html`; optional exact `source_file`). SQL-first; no implicit sync, download, or extraction. |
| GET | `/tdocs/{id}/download` | Download the cached source zip (404 `cache_miss` with a hint when unparsed). |
| GET | `/tdocs/by-url` | URL-anchored TDoc show (`?ftp_url=<url>`; HTML or JSON). 404 when the URL matches no row in any of the six URL-keyed tables. |
| GET | `/tsgs` | List TSGs. |
| GET | `/tsgs/schema` | TSG field descriptors (HTML grouped by table; `?format=json` is the `doc3gpp tsg schema --format json` payload verbatim). No DB access. |
| GET | `/tsgs/{short_name}` | TSG detail. |
| GET | `/meetings/schema` | Meeting field descriptors (HTML grouped by table; `?format=json` is the `doc3gpp meeting schema --format json` payload verbatim). No DB access. |
| GET | `/tdocs/schema` | TDoc field descriptors across the ten `tdoc` tables (99 fields; HTML grouped by table; `?format=json` is the `doc3gpp tdoc schema --format json` payload verbatim). No DB access. |
| GET | `/wis/schema` | WI field descriptors (HTML grouped by table; `?format=json` is the `doc3gpp wi schema --format json` payload verbatim). No DB access. |
| GET | `/specs/schema` | Spec field descriptors across `specs` + `spec_versions` (HTML grouped by table; `?format=json` is the `doc3gpp spec schema --format json` payload verbatim). No DB access. |
| GET | `/testcases/schema` | Testcase field descriptors across the three testcase tables (HTML grouped by table; `?format=json` is the `doc3gpp testcase schema --format json` payload verbatim). No DB access. |
| GET | `/wis` | List WIs. |
| GET | `/specs` | List specs (`?format=json`; filters include `tsg,type,spec_id,title,status,radio_tech,initial_release,wis,rapporteurs,parsed,limit,offset`; `parsed=true|false` filters by parsed status before pagination; default JSON fields include `parsed`). |
| GET | `/testcases` | List testcases (`?format=json`; filters `testcase,title,ats,feature,release,wis,spec,group,status,gcf_status,limit,offset`; default limit 50, `_LIMIT_CAP=200`; unknown `group` → 400 `invalid_filter`). JSON is the bare row array byte-identical to `doc3gpp testcase list --format json` (nested `statuses` list of `{path, gcf_ptcrb, ttcn_status}` objects via `render.testcase_rows`; `null` preserved). |
| GET | `/specs/{spec_id}/docs` | Human-facing collapsed, version-first spec-document page (`?version=` required; optional `release`, `sections`, `tables`, `limit`, `offset`) with source state, parse/re-parse form, collapsed TOC, and paginated chunks (`spec_doc_show.html`; HTMX fragment `partials/spec_doc_show_results.html`). |
| GET | `/specs/{spec_id}/docs/toc` | Spec-doc TOC for one `(spec_id, version)` pair (`?version=` required, `?release=` informational; `?format=json` is the `doc3gpp spec doc toc show --format json` envelope verbatim: `{spec_id, version, release, docx_count, entries, files}`; full page `spec_doc_toc.html`, HTMX fragment `partials/spec_doc_toc_results.html`). Miss → 404. |
| GET | `/spec-docs/search` | Unified spec-doc search (`?text=`, `?semantic=`, filters `spec,release,version,sections,tables,limit,offset`; `limit` accepts 1 to 150, larger values return 400 `invalid_filter`; `?format=json` is the `doc3gpp spec doc search --format json` flattened result array verbatim; full page `spec_docs_search.html`, HTMX fragment `partials/spec_doc_search_results.html`). Text-only, semantic-only, both, and neither select FTS5, vector, hybrid, and filter-only modes. |
| GET | `/spec-docs/index` | Spec-doc FTS5/vector index status (`?format=json` returns the `IndexStatus` object). |
| GET | `/spec-docs/schema` | Spec-doc field descriptors across the three spec-doc tables (HTML grouped by table; `?format=json` is the `doc3gpp spec doc schema --format json` payload verbatim). No DB access. |
| GET | `/tdocs/search` | Unified TDoc search (`?text=`, `?semantic=`, filters, `limit` accepts 1 to 150 (larger values return 400 `invalid_filter`), `?format=json`). Text-only, semantic-only, both, and neither select FTS5, vector, hybrid, and filter-only modes. |
| GET | `/tdocs/index` | TDoc FTS5/vector index status (`?format=json` returns the `IndexStatus` object). |
| GET | `/jobs`, `/jobs/{id}` | List / show jobs. |
| GET | `/jobs/{id}/events` | SSE stream for a job. |
| POST | `/jobs/sync/meetings` | Enqueue `sync_meetings`. A `tsg` not present in the `tsgs` reference table is rejected (the job fails fast before any network work). |
| POST | `/jobs/sync/wis` | Enqueue `sync_wis` (`{"tsg": "R5"}`). The worker validates the TSG against the reference table before fetching WIs and returns the number of stored rows. |
| POST | `/jobs/sync/tdocs` | Enqueue `sync_tdocs` (by meeting id or name). |
| POST | `/jobs/sync/tdocs/all` | Enqueue `sync_all_tdocs`. |
| POST | `/jobs/sync/specs` | Enqueue `sync_specs` (exactly one of `tsg` / `spec_id`). A `tsg` not present in the `tsgs` reference table is rejected (the job fails fast before any network work). |
| POST | `/jobs/sync/testcases` | Enqueue `sync_testcases` (`{"force": bool}`, default `false`) → 202 job envelope. |
| POST | `/jobs/parse/tdocs` | Enqueue `parse_tdocs`. The filter map accepts the exact numeric `meeting_id` filter. Completed results include `requested`, `successes`, `failures`, and `skipped` counts plus per-TDoc `failure_details` and `skipped_details` maps. |
| POST | `/jobs/tdocs/index` | Enqueue unified TDoc FTS5/vector index maintenance. |
| POST | `/jobs/spec-docs/index` | Enqueue unified spec-doc FTS5/vector index maintenance. |
| POST | `/jobs/cache/purge` | Enqueue `cache_purge` (requires `yes: true`). |
| POST | `/jobs/parse/tdoc-url` | Enqueue `parse_tdoc_url` (a single TDoc file or folder on the canonical 3GPP FTP site; accepts HTTPS `https://www.3gpp.org/ftp/...` or bare `www.3gpp.org/ftp/...`, normalizes the latter, rejects explicit HTTP and non-TDoc spec archives, and permits an optional folder trailing slash; `recursive` XOR `max_depth`). Successful per-file summaries include `main_source_file` and ordered document IDs/source paths/file orders. |
| POST | `/jobs/parse/spec-docs` | Enqueue `parse_spec_docs` for one spec id (`spec_id`, optional `release`, `version`, and `force`); progress is streamed through the normal job SSE endpoint. This job is intentionally not a panel on the twelve-form `/sync` hub. |
| POST | `/jobs/{id}/cancel` | Cancel a queued/running job. Accepts `?format=html` to return the refreshed job row as an `outerHTML` swap target for the list page's per-row Cancel button. JSON otherwise. |
| POST | `/jobs/sync_tdocs` | Flat alias for `sync_tdocs` (form or JSON). |
| GET | `/sync` | Sync hub page: twelve enqueue forms (meetings, WIs, tdocs, all-tdocs, specs-by-tsg, specs-by-id, parse-tdocs, parse-tdoc-url, TDoc index maintenance, spec-document index maintenance, cache-purge, testcases) + a "Recent sync jobs" table. The WI form requires a blank-by-default TSG field and has no force option; the Parse TDocs form includes an exact numeric Meeting ID filter. |
| GET | `/sync?format=fragment` | Recent-jobs table fragment (wrapped in `<div id="recent-jobs">`) for HTMX `outerHTML` swap. |

### TDoc content and source selectors

`GET /tdocs/{tdoc_id}/content` accepts `format=html|markdown|json` (default
`html`) and an optional exact `source_file` query parameter. The
URL-anchored sibling `GET /tdocs/by-url/content?ftp_url=<url>` has the same
format/source options and is registered before parameterized TDoc routes.
`source_file` is the full case-preserving member path inside that ZIP, not a
local path; it is scoped to the selected TDoc or exact ZIP URL. An unknown
source is a not-found response, never a fallback to the main document.

The JSON envelope is shared with MCP and contains extraction metadata even
when one source is selected:

```json
{
  "tdoc_id": "R5-123456",
  "ftp_url": "R5/26.001/R5-123456.zip",
  "extraction_version": 2,
  "complete": true,
  "documents": [
    {"document_id": "…", "source_file": "CR/Main.docx", "is_main": true, "markdown": "…"},
    {"document_id": "…", "source_file": "Annex/Info.docx", "is_main": false, "markdown": "…"}
  ]
}
```

`complete` describes the stored extraction, not the number of documents in a
source-filtered response. Markdown returns one body unchanged or multiple
main-first blocks prefixed with `<!-- doc3gpp-source: <JSON source_file> -->`;
HTML renders distinct Main/Supporting document sections and exact-source
links. Content reads are SQL-first: the Markdown stored in the content tables
(`tdoc_cr_annexes`, `tdoc_wid_contents`, `tdoc_ls_contents`, and
`tdoc_discussion_contents`) remains available after Markdown-cache purge/eviction,
and cache eviction does not prevent an FTS5 rebuild. The one exception is the CR
main, which is read from the derived archive; when that archive is missing the CR
content read fails with the re-parse hint and no partial envelope. The ZIP and its
manifest under the cache are derived artifacts, not the content source of truth
for SQL-backed documents.

Legacy plain UTF-8, gzip, and single-member Markdown ZIP artifacts can be read
when their source is known, but are returned as version 1 with `complete=false`
and never upgraded to a complete version-2 extraction by cache decoding. HTTP
HTML/Markdown responses mark incomplete text with
`X-Doc3gpp-Extraction-Complete: false`; the JSON envelope carries
`"complete": false`. Inconsistent version-2 parent/document rows return an
incomplete-extraction error with a re-parse hint rather than falling back to a
stale cache artifact.

The MCP `get_tdoc_content` tool accepts exactly one of `tdoc_id` or `ftp_url`,
plus optional `source_file` and `format=markdown|html|json` (default
`markdown`). JSON uses the shared serializer and byte-matches the corresponding
HTTP response. MCP text-only requests for incomplete legacy content return an
explicit error directing the caller to JSON or re-parsing; warning text is not
prepended to the Markdown body. Neither HTTP nor MCP content reads trigger
sync, download, or conversion.

`parse_spec_docs` records the spec error in `result.failure_details` and the
job log. A failed parse job has a populated `error` and result; a successful or
skipped parse reports its corresponding one-spec result.

`parse_tdocs` preserves per-TDoc reasons in both its queued parallel pipeline
and synchronous fallback. Ordinary extraction errors are recorded in
`failure_details` as `ExceptionClass: message`; unavailable-on-FTP and
oversized-document errors are recorded in `skipped_details` with the same
format. Skipped/failed keys are the TDoc id (database mode) or normalized FTP URL
(folder mode). These item-level results do not change the existing job terminal-status
policy.

Append `?format=json` to any list/detail route to get the CLI-equivalent
JSON. Append `?format=html` (or omit) for the browsable HTML view.

The seven `/<resources>/schema` routes render the shared `schema.html`
template by default (one section per DB table: field, type,
nullable as `yes`/`no`, description, possible values) and return the
CLI JSON payload verbatim at `?format=json`; an HTMX request swaps
only the `partials/schema_results.html` fragment (`#results`). No DB
access on any schema route.

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

The spec list form has a `Parsed` control with `Any`, `true`, and `false`
choices. `GET /specs?parsed=true|false` uses the same case-insensitive
boolean filter; an omitted or empty value means `Any`, while another
non-empty value is rejected with HTTP 400. Parsed filtering happens before
`limit` / `offset` pagination. The status is derived from a non-null
`spec_doc_sources.parsed_at` in the separate specdata database, not from a
column in the main `specs` or `spec_versions` tables, so `/specs/schema` does
not add a `parsed` field.

Spec-list JSON returns `parsed` as a comma-separated numeric-newest-first
version string, or native JSON `null` when no version is parsed; the HTML
list displays `-` for the null state. Spec-show version JSON returns native
boolean `parsed` values for every version. These values match the CLI and MCP
outputs.

The spec detail page shows a Sync card with a Force sync checkbox that
enqueues a single-spec sync job for that spec; the page auto-refreshes
when the job completes. A "Per-version details" checkbox alongside
"Force sync" forwards `per_version_details=true` to the job so the
worker always re-fetches the ETSI PDF + CR-list follow-ups for every
version (default OFF — without it, cached rows are preserved).

### Spec-document portal

The human-facing spec-document flow is version-first. The `/specs/{spec_id}`
page has a `Docs` column with a `show` link for every stored version. Each
link opens `/specs/{spec_id}/docs?version={version}`; the page also accepts
`release`, `sections`, `tables`, `limit`, and `offset` query parameters for the
displayed document data. `sections` and `tables` filter the newline-delimited
combined identifier/title metadata stored on chunks; low-level DOCX blocks and
TOC entries continue to use `section_no` and table fields.

The version page's source card exposes three states: a version with no
specdata source row has not been downloaded or parsed; a downloaded source is
waiting for parsing; and a parsed source shows its parse timestamp, DOCX and
chunk counts. The only document action is `Parse document` (or `Force
re-parse`, available only for a parsed source. It enqueues the existing
`POST /jobs/parse/spec-docs` job with scalar `spec_id`, the selected exact
`version`, and `force`, then
polls the normal job status. The parser downloads a missing ZIP internally;
there is intentionally no standalone spec-document fetch route or fetch
control.

Parsed versions render the stored table of contents followed by individually
collapsed chunk cards. Chunk display is paginated through `limit` / `offset`,
using the filtered matching-chunk count rather than the source ledger's
unfiltered total. Pagination links preserve `version`, `release`, `sections`,
`tables`, and `limit`. The footer shows the visible range, first/previous/next/
last controls, and up to ten rolling numeric page links with ellipses. An
out-of-range offset clamps to the last page, while zero filtered matches omit
the pagination controls.
The search form uses a full-width Query field and exposes the same plural
`sections` / `tables` filters. The `TDoc Search` and `Spec Docs Search` tabs
are shared by both search families, and Spec Docs results link to the matching
version page and chunk anchor
(`/specs/{spec_id}/docs?version={version}#chunk-{chunk_index}`).

The spec-document corpus is not repaired at runtime. The pre-deployment local
corpus is repaired once from cached ZIPs using an internal maintenance helper;
there is no public repair command or migration path. New deployments use the
fresh specdata schema and the dedicated `~/.cache/doc3gpp/specs` cache root by
default, while TDoc extraction remains under `~/.cache/doc3gpp/tdocs`.

The sync hub (`/sync`) is a single page for enqueueing every sync-shaped job. Each panel submits a JSON body to the matching `/jobs/...` route via the shared `bindJobPolling` helper; when the job reaches a terminal state the bottom "Recent sync jobs" table is refreshed in place via HTMX (`GET /sync?format=fragment`) rather than a full page reload, so the user keeps their scroll position. The WI form (`id="sync-wis-form"`) submits its required TSG to `POST /jobs/sync/wis`; the worker (`_sync_wis`, `JobKind.SYNC_WIS = "sync_wis"`) validates the reference TSG, calls `services.wi.sync`, and returns `{"status","reason","synced_count"}`. The `tdoc-index-form` and `spec-doc-index-form` forms enqueue `POST /jobs/tdocs/index` and `POST /jobs/spec-docs/index`; the twelfth form (`id="testcase-form"`) enqueues `POST /jobs/sync/testcases` with `{force}` from its Force-sync checkbox (`sync_hub.js` `"testcase-form"` body builder). The Parse TDocs form's `Meeting ID` input is serialized as the numeric `meeting_id` filter. The index handlers run the unified FTS5/vector maintenance services, and the testcase handler (`_sync_testcases`, `JobKind.SYNC_TESTCASES = "sync_testcases"`) calls `services.testcase.sync(force=force, on_progress=...)` and returns `{"status","reason","synced_count"}`.

The testcase list page (`/testcases`) mirrors the spec list: an HTMX filter form (`partials/testcase_filters.html`) swaps the `#results` partial (`partials/testcase_results.html`) on `HX-Request: true`, otherwise the full `testcase_list.html` page renders. Columns are TC, Title, Spec, Group, Release, Statuses (as `path=gcf/ttcn` chips), each row linking to its group-scoped detail page (`/testcases/{id}?group={group}` → `testcase_show.html`: one header-card + status-triples table per group, with `path` / `gcf_ptcrb` / `ttcn_status`).

The header nav is ordered Home, TSGs, Meetings, TDocs, Specs, Testcases, WIs, Search, Jobs, Sync. Spec Docs is discoverable from the landing page and the shared search tabs rather than as a second top-level nav item.
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

The TDoc and spec-document search forms submit the same `text` and
`semantic` inputs as the CLI. Text-only, semantic-only, both, and neither
select FTS5, vector, hybrid, and filter-only modes. The TDoc form also carries
`tdoc-id`; spec-document search carries `spec`, `release`, `version`,
`sections`, and `tables` filters. Hybrid weighting is read from
`[semantic_search].fts5_weight`; there is no request-level weight or alternate
`/search/sem` route.

FTS5 and hybrid TDoc search can match Markdown in stored content documents (CR
annexes, and the main and annexes of WID, LS, and discussion TDocs), and each source
match links to its exact ZIP URL and URL-encoded `source_file`. Semantic content chunks
cover the same documents, capped per TDoc by `max_content_chunks_per_tdoc`. The CR main
body is not full-text indexed; its cover, change, and TTCN fields are.

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
when the Status column is hidden. `?format=json` returns the 11 default `tdoc list`
fields (including `parsed`) unless `fields` names others, and the MCP `list_tdocs`
tool always returns those 11. `document_count` is not in the default set; request
it with `fields`.

## Jobs

Long-running operations (meeting sync, TDoc sync, spec-document parse, TDoc search
index rebuild, and cache purge) run as background jobs. A job is a row in the SQLite `jobs`
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

The installed package includes a static AI-agent skill at
`doc3gpp/data/skills/doc3gpp/SKILL.md`. Give this file to an MCP-capable
agent for a compact sync → find → parse → search workflow covering meetings,
TDocs, specs, spec documents, and RAN5 testcases. It is reference material,
not an MCP tool.

The MCP endpoint is mounted at `/mcp` whenever `server.enabled` and
`mcp.enabled` are both true. The transport is selected by `[mcp] transport`:

- `streamable_http` (default) — a single POST endpoint at `/mcp`, answered
  with a plain `application/json` body (the mode every modern MCP client,
  including the TypeScript SDK, expects; the legacy SSE-streamed response
  is rejected by those clients with "Legacy MCP SSE endpoints are not
  supported").
- `sse` — the legacy two-endpoint protocol: `GET /mcp/sse` (event stream)
  and `POST /mcp/messages/` (client→server messages).

Browser-based MCP clients (e.g. a Chrome extension at `http://127.0.0.1`)
must have their origin listed in `[mcp] allowed_origins`; the MCP SDK's
transport-security layer otherwise rejects cross-origin requests with a
403 "Invalid Origin header". The default allows `http://127.0.0.1` and
`http://localhost`. Set `allowed_origins = []` to disable the origin check.

Chrome extensions send `Origin: chrome-extension://<extension-id>`, which
is never in the defaults — add the extension's id to `allowed_origins`
(e.g. `"chrome-extension://jfgfiigpkhlkbnfnbobbkinehhfdhndo"`). Some
clients surface the resulting 403 as the misleading "Legacy MCP SSE
endpoints are not supported" error; when you see that, check the server
log for an `Invalid Origin header` warning before touching the transport.

The tool set and the JSON parity guarantees are identical across both
transports; `sse` exists for clients that only speak the legacy protocol.
The unified search/index tools are `search_tdoc`, `search_spec_docs`,
`get_tdoc_index`, `index_tdocs`, `get_spec_doc_index`, and
`index_spec_docs`. Removed `/search/sem` routes and split semantic or
rebuild-only tool names are not current APIs.
**Read tools** — `list_meetings`, `get_meeting`, `list_tdocs`, `get_tdoc`,
`get_tdoc_content`, `list_tsgs`, `get_tsg`, `list_wis`, `list_specs`,
`get_spec`, `list_testcases`, `get_testcase`, `get_tsg_schema`,
`get_meeting_schema`, `get_tdoc_schema`, `get_wi_schema`,
`get_spec_schema`, `get_testcase_schema`, `search_tdoc`, `search_spec_docs`,
`get_tdoc_index`, `get_spec_toc`, `get_spec_doc_index`, and
`get_spec_doc_schema`.

`list_specs(..., parsed=None, limit=50, offset=0)` accepts an optional native
boolean `parsed` argument. `true` keeps specs with at least one parsed
version, `false` keeps specs with none, and `None` leaves the filter unset;
the service applies this filter before pagination. The list payload uses the
same native JSON `null` or comma-separated numeric-newest-first version
semantics as `GET /specs?format=json`. `get_spec` emits native boolean
`parsed` values on every version row. Both are derived from the separate
specdata `spec_doc_sources.parsed_at` ledger and are not main-table/schema
fields.

`get_tdoc` accepts `tdoc_id` (canonical id, e.g. `R5-260013`) and/or
`ftp_url` (a 3GPP FTP URL or relative path); when both are supplied
`ftp_url` wins and `tdoc_id` is ignored, and an invalid-params error
is raised only when neither is supplied. The URL mode surfaces every
row across the six URL-keyed tables whose `ftp_url` matches; auto-sync
is never triggered (no parent TDoc / meeting to anchor on). A
`tdoc_url_not_found` error is raised when the URL resolves to no rows.

**Job tools** — `sync_meetings`, `sync_tdocs`, `sync_tdocs_by_meeting`,
`sync_all_tdocs`, `sync_specs`, `parse_tdocs`, `parse_tdoc_url`,
`parse_spec_docs`, `sync_testcases`, `index_tdocs`, `index_spec_docs`, `purge_cache`,
`get_job`, `cancel_job`, `list_jobs`.
`cancel_job` is idempotent on terminal jobs: cancelling a job that has
already reached SUCCEEDED / FAILED / CANCELLED returns the envelope
instead of erroring, so callers can inspect the result without a
separate `get_job` call.

`list_testcases(testcase,title,ats,feature,release,wis,spec,group,status,gcf_status,limit=50,offset=0)`
calls `services.testcase.list_recent(...)` and returns
`_to_json(render.testcase_rows(rows, _TESTCASE_FIELDS))` where
`_TESTCASE_FIELDS = ["testcase_id","title","spec","group","release","statuses"]`
(`statuses` stays a nested list of `{path, gcf_ptcrb, ttcn_status}` objects, `null` preserved). `get_testcase(testcase_id, group=None)` returns
every stored group as an array of flat per-`(id, group)` objects with
nested `statuses` (single-element with `group` set, or when one group matches): with
`group` it calls `services.testcase.get(testcase_id, canonical)`; without
it calls `services.testcase.get_all(testcase_id)`; empty → raises
`TestcaseNotFoundError` (MCP `-32004`). Each payload's statuses go through
render.testcase_status_rows(...)}` over `["path","gcf_ptcrb","ttcn_status"]`
(an invalid `group` value is not pre-validated here — an unknown-canonical
group simply misses and raises `TestcaseNotFoundError`).
`sync_testcases(force=False)` enqueues `JobKind.SYNC_TESTCASES` with
`{"force": force}` and returns the `{job_id,status,message,links{self,events}}`
envelope.

The seven schema tools — `get_tsg_schema`, `get_meeting_schema`,
`get_tdoc_schema`, `get_wi_schema`, `get_spec_schema`,
`get_testcase_schema`, and `get_spec_doc_schema` — take no params and return
`_to_json(schema_payload(...))` for their resource: a bare array of
`{table, field, type, nullable, description, values}` rows
(`nullable` a JSON bool, `values` comma-joined or `"-"`),
byte-identical to the matching `GET /<resources>/schema?format=json`
route and the matching `doc3gpp <resource> schema --format json`
output.

Every read tool returns exactly the bytes of the equivalent
`?format=json` HTTP route. `search_tdoc` and `search_spec_docs` accept
optional `text` and `semantic` arguments and use the same four mode-selection
rules as the CLI and HTTP routes. Their `limit` accepts 1 to 150; larger values fail
parameter validation. Search rows are flattened, carry one
`score`, `search_mode`, and FTS5-only `previews`, and are returned in final
rank order. Every TDoc search row includes `matched_documents` as an array of
`{document_id, ftp_url, source_file, score, previews}` source matches. FTS5
and hybrid may populate it; semantic-only and filter-only rows always contain
`[]`. The top-level `ftp_url` remains the canonical parent URL. The job tools
enqueue the same work as the HTTP `POST /jobs/...`
routes, but the enqueue envelope adds a `message` key (the only parity
exception).

The spec-document tools use the separate specdata database. `get_spec_toc`
reads one parsed `(spec_id, version)` TOC, `search_spec_docs` selects among
FTS5, vector, hybrid, and filter-only chunk search, and `get_spec_doc_index`
reports index status. `parse_spec_docs` enqueues the same
`PARSE_SPEC_DOCS` job as `POST /jobs/parse/spec-docs`; it accepts one
`spec_id`, optional `release`/`version`, and `force`.

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
    "http://127.0.0.1:13999/mcp",
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
