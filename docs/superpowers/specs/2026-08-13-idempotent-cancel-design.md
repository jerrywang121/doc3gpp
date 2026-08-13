# Idempotent cancel_job — design spec

**Status:** approved (brainstorming session 2026-08-13)
**Branch:** `idempotent-cancel`
**Author:** code3gpp coding agent
**Affects:** `doc3gpp/web/mcp_server.py`, `doc3gpp/web/routes/jobs.py`, `doc3gpp/web/errors.py`

## Background

A user cancelled a `parse_tdoc_url` job after it had already finished. The MCP client received:

```
Error: MPC -32603: 463c85b9b6084e20b997c3ec934c19fa
```

Two problems:

1. **Wrong MCP error code.** `JobAlreadyTerminalError` is mapped to `-32603` (`Internal Error`) — it's a 5xx-style code for a 4xx-style condition (the client asked to cancel something that can't be cancelled). The original 409 mapping on the HTTP route is correct semantically; the MCP mapping is not.
2. **Unhelpful message.** The exception is constructed as `JobAlreadyTerminalError(job_id)` and `map_mcp_error` uses `str(exc)` for the MCP error `message`. So the message is the raw UUID — no human-readable text, no hint that the job was already terminal, no pointer to `get_job`.

The user requested an additional behavior change: cancel on a terminal job should **return the terminal job's envelope** so the caller can inspect the result without a separate `get_job` call.

## Goals

- `cancel_job` (MCP tool and HTTP route) becomes **idempotent**: calling it on a terminal job returns the job's envelope instead of erroring.
- The two transports remain **byte-for-byte consistent** for cancel responses.
- The `JobAlreadyTerminalError` exception class and its MCP/HTTP mapping become dead code and are removed.

## Non-goals

- No new MCP error code for other conflict conditions.
- No new flag (`if_pending`, etc.) — the new behavior is unconditional.
- No schema migration. No persistence changes.
- No change to the worker's behavior on `cancel_event` (still cooperative cancellation between progress callbacks).

## Design

### Behavior matrix

| Current state | Action | New HTTP code | New MCP error code | New response body |
| --- | --- | --- | --- | --- |
| Unknown `job_id` | cancel | **404** (unchanged) | **-32004** (unchanged) | `{"error": "job_not_found", ...}` |
| `PENDING` | cancel | 200 | (success) | envelope (status `PENDING`) + cancel event set |
| `RUNNING` | cancel | 200 | (success) | envelope (status `RUNNING`) + cancel event set |
| `SUCCEEDED` | cancel | **200** (was 409) | **(success)** (was -32603) | envelope (status `SUCCEEDED`, `result_summary` present) |
| `FAILED` | cancel | **200** (was 409) | **(success)** (was -32603) | envelope (status `FAILED`, `error` field present) |
| `CANCELLED` | cancel | **200** (was 409) | **(success)** (was -32603) | envelope (status `CANCELLED`) |

The pre-cancel envelope for `PENDING` / `RUNNING` reflects the state at the moment the cancel event was set; the worker observes the event between progress callbacks and transitions to `CANCELLED` shortly after. This matches today's behavior for the running case — no change there.

### Code changes

#### `src/doc3gpp/web/mcp_server.py` — `cancel_job` tool (lines 606–619)

```python
@server.tool(
    name="cancel_job",
    description=(
        "Request cooperative cancellation of a queued or running job. "
        "Idempotent: when the job is already terminal (SUCCEEDED / FAILED / "
        "CANCELLED), returns the current job envelope so the caller can "
        "inspect the result without a separate get_job call."
    ),
)
@_mcp_error_guard
def cancel_job(
    job_id: Annotated[str, Field(description="Job id (UUID4 hex string) to cancel.")],
) -> str:
    job = state.services.job_repo.get(job_id)
    if job is None:
        raise JobNotFoundError(job_id)
    from doc3gpp.models.jobs import JobStatus
    if job.status not in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED):
        state.jobs.cancel(job_id)
    from doc3gpp.web.routes.jobs import _envelope
    return _to_json(_envelope(job))
```

Drops the `raise JobAlreadyTerminalError(job_id)` branch.

#### `src/doc3gpp/web/routes/jobs.py` — `cancel_job` HTTP route (lines 462–475)

```python
@router.post("/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    job_repo: JobRepository = Depends(get_job_repo),
    handle: JobWorkerHandle = Depends(get_job_worker),
) -> JSONResponse:
    """Request cooperative cancellation; idempotent on terminal jobs (returns 200)."""
    job = _load_job(job_repo, job_id)
    from doc3gpp.models.jobs import JobStatus
    if job.status not in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED):
        handle.cancel(job_id)
    return JSONResponse(content=_envelope(job))
```

Same simplification — the `_load_job` call still raises `HTTPException(404)` via `JobNotFoundError` for unknown ids.

#### `src/doc3gpp/web/errors.py`

Remove the `JobAlreadyTerminalError` class, its slug in `_ERROR_SLUGS` (`"job_already_terminal"`), and its entries in `_MCP_RESOURCE_BY_EXC` and `_STATUS_BY_EXC`. Also remove the explicit branch in `map_mcp_error` (line 130) that special-cases `JobAlreadyTerminalError` — after the removal that branch falls through to the generic return.

### Why no shared helper

The cancellation logic is now ~3 lines per call site: load, decide, return. A shared helper would add indirection without removing meaningful duplication. Both transports already share `_envelope`. If a third transport (e.g. gRPC) is added later, the helper can be extracted then.

## Data flow (unchanged for non-terminal case)

```
MCP / HTTP cancel_job(job_id)
  → load job (404 / JobNotFoundError if missing)
  → if terminal → return envelope (200 OK, status reflects actual state)
  → else        → handle.cancel(job_id); return envelope (200 OK, status still PENDING/RUNNING)
  → worker observes cancel_event between progress callbacks, transitions to JobStatus.CANCELLED
```

## Testing

### Unit tests — `tests/unit/test_web_jobs_routes.py`

For the HTTP `cancel_job` route, add and update:

- `cancel_pending_job_sets_event_and_returns_200` — happy path, assert `response.status_code == 200`, envelope `status == "pending"`, cancel event set on the handle.
- `cancel_running_job_returns_envelope` — same but for RUNNING.
- `cancel_succeeded_job_returns_envelope_idempotent` — NEW. Pre-seed a SUCCEEDED job; call cancel; assert 200, envelope `status == "succeeded"`, `result_summary` present, no error field.
- `cancel_failed_job_returns_envelope_idempotent` — NEW. Mirror for FAILED, assert `error` field present in envelope.
- `cancel_cancelled_job_returns_envelope_idempotent` — NEW. Mirror for CANCELLED.
- `cancel_unknown_job_returns_404` — already covered; verify still 404.
- Remove (or invert) any test that asserts 409 on a terminal job.

### Integration tests — `tests/integration/test_mcp_end_to_end.py`

For the MCP `cancel_job` tool, the existing test at ~line 573–582 (which asserts `pytest.raises(MCPError)` on cancel-already-terminal) is **inverted** to assert the envelope is returned:

- `cancel_succeeded_job_returns_envelope` — assert `result.is_error is False`, parse `result.content[0].text`, assert `status == "succeeded"`, `result_summary` present.
- `cancel_failed_job_returns_envelope` — mirror for FAILED.
- `cancel_unknown_job_raises_job_not_found` — verify the existing 404 mapping still raises `MCPError` with code `-32004`.
- Update `test_list_tools_exposes_read_and_job_tools` if any tool description changed (it does — `description` arg updated; tools themselves unchanged).

### Error-mapping tests — `tests/unit/test_web_errors.py`

- Remove any test that asserts `JobAlreadyTerminalError` → MCP `-32603` mapping.
- Verify the remaining `map_mcp_error` unit tests still cover all known exception classes (no test references `JobAlreadyTerminalError` after the cleanup).

### Worker / handler tests

No change. The worker's handling of `cancel_event` is unchanged.

## Documentation

- `AGENTS.md` — under the "MCP tools" / "Jobs" workflow entries, add a one-line note: `cancel_job` is idempotent on terminal jobs (returns the envelope). The `web/` boundary row already mentions MCP/JSON parity; this note lives next to it.
- `docs/web-server.md` — in the MCP section (line 326+), the "Job tools" paragraph notes idempotency for `cancel_job`.
- `docs/cli.md` — no change (CLI has no cancel command).
- `README.md` — no change (no per-MCP-tool list).

## Risks

1. **HTTP callers relying on 409 status.** Any external client checking `response.status_code == 409` to detect "already terminal" must now check the envelope's `status` field. Mitigated by: this is a local FastAPI app; the only known clients are the same doc3gpp user-facing surfaces (CLI/MCP) and tests. The transport consistency rule in `AGENTS.md` already states HTTP and MCP must match; this fix brings them in line.
2. **Test churn.** ~3 test files need updates. All changes are mechanical inversions of existing assertions.
3. **`JobAlreadyTerminalError` removal could be reverted by a revert commit.** Acceptable: removing dead code is good hygiene; if a future conflict condition needs it, it's a one-line re-introduction.

## Spec self-review

- **Placeholders:** none.
- **Internal consistency:** the cancel branch in both transports uses the same terminal-status check; the envelope path is the same. ✓
- **Scope:** single-feature, single-iteration implementation plan. ✓
- **Ambiguity:** none — the behavior matrix is exhaustive.
