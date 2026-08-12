# MCP Spec Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `sync_specs` background job (JobKind + handler + HTTP route + MCP tool) so specs can be synced through the web/MCP surface, mirroring the existing `sync_meetings` / `sync_tdocs` pattern.

**Architecture:** A new `SYNC_SPECS` `JobKind` is added to the enum. A `_sync_specs` handler in `web/workers/handlers.py` calls the already-wired `services.spec.sync(...)` (blocking, like the other handlers). A `POST /jobs/sync/specs` route and a `sync_specs` MCP tool both enqueue the job via `job_repo.create(JobKind.SYNC_SPECS, ...)`. The `spec` service is already in `ServiceContainer`, so no container change is needed.

**Tech Stack:** Python 3.10+, FastAPI, MCP v2 (`MCPServer`), SQLAlchemy 2.0, pytest.

## Global Constraints

- `JobKind` string values must match the URL slug (`SYNC_SPECS.value == "sync_specs"`).
- MCP tool results must byte-match the equivalent HTTP `?format=json` route; the job-enqueue envelope adds a `message` key (the only parity exception).
- Handlers are blocking (httpx + sqlalchemy) and occupy the event loop — this is the documented v1 trade-off (`job_worker.py:14-16`).
- New job kinds are registered in `JobHandlers.KIND_TO_HANDLER` — nowhere else.
- `tsg` is required for a spec sync; a missing `tsg` yields a 422 on the HTTP route and an `InvalidFilterError` on the MCP tool.
- Follow existing code style: no comments unless needed, `from __future__ import annotations`, ruff-clean.

---

### Task 1: Add `SYNC_SPECS` JobKind

**Files:**
- Modify: `src/doc3gpp/models/jobs.py:39-44` (the `JobKind` enum body)

**Interfaces:**
- Consumes: nothing
- Produces: `JobKind.SYNC_SPECS` with `.value == "sync_specs"`

- [ ] **Step 1: Add the enum member**

In `src/doc3gpp/models/jobs.py`, inside `class JobKind(str, Enum)`, add `SYNC_SPECS = "sync_specs"` after `SYNC_TDOCS_ALL`:

```python
    SYNC_MEETINGS = "sync_meetings"
    SYNC_TDOCS = "sync_tdocs"
    SYNC_TDOCS_ALL = "sync_tdocs_all"
    SYNC_SPECS = "sync_specs"
    PARSE_TDOCS = "parse_tdocs"
    REBUILD_SEARCH = "rebuild_search"
    CACHE_PURGE = "cache_purge"
```

- [ ] **Step 2: Verify the enum value**

Run: `python -c "from doc3gpp.models.jobs import JobKind; assert JobKind.SYNC_SPECS.value == 'sync_specs'; print('ok')"`
Expected: prints `ok`

- [ ] **Step 3: Commit**

```bash
git add src/doc3gpp/models/jobs.py
git commit -m "feat(jobs): add SYNC_SPECS job kind"
```

---

### Task 2: Add the `_sync_specs` handler

**Files:**
- Modify: `src/doc3gpp/web/workers/handlers.py` (add `_sync_specs` after `_sync_tdocs_all`, and register it in `JobHandlers.KIND_TO_HANDLER`)

**Interfaces:**
- Consumes: `JobKind.SYNC_SPECS` (Task 1); `services.spec.sync(tsg, *, force, on_progress)` from `ServiceContainer`; `SyncOutcome` fields `status`/`reason`/`synced_count`/`version_count`
- Produces: `_sync_specs(job, services, settings, *, progress, cancel_event) -> Mapping[str, JSONValue]`, registered as `JobKind.SYNC_SPECS` in `KIND_TO_HANDLER`

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_job_worker.py`, add a `_FakeSpecService` class and wire it into `_make_state`, then add a test. First add the fake class after `_FakeMeetingService` (around line 36):

```python
class _FakeSpecService:
    """Fake ``SpecService`` whose ``sync`` returns a canned outcome."""

    def __init__(self, *, fail: bool = False) -> None:
        from doc3gpp.models.sync import SyncOutcome

        if fail:
            self.sync = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        else:
            self.sync = lambda *a, **k: SyncOutcome(
                status="synced",
                reason="spec sync ok",
                synced_count=5,
                version_count=12,
            )
```

Then update `_make_state` to pass the fake into the `spec` field (replace `spec=None,` with `spec=_FakeSpecService(fail=fail),`):

```python
        spec=_FakeSpecService(fail=fail),  # type: ignore[arg-type]
```

Then add a test after `test_worker_runs_queued_job` (around line 143):

```python
def test_worker_runs_spec_sync_job() -> None:
    """A ``SYNC_SPECS`` job is claimed, logs, and succeeds."""
    repo = _make_repo()
    state = _make_state(repo)
    job = repo.create(JobKind.SYNC_SPECS, {"tsg": "R5", "force": True})
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.SUCCEEDED
    assert done.result_summary == {
        "status": "synced",
        "reason": "spec sync ok",
        "synced_count": 5,
        "version_count": 12,
    }
    assert len(done.log_lines) >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_job_worker.py::test_worker_runs_spec_sync_job -v`
Expected: FAIL — the job is marked `FAILED` with `unknown job kind: sync_specs` (no handler registered yet).

- [ ] **Step 3: Write the handler**

In `src/doc3gpp/web/workers/handlers.py`, add `_sync_specs` after `_sync_tdocs_all` (after line 125):

```python
async def _sync_specs(
    job: Job,
    services: ServiceContainer,
    settings: Settings,
    *,
    progress: ProgressFn,
    cancel_event: asyncio.Event,
) -> Mapping[str, JSONValue]:
    tsg = job.params.get("tsg")
    if not tsg or not isinstance(tsg, str):
        raise ValueError("sync_specs job requires a 'tsg' string parameter")
    force = bool(job.params.get("force", False))
    progress(f"syncing specs for TSG {tsg}")

    def on_progress(event: str, data: Mapping[str, object]) -> None:
        if event == "list_parsed":
            progress(f"parsed {data.get('total', 0)} specs for TSG {tsg}")
        elif event == "spec_done":
            progress(f"spec {data.get('spec_id', '')} done")

    outcome = services.spec.sync(tsg, force=force, on_progress=on_progress)
    progress(outcome.reason)
    return {
        "status": outcome.status,
        "reason": outcome.reason,
        "synced_count": outcome.synced_count,
        "version_count": outcome.version_count,
    }
```

Then register it in `JobHandlers.KIND_TO_HANDLER` (around line 269):

```python
    KIND_TO_HANDLER: dict[JobKind, Handler] = {
        JobKind.SYNC_MEETINGS: _sync_meetings,
        JobKind.SYNC_TDOCS: _sync_tdocs,
        JobKind.SYNC_TDOCS_ALL: _sync_tdocs_all,
        JobKind.SYNC_SPECS: _sync_specs,
        JobKind.PARSE_TDOCS: _parse_tdocs,
        JobKind.REBUILD_SEARCH: _rebuild_search,
        JobKind.CACHE_PURGE: _cache_purge,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_job_worker.py::test_worker_runs_spec_sync_job -v`
Expected: PASS

- [ ] **Step 5: Run the full worker test module**

Run: `pytest tests/unit/test_job_worker.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/workers/handlers.py tests/unit/test_job_worker.py
git commit -m "feat(jobs): add sync_specs handler"
```

---

### Task 3: Add the `POST /jobs/sync/specs` HTTP route

**Files:**
- Modify: `src/doc3gpp/web/routes/jobs.py` (add `_SyncSpecsBody` after `_SyncTDocsBody`, and the route after `post_sync_tdocs_all`)

**Interfaces:**
- Consumes: `JobKind.SYNC_SPECS` (Task 1); `_envelope(job, queued=True)` (existing)
- Produces: `POST /jobs/sync/specs` accepting `{"tsg": str, "force": bool}` → 202 queued envelope

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_web_jobs_routes.py`, add two tests after `test_post_sync_tdocs_all` (around line 135):

```python
def test_post_sync_specs_creates_job(client: Any) -> None:
    c, repo, _ = client
    r = c.post("/jobs/sync/specs", json={"tsg": "R5", "force": True})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert body["job_id"]
    assert body["links"]["self"] == f"/jobs/{body['job_id']}"
    assert body["links"]["events"] == f"/jobs/{body['job_id']}/events"
    job = repo.get(body["job_id"])
    assert job is not None
    assert job.kind is JobKind.SYNC_SPECS
    assert job.params == {"tsg": "R5", "force": True}


def test_post_sync_specs_requires_tsg(client: Any) -> None:
    c, _, _ = client
    r = c.post("/jobs/sync/specs", json={})
    assert r.status_code == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_web_jobs_routes.py::test_post_sync_specs_creates_job tests/unit/test_web_jobs_routes.py::test_post_sync_specs_requires_tsg -v`
Expected: FAIL — `404` (route not found).

- [ ] **Step 3: Write the body model and route**

In `src/doc3gpp/web/routes/jobs.py`, add the body model after `_SyncTDocsBody` (around line 114):

```python
class _SyncSpecsBody(BaseModel):
    tsg: str
    force: bool = False
```

Add the route after `post_sync_tdocs_all` (after line 174):

```python
@router.post("/sync/specs", status_code=202)
async def post_sync_specs(
    body: _SyncSpecsBody,
    job_repo: JobRepository = Depends(get_job_repo),
) -> JSONResponse:
    job = job_repo.create(
        JobKind.SYNC_SPECS,
        {"tsg": body.tsg, "force": body.force},
    )
    return JSONResponse(status_code=202, content=_envelope(job, queued=True))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_web_jobs_routes.py::test_post_sync_specs_creates_job tests/unit/test_web_jobs_routes.py::test_post_sync_specs_requires_tsg -v`
Expected: PASS

- [ ] **Step 5: Run the full jobs-routes test module**

Run: `pytest tests/unit/test_web_jobs_routes.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/routes/jobs.py tests/unit/test_web_jobs_routes.py
git commit -m "feat(jobs): add POST /jobs/sync/specs route"
```

---

### Task 4: Add the `sync_specs` MCP tool

**Files:**
- Modify: `src/doc3gpp/web/mcp_server.py` (add the tool in the Jobs section, after `sync_all_tdocs`)

**Interfaces:**
- Consumes: `JobKind.SYNC_SPECS` (Task 1); `_enqueue(state, kind, params, message)` (existing); `InvalidFilterError` (existing import)
- Produces: MCP tool `sync_specs(tsg, force=False) -> str`

- [ ] **Step 1: Write the failing test**

In `tests/integration/test_mcp_end_to_end.py`:

First, add `"sync_specs"` to the `expected` set in `test_list_tools_exposes_read_and_job_tools` (around line 72, after `"sync_all_tdocs"`):

```python
        "sync_all_tdocs",
        "sync_specs",
        "parse_tdocs",
```

Then add an enqueue test after `test_job_tools_enqueue_and_poll` (around line 152):

```python
def test_sync_specs_tool_enqueues(sqlite_env) -> None:
    """``sync_specs`` MCP tool returns the queued envelope."""
    import asyncio
    import json

    state, server = _state_and_server()

    async def run():
        created = await server.call_tool("sync_specs", {"tsg": "R5", "force": True})
        envelope = json.loads(created.content[0].text)
        assert envelope["status"] == "queued"
        assert "links" in envelope and envelope["links"]["self"].startswith("/jobs/")
        job_id = envelope["job_id"]
        detail = await server.call_tool("get_job", {"job_id": job_id})
        return created, detail

    created, detail = asyncio.run(run())
    assert created.is_error is False
    assert detail.is_error is False
    detail_payload = json.loads(detail.content[0].text)
    assert detail_payload["kind"] == "sync_specs"
    assert detail_payload["params"] == {"tsg": "R5", "force": True}
    del state.engine
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_mcp_end_to_end.py::test_list_tools_exposes_read_and_job_tools tests/integration/test_mcp_end_to_end.py::test_sync_specs_tool_enqueues -v`
Expected: FAIL — `sync_specs` not in the tool set / tool not found.

- [ ] **Step 3: Write the MCP tool**

In `src/doc3gpp/web/mcp_server.py`, add the tool after `sync_all_tdocs` (after line 478):

```python
    @server.tool(name="sync_specs", description="Enqueue a spec sync for a TSG.")
    @_mcp_error_guard
    def sync_specs(
        tsg: Annotated[str, Field(description="TSG short name to sync specs for (e.g. 'R5').")],
        force: Annotated[bool, Field(description="Bypass the spec sync interval skip rule.")] = False,
    ) -> str:
        if not tsg:
            raise InvalidFilterError("tsg is required")
        return _enqueue(
            state,
            JobKind.SYNC_SPECS,
            {"tsg": tsg, "force": force},
            f"queued sync_specs for TSG {tsg}",
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_mcp_end_to_end.py::test_list_tools_exposes_read_and_job_tools tests/integration/test_mcp_end_to_end.py::test_sync_specs_tool_enqueues -v`
Expected: PASS

- [ ] **Step 5: Run the full MCP end-to-end test module**

Run: `pytest tests/integration/test_mcp_end_to_end.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/mcp_server.py tests/integration/test_mcp_end_to_end.py
git commit -m "feat(mcp): add sync_specs tool"
```

---

### Task 5: Update documentation

**Files:**
- Modify: `docs/web-server.md` (route table + tool list + tool count)
- Modify: `docs/code-map.md` (routes/jobs.py row + JobKind row)
- Modify: `README.md` (tool count, two occurrences)
- Modify: `AGENTS.md` (add a "spec sync" row to the "Where to look" table)

**Interfaces:**
- Consumes: the `sync_specs` tool and `POST /jobs/sync/specs` route from Tasks 3-4

- [ ] **Step 1: Update `docs/web-server.md`**

In the route table (around line 181), add a row after `POST /jobs/sync/tdocs/all`:

```markdown
| POST | `/jobs/sync/specs` | Enqueue `sync_specs`. |
```

In the tool list (around line 320), add `sync_specs` to the Job tools line:

```markdown
**Job tools** — `sync_meetings`, `sync_tdocs`, `sync_tdocs_by_meeting`,
`sync_all_tdocs`, `sync_specs`, `parse_tdocs`, `rebuild_search_index`,
`purge_cache`, `get_job`, `cancel_job`, `list_jobs`.
```

Bump "22 tools" to "23 tools" (around line 315).

- [ ] **Step 2: Update `docs/code-map.md`**

In the `routes/jobs.py` row (around line 220), add `sync/specs` to the enqueue list:

```markdown
| `routes/jobs.py` | APIRouter | `web/routes/jobs.py` | `/jobs` — enqueue (sync/meetings, sync/tdocs, sync/tdocs/all, sync/specs, parse/tdocs, search/rebuild, cache/purge, sync_tdocs), list, get, SSE `/events`, cancel |
```

In the `JobKind` row (around line 224), add `SYNC_SPECS`:

```markdown
| `JobKind` / `JobStatus` | enum | `models/jobs.py` | `SYNC_MEETINGS/SYNC_TDOCS/SYNC_TDOCS_ALL/SYNC_SPECS/PARSE_TDOCS/REBUILD_SEARCH/CACHE_PURGE`; `QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED` |
```

- [ ] **Step 3: Update `README.md`**

In the `server` section (around line 397), bump "22 tools" to "23 tools":

```markdown
- **MCP** — `http://127.0.0.1:8765/mcp` exposes 23 tools covering the
  same reads plus job lifecycle.
```

- [ ] **Step 4: Update `AGENTS.md`**

In the "Where to look" table, add a row after the "Add a background job kind" row (around line 88):

```markdown
| Add a spec sync job / MCP tool | `src/doc3gpp/models/jobs.py` (`JobKind.SYNC_SPECS`) + `src/doc3gpp/web/workers/handlers.py` (`_sync_specs`) + `src/doc3gpp/web/routes/jobs.py` (`POST /jobs/sync/specs`) + `src/doc3gpp/web/mcp_server.py` (`sync_specs`) | Enqueue from route/MCP via `JobWorkerHandle.enqueue`; the handler calls `services.spec.sync(tsg, force=force, on_progress=...)`. |
```

- [ ] **Step 5: Verify docs render**

Run: `grep -rn "sync_specs\|23 tools" docs/web-server.md docs/code-map.md README.md AGENTS.md`
Expected: matches in all four files.

- [ ] **Step 6: Commit**

```bash
git add docs/web-server.md docs/code-map.md README.md AGENTS.md
git commit -m "docs: document sync_specs job and MCP tool"
```

---

### Task 6: Final verification

**Files:**
- None (verification only)

**Interfaces:**
- Consumes: all prior tasks

- [ ] **Step 1: Run ruff**

Run: `ruff check src/doc3gpp/models/jobs.py src/doc3gpp/web/workers/handlers.py src/doc3gpp/web/routes/jobs.py src/doc3gpp/web/mcp_server.py tests/unit/test_job_worker.py tests/unit/test_web_jobs_routes.py tests/integration/test_mcp_end_to_end.py`
Expected: no errors.

- [ ] **Step 2: Run the affected test modules**

Run: `pytest tests/unit/test_job_worker.py tests/unit/test_web_jobs_routes.py tests/integration/test_mcp_end_to_end.py -v`
Expected: all pass.

- [ ] **Step 3: Run the full sqlite suite**

Run: `./scripts/test_sqlite.sh`
Expected: all pass.
