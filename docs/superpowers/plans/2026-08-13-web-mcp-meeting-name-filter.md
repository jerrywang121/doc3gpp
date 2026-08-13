# Web + MCP Meeting Name Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `name` rich-filter input to the web `/meetings` filter form and route (matching the CLI's `meeting list --name`), and add MCP `list_meetings` tests that lock the existing behaviour.

**Architecture:** Wire the existing `MeetingRepository.list(name_like=...)` through the FastAPI route and Jinja template. No protocol / service / repo change. No new helper. The MCP `list_meetings` tool already accepts `name`; this plan adds tests that exercise it.

**Tech Stack:** Python 3.10+, FastAPI, Jinja2, HTMX, SQLAlchemy 2.0, Pydantic v2, Typer, mcp v2.

## Global Constraints

- Layered architecture is strict in `src/doc3gpp/`: `models/` never leaks ORM attrs; `services/` reaches storage only through `repository/` Protocols; `scraping/` is network-only; `parsers/` is parse-only.
- No new dependency.
- The web route's existing `parse_text_query` helper is the single seam for query-string → service-kwarg conversion. `name` must use it (mirrors `tsg` and `location`).
- The rich-filter grammar (`null` / `not-null` / `!pattern` / plain LIKE) is interpreted **downstream** by the SQL `name_like` clause; the route must forward the value verbatim (no string mutation), exactly like `tsg` and `location` do today.
- No comments in code unless the surrounding block documents non-obvious behavior (match existing style).
- Run `ruff check .` and the full sqlite suite (`./scripts/test_sqlite.sh`) before completion.
- The existing MCP-vs-HTTP parity test (`test_read_tools_parity_with_http_json`) must keep passing without modification — both sides go through `MeetingService.list_recent(name_like=None, …)` for empty args, so byte-for-byte equality holds.

---

### Task 1: Web route — add `name` query param + parse + forward

**Files:**
- Modify: `src/doc3gpp/web/routes/meetings.py:59-127` (`list_meetings` handler)
- Test: `tests/unit/test_web_routes.py` (new tests at end of file)

**Interfaces:**
- Consumes: existing `Query(default=None)` + `parse_text_query` (`src/doc3gpp/web/filters.py`).
- Produces: `name: str | None` query param; `parsed_name: str | None`; `MeetingService.list_recent(name_like=parsed_name, …)`; `filters["name"]` template context value.

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/test_web_routes.py` (after `test_meetings_list_empty_numeric_filter_returns_200` at line 967):

```python
def test_meeting_list_filters_form_has_name_input(client: TestClient) -> None:
    """``GET /meetings`` renders the Name filter input."""
    html = client.get("/meetings").text
    assert 'name="name"' in html


def test_meetings_list_name_filter_returns_200(client: TestClient) -> None:
    """``GET /meetings?name=SA2%23`` is 200 (pass-through to service)."""
    response = client.get("/meetings?name=SA2%23")
    assert response.status_code == 200


def test_meetings_list_name_filter_empty_returns_200(client: TestClient) -> None:
    """``GET /meetings?name=`` is 200, not 422 (empty form field)."""
    response = client.get("/meetings?name=&tsg=c6")
    assert response.status_code == 200
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/unit/test_web_routes.py::test_meeting_list_filters_form_has_name_input tests/unit/test_web_routes.py::test_meetings_list_name_filter_returns_200 tests/unit/test_web_routes.py::test_meetings_list_name_filter_empty_returns_200 -v`
Expected: FAIL — `name="name"` is not in the HTML and the route does not yet parse a `name` query string (FastAPI's 422 fires on `?name=…` because the route signature has no such param).

- [ ] **Step 3: Add `name` to the route signature and forward it**

In `src/doc3gpp/web/routes/meetings.py`, edit `list_meetings` (lines 59-127). Add a `name` parameter after `tsg`:

```python
async def list_meetings(
    request: Request,
    tsg: str | None = Query(default=None),
    name: str | None = Query(default=None),
    year: str | None = Query(default=None),
    location: str | None = Query(default=None),
    tdoc: str | None = Query(default=None),
    limit: str | None = Query(default="50"),
    offset: str | None = Query(default="0"),
    format: str | None = Query(default=None, alias="format"),
    service: MeetingService = Depends(get_meeting_service),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
```

In the handler body, after the existing `parsed_tsg = parse_text_query(tsg)` line, add:

```python
    parsed_name = parse_text_query(name)
```

Update the `service.list_recent(...)` call to forward `name_like=parsed_name`:

```python
    meetings = service.list_recent(
        limit=parsed_limit,
        offset=parsed_offset,
        tsg=parsed_tsg,
        name_like=parsed_name,
        location_like=parsed_location,
        year=parsed_year,
        tdoc_id=parsed_tdoc,
    )
```

Update the template `filters` context dict to include `"name": parsed_name or ""`:

```python
            "filters": {
                "tsg": parsed_tsg or "",
                "name": parsed_name or "",
                "year": year,
                "location": parsed_location or "",
                "tdoc": tdoc or "",
                "limit": parsed_limit,
            },
```

The `name` key is placed between `tsg` and `year` to mirror the CLI's `meeting list` flag order (`--tsg / --name / --location / --year / --tdoc`).

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `pytest tests/unit/test_web_routes.py::test_meeting_list_filters_form_has_name_input tests/unit/test_web_routes.py::test_meetings_list_name_filter_returns_200 tests/unit/test_web_routes.py::test_meetings_list_name_filter_empty_returns_200 -v`
Expected: PASS for the empty/pass-through cases. The form-input test still fails until Task 2 (form change) lands.

- [ ] **Step 5: Run the full web-routes suite to confirm no regression**

Run: `pytest tests/unit/test_web_routes.py -q`
Expected: PASS. (The existing `test_read_tools_parity_with_http_json` in the integration suite is unaffected — it exercises empty-args, where `name_like=None` on both sides.)

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/routes/meetings.py tests/unit/test_web_routes.py
git commit -m "feat(web): accept name filter on /meetings route"
```

---

### Task 2: Web form — add the `name` input

**Files:**
- Modify: `src/doc3gpp/web/templates/partials/meeting_filters.html`

**Interfaces:**
- Consumes: `filters["name"]` (template context value produced by Task 1).
- Produces: an `<input name="name">` rendered between the `tsg` and `year` labels; HTMX auto-includes it in the form submission.

- [ ] **Step 1: Add the `name` input** — in `src/doc3gpp/web/templates/partials/meeting_filters.html`, insert this block between the existing `TSG` and `Year` labels:

```html
  <label>Name
    <input type="text" name="name" value="{{ filters.name or '' }}">
  </label>
```

The full file becomes:

```html
<form
  hx-get="/meetings"
  hx-target="#results"
  hx-trigger="change, submit"
  hx-swap="outerHTML"
  class="filters"
>
  <label>TSG
    <input type="text" name="tsg" value="{{ filters.tsg or '' }}">
  </label>
  <label>Name
    <input type="text" name="name" value="{{ filters.name or '' }}">
  </label>
  <label>Year
    <input type="number" name="year" value="{{ filters.year or '' }}">
  </label>
  <label>Location
    <input type="text" name="location" value="{{ filters.location or '' }}">
  </label>
  <label>TDoc
    <input type="text" name="tdoc" value="{{ filters.tdoc or '' }}">
  </label>
  <label>Limit
    <input type="number" name="limit" value="{{ filters.limit }}" min="1" max="200">
  </label>
  <input type="hidden" name="offset" value="0">
  <button type="submit" class="btn">Apply</button>
</form>
```

- [ ] **Step 2: Run the form-input test to verify it passes**

Run: `pytest tests/unit/test_web_routes.py::test_meeting_list_filters_form_has_name_input -v`
Expected: PASS.

- [ ] **Step 3: Manually smoke-test the form round-trip** (optional)

If you have a working dev venv, start the web app and curl the form:

```bash
doc3gpp server start --port 8765 &
PID=$!
sleep 2
curl -s 'http://127.0.0.1:8765/meetings' | grep -o 'name="name"'
kill $PID 2>/dev/null || true
```

Expected: the curl output contains `name="name"`. If the `server start` command isn't wired in your venv, skip this step — the unit test in Step 2 already locks the contract.

- [ ] **Step 4: Commit**

```bash
git add src/doc3gpp/web/templates/partials/meeting_filters.html
git commit -m "feat(web): add name input to meeting filter form"
```

---

### Task 3: MCP — add `list_meetings` `name` filter tests

**Files:**
- Modify: `tests/integration/test_mcp_end_to_end.py` (new tests at end of file)

**Interfaces:**
- Consumes: existing `_state_and_server` + `_seed_corpus` helpers; seeded meeting is `meeting_id=156, name="SA2#156"`.
- Produces: two new tests that exercise the existing `list_meetings` MCP tool's `name` argument (no server code change).

- [ ] **Step 1: Write the failing tests** — append to `tests/integration/test_mcp_end_to_end.py` (next to `test_call_list_meetings_empty` at line 90):

```python
def test_call_list_meetings_name_filter(sqlite_env) -> None:
    """``list_meetings`` accepts a ``name`` filter and applies it to the seeded row."""
    import asyncio
    import json

    _state_and_server()  # runs create_schema()
    _seed_corpus()
    _, server = _state_and_server()

    async def run():
        return await server.call_tool("list_meetings", {"name": "%SA2%"})

    result = asyncio.run(run())
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert len(payload) == 1
    assert payload[0]["name"] == "SA2#156"


def test_call_list_meetings_name_no_match(sqlite_env) -> None:
    """``list_meetings`` with a no-match ``name`` pattern returns ``[]``."""
    import asyncio
    import json

    _state_and_server()  # runs create_schema()
    _seed_corpus()
    _, server = _state_and_server()

    async def run():
        return await server.call_tool("list_meetings", {"name": "no-match-%"})

    result = asyncio.run(run())
    assert result.is_error is False
    assert result.content[0].text == "[]"
```

- [ ] **Step 2: Run the new tests to verify they pass**

Run: `pytest tests/integration/test_mcp_end_to_end.py::test_call_list_meetings_name_filter tests/integration/test_mcp_end_to_end.py::test_call_list_meetings_name_no_match -v`
Expected: PASS — the MCP `list_meetings` tool already accepts `name` and the `MeetingRepository.list(name_like=...)` clause already applies the LIKE pattern, so no code change is needed; these tests lock the existing behaviour.

- [ ] **Step 3: Run the full MCP end-to-end suite to confirm no regression**

Run: `pytest tests/integration/test_mcp_end_to_end.py -q`
Expected: PASS — the parity test (`test_read_tools_parity_with_http_json`) at line 452 still compares empty-args bytes and is unaffected.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_mcp_end_to_end.py
git commit -m "test(mcp): cover list_meetings name filter"
```

---

### Task 4: Landing page — update the Meetings description

**Files:**
- Modify: `src/doc3gpp/web/routes/landing.py:35`

**Interfaces:**
- Consumes: none (copy-only).
- Produces: a one-line description update that mentions the new `name` filter.

- [ ] **Step 1: Update the description** — in `src/doc3gpp/web/routes/landing.py`, change the `Meetings` section entry (line 33-36) from:

```python
    {
        "label": "Meetings",
        "href": "/meetings",
        "description": "Browse stored meeting records, optionally filtered by TSG / year.",
    },
```

to:

```python
    {
        "label": "Meetings",
        "href": "/meetings",
        "description": "Browse stored meeting records, optionally filtered by TSG / name / year / location.",
    },
```

- [ ] **Step 2: Run the landing-page test to confirm it still passes**

Run: `pytest tests/unit/test_web_routes.py -q -k "landing"`
Expected: PASS. (The landing test asserts section presence, not description text, so the copy change is safe.)

- [ ] **Step 3: Commit**

```bash
git add src/doc3gpp/web/routes/landing.py
git commit -m "docs(web): mention name filter on Meetings landing section"
```

---

### Task 5: Web + CLI docs — note the parity

**Files:**
- Modify: `docs/web-server.md` (single sentence after the TDoc-sync-freshness paragraph at line 194-203)
- Modify: `docs/cli.md` (single line under `### doc3gpp meeting list` near line 220)

**Interfaces:**
- Consumes: existing doc prose.
- Produces: a one-sentence web-side note + a one-line CLI-side forward-pointer.

- [ ] **Step 1: Add the web-side sentence** — in `docs/web-server.md`, immediately after the existing sentence *"Clicking it enqueues the TDoc-list sync for that meeting…"* (around line 197-198), insert:

```markdown
The filter form supports TSG, name, year, location, and a TDoc id
selector, all with the same rich-filter grammar as the CLI.
```

- [ ] **Step 2: Add the CLI-side forward-pointer** — in `docs/cli.md`, immediately under the `### doc3gpp meeting list` heading (around line 218-220), add one line:

```markdown
The same filter set is available on the web at `/meetings`
(form-driven, HTMX-powered, JSON at `?format=json`).
```

(If the surrounding prose already notes web parity, skip the line — do not duplicate.)

- [ ] **Step 3: Run the docs cross-check** — search the repo for any other place that documents the web meeting filter (e.g. `README.md`, `AGENTS.md`, `docs/architecture.md`) and confirm no other drift needs fixing:

Run: `rg -n "filter.*name|name.*filter|/meetings" README.md AGENTS.md docs/architecture.md`
Expected: either no matches, or matches that already reflect the new state (no fix needed).

- [ ] **Step 4: Commit**

```bash
git add docs/web-server.md docs/cli.md
git commit -m "docs: note web /meetings name filter parity with CLI"
```

---

### Task 6: Final verification

**Files:**
- Read-only verification across the workspace.

- [ ] **Step 1: Lint**

Run: `ruff check .`
Expected: PASS — no new lint issues.

- [ ] **Step 2: Full sqlite test suite**

Run: `./scripts/test_sqlite.sh`
Expected: PASS — all unit + integration tests green; the new web route, form, and MCP tests pass; the existing parity test still passes.

- [ ] **Step 3: Spec ↔ plan cross-check**

Re-read `docs/superpowers/specs/2026-08-13-web-mcp-meeting-name-filter-design.md`. Confirm every requirement has a corresponding task:

- Web route accepts `name` with rich-filter grammar → Task 1.
- Web form has `name` input → Task 2.
- MCP `list_meetings` `name` argument is tested → Task 3.
- Landing copy updated → Task 4.
- Web + CLI docs note parity → Task 5.
- Out-of-scope items (auto-sync for web, parser refactor, new filters) are NOT implemented → confirmed by the absence of any auto-sync / helper-extraction work in the tasks above.

Expected: all spec requirements covered, no scope creep.

- [ ] **Step 4: Final commit (if any unstaged changes)**

If `git status` shows any leftover changes (e.g. a docs cross-check fix), stage and commit them with a `chore:` or `docs:` prefix. If clean, do nothing.
