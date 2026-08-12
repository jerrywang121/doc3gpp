# MCP spec sync — design

**Date:** 2026-08-12
**Status:** Approved

## Problem

The MCP server exposes spec **read** tools (`list_specs`, `get_spec`) but
no way to **sync** specs. Every other sync surface (meetings, TDocs) is
exposed as a background job via `JobKind` + handler + HTTP route + MCP
tool. Spec sync is the only mutating spec operation and is missing from
all four layers.

## Approach

Add a `sync_specs` background job end-to-end, mirroring the existing
`sync_meetings` / `sync_tdocs` pattern exactly. The `spec` service is
already wired into `ServiceContainer` (`web/state.py:152`), so no
container change is needed.

## Changes

### 1. `JobKind` — `src/doc3gpp/models/jobs.py`

Add `SYNC_SPECS = "sync_specs"` to the enum. The string value matches
the URL slug `POST /jobs/sync/specs` (consistent with the other kinds).

### 2. Handler — `src/doc3gpp/web/workers/handlers.py`

Add `_sync_specs(job, services, settings, *, progress, cancel_event)`:

- Read `tsg` (required string) and `force` (bool, default `False`) from
  `job.params`; raise `ValueError` when `tsg` is missing/not a string
  (mirrors `_sync_meetings`).
- Call `services.spec.sync(tsg, force=force, on_progress=...)`. The
  `on_progress` callback maps the two `SpecService.sync` events to
  `progress(...)` lines:
  - `"list_parsed"` → `progress(f"parsed {data['total']} specs for TSG {tsg}")`
  - `"spec_done"` → `progress(f"spec {data['spec_id']} done")`
- Return summary `{"status", "reason", "synced_count", "version_count"}`
  (the fields `SpecService.sync` returns on its `SyncOutcome`).
- Register in `JobHandlers.KIND_TO_HANDLER` as
  `JobKind.SYNC_SPECS: _sync_specs`.

The handler is blocking (httpx + thread pools), consistent with the
existing handlers and the documented v1 trade-off in
`job_worker.py:14-16`.

### 3. HTTP route — `src/doc3gpp/web/routes/jobs.py`

Add `_SyncSpecsBody(tsg: str, force: bool = False)` and:

```python
@router.post("/sync/specs", status_code=202)
async def post_sync_specs(body, job_repo=Depends(get_job_repo)):
    job = job_repo.create(JobKind.SYNC_SPECS, {"tsg": body.tsg, "force": body.force})
    return JSONResponse(status_code=202, content=_envelope(job, queued=True))
```

`tsg` is a required Pydantic field, so a missing body yields a 422
(matching `post_sync_meetings`).

### 4. MCP tool — `src/doc3gpp/web/mcp_server.py`

Add a `sync_specs` tool in the Jobs section, mirroring `sync_meetings`:

```python
@server.tool(name="sync_specs", description="Enqueue a spec sync for a TSG.")
@_mcp_error_guard
def sync_specs(tsg, force=False) -> str:
    if not tsg:
        raise InvalidFilterError("tsg is required")
    return _enqueue(state, JobKind.SYNC_SPECS, {"tsg": tsg, "force": force},
                    f"queued sync_specs for TSG {tsg}")
```

### 5. Tests

- `tests/unit/test_web_jobs_routes.py`:
  - `test_post_sync_specs_creates_job` — POST `/jobs/sync/specs` with
    `{"tsg": "R5", "force": true}` → 202, `kind is JobKind.SYNC_SPECS`,
    `params == {"tsg": "R5", "force": true}`.
  - `test_post_sync_specs_requires_tsg` — POST `{}` → 422.
- `tests/unit/test_job_worker.py`:
  - Add a `_FakeSpecService` (canned `SyncOutcome(status="synced", ...)`)
    and wire it into `_make_state`'s `spec` field.
  - `test_worker_runs_spec_sync_job` — enqueue `SYNC_SPECS`, run once,
    assert `SUCCEEDED` and `result_summary` carries `status`/`reason`/
    `synced_count`/`version_count`.
- `tests/integration/test_mcp_end_to_end.py`:
  - Add `"sync_specs"` to the expected tool set in
    `test_list_tools_exposes_read_and_job_tools`.
  - Add an enqueue test mirroring `test_job_tools_enqueue_and_poll` for
    `sync_specs`.

### 6. Docs

- `docs/web-server.md`:
  - Route table: add `POST /jobs/sync/specs | Enqueue sync_specs.`
  - Tool list: add `sync_specs` to the Job tools line; bump "22 tools"
    to "23 tools".
- `docs/code-map.md`:
  - `routes/jobs.py` row: add `sync/specs` to the enqueue list.
  - `JobKind` row: add `SYNC_SPECS`.
- `README.md`:
  - `server` section: bump "22 tools" to "23 tools" (two occurrences).
- `AGENTS.md`:
  - "Add a background job kind" row already covers the pattern; no change
    needed beyond the code-map/web-server updates.

## Out of scope

- No web UI button for spec sync (the spec list page has no sync form;
  the MCP tool + HTTP route are the surface).
- No `sync_all_specs` bulk tool (spec sync is per-TSG only, matching the
  CLI `spec sync --tsg`).
