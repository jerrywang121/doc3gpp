# Web server and HTTP API

The optional FastAPI/Uvicorn server provides a browser UI, JSON reads, durable
background jobs, and an MCP mount. **MCP connection and tool documentation lives
in [mcp.md](mcp.md)**. Settings live in [config.md](config.md); internal work,
skip rules, and pools live in [processes.md](processes.md).

## Install and run

```bash
uv tool install 'doc3gpp[cli,web,extract]'
doc3gpp config init --target user
doc3gpp db init --scope all
doc3gpp server start
```

If the CLI is already installed, reinstall it with the full set of desired
extras, for example `uv tool install --reinstall 'doc3gpp[cli,web,extract]'`.

The default is `http://127.0.0.1:13999/`. Server commands are enabled by default;
`[server] enabled = false` disables them. Add `[semantic]` and configure an
embedding endpoint if vector/hybrid search is needed, or install `[all]`.
Legacy Office conversion additionally requires LibreOffice.

Keep the server on a trusted local machine: **there is no authentication**.
Use one HTTP or stdio worker process per database; worker recovery does not
coordinate multiple processes. A non-loopback bind requires your own access
controls and does not automatically relax MCP host/origin restrictions.

`server.enabled` and `mcp.enabled` both default to true. The HTTP server is
gated by `server.enabled`, and its MCP mount additionally requires
`mcp.enabled`; the standalone `doc3gpp mcp` command checks only `mcp.enabled`.
The `mcp.transport` setting selects the HTTP transport only. See
[MCP connections](mcp.md#connect) for stdio installation and a client example.

### Process management

```bash
doc3gpp server start --no-open
doc3gpp server status
doc3gpp server logs
doc3gpp server stop
```

See [server commands](cli.md#server-commands) for all flags, reload/background
behavior, per-job logs, and systemd/launchd install/uninstall. `start` runs in
the background by default; `--reload` is the foreground development mode. Installed
services need the correct executable/environment, config discovery location,
and permissions; do not assume an interactive shell's working directory or
`PATH`. Restart after changing settings or LibreOffice availability.

By default the PID and server log files are under `cache.dir`. Job progress
logs are also persisted in the main database. `server.log_retention` removes
old terminal job records/logs from SQL; it is **not server.log rotation**.
Provide external log rotation if required.

## Browser UI

| Page | Use |
| --- | --- |
| `/` | Landing page and navigation |
| `/meetings` | Filter calendars, open a meeting, enqueue TDoc sync |
| `/tdocs` | Filter TDoc metadata and open detail/content |
| `/tdocs/search` | Unified text/vector/hybrid/filter-only TDoc search |
| `/spec-docs/search` | Unified specification-document chunk search |
| `/tsgs`, `/wis`, `/specs`, `/testcases` | Reference/corpus lists and available detail pages |
| `/sync` | Forms to enqueue sync, parse, index, and cache work |
| `/jobs` | Queue, progress, summaries, logs, cancellation |
| `/listserv`, `/listserv/show` | Search stored public LISTSERV messages; forms use existing LISTSERV endpoints and background jobs |

List/search filter forms use HTMX fragments when `HX-Request: true`; normal
navigation receives complete pages. Explicit `format=json` selects JSON on
routes that expose it. Raw upstream strings and inventory filenames are
autoescaped; Markdown rendering disables raw HTML. Source paths are encoded
in links rather than treated as filesystem paths.

`/listserv/show` uses the exact `url` query value. Opening LISTSERV pages does
not sync archives or parse messages.

### TDoc pages

`/tdocs/{tdoc_id}` composes metadata, cover/sidecars, extraction state,
auxiliary files, and ordered outer-ZIP inventory. Non-TTCN CRs can show
body-derived “Extracted changes”; TTCN CRs can show “Required changes”. The
cards depend on their sidecars, not just a generic parsed flag.

`/tdocs/by-url?ftp_url=...` reads the exact normalized URL snapshot, including
historical URLs and inventory-only records. It never syncs/downloads/parses.
Auxiliary `files`, outer `zip_files`, and converted `documents` are different
concepts. Unknown inventory is `null`, known empty inventory is `[]`.

Content routes read the shared `TDocContentService` snapshot. They default to
HTML and accept `format=markdown|html|json`; exact `source_file` selects a ZIP
member. JSON includes extraction completeness. Markdown/HTML can return partial
content with `X-Doc3gpp-Extraction-Complete: false`; unlike the
[MCP text formats](mcp.md#detail-and-content-contracts), HTTP does not reject an incomplete
extraction. Missing content returns a recovery hint rather than triggering
extraction. SQL stores annex/family content; CR main Markdown can be unavailable
after derived-cache eviction.

### Specification-document pages

`/specs/{spec_id}` links versions to `/specs/{spec_id}/docs?version=...`.
The version page shows downloaded/parsed state, outer-ZIP inventory, a collapsed
TOC, paginated/filterable chunks, and stored presentations in source order.
Parse/force-reparse actions enqueue jobs; opening a page does not fetch files.
Inventory sits outside the replaceable HTMX result fragment.

Full presentation links open `/specs/{spec_id}/docs/content` with `version`
and exact `source_file`. The route serves safe HTML or the shared JSON envelope
from SQL only. Spec search exposes `sections`/`tables` filters and links hits
to version/chunk anchors. Presentations have text chunks but no inferred TOC,
section, or table metadata.

## HTTP reads

Paths below are relative to the server origin. Where JSON is supported, append
`?format=json` (or `&format=json` to an existing query). Filters/selectors follow
their route signature; use `/openapi.json` or `/docs` for generated schemas.

| GET path | Purpose |
| --- | --- |
| `/healthz` | Liveness: `{"ok":true}`; not a full DB/upstream health check |
| `/meetings`, `/meetings/{meeting_id}` | Calendar rows/detail |
| `/tdocs`, `/tdocs/{tdoc_id}` | TDoc metadata/detail |
| `/tdocs/by-url?ftp_url=...` | Exact URL-keyed detail |
| `/tdocs/{tdoc_id}/content` | Stored content by parent ID |
| `/tdocs/by-url/content?ftp_url=...` | Stored content by exact URL |
| `/tdocs/{tdoc_id}/download` | TDoc download route; distinct from converted-content reads |
| `/tsgs`, `/tsgs/{short_name}` | TSG list/detail |
| `/wis` | Work items |
| `/specs`, `/specs/{spec_id}` | Spec headers/version detail |
| `/testcases`, `/testcases/{testcase_id}` | Headers with nested statuses; optional group selector |
| `/specs/{spec_id}/docs` | HTML version portal |
| `/specs/{spec_id}/docs/content` | Exact stored full presentation; version/source_file required |
| `/specs/{spec_id}/docs/toc` | Stored TOC |
| `/tdocs/search`, `/spec-docs/search` | Unified search |
| `/tdocs/index`, `/spec-docs/index` | Component index status |
| `/jobs`, `/jobs/{job_id}` | Job lists/detail |
| `/jobs/{job_id}/events` | SSE progress events |

Static schema routes are `/tsgs/schema`, `/meetings/schema`, `/tdocs/schema`,
`/wis/schema`, `/specs/schema`, `/testcases/schema`, `/spec-docs/schema`.
They read no database and expose the same schema payload as CLI/MCP.

Examples:

```bash
curl 'http://127.0.0.1:13999/tdocs?format=json&limit=10'
curl 'http://127.0.0.1:13999/tdocs/search?text=handover&format=json'
curl 'http://127.0.0.1:13999/specs/38.331?format=json'
```

Search chooses FTS5, semantic, hybrid, or filter-only from `text`/`semantic`.
Shared row contracts and source/chunk provenance are documented under
[querying](processes.md#querying). Matching HTTP JSON/MCP reads share compact
serialization and Unicode handling; HTML pages are not JSON envelopes.

## Enqueue work

POST a JSON body to create a durable job. Accepted enqueue requests return
**202**, including `job_id`, current status, and links to detail/events.

| POST path | Principal body fields |
| --- | --- |
| `/jobs/sync/meetings` | `tsg` |
| `/jobs/sync/wis` | `tsg` |
| `/jobs/sync/tdocs` | `meeting_id` or `meeting` |
| `/jobs/sync/tdocs/all` | No selector |
| `/jobs/sync/specs` | `tsg` or `spec_id`, optional `force`, `per_version_details` |
| `/jobs/sync/testcases` | Optional `force` |
| `/jobs/parse/tdocs` | `filter` dictionary, optional `force` |
| `/jobs/parse/tdoc-url` | `url`, optional `recursive`/`max_depth`, `force` |
| `/jobs/parse/spec-docs` | One `spec_id`, optional `release`, `version`, `force` |
| `/jobs/tdocs/index` | Rebuild flags, optional `batch`, `resume`, `stale_only` |
| `/jobs/spec-docs/index` | Same index maintenance selectors |
| `/jobs/cache/purge` | `scope` (`markdown`, `zips`, `all`) and mandatory `yes: true` |

`POST /jobs/sync_tdocs` is the meeting-page form alias. Browser forms may have
an adapter-specific submission path; API clients should use the JSON routes.
Consult generated OpenAPI for exact validation rather than treating this table
as a complete request schema.

```bash
curl -X POST 'http://127.0.0.1:13999/jobs/sync/meetings' \
  -H 'Content-Type: application/json' -d '{"tsg":"R5"}'
```

## Monitor and cancel

Poll `GET /jobs/{job_id}?format=json`, or consume the event stream with
`curl -N http://127.0.0.1:13999/jobs/<job_id>/events`. Terminal states are
`succeeded`, `failed`, `cancelled`; inspect summary counts and failure details.
The UI reuses the same queue and polling/event mechanisms.

`POST /jobs/{job_id}/cancel` persists queued cancellation immediately and
signals running work cooperatively. Terminal cancellation is idempotent and
returns the existing envelope. It is not a rollback of completed writes or
a hard kill of LibreOffice. See [job internals](processes.md#pools-and-background-jobs)
for throttling, bounded event queues, orphan recovery, and shutdown behavior.

## Errors and troubleshooting

Named errors return JSON with `error`, `detail`, and sometimes `hint`/`resource`.
Not-found/cache-miss responses use 404, invalid filters 400, invalid request
bodies may use FastAPI's 422, disabled/unavailable search 503, and upstream
HTTP failures 502. Unexpected errors return a correlation `request_id`; use
server logs to investigate without exposing internal exception details.

| Problem | Action |
| --- | --- |
| Missing table after upgrade | Run additive `db init` for the relevant scope |
| Old parsed version lacks inventory/presentation | Force reparse; initialization does not backfill |
| Job remains queued briefly | Pickup uses `poll_interval_seconds`, not cleanup cadence |
| Restart left a failed `orphaned_after_restart` job | Inspect partial effects and explicitly re-enqueue |
| Empty/old search results | Check parsed SQL content, enabled hooks, and component index status |
| Legacy Office conversion fails | Check Writer/Impress, service `PATH`, permissions, and timeout |
| MCP connection problem | Use [MCP troubleshooting](mcp.md#troubleshooting) |

Further deployment and storage caveats are in [known constraints](known-constraints.md).

## LISTSERV archive endpoints

These routes are mounted under `/api/listserv`. Reads use stored SQL and shared
SDK payload serializers; they do not sync, parse, or fetch ETSI content.

| Method and path | Inputs / response |
| --- | --- |
| `GET /api/listserv/lists` | `{"list_ids": [...]}`; distinct IDs found in stored message rows, not a static catalogue |
| `GET /api/listserv/search` | Query: repeatable `date`; `list_id`, `sender`, `subject`, `tdocs`, `text`, `semantic`, `limit` (default 20, 0–150), `offset` (default 0); response is a bare array of search payloads |
| `GET /api/listserv/show?url=...` | Required exact stored message detail URL; response is the full stored-record payload |
| `GET /api/listserv/index/status` | Independent FTS/vector `IndexStatus` JSON; no upstream request |
| `POST /api/listserv/sync` | JSON `{"list_id":"RAN-WG1","dates":[">= 2026-10-01"],"force":false}` |
| `POST /api/listserv/parse` | Same JSON fields; parse operates on already-synced messages |
| `POST /api/listserv/index` | `rebuild`, `rebuild_embeddings`, `rebuild_all`, optional positive `batch`, `resume`, `stale_only` |

The example ID is illustrative only; any valid public LISTSERV list ID can be
supplied. `dates` is a list of displayed-calendar-date comparisons; sync and
parse require one exact list ID. Accepted mutations return HTTP 202 with a
queued job envelope. Their job kinds are `listserv_sync`, `listserv_parse`, and
`listserv_index`. The general
[monitor/cancel API](#monitor-and-cancel) applies.

Sync/parse job results include `list_id`, `processed`, `skipped`, `failed`,
`index_failed`, `failures`, `index_failures`, and `message`. Source failures
and derived-index failures stay distinct. Either makes the job `failed`, but an
index failure never rolls back committed source data; inspect the summary and
rebuild a stale index if needed. Cancellation is cooperative, not rollback.
The standard job-detail JSON fields are `job_id`, `kind`, `status`, `params`,
`result`, `error`, `summary`, `log_tail`, `created_at`, `started_at`,
`completed_at`, and `links`; the HTTP enqueue response is the slimmer queued
envelope.

The shared show JSON fields are `url`, `list_id`, `subject`, `sender`, `date`,
`date_precision`, `week`, `tdocs`, `attachments`, `content`,
`content_truncated`, `parser_version`, and `parsed_at`. Search fields are those
message/detail fields except full `content`, plus `score`, `snippet`,
`matched_source`, `content_preview`, and `preview_truncated`. The `url` is the
exact detail URL and `tdocs` are the effective current subject/detail IDs.
Search exposes only a `content_preview` of at most 200 characters. The
`content_truncated` source-to-storage flag and `preview_truncated`
storage-to-preview flag are separate and nullable; both are null until a
message has been parsed. The default stored-body limit is 10,000 Unicode
characters; neither show nor search can recover text discarded before storage.
The CLI, HTTP, and MCP read surfaces use these same JSON payload builders. See
[sync/parse/index details](processes.md#listserv-archive-flow).

Examples:

```bash
curl 'http://127.0.0.1:13999/api/listserv/lists'
curl 'http://127.0.0.1:13999/api/listserv/search?list_id=RAN-WG1&date=%3E%3D%202026-10-01'
curl --get 'http://127.0.0.1:13999/api/listserv/show' \
  --data-urlencode 'url=https://list.etsi.org/scripts/wa.exe?A2=RAN-WG1;2026-10-15;EXAMPLE'
curl -X POST 'http://127.0.0.1:13999/api/listserv/sync' \
  -H 'Content-Type: application/json' \
  -d '{"list_id":"RAN-WG1","dates":[">= 2026-10-01","<= 2026-10-31"],"force":false}'
```

Search, status, and detail routes are SQL-only. Semantic/hybrid requests can
return an actionable unavailable error if embeddings/vector support is not
configured; filter-only and FTS search do not require vector support. The
browser UI is at `/listserv`; empty or whitespace-only optional search fields
are ignored in both HTTP search routes. Its list-ID search field accepts the
same rich filter syntax as the search API (`%`/`_` LIKE patterns, leading `!`
negation, `null`, and `not-null`). Stored list IDs appear as suggestions, and
an empty field searches all lists. Sync and parse forms still require one exact
list ID.
`/listserv/show` takes the exact `url` query value. Its forms reuse these
routes/jobs, and opening either page never syncs or parses. HTTP server
authentication and job-queue deployment limitations remain as described above.
