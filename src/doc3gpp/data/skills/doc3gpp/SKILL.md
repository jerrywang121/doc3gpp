---
name: doc3gpp
description: Use when an AI agent needs to find or refresh 3GPP meetings, TDocs, specifications, specification documents, or RAN5 testcases through the doc3gpp MCP server.
---

# Using doc3gpp MCP

doc3gpp exposes a local 3GPP database through MCP. Read cached rows first; enqueue a sync, parse, index, or cache-purge job when data is missing or stale. An enqueued job is not completed work.

## Connect and poll jobs

The default Streamable HTTP endpoint is `http://127.0.0.1:13999/mcp` (host and port may be configured). Install the `doc3gpp[web]` extra, enable `[server] enabled` and `[mcp] enabled`, then run `doc3gpp server start --no-open`. For the legacy SSE transport, connect to `/mcp/sse` instead. Tool names below are literal MCP names; the JSON objects are tool arguments, not HTTP bodies.

Read tools return one text item containing JSON, except `get_tdoc_content`, whose Markdown or HTML modes return content directly. Job-producing tools return compact JSON shaped like:

```json
{"job_id":"...","status":"queued","message":"...","links":{"self":"/jobs/...","events":"/jobs/.../events"}}
```

Poll `get_job({"job_id":"..."})` until `status` is `succeeded`, `failed`, or `cancelled`; inspect `result`, `error`, and `log_tail`. `cancel_job({"job_id":"..."})` is safe to repeat and returns the terminal envelope if the job already finished. A `queued` or `running` job is not evidence that rows are available. If a job fails, report the error rather than silently presenting old data as fresh.

## Read tools

- Meetings: `list_meetings(tsg,name,location,year,limit,offset)` and `get_meeting(meeting_id)`. Use `list_tsgs()` / `get_tsg(short_name)` for the TSG reference table and its recent meetings.
- TDocs: `list_tdocs(...)` supports the meeting, ID, title, type, status, CR, spec, WI, release/version, source, auxiliary-file, date, and `parsed` filters; `meeting_id` is an exact numeric filter. `get_tdoc(tdoc_id=...)` returns metadata by ID. `get_tdoc(ftp_url=...)` normalizes full or bare 3GPP FTP paths, does not auto-sync, and returns the URL-anchored record. If both selectors are supplied, `ftp_url` wins.
- TDoc content: `get_tdoc_content` requires exactly one of `tdoc_id` or `ftp_url`. `format` is `markdown` (default), `html`, or `json`; `source_file` selects one exact full ZIP member path. Markdown/HTML require complete extraction; use JSON to inspect completion metadata or re-parse after a cache-miss error.
- WIs: `list_wis(tsg,name,acronym,release,limit)`.
- Specs: `list_specs(...)` and `get_spec(spec_id,limit,offset,version,no_wis_crs)`. `parsed` is transient status derived from the separate specdata ledger and is applied before pagination.
- Spec documents: `get_spec_toc(spec_id,version,release)` reads an already parsed exact pair; `release` is informational. `search_spec_docs(text,semantic,spec_id,release,version,sections,tables,limit,offset)` searches chunks.
- Testcases: `list_testcases(testcase,title,ats,feature,release,wis,spec,group,status,gcf_status,limit,offset)` and `get_testcase(testcase_id,group)`. Omit `group` for every stored group; supply it for one group. Groups are exactly `5G`, `LTE`, `IMS`, `UTRA`, `POS`, or `MCX`; statuses are nested `{path,gcf_ptcrb,ttcn_status}` objects.
- Schemas: the no-argument `get_tsg_schema`, `get_meeting_schema`, `get_tdoc_schema`, `get_wi_schema`, `get_spec_schema`, `get_testcase_schema`, and `get_spec_doc_schema` return the same JSON schema rows as the corresponding CLI/HTTP schema surfaces.
- Index status: `get_tdoc_index()` and `get_spec_doc_index()` report FTS5/vector index state.
- Jobs: `list_jobs(status,limit,offset)` lists jobs; `get_job(job_id)` reads one; `cancel_job(job_id)` requests cancellation.

List tools default to 50 rows (where they expose a limit); use `limit` and `offset` to page. Rich text filters generally accept SQL-LIKE `%` wildcards, while testcase `group` is exact. Search limits are 1–150. `search_tdoc(text,semantic,tsg,meeting,meeting_id,tdoc_id,release,spec,since,until,limit,snippet_tokens)` and `search_spec_docs(...)` select FTS5 with `text`, vector search with `semantic`, hybrid with both, and filter-only with neither. TDoc `snippet_tokens` is 1–64. Semantic modes require a configured embedding service.

## Job tools

- `sync_meetings({"tsg":"R5"})` syncs one calendar. `sync_tdocs({"meeting_id":260013})` requires a numeric meeting ID; `sync_tdocs_by_meeting({"meeting":"RAN5#106"})` requires a non-empty meeting name; `sync_all_tdocs({})` syncs every tracked meeting.
- `sync_specs` requires exactly one of `tsg` or `spec_id`; `force` bypasses the per-spec interval and `per_version_details` fetches ETSI PDF/CR-list follow-ups.
- `parse_tdocs` requires a non-empty `filter` object. Supported keys include `tdoc_id`, `meeting`, `meeting_id`, `status`, `cr_cat`, `spec`, `wi`, `revision_of`, `revised_to`, `title`, `ftp_url`, `source`, `tdoc_type`, `uploaded_date`, `release`, `version`, `cr_num`, and `cr_pack`; `force` re-parses existing rows and `max_batch` caps the job.
- `parse_tdoc_url({"url":"https://www.3gpp.org/ftp/..."})` accepts a canonical HTTPS or bare `www.3gpp.org/ftp/...` file/folder URL; explicit HTTP is rejected. Use `recursive:true` for BFS through subfolders or `max_depth` for a bounded scan. `recursive:true` cannot be combined with a non-default `max_depth`; `force` re-parses cached files.
- `sync_testcases({"force":true})` re-downloads and re-parses the latest RAN5 status file.
- `parse_spec_docs({"spec_id":"38.331","release":"Rel-18","version":"18.5.0","force":false})` parses one spec-document version. Omit `version` to resolve a suitable stored ZIP-backed version; `release` and `version`, when both supplied, must agree.
- `index_tdocs` and `index_spec_docs` accept `rebuild`, `rebuild_embeddings`, `rebuild_all`, `batch`, `stale_only`, and `resume`. `rebuild_all` is mutually exclusive with the individual rebuild flags; `batch` must be positive. Use the matching index-status tool afterward.
- `purge_cache({"scope":"markdown","yes":true})` enqueues cache deletion. `scope` is `markdown`, `zips`, or `all`; `yes:true` is required.

## Example: meeting → TDoc → spec-document search

After **each** job-producing call, poll `get_job` to a terminal status before using the result.

```text
sync_meetings       {"tsg":"R5"}
get_job             {"job_id":"<meeting-sync job_id>"}
list_meetings       {"tsg":"R5","name":"%RAN5%","limit":10}
get_meeting         {"meeting_id":<meeting_id>}
sync_tdocs          {"meeting_id":<meeting_id>}
get_job             {"job_id":"<tdoc-sync job_id>"}
list_tdocs          {"meeting_id":<meeting_id>,"title":"%handover%"}
parse_tdocs         {"filter":{"tdoc_id":"<tdoc_id>"}}
get_job             {"job_id":"<tdoc-parse job_id>"}
get_tdoc            {"tdoc_id":"<tdoc_id>"}
search_tdoc         {"text":"handover","meeting_id":<meeting_id>}
sync_specs          {"spec_id":"38.331"}
get_job             {"job_id":"<spec-sync job_id>"}
get_spec            {"spec_id":"38.331"}
parse_spec_docs     {"spec_id":"38.331","version":"<ZIP-backed version>"}
get_job             {"job_id":"<spec-doc-parse job_id>"}
get_spec_toc        {"spec_id":"38.331","version":"<parsed version>"}
search_spec_docs    {"text":"handover","spec_id":"38.331","version":"<parsed version>","sections":"5.2.2.3%"}
```

For a testcase question, use `sync_testcases({})`, poll `get_job`, then call `list_testcases` (the list filter is `testcase`) and `get_testcase` (the detail selector is `testcase_id`). A sync may be skipped when the latest source was already parsed; inspect the job result before interpreting an empty list. TDoc sync needs a stored meeting, and spec-document parsing needs a stored spec/version with a ZIP link. Do not infer document contents or testcase status from a title alone.
