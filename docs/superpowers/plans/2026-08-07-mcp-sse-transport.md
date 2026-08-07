# MCP SSE Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in SSE transport to the MCP mount so clients can connect via the legacy two-endpoint SSE protocol in addition to the existing Streamable HTTP transport.

**Architecture:** The installed `mcp` SDK (v2.0.0) already exposes `MCPServer.sse_app(sse_path, message_path)` returning a Starlette app, identical in shape to `streamable_http_app`. We widen the `MCPSettings.transport` literal to accept `"sse"`, branch the mount in `_mount_mcp_in_lifespan` on the chosen transport, and document the new endpoints. The existing `server.session_manager.run()` lifespan handling carries over unchanged because both transports return Starlette apps.

**Tech Stack:** Python 3.10+, FastAPI/Starlette, `mcp` v2.0.0, pydantic-settings, pytest.

## Global Constraints

- `mcp` package version floor: **2.0.0** (provides `MCPServer.sse_app`). No dependency change required.
- `MCPSettings.transport` is **TOML-only** (not in the env allowlist) — keep it that way.
- Default transport stays **`streamable_http`**; SSE is opt-in and must not change existing behavior.
- The MCP mount is gated on `server.enabled AND mcp.enabled` — unchanged.
- Follow the repo's documentation-sync convention: update `doc3gpp.toml.example`, `docs/web-server.md`, and `AGENTS.md` in the same change set when CLI/behaviour surface changes.
- No comments in code unless they explain non-obvious behavior (the existing `_mount_mcp_in_lifespan` docstring is such a case — preserve it).

---

### Task 1: Widen the transport setting

**Files:**
- Modify: `src/doc3gpp/settings/schema.py:644-687` (`MCPSettings`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `MCPSettings.transport: Literal["streamable_http", "sse"]` (default `"streamable_http"`). Task 2 reads `settings.mcp.transport`.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_settings.py` (create the file if it does not exist; check for an existing settings test module first and add there):

```python
def test_mcp_transport_accepts_sse() -> None:
    from doc3gpp.settings.schema import MCPSettings

    assert MCPSettings(transport="sse").transport == "sse"
    assert MCPSettings().transport == "streamable_http"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_settings.py -v`
Expected: FAIL with `Input should be 'streamable_http'` (the literal rejects `"sse"`).

- [ ] **Step 3: Widen the literal and update the docstring**

In `src/doc3gpp/settings/schema.py`, change:

```python
    transport: Literal["streamable_http"] = Field(
        default="streamable_http",
        description="HTTP transport used by the MCP mount.",
    )
```

to:

```python
    transport: Literal["streamable_http", "sse"] = Field(
        default="streamable_http",
        description=(
            "HTTP transport used by the MCP mount. streamable_http is a "
            "single POST endpoint; sse uses the legacy two-endpoint "
            "GET /sse + POST /messages/ protocol."
        ),
    )
```

Also update the class docstring paragraph that currently reads "``transport`` is locked to ``streamable_http`` for v1 — the older SSE-only transport is deprecated upstream..." to state that both `streamable_http` and `sse` are supported, with `streamable_http` the default.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_settings.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/settings/schema.py tests/unit/test_settings.py
git commit -m "feat(mcp): allow sse transport in MCPSettings"
```

---

### Task 2: Branch the MCP mount on transport

**Files:**
- Modify: `src/doc3gpp/web/app.py:76-113` (`_mount_mcp_in_lifespan`)

**Interfaces:**
- Consumes: `MCPSettings.transport` (from Task 1), `build_mcp_server(state)` (existing, returns `MCPServer`).
- Produces: mounts the MCP sub-app at `/mcp` with either `streamable_http_app(streamable_http_path="/")` (unchanged) or `sse_app(sse_path="/sse", message_path="/messages/")`. Both are Starlette apps; the surrounding `async with server.session_manager.run():` block is unchanged.

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_mcp_end_to_end.py`:

```python
def test_sse_transport_mounts_two_endpoints(sqlite_env) -> None:
    """transport='sse' mounts GET /mcp/sse and POST /mcp/messages/."""
    import asyncio

    from fastapi.testclient import TestClient

    from doc3gpp.settings.schema import CacheSettings, MCPSettings, ServerSettings, Settings
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.web.app import build_app

    state, _ = _state_and_server()
    app = build_app(
        Settings(
            server=ServerSettings(enabled=True, port=8765),
            mcp=MCPSettings(enabled=True, transport="sse"),
            cache=CacheSettings(dir=state.settings.cache.dir),
        )
    )
    with TestClient(app) as client:
        # GET /mcp/sse opens the SSE stream (may 200 or 405 depending on
        # SDK version; assert it is not a 404 so the route is mounted).
        resp = client.get("/mcp/sse")
        assert resp.status_code != 404, "sse endpoint not mounted"
        # POST /mcp/messages/ is the message endpoint.
        resp2 = client.post("/mcp/messages/", json={})
        assert resp2.status_code != 404, "messages endpoint not mounted"

    get_engine.cache_clear()
    del state.engine
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_mcp_end_to_end.py::test_sse_transport_mounts_two_endpoints -v`
Expected: FAIL — `GET /mcp/sse` returns 404 because the mount still uses `streamable_http_app`.

- [ ] **Step 3: Branch the mount on transport**

In `src/doc3gpp/web/app.py`, replace the single mount line:

```python
        server = build_mcp_server(app.state.web)
        app.mount("/mcp", server.streamable_http_app(streamable_http_path="/"))
        async with server.session_manager.run():
            yield
```

with:

```python
        server = build_mcp_server(app.state.web)
        if settings.mcp.transport == "sse":
            app.mount("/mcp", server.sse_app(sse_path="/sse", message_path="/messages/"))
        else:
            app.mount("/mcp", server.streamable_http_app(streamable_http_path="/"))
        async with server.session_manager.run():
            yield
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/integration/test_mcp_end_to_end.py::test_sse_transport_mounts_two_endpoints -v`
Expected: PASS.

- [ ] **Step 5: Run the full MCP + web test suite to confirm no regression**

Run: `pytest tests/integration/test_mcp_end_to_end.py tests/integration/test_web_*.py -q`
Expected: all PASS (the default `streamable_http` path is unchanged).

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/app.py tests/integration/test_mcp_end_to_end.py
git commit -m "feat(mcp): mount sse transport when configured"
```

---

### Task 3: Update config example and docs

**Files:**
- Modify: `doc3gpp.toml.example:276-278`
- Modify: `docs/web-server.md:76-79, 299-342`
- Modify: `AGENTS.md` (MCP row in the "Where to look" table, if it mentions transport)

**Interfaces:**
- Consumes: the `transport` literal values from Task 1 and the mount paths from Task 2.
- Produces: documentation only.

- [ ] **Step 1: Update `doc3gpp.toml.example`**

Replace:

```toml
# HTTP transport used by the MCP mount. v1 ships streamable_http only;
# older transports are intentionally absent.
# transport = "streamable_http"
```

with:

```toml
# HTTP transport used by the MCP mount. streamable_http (default) is a
# single POST endpoint; sse uses the legacy two-endpoint protocol
# (GET /sse + POST /messages/).
# transport = "streamable_http"
```

- [ ] **Step 2: Update `docs/web-server.md`**

In the `[mcp]` config block (around line 78), change the `transport` line comment to note both values. Then update the "MCP reference" section (around line 299) to describe both transports:

- `streamable_http` (default): single POST endpoint at `/mcp`.
- `sse`: two endpoints — `GET /mcp/sse` (event stream) and `POST /mcp/messages/` (client→server messages).

Add a short note that the SSE transport is for clients that only speak the legacy protocol, and that the tool set and JSON parity guarantees are identical across both transports.

- [ ] **Step 3: Update `AGENTS.md`**

In the "Add an MCP tool" row of the "Where to look" table, append a sentence noting the MCP mount supports both `streamable_http` and `sse` transports, selected via `[mcp] transport` in `doc3gpp.toml`.

- [ ] **Step 4: Verify docs render / no broken references**

Run: `rtk grep -n "streamable_http\|transport" docs/web-server.md doc3gpp.toml.example AGENTS.md`
Expected: the transport values and endpoints are consistent across all three files.

- [ ] **Step 5: Commit**

```bash
git add doc3gpp.toml.example docs/web-server.md AGENTS.md
git commit -m "docs(mcp): document sse transport option"
```

---

### Task 4: Full verification

**Files:**
- None (verification only).

- [ ] **Step 1: Run the full sqlite test suite**

Run: `./scripts/test_sqlite.sh`
Expected: all unit + integration tests pass.

- [ ] **Step 2: Run lint**

Run: `ruff check .`
Expected: no errors.

- [ ] **Step 3: Manual smoke test of the SSE transport**

Run the server with SSE transport and confirm both endpoints respond:

```bash
doc3gpp server run --port 8765 &
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/mcp/sse
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8765/mcp/messages/
```

Expected: neither returns 404 (the exact status depends on the SDK's handling of an uninitialised session, but the routes must be mounted). Kill the server afterwards.

- [ ] **Step 4: Commit any leftover changes**

```bash
git status
```

Expected: clean working tree (or only intentional changes already committed).

---

## Self-Review

**Spec coverage:**
- Widen transport literal → Task 1.
- Branch mount on transport → Task 2.
- Docs/config/AGENTS sync → Task 3.
- Verification → Task 4.

**Placeholder scan:** No TBD/TODO; every code step has concrete code and every test has concrete assertions.

**Type consistency:** `MCPSettings.transport` literal is widened in Task 1 and read in Task 2 as `settings.mcp.transport == "sse"`. `sse_app(sse_path="/sse", message_path="/messages/")` matches the SDK signature verified earlier. Mount paths `/mcp/sse` and `/mcp/messages/` are consistent between Task 2's test and Task 3's docs.
