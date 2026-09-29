---
name: doc3gpp
description: Use when an AI agent needs to find or refresh 3GPP meetings, TDocs, specifications, or specification-document content through the doc3gpp MCP server.
---

# Using doc3gpp MCP

doc3gpp exposes a local 3GPP database through MCP. Use read tools to inspect cached data; enqueue sync or parse jobs when the data is missing or stale. Do not treat an enqueued job as completed work.

## Connect and run jobs

Connect an MCP client to the app's default Streamable HTTP endpoint `http://127.0.0.1:13999/mcp` (the host/port may be configured). The server requires the `doc3gpp[web]` extra and both `[server] enabled` and `[mcp] enabled`; `doc3gpp server start --no-open` starts it. If configured for legacy SSE, connect to `/mcp/sse` instead. The MCP tool names below are literal; the JSON objects are tool **arguments**, not HTTP request bodies.

Read tools return a single text item containing JSON (except `get_tdoc_content`, which returns the content itself). Parse that JSON before using IDs, versions, or links. Sync, parse, and index tools return `{job_id, status, message, links}`. Call `get_job({"job_id":"..."})` until `status` is `succeeded`, `failed`, or `cancelled`; inspect `result`, `error`, and `log_tail`. A `queued` or `running` job is not evidence that rows are available. If a job fails, report the error rather than silently presenting old cached data as fresh.

## Workflow by resource

| Goal | Find existing rows | Refresh / parse | Inspect or search |
| --- | --- | --- | --- |
| Meeting calendar | `list_meetings` with `tsg`, `name`, or `year`; `get_meeting` with numeric `meeting_id` | `sync_meetings({"tsg":"R5"})` fetches and parses that TSG's calendar | Re-list, then use `get_meeting` to obtain its meeting ID and FTP URL. There is no separate meeting-parse tool. |
| Meeting TDocs | `list_tdocs` with `meeting_id`, `tdoc_id`, `title`, `spec`, etc.; `get_tdoc` with `tdoc_id` or `ftp_url` | `sync_tdocs({"meeting_id":260013})` after the meeting exists; alternatively `sync_tdocs_by_meeting({"meeting":"RAN5#106"})`. For document extraction, `parse_tdocs({"filter":{"tdoc_id":"R5-260013"}})`; for a direct 3GPP FTP URL use `parse_tdoc_url({"url":"https://www.3gpp.org/ftp/..."})`. | `get_tdoc` for cover/extract metadata; `get_tdoc_content({"tdoc_id":"R5-260013"})` for cached markdown if available; `search_tdoc` for indexed content and filters. |
| Specifications | `list_specs` with `tsg`, `spec_id`, `title`, or `parsed`; `get_spec` with dotted `spec_id` | `sync_specs({"tsg":"R5"})` or `sync_specs({"spec_id":"38.331"})` (exactly one selector). `force` bypasses the sync interval; `per_version_details` fetches extra PDF/CR-list details. | `get_spec` returns versions, release markers, ZIP URLs, and per-version `parsed` state. Paginate versions with `limit`/`offset` if needed. |
| Specification documents | Start with `get_spec` to identify a version with a ZIP URL; `get_spec_toc` needs an already parsed exact `(spec_id, version)` | `parse_spec_docs({"spec_ids":["38.331"],"version":"18.5.0"})` downloads if needed and parses; omit `version` to resolve a suitable stored version (optionally constrain with `release`). `force` re-downloads and re-parses. | `get_spec_toc({"spec_id":"38.331","version":"18.5.0"})`; `search_spec_docs` with `spec_id`, `version`, `text` or `semantic`, `sections`, `tables`. |

Filters on list/search tools generally accept SQL-LIKE `%` wildcards (for example `title:"%handover%"`); plain values match exactly. `list_meetings`, `list_tdocs`, and `list_specs` default to 50 rows: use `limit` and `offset` to page. `search_tdoc` and `search_spec_docs` select FTS5 with `text`, vector search with `semantic`, hybrid with both, and filter-only with neither. Semantic search requires its configured embedding service. If text search reports an unavailable or corrupt index, inspect `get_tdoc_index` or `get_spec_doc_index`; use `index_tdocs({"rebuild":true})` or `index_spec_docs({"rebuild":true})` when rebuilding is needed, then poll that job. Parse normally attempts automatic indexing, but indexing can fail independently.

## Example: investigate a meeting and its spec text

These are successive MCP tool calls. Replace example IDs with values returned by earlier reads; after **each** job-producing call, poll `get_job` to `succeeded` before proceeding.

```text
sync_meetings       {"tsg":"R5"}
get_job             {"job_id":"<meeting-sync job_id>"}  # poll until terminal
list_meetings       {"tsg":"R5","name":"%RAN5%","limit":10}
get_meeting         {"meeting_id":<meeting_id from list_meetings>}
sync_tdocs          {"meeting_id":<meeting_id>}
get_job             {"job_id":"<tdoc-sync job_id>"}     # poll until terminal
list_tdocs          {"meeting_id":<meeting_id>,"title":"%handover%"}
parse_tdocs         {"filter":{"tdoc_id":"<tdoc_id from list_tdocs>"}}
get_job             {"job_id":"<tdoc-parse job_id>"}    # poll until terminal
get_tdoc            {"tdoc_id":"<tdoc_id>"}
search_tdoc         {"text":"handover","meeting_id":<meeting_id>}
sync_specs          {"spec_id":"38.331"}
get_job             {"job_id":"<spec-sync job_id>"}    # poll until terminal
get_spec            {"spec_id":"38.331"}
parse_spec_docs     {"spec_ids":["38.331"],"version":"<ZIP-backed version from get_spec>"}
get_job             {"job_id":"<spec-doc-parse job_id>"} # poll until terminal
get_spec_toc        {"spec_id":"38.331","version":"<parsed version>"}
search_spec_docs    {"text":"handover","spec_id":"38.331","version":"<parsed version>"}
```

If a list is empty, confirm the upstream sync job succeeded and that the filters match the returned IDs. TDoc sync needs a stored meeting; spec-doc parsing needs a stored spec/version with a ZIP link. A TOC or body-content cache miss means the relevant document has not been parsed into that cache yet. Use the returned search hits and source/version identifiers when answering; do not infer document contents from a title alone.
