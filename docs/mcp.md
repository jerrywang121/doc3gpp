# MCP integration

doc3gpp exposes Model Context Protocol tools through a standalone stdio command
and the optional local web server. For HTTP operation see
[web-server.md](web-server.md); for settings see
[config.md](config.md#pools-server-and-mcp). Both MCP surfaces share the same
tool registry and reuse services and JSON payload builders from HTTP/CLI adapters.

## Connect

For a fresh installation, install both the CLI and web/MCP dependencies:

```bash
uv tool install 'doc3gpp[cli,web]'
doc3gpp db init --scope all
```

If the CLI is already installed, reinstall it with the full set of desired
extras, for example `uv tool install --reinstall 'doc3gpp[cli,web]'`. Both
`server.enabled` and `mcp.enabled` default to `true`.

### Local stdio client

Run `doc3gpp mcp` as a foreground subprocess in the working directory whose
configuration and databases you want to use. It binds no HTTP port. Only
`mcp.enabled` gates this command; `server.enabled` may be false. The
`mcp.transport` setting selects the HTTP transport only and does not affect
stdio. Stdout is reserved for MCP protocol messages; diagnostics go to stderr.

For example, a compatible local client can launch:

```json
{
  "mcpServers": {
    "doc3gpp": {"command": "doc3gpp", "args": ["mcp"]}
  }
}
```

Client configuration keys vary; use the format required by your client and
ensure its environment, working directory, and `PATH` find the intended config
and executable. On client EOF, cleanup is attempted; shutdown is not guaranteed
to be prompt. The command starts a background job worker: concurrent HTTP or
stdio worker processes sharing a database can interfere with job ownership and
recovery. See [runtime constraints](known-constraints.md#pools-jobs-and-deployment).

### HTTP clients

Start the HTTP server with `doc3gpp server start`.

Configure an HTTP-capable MCP client with:

| Transport | URL | Notes |
| --- | --- | --- |
| Streamable HTTP (default) | `http://127.0.0.1:13999/mcp/` | Single MCP endpoint; POST responses use `application/json` |
| Legacy SSE | `http://127.0.0.1:13999/mcp/sse` | Only with `[mcp] transport = "sse"`; messages use `/mcp/messages/` |

Use the trailing slash on the default mount to avoid redirects. HTTP transport
selection is server-wide; restart after changing it. The MCP mount
is enabled only when both `server.enabled` and `mcp.enabled` are true and the
MCP dependency loads successfully.

Client configuration keys vary by application: select its Streamable HTTP
transport and the URL above rather than assuming a universal JSON schema.
Use `tools/list` after initialization for the exact current input schemas,
defaults, and supported filter keys.

### Python client example

With a recent MCP Python SDK (`streamable_http_client` spelling):

```python
import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    async with streamable_http_client("http://127.0.0.1:13999/mcp/") as (
        read, write, _
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_skill", {})
            for block in result.content:
                if block.type == "text":
                    print(block.text)


asyncio.run(main())
```

Use an SDK to handle initialization, protocol headers, and session lifecycle;
a naked JSON-RPC `tools/call` request is not a complete connection example.
See the [SDK client guide](https://github.com/modelcontextprotocol/python-sdk/blob/v1.x/docs/client.md)
if an older installed SDK uses the legacy `streamablehttp_client` name.

## Tool catalog

The authoritative registration is
[`web/mcp_server.py`](../src/doc3gpp/web/mcp_server.py).

| Area | Read tools |
| --- | --- |
| Agent instructions | `get_skill` |
| Meetings | `list_meetings`, `get_meeting` |
| TDocs | `list_tdocs`, `get_tdoc`, `get_tdoc_content`, `search_tdoc`, `get_tdoc_index` |
| TSGs / work items | `list_tsgs`, `get_tsg`, `list_wis` |
| Specifications | `list_specs`, `get_spec` |
| Spec documents | `get_spec_toc`, `get_spec_doc_content`, `search_spec_docs`, `get_spec_doc_index` |
| Testcases | `list_testcases`, `get_testcase` |
| Jobs | `get_job`, `list_jobs` |
| Schema | `get_tsg_schema`, `get_meeting_schema`, `get_tdoc_schema`, `get_wi_schema`, `get_spec_schema`, `get_testcase_schema`, `get_spec_doc_schema` |

| Work | Enqueue / control tools |
| --- | --- |
| Sync | `sync_meetings`, `sync_tdocs`, `sync_tdocs_by_meeting`, `sync_all_tdocs`, `sync_specs`, `sync_testcases` |
| Extract | `parse_tdocs`, `parse_tdoc_url`, `parse_spec_docs` |
| Maintain indexes | `index_tdocs`, `index_spec_docs` |
| Purge / cancel | `purge_cache`, `cancel_job` |

Do not infer a tool from an HTTP route: for example HTTP exposes WI sync, but
there is currently no `sync_wis` MCP tool. Tool names are intentionally not all
plural (`search_tdoc` is singular).

## Recommended agent workflow

1. Call **`get_skill`** for the full packaged Markdown skill, including its
   frontmatter. It takes no arguments, performs no DB read, and has no HTTP
   counterpart.
2. Inspect relevant schema tools. These read the static schema registry without
   database access; they describe SQL columns, not every transient output field.
3. Search/filter metadata to find a TDoc or spec version, then inspect its detail.
4. Read stored full content using the exact `source_file` returned by results or
   inventory/content metadata. Inventory lists all outer ZIP files, not only
   files with converted Markdown.
5. If content is absent or incomplete, explicitly enqueue parsing and monitor
   the job. Re-read after successful completion.

Content reads are snapshots: they do not sync, download, or parse. The CLI's
auto-sync and ID-based raw-extraction convenience do not apply to MCP reads.
See [process flows](processes.md) for eligibility, skip rules, and cache recovery.

### Detail and content contracts

- `get_tdoc(tdoc_id=...)` reads the current parent; `get_tdoc(ftp_url=...)`
  composes the exact normalized URL snapshot, including historical URLs.
  If both are supplied, `ftp_url` takes precedence. The CLI detail selector
  instead requires exactly one.
- `get_tdoc_content` requires **exactly one** of `tdoc_id` or `ftp_url`.
  Optional `source_file` is an exact full member path scoped to that ZIP.
  Formats are `markdown` (default), `html`, `json`; HTTP defaults to HTML.
- Content JSON can describe incomplete extraction and skipped sources.
  Markdown/HTML require complete content and otherwise return a cache-miss
  error with a force-parse hint. CR main Markdown depends on its derived cache;
  annex/family content and complete selected presentations are read from SQL.
- `get_spec_doc_content(spec_id, version, source_file)` returns the shared JSON
  envelope containing full stored presentation Markdown. It reads SQL only.
  Word spec content is accessed through TOC/chunks, not this presentation tool.
- `get_testcase` without `group` returns an array across all stored groups;
  each header has a nested status array, preserving native JSON nulls.
- `get_spec` versions expose `zip_files` last: `null` for unknown, `[]` for
  known empty, otherwise ordered names preserving duplicates. Parsed status
  and inventory come from the separate specdata database.

### Search contracts

`search_tdoc` and `search_spec_docs` accept optional `text` and `semantic`.
Text only selects FTS5, semantic only vector search, both hybrid RRF, and
neither filter-only SQL. Results use the shared flattened rows, final rank
order, `search_mode`, mode-specific `score` (or `null`), and FTS-only previews.

TDoc rows always include `matched_documents`, with source identity and verified
`matched_chunk` or `null`. Semantic-only source hits are supported; a whole-file
FTS match does not imply a particular chunk satisfied the entire expression.
See [querying](processes.md#querying) for provenance and fusion rules.
Search pagination validates `limit` in `1..150`, nonnegative `offset`, and
optional `snippet_tokens` in `1..64` where exposed.

Matching JSON reads byte-match their HTTP `format=json` counterparts, using
compact separators and Unicode-preserving serialization. The MCP protocol
still wraps the returned string in a tool result; compare the text payload,
not the entire protocol envelope. `get_skill`, rendered HTML/Markdown, and
enqueue messages are distinct contracts, not generic JSON-parity promises.

## Jobs

Mutation tools enqueue durable work rather than blocking on downloads/parsing.
The returned JSON includes `job_id`, `status`, a message, and links to the job
and its SSE events. Poll `get_job(job_id)` until `succeeded`, `failed`, or
`cancelled`; inspect summary counts and failure details as well as status.

- `parse_tdocs` takes a supported filter dictionary and optional force flag;
  the registry eligibility and batch/completeness rules still apply.
- `parse_tdoc_url` accepts canonical 3GPP FTP files/folders, not arbitrary local
  paths or arbitrary remote URLs. Depth/recursive selectors follow its schema.
- `parse_spec_docs` takes one scalar `spec_id` plus optional release/version/force.
- `sync_specs` accepts TSG or spec ID; inspect its schema for selector rules.
- `purge_cache` requires `yes=true`; scope is `markdown` (default), `zips`, or
  `all`. It purges the TDoc cache, not SQL or the separate spec cache.
- `cancel_job` is idempotent on terminal jobs and cooperative on running work.

Job retention, resource limits, cancellation boundaries, and startup orphan
handling are detailed in [pools and jobs](processes.md#pools-and-background-jobs).

## Security

There is **no built-in authentication or authorization**. Tool access includes
network syncs, parsing, index rebuilds, cache purge, and job cancellation.
Keep the server bound to loopback or add a trusted authenticated gateway.

The MCP SDK checks hosts (`127.0.0.1:*`, `localhost:*`) and the configured
`allowed_origins`. An Origin allowlist is not user authentication or general
HTTP CORS configuration. For a browser/extension client, add its exact origin
to the existing allowed values; do not disable checking as a routine fix.
Binding the HTTP server to another host does not expand the MCP host allowlist.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `/mcp/` missing | Install `[web]`, check both enabled flags and server startup warnings |
| Stdio command refuses to start | Check `mcp.enabled`, the executable/environment, and stderr for dependency hints |
| “Legacy MCP SSE endpoints are not supported” | Use `streamable_http`, restart, point the client at `/mcp/`; default POST responses are plain JSON |
| Client requests `/mcp/sse` but gets 404 | Transport mismatch; use the default URL or explicitly enable legacy SSE |
| Origin/host rejected | Inspect the actual client Origin/Host and MCP security policy; this is separate from network binding |
| Tool succeeds but work is not finished | The result is a job envelope; poll it |
| Content absent after sync | Metadata sync does not extract bodies; enqueue parsing |
| CR main unavailable after purge | Force reparse to restore the derived archive |
| Semantic search unavailable | Check extras, embedding URL/model, remote service, and vector index |
| Content/error payload differs from expectation | Refresh `tools/list`; inspect `format`, selectors, completion state, and current schema |

Mapped errors include not-found `-32004`, cache-miss `-32005`, too-large
`-32006`, invalid-parameter `-32602`, and internal/unavailable `-32603`.
Where supplied, error data includes the resource/detail and a recovery hint.
Some SDK validation/tool errors have their own wrapper; consult the returned
error rather than treating every failed call as transport failure.
