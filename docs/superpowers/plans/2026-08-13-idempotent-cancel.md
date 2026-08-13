# Idempotent cancel_job Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `cancel_job` idempotent on already-terminal jobs (returns the job envelope instead of erroring) on both the MCP and HTTP transports, and remove the now-dead `JobAlreadyTerminalError` exception class.

**Architecture:** Drop the `raise JobAlreadyTerminalError(...)` branch from both the MCP tool (`mcp_server.py:614-615`) and the HTTP route (`routes/jobs.py:470-473`). Both call sites already share `_envelope(job)`; the only difference is the cancel-event dispatch on pending/running jobs. Remove the exception class, its slug, and its entries from the three mapping tables in `errors.py`.

**Tech Stack:** Python 3.10+, FastAPI, pydantic, SQLAlchemy 2.0, pytest. No new dependencies.

## Global Constraints

- **Branch:** `idempotent-cancel` (worktree at `/home/jerry/personal/doc3gpp/.worktrees/idempotent-cancel/`). All work happens here. The previous merge of `mcp-parse-tdoc-url` is already on `main`; `idempotent-cancel` branched from that HEAD.
- **Python interpreter:** use `/home/jerry/personal/doc3gpp/.venv/bin/python`. pytest reads `pythonpath = ["src"]` from `pyproject.toml` so it resolves `doc3gpp.*` from the worktree's `src/` automatically. Direct `python -c` calls need `PYTHONPATH=src`.
- **No new dependencies.** No schema migration. No CLI surface change.
- **Ruff is the only configured linter** (`ruff check .`). mypy is not configured.
- **Idempotent semantics:** terminal job → 200 OK (HTTP) / success envelope (MCP) with the job's actual state. Pending or running → 200 OK + cancel event set (unchanged from today).
- **Unknown job_id still raises `JobNotFoundError`** (404 / MCP -32004) — unchanged.
- **Tests must follow TDD:** write the failing test, run it, implement the code, re-run, commit.
- **Commit policy:** one commit per task, with the message specified in the task's Step 5.
- **Run `./scripts/test_sqlite.sh` once at the end** as a final verification gate (Task 8 / Task 5 of this plan).

---

## File Structure

| File | Change | Purpose |
| --- | --- | --- |
| `src/doc3gpp/web/mcp_server.py` | edit lines 606–619 | Drop terminal-error raise from `cancel_job` tool; always return envelope |
| `src/doc3gpp/web/routes/jobs.py` | edit lines 462–475; clean up imports + module docstring lines 20, 37 | Same simplification on the HTTP route |
| `src/doc3gpp/web/errors.py` | delete `JobAlreadyTerminalError` class, three mapping entries, module-docstring line 16; update `__all__` | Remove dead code |
| `tests/unit/test_web_jobs_routes.py` | invert `test_cancel_returns_409_when_terminal`; add three terminal-state idempotent tests | Lock in new behavior |
| `tests/integration/test_web_end_to_end.py` | update the "cancel on a terminal job is a 409" assertion at line 170–172 to assert 200 + envelope | Lock in new HTTP behavior end-to-end |
| `tests/integration/test_mcp_end_to_end.py` | add three new tests (cancel on SUCCEEDED / FAILED / CANCELLED returns envelope); update tool description if the existing list test asserts a description string | Lock in new MCP behavior end-to-end |
| `tests/unit/test_web_errors.py` | remove `JobAlreadyTerminalError` from import + `_MAPPING_CASES` + `_LEGACY_DETAIL_CASES` (verify not present) + handler-loop assertion + `test_map_mcp_error_terminal_and_disabled_map_to_internal` | Drop tests for deleted exception |
| `docs/web-server.md` | update Job tools paragraph to note idempotency | Document new behavior |
| `AGENTS.md` | one-line note in the `web/` boundary row | Document new behavior |

The `JobAlreadyTerminalError` removal and the per-test changes are coupled: if Task 1 is implemented but Task 2 isn't, tests fail (since they still import the deleted class). The task order ensures tests are updated before the implementation lands.

---

## Task 1: Update `test_web_jobs_routes.py` to lock in the new idempotent HTTP behavior

**Files:**
- Modify: `tests/unit/test_web_jobs_routes.py:365-388`

**Interfaces:**
- Consumes: existing `client` fixture (returns `(TestClient, JobRepository, FakeHandle)`).
- Produces: replaced test `test_cancel_returns_200_when_terminal_idempotent` plus three sibling tests asserting each terminal status.

- [ ] **Step 1: Replace `test_cancel_returns_409_when_terminal` and add three idempotent tests**

Open `tests/unit/test_web_jobs_routes.py`, find the cancel section (lines 360–388), and replace lines 375–382 with:

```python
def test_cancel_returns_200_when_terminal_idempotent_succeeded(client: Any) -> None:
    """Cancel on a SUCCEEDED job returns 200 + envelope (idempotent)."""
    c, repo, handle = client
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    repo.mark_succeeded(job.id, summary={"ok": True})
    r = c.post(f"/jobs/{job.id}/cancel")
    assert r.status_code == 200
    assert handle.cancelled == []  # no cancel event on a terminal job
    body = r.json()
    assert body["job_id"] == job.id
    assert body["status"] == "succeeded"
    assert body["result_summary"] == {"ok": True}


def test_cancel_returns_200_when_terminal_idempotent_failed(client: Any) -> None:
    """Cancel on a FAILED job returns 200 + envelope + error field."""
    c, repo, _ = client
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    repo.mark_failed(job.id, error="boom")
    r = c.post(f"/jobs/{job.id}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == job.id
    assert body["status"] == "failed"
    assert body["error"] == "boom"


def test_cancel_returns_200_when_terminal_idempotent_cancelled(client: Any) -> None:
    """Cancel on an already-CANCELLED job returns 200 + envelope."""
    c, repo, _ = client
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    repo.mark_cancelled(job.id)
    r = c.post(f"/jobs/{job.id}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == job.id
    assert body["status"] == "cancelled"
```

Keep the existing `test_cancel_returns_200_for_running` (line 365) and `test_cancel_returns_404_for_unknown` (line 384) unchanged.

- [ ] **Step 2: Run the new tests — expect them to FAIL (HTTP still returns 409)**

Run from the worktree root:
```bash
cd /home/jerry/personal/doc3gpp/.worktrees/idempotent-cancel
./scripts/test_sqlite.sh 2>&1 | tail -40 || true
# Or, more targeted:
PYTHONPATH=src python -m pytest tests/unit/test_web_jobs_routes.py -k "cancel" -v
```

Expected: `test_cancel_returns_200_when_terminal_idempotent_*` FAIL with `assert 409 == 200`. `test_cancel_returns_200_for_running` and `test_cancel_returns_404_for_unknown` PASS unchanged.

- [ ] **Step 3: Skip implementation in this task** — implementation lands in Task 3.

- [ ] **Step 4: Skip commit in this task** — commit lands at the end of Task 3 (after HTTP route + MCP tool are both updated).

---

## Task 2: Update `test_mcp_end_to_end.py` to lock in the new idempotent MCP behavior

**Files:**
- Modify: `tests/integration/test_mcp_end_to_end.py`

**Interfaces:**
- Consumes: existing `_state_and_server(sqlite_env)` fixture (returns `(state, MCPServer)`); existing `sqlite_env` fixture; existing `await server.call_tool(name, args)` helper.
- Produces: three new tests asserting `cancel_job` on terminal jobs returns the envelope.

- [ ] **Step 1: Add three idempotent cancel tests**

Append to `tests/integration/test_mcp_end_to_end.py` (after the last existing test in the file):

```python
@pytest.mark.asyncio
async def test_cancel_succeeded_job_returns_envelope(sqlite_env) -> None:
    """cancel_job on a SUCCEEDED job returns the envelope (idempotent)."""
    _, server = _state_and_server(sqlite_env)
    repo = server_tools_state(sqlite_env).services.job_repo
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    repo.mark_succeeded(job.id, summary={"ok": True})

    result = await server.call_tool("cancel_job", {"job_id": job.id})

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["job_id"] == job.id
    assert payload["status"] == "succeeded"
    assert payload["result_summary"] == {"ok": True}


@pytest.mark.asyncio
async def test_cancel_failed_job_returns_envelope(sqlite_env) -> None:
    """cancel_job on a FAILED job returns the envelope + error field."""
    _, server = _state_and_server(sqlite_env)
    repo = server_tools_state(sqlite_env).services.job_repo
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    repo.mark_failed(job.id, error="boom")

    result = await server.call_tool("cancel_job", {"job_id": job.id})

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "failed"
    assert payload["error"] == "boom"


@pytest.mark.asyncio
async def test_cancel_cancelled_job_returns_envelope(sqlite_env) -> None:
    """cancel_job on an already-CANCELLED job returns the envelope."""
    _, server = _state_and_server(sqlite_env)
    repo = server_tools_state(sqlite_env).services.job_repo
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    repo.mark_cancelled(job.id)

    result = await server.call_tool("cancel_job", {"job_id": job.id})

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "cancelled"
```

Imports required at the top of the test file (add if not already present):
```python
import json
import pytest
```

`server_tools_state` is a helper that returns the `WebState` from `_state_and_server`. If `_state_and_server` already returns `(state, server)`, use `state = _state_and_server(sqlite_env)[0]` instead of the helper above — adapt to the actual fixture shape (see the existing tests in the file for the convention).

**How to verify the fixture shape:** open `tests/integration/test_mcp_end_to_end.py` and find any existing test that calls `state.services.job_repo.create(...)`. Use the same accessor pattern in the new tests.

- [ ] **Step 2: Add a known-good test for the unknown-id error path** (lock in the unchanged 404 behavior)

```python
@pytest.mark.asyncio
async def test_cancel_unknown_job_raises_job_not_found(sqlite_env) -> None:
    """cancel_job on an unknown id still raises MCPError(code=-32004)."""
    from mcp.shared.exceptions import MCPError

    _, server = _state_and_server(sqlite_env)
    with pytest.raises(MCPError) as exc_info:
        await server.call_tool("cancel_job", {"job_id": "deadbeef"})
    assert "deadbeef" in str(exc_info.value)
```

- [ ] **Step 3: Run the new tests — expect them to FAIL (MCP still raises on terminal)**

Run from the worktree root:
```bash
PYTHONPATH=src python -m pytest tests/integration/test_mcp_end_to_end.py -k "cancel" -v
```

Expected: `test_cancel_*_returns_envelope` FAIL with `MCPError` (the old `JobAlreadyTerminalError` raises through the guard). `test_cancel_unknown_job_raises_job_not_found` PASSES (404 mapping unchanged).

- [ ] **Step 4: Skip implementation** — lands in Task 3.

- [ ] **Step 5: Skip commit** — commit at the end of Task 3.

---

## Task 3: Make `cancel_job` idempotent in MCP and HTTP, remove `JobAlreadyTerminalError`

**Files:**
- Modify: `src/doc3gpp/web/mcp_server.py:606-619`
- Modify: `src/doc3gpp/web/routes/jobs.py:20,37,462-475`
- Modify: `src/doc3gpp/web/errors.py:16,64-65,98-148,160-161,225-242`
- Modify: `tests/integration/test_web_end_to_end.py:170-172`
- Modify: `tests/unit/test_web_errors.py:19,39,128,180`
- Modify: `tests/unit/test_web_jobs_routes.py` (Step 1 of Task 1 — already applied)
- Modify: `tests/integration/test_mcp_end_to_end.py` (Step 1 of Task 2 — already applied)

**Interfaces:**
- Consumes: `JobStatus` (SUCCEEDED, FAILED, CANCELLED), `JobNotFoundError`, `JobWorkerHandle.cancel`, `JobRepository.get`, `_envelope`.
- Produces: same public surface; `JobAlreadyTerminalError` removed.

- [ ] **Step 1: Update the MCP `cancel_job` tool** (`src/doc3gpp/web/mcp_server.py:606-619`)

Replace the body with:

```python
    @server.tool(
        name="cancel_job",
        description=(
            "Request cooperative cancellation of a queued or running job. "
            "Idempotent on terminal jobs: when the job has already reached "
            "SUCCEEDED / FAILED / CANCELLED, returns the current envelope "
            "so the caller can inspect the result without a separate "
            "get_job call."
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

Remove `JobAlreadyTerminalError` from the import block at line 30 of `mcp_server.py`. The remaining `JobNotFoundError` import stays.

- [ ] **Step 2: Update the HTTP `cancel_job` route** (`src/doc3gpp/web/routes/jobs.py:462-475`)

Replace the route body with:

```python
@router.post("/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    job_repo: JobRepository = Depends(get_job_repo),
    handle: JobWorkerHandle = Depends(get_job_worker),
) -> JSONResponse:
    """Request cooperative cancellation; idempotent on terminal jobs (returns 200 + envelope)."""
    job = _load_job(job_repo, job_id)
    from doc3gpp.models.jobs import JobStatus
    if job.status not in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED):
        handle.cancel(job_id)
    return JSONResponse(content=_envelope(job))
```

Update the module docstring at line 20 (replace ``JobAlreadyTerminalError`` -> 409`` with the idempotent note) and remove `JobAlreadyTerminalError` from the import block at line 37.

New module docstring line (replaces the current line 20):
```python
"""Job lifecycle endpoints: enqueue, status, cancel.

The cancel endpoint is **idempotent** — cancelling a job that has already
reached a terminal state (``JobStatus.SUCCEEDED``, ``FAILED`` or
``CANCELLED``) returns the current envelope with ``200`` instead of an
error so the caller can inspect the result without a separate
``GET /jobs/{job_id}`` round-trip. ``JobNotFoundError`` still surfaces
as a 404 when the ``job_id`` is unknown.
"""
```

- [ ] **Step 3: Remove `JobAlreadyTerminalError` from `src/doc3gpp/web/errors.py`**

Edit the file:

- Line 16 (module docstring): change ``:class:`JobAlreadyTerminalError` -> 409`` to drop that bullet (or rephrase the docstring to remove the entry).
- Lines 64–65: delete the class definition:
  ```python
  class JobAlreadyTerminalError(RuntimeError):
      """Raised when an action is attempted on a job that has already reached a terminal state."""
  ```
- Line 130: change `if isinstance(exc, (JobAlreadyTerminalError, SettingsDisabledError, httpx.HTTPError)):` to `if isinstance(exc, (SettingsDisabledError, httpx.HTTPError)):`.
- Line 145: delete the line `JobAlreadyTerminalError: "job_already_terminal",` from `_ERROR_SLUGS`.
- Line 161: delete the line `JobAlreadyTerminalError: 409,` from `_STATUS_BY_EXC`.
- Line 233: delete `"JobAlreadyTerminalError",` from `__all__`.

- [ ] **Step 4: Update `tests/integration/test_web_end_to_end.py:170-172`**

Find the assertion:
```python
# cancel on a terminal job is a 409
conflict = client.post(f"/jobs/{job_id}/cancel")
assert conflict.status_code == 409
```

Replace with:
```python
# cancel on a terminal job is a 200 (idempotent) returning the envelope
envelope = client.post(f"/jobs/{job_id}/cancel")
assert envelope.status_code == 200
assert envelope.json()["status"] == "succeeded"
```

- [ ] **Step 5: Update `tests/unit/test_web_errors.py`**

Remove `JobAlreadyTerminalError` from the import block at line 19. Delete the line 39 param entry:
```python
pytest.param(JobAlreadyTerminalError, "succeeded", 409, "job_already_terminal", id="job_already_terminal"),
```
Delete `JobAlreadyTerminalError,` from the handler-loop list at line 128. Delete the entire `test_map_mcp_error_terminal_and_disabled_map_to_internal` function at lines 178–181 (replace with the smaller version below that drops the `JobAlreadyTerminalError` assertion):
```python
def test_map_mcp_error_disabled_maps_to_internal() -> None:
    """``SettingsDisabledError`` maps to the generic -32603 internal code."""
    assert map_mcp_error(SettingsDisabledError("off"))[0] == MCP_CODE_INTERNAL_ERROR
```

- [ ] **Step 6: Run the cancel tests — expect them to PASS now**

Run from the worktree root:
```bash
PYTHONPATH=src python -m pytest tests/unit/test_web_jobs_routes.py tests/unit/test_web_errors.py tests/integration/test_web_end_to_end.py tests/integration/test_mcp_end_to_end.py -k "cancel or terminal or job_already" -v
```

Expected: all cancel / terminal tests pass. The 404 path still 404s. `JobAlreadyTerminalError` is gone from the import graph.

- [ ] **Step 7: Run the full offline test suite to confirm no regressions**

```bash
./scripts/test_sqlite.sh
```

Expected: all previously-passing tests still pass; the new tests pass; no `JobAlreadyTerminalError` `ImportError` or `AttributeError` anywhere.

- [ ] **Step 8: Run ruff**

```bash
ruff check .
```

Expected: no findings.

- [ ] **Step 9: Commit**

```bash
git add -A src/doc3gpp/web/mcp_server.py src/doc3gpp/web/routes/jobs.py src/doc3gpp/web/errors.py tests/
git commit -m "feat(jobs): idempotent cancel_job on terminal jobs

cancel_job (both MCP tool and HTTP route) now returns the job's envelope
when the job is already in a terminal state (SUCCEEDED / FAILED /
CANCELLED), so the caller can inspect the result without a separate
get_job call. HTTP 409 → 200 for terminal jobs. MCP -32603 → success.

Removes the now-dead JobAlreadyTerminalError exception class, its slug,
and its entries in the MCP/HTTP error-mapping tables."
```

---

## Task 4: Documentation sync

**Files:**
- Modify: `docs/web-server.md` (Job tools paragraph around line 326+)
- Modify: `AGENTS.md` (`web/` boundary row at line 117)

- [ ] **Step 1: Update `docs/web-server.md`**

Find the "Job tools" / "Every read tool returns exactly the bytes…" paragraph in `docs/web-server.md`. After the existing prose, add a one-line note about `cancel_job` idempotency. Adapt to the exact paragraph structure (a fresh sentence fits best after the existing per-tool descriptions and before the "Every read tool returns exactly the bytes…" sentence). Suggested wording:

```
`cancel_job` is idempotent on terminal jobs: cancelling a job that has
already reached SUCCEEDED / FAILED / CANCELLED returns the envelope
instead of erroring, so callers can inspect the result without a
separate `get_job` call.
```

- [ ] **Step 2: Update `AGENTS.md`**

At line 117, the `web/` boundary row currently ends with:
```
MCP via `web/mcp_server.py`; background jobs via `web/workers/`.
```

Extend it to a one-liner (matches the existing pattern in this row):
```
MCP via `web/mcp_server.py`; background jobs via `web/workers/`.
`cancel_job` is idempotent on terminal jobs (returns the envelope
instead of erroring).
```

- [ ] **Step 3: Verify**

```bash
grep -n "idempotent\|cancel_job is" docs/web-server.md AGENTS.md
```

Expected: at least one match per file.

- [ ] **Step 4: Commit**

```bash
git add docs/web-server.md AGENTS.md
git commit -m "docs: note idempotent cancel_job on terminal jobs"
```

---

## Task 5: Final verification

**Files:** none — verification only.

- [ ] **Step 1: Full offline test suite**

```bash
cd /home/jerry/personal/doc3gpp/.worktrees/idempotent-cancel
./scripts/test_sqlite.sh
```

Expected: all tests pass. Capture the `N passed, N skipped` line for the report.

- [ ] **Step 2: Ruff**

```bash
ruff check .
```

Expected: `All checks passed!`.

- [ ] **Step 3: MCP tool registration smoke test**

```bash
PYTHONPATH=src python -c "
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.web.app import build_state
from doc3gpp.web.mcp_server import build_mcp_server
create_schema()
state = build_state(get_settings())
server = build_mcp_server(state)
import asyncio
tools = asyncio.run(server.list_tools())
names = sorted(t.name for t in tools)
print('parse_tdoc_url' in names, 'cancel_job' in names)
"
```

Expected: `True True`.

- [ ] **Step 4: Final commit (if any small fix-ups were applied)**

If Step 1 / Step 2 surfaced a fix-up, commit it on this branch with a `chore:` or `fix:` prefix. Otherwise this step is a no-op.

---

## Self-review

**1. Spec coverage:**

| Spec requirement | Task |
| --- | --- |
| `cancel_job` returns envelope on terminal job (MCP) | Task 3, Step 1 |
| `cancel_job` returns 200 + envelope on terminal job (HTTP) | Task 3, Step 2 |
| `JobAlreadyTerminalError` removed | Task 3, Step 3 |
| HTTP/MCP transport parity | Task 3, Steps 1 + 2 |
| Unit tests for HTTP cancel behavior | Task 1 + Task 3 Step 5 |
| Integration tests for MCP cancel behavior | Task 2 + Task 3 Step 5 |
| Integration test for HTTP cancel | Task 3 Step 4 |
| Error-mapping tests cleanup | Task 3 Step 5 |
| Documentation sync (web-server.md, AGENTS.md) | Task 4 |
| Final verification | Task 5 |
| 404 / JobNotFoundError unchanged | Task 2 Step 2 + unchanged existing test |
| Pending/running cancel still works | Task 1 (existing test unchanged) + Task 3 |

**2. Placeholder scan:** no `TBD` / `TODO` / `fill in later` / `add validation` / `handle edge cases` in the plan. Every code block contains the actual implementation.

**3. Type / method consistency:**
- `JobStatus.SUCCEEDED`, `JobStatus.FAILED`, `JobStatus.CANCELLED` — match `src/doc3gpp/models/jobs.py:49`.
- `JobRepository.get(job_id) -> Job | None` — match `src/doc3gpp/repository/protocols.py`.
- `JobWorkerHandle.cancel(job_id) -> bool` — match `src/doc3gpp/web/state.py:41`.
- `_envelope(job) -> dict` — match `src/doc3gpp/web/routes/jobs.py:60`.
- `JobNotFoundError(job_id)` — match `src/doc3gpp/web/errors.py:60`.
- `_load_job(job_repo, job_id)` — match `src/doc3gpp/web/routes/jobs.py` (used in the existing cancel route).
- `server_tools_state(sqlite_env)` is speculative — see Task 2 Step 1's "How to verify the fixture shape" note; the implementer reads the existing test file for the actual pattern.
