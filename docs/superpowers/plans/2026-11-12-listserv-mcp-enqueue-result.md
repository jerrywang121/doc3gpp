# LISTSERV MCP Enqueue Response Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make all three LISTSERV MCP enqueue tools return exactly the standard MCP job response: `job_id`, `status`, `message`, and `links`, with no `url` field.

**Architecture:** Add one shared serializer for an already-created job. Keep `_enqueue` responsible for creating standard jobs and have it delegate serialization; LISTSERV tools keep their existing enqueue functions and serialize their returned jobs through the same helper. Update only the MCP contract documentation; HTTP LISTSERV responses remain unchanged.

**Tech Stack:** Python 3.10+, FastAPI/MCP integration tests, pytest, Ruff, Markdown.

**Spec:** `docs/superpowers/specs/2026-11-12-listserv-mcp-enqueue-result-design.md`

## Global Constraints

- Drop `url`; do not retain it as an alias or extra field.
- Keep scope to MCP responses. The HTTP LISTSERV routes under `/api/listserv/{sync,parse,index}` retain their current response contract.
- Preserve existing validation, normalized job parameters, job kinds, queue behavior, status values, and `get_job` behavior.
- No database schema, worker, lifecycle, or dependency changes.
- Do not auto-commit.

## Review Focus

- Sync and parse must preserve list-ID normalization, date filters, and `force`; the existing cases assert exact job parameters.
- The index-rebuild tool's message must use job kind `listserv_index`, not tool name `listserv_index_rebuild`; assert its exact message.
- Both links must be derived from the returned job ID with exact `/jobs/{id}` and `/jobs/{id}/events` paths; assert both values.
- No legacy `url` may remain as an extra field; assert the exact top-level key set for every LISTSERV tool.
- Refactoring the shared formatter must not change ordinary MCP enqueue responses; add a `sync_meetings` response regression test.

---

### Task 1: Share and document the MCP enqueue response

**Files:**
- Modify: `src/doc3gpp/web/mcp_server.py` — shared serializer, `_enqueue`, and the three LISTSERV MCP handlers
- Test: `tests/integration/test_listserv_mcp.py` — LISTSERV response contract and standard-enqueue regression
- Test: `tests/unit/test_listserv_docs.py` — keep the MCP documentation contract assertion current
- Modify: `docs/mcp.md` — remove the LISTSERV-specific MCP response exception

**Interfaces:**
- Consumes: existing `enqueue_listserv_sync`, `enqueue_listserv_parse`, and `enqueue_listserv_index` functions, each returning an already-created job; existing `_job_url(job_id)` and `_to_json(payload)` helpers.
- Produces: `_job_result(job: Any, message: str) -> str`, serializing exactly `job_id`, `status`, `message`, and `links` with `self` and `events` entries. `_enqueue` and all LISTSERV MCP enqueue handlers use this formatter.

- [x] **Step 1: Extend the LISTSERV enqueue contract test**

In `test_mcp_sync_parse_and_rebuild_enqueue_the_http_job_kinds`, add the expected message to each case (`queued listserv_sync`, `queued listserv_parse`, and `queued listserv_index`). Assert that each result has exactly `job_id`, `status`, `message`, and `links`; that `links` has exactly `self` and `events`; and that both paths use the returned job ID. Retain the current job-kind and parameter assertions.

Also add `test_mcp_standard_enqueue_response_shape_is_unchanged` using the existing `surfaces` fixture and `sync_meetings` tool. Assert its exact top-level and nested link keys, queued status, non-empty job ID, message `queued sync_meetings for TSG R5`, and the expected `self` and `events` paths. This pins the existing non-LISTSERV contract before the shared formatter changes.

- [x] **Step 2: Run the LISTSERV contract test and verify the expected failure**

Run: `python -m pytest tests/integration/test_listserv_mcp.py::test_mcp_sync_parse_and_rebuild_enqueue_the_http_job_kinds -q`

Expected: FAIL because the current LISTSERV response has `url` and lacks `message` and `links`.

- [x] **Step 3: Add and use the shared formatter**

In `src/doc3gpp/web/mcp_server.py`, add `_job_result(job: Any, message: str) -> str` near `_enqueue`. Serialize the exact fields and link paths currently constructed by `_enqueue`. Make `_enqueue` create its job once and return `_job_result(job, message)`. Change each LISTSERV handler to pass its existing enqueue result to `_job_result` with the exact message specified in the spec. Remove `_listserv_job_result`; do not call `_enqueue` from LISTSERV handlers or enqueue a job a second time.

- [x] **Step 4: Run both focused enqueue tests**

Run: `python -m pytest tests/integration/test_listserv_mcp.py::test_mcp_sync_parse_and_rebuild_enqueue_the_http_job_kinds tests/integration/test_listserv_mcp.py::test_mcp_standard_enqueue_response_shape_is_unchanged -q`

Expected: PASS; all three LISTSERV jobs retain their current kinds and parameters, and the ordinary MCP enqueue envelope is unchanged.

- [x] **Step 5: Update the MCP documentation**

In `docs/mcp.md`, revise the LISTSERV mutation paragraph to document `job_id`, `status`, `message`, and `links`, including the `self` and `events` links. In the generic Jobs section, remove the exception saying LISTSERV MCP tools return `job_id`/`status`/`url` and state that MCP enqueue tools share the same response shape. Keep `get_job` polling guidance. Do not edit HTTP LISTSERV documentation.

Keep `test_mcp_enqueue_result_documents_exact_response_fields` in `tests/unit/test_listserv_docs.py` aligned with that documented contract: assert the exact fields and links, the shared Jobs-section wording, and the absence of the old distinct MCP contract.

- [x] **Step 6: Run focused verification and inspect the documentation diff**

Run: `python -m pytest tests/integration/test_listserv_mcp.py -q`

Expected: PASS.

Run: `python -m pytest tests/unit/test_listserv_docs.py::test_mcp_enqueue_result_documents_exact_response_fields -q`

Expected: PASS.

Run: `ruff check src/doc3gpp/web/mcp_server.py tests/integration/test_listserv_mcp.py tests/unit/test_listserv_docs.py`

Expected: exit code 0.

Review the `docs/mcp.md` diff to confirm there is no remaining distinct LISTSERV MCP enqueue contract and no unrelated documentation change. Confirm the edit adds no relative links and that any link text in the edited section still points to the existing anchors. Do not commit.
