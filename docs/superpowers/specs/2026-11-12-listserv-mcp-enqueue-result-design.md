# LISTSERV MCP enqueue response alignment

## Status

- Conversational design: approved by the user.
- Written spec: approved by the user.
- Implementation: complete and verified; no product-code changes are pending.

## Purpose

Make LISTSERV MCP enqueue tools return the same JSON response shape as the
other MCP tools that enqueue background jobs. Today, the general MCP enqueue
path returns `job_id`, `status`, `message`, and `links`, while LISTSERV MCP
tools return `job_id`, `status`, and `url`. Both paths use the same job queue;
the difference is only in response formatting.

The aligned MCP response will have exactly these top-level keys:

```json
{
  "job_id": "...",
  "status": "queued",
  "message": "queued listserv_sync",
  "links": {
    "self": "/jobs/...",
    "events": "/jobs/.../events"
  }
}
```

Use concise, operation-specific queue messages consistent with existing MCP
wording:

| MCP tool | `message` |
| --- | --- |
| `listserv_sync` | `queued listserv_sync` |
| `listserv_parse` | `queued listserv_parse` |
| `listserv_index_rebuild` | `queued listserv_index` |

The `url` key will be removed; the job detail and event paths are represented
by `links.self` and `links.events`.

## User intent and constraints

- Align `listserv_sync`, `listserv_parse`, and `listserv_index_rebuild` MCP
  responses exactly with the standard MCP enqueue response shape.
- Drop `url`; do not retain it as an alias or extra field.
- Keep scope to MCP responses. The HTTP LISTSERV routes under
  `/api/listserv/{sync,parse,index}` retain their current response contract.
- Preserve existing validation, normalized job parameters, job kinds, queue
  behavior, status values, and `get_job` behavior.
- MCP stdio and HTTP MCP share the registered tools, so both receive the same
  aligned response.
- No database schema, worker, lifecycle, or dependency changes.
- Do not auto-commit.

## Design

### Shared response formatting

Factor the existing response construction in `src/doc3gpp/web/mcp_server.py`
into `_job_result(job, message)`, a helper that accepts an already-created job
and a message. It will serialize `job_id`, `status`, `message`, and the existing
`self` and `events` links through `_to_json`.

The general `_enqueue` helper will continue to create a job once, then delegate
formatting to the shared helper. Each LISTSERV MCP tool will continue to use its
existing `enqueue_listserv_sync`, `enqueue_listserv_parse`, or
`enqueue_listserv_index` function exactly once, then pass the returned job to
the same formatter with its job-kind message. This avoids duplicate job
creation and leaves LISTSERV validation/enqueue logic unchanged.

The resulting fields and link construction must match `_enqueue` exactly.
Remove `_listserv_job_result`; all MCP enqueue tools will use `_job_result` so
there is only one MCP job-response shape.

### Documentation

Update `docs/mcp.md` to document the uniform enqueue fields and remove the
statement that LISTSERV MCP tools have a distinct `job_id`/`status`/`url`
contract. Do not change HTTP documentation or response serializers, because
HTTP LISTSERV responses are explicitly out of scope.

## Verification

- Extend the focused LISTSERV MCP integration test to assert, for all three
  enqueue tools, the exact top-level key set, exact `links` key set, expected
  job-kind message, absence of `url`, and correct `self`/`events` paths.
- Keep assertions for job ID, queued status, job kind, and parameters to prove
  that only response formatting changed and each tool still creates its
  expected job.
- Verify a non-LISTSERV MCP enqueue response remains unchanged after sharing
  the formatter.
- Run the focused LISTSERV MCP integration tests and relevant lint checks.
- Review `docs/mcp.md` for consistency with the implementation and validate
  its relative links.

## Out of scope

- Changing the HTTP LISTSERV enqueue envelope.
- Changing standard MCP messages, `get_job`, polling, cancellation, SSE events,
  job persistence, or lifecycle behavior.
- Renaming tools or changing their input schemas.
