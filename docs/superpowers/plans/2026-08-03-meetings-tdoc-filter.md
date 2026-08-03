# Meetings Page TDoc Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `TDoc` text filter to the web meetings list page (`GET /meetings`) that narrows results to meetings whose `start_doc`/`end_doc` range brackets the given TDoc id.

**Architecture:** The backend plumbing (service param, repo SQL, id validation) already exists and is used by the CLI `meeting list --tdoc` flag. This plan only wires the web layer: a `tdoc` query param on the `list_meetings` route parsed through `web.filters.parse_tdoc_id_query`, a form input in `meeting_filters.html`, and route tests.

**Tech Stack:** Python 3.10+, FastAPI, Jinja2 templates, pytest (httpx `TestClient`).

## Global Constraints

- Empty form field (`tdoc=`) must be 200, not 422 — declare the route param as `str | None = Query(default=None)` and parse only when non-empty.
- Malformed value → 400 with `{"error": "invalid_filter"}` envelope (via `InvalidFilterError` from `parse_tdoc_id_query`).
- No auto-sync: this route must not trigger auto-sync (matches current behavior).
- Valid TDoc ids use `parse_tdoc_id_query` (wraps `parse_tdoc_id` from `cli_filters.py`; regex `[RSC][1-9][-sw]\d{6,7}`, case-insensitive; returns `(prefix, int)`).
- No repository, service, or CLI changes.
- Lint gate: `ruff check .` must pass. Test gate: `./scripts/test_sqlite.sh` must pass.

---

### Task 1: Route + form wiring

**Files:**
- Modify: `src/doc3gpp/web/routes/meetings.py:54-116` (`list_meetings` route)
- Modify: `src/doc3gpp/web/templates/partials/meeting_filters.html:14-16` (after Location label)
- Test: `tests/unit/test_web_routes.py` (append tests near `test_meeting_list_filters_form_fields`, line ~573)

**Interfaces:**
- Consumes: `doc3gpp.web.filters.parse_tdoc_id_query(raw: str) -> tuple[str, int]` (raises `InvalidFilterError` on malformed input — already exported in the module's `__all__`); `MeetingService.list_recent(limit, offset, tsg=..., location_like=..., year=..., tdoc_id=tuple[str,int] | None)`.
- Produces: `GET /meetings?tdoc=<id>` route behavior; `filters["tdoc"]` string in the template context.

- [ ] **Step 1: Write the failing tests**

Append these tests to `tests/unit/test_web_routes.py` right after `test_meeting_list_filters_form_fields` (line ~584):

```python
def test_meeting_list_filters_form_has_tdoc_input(client: TestClient) -> None:
    """``GET /meetings`` renders the TDoc filter input."""
    html = client.get("/meetings").text
    assert 'name="tdoc"' in html


def test_meetings_list_tdoc_filter_returns_200(client: TestClient) -> None:
    """``GET /meetings?tdoc=R5-260013`` is 200 (pass-through to service)."""
    response = client.get("/meetings?tdoc=R5-260013")
    assert response.status_code == 200


def test_meetings_list_empty_tdoc_filter_returns_200(client: TestClient) -> None:
    """``GET /meetings?tdoc=`` is 200, not 422 (empty form field)."""
    response = client.get("/meetings?tdoc=&tsg=c6")
    assert response.status_code == 200


def test_meetings_list_invalid_tdoc_filter_returns_400(client: TestClient) -> None:
    """``GET /meetings?tdoc=not-a-tdoc`` is 400 with invalid_filter envelope."""
    response = client.get("/meetings?tdoc=not-a-tdoc")
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_filter"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_web_routes.py -k "tdoc" -v`
Expected: the four new tests FAIL (the route ignores `tdoc`; 200/400 assertions fail or the input is missing).

- [ ] **Step 3: Wire the route param**

In `src/doc3gpp/web/routes/meetings.py`:

1. Import `parse_tdoc_id_query` in the `web.filters` import on line 28:

```python
from doc3gpp.web.filters import (
    is_htmx_request,
    parse_int_query,
    parse_tdoc_id_query,
    parse_text_query,
)
```

2. Add the query param after `location` (line 58):

```python
    tdoc: str | None = Query(default=None),
```

3. Parse after `parsed_location` (line 81):

```python
    parsed_tdoc = parse_tdoc_id_query(tdoc) if tdoc else None
```

4. Pass it through to the service call (line 84-90):

```python
    meetings = service.list_recent(
        limit=parsed_limit,
        offset=parsed_offset,
        tsg=parsed_tsg,
        location_like=parsed_location,
        year=parsed_year,
        tdoc_id=parsed_tdoc,
    )
```

5. Add it to the `filters` context dict (line 109-114):

```python
            "filters": {
                "tsg": parsed_tsg or "",
                "year": year,
                "location": parsed_location or "",
                "tdoc": tdoc or "",
                "limit": parsed_limit,
            },
```

- [ ] **Step 4: Add the form input**

In `src/doc3gpp/web/templates/partials/meeting_filters.html`, after the Location label (line 16), before the Limit label:

```html
  <label>TDoc
    <input type="text" name="tdoc" value="{{ filters.tdoc or '' }}">
  </label>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_web_routes.py -k "tdoc or meeting_list" -v`
Expected: all four new tests PASS; existing meetings tests still PASS.

- [ ] **Step 6: Lint + full test gate**

Run: `ruff check .`
Run: `./scripts/test_sqlite.sh`
Expected: ruff clean; full suite PASS.

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/web/routes/meetings.py src/doc3gpp/web/templates/partials/meeting_filters.html tests/unit/test_web_routes.py
git commit -m "feat(web): TDoc filter on meetings list page"
```

---

### Task 2: Docs sync

**Files:**
- Modify: `docs/web-server.md` (meetings-page filters section)

**Interfaces:**
- Consumes: the `tdoc` query param and TDoc filter input added in Task 1.

- [ ] **Step 1: Update docs**

Find the meetings page section in `docs/web-server.md` that documents the filter form fields (`tsg` / `year` / `location` / `limit`). Add the `tdoc` filter to that list, noting:

- text input accepting a CR-shape TDoc id (e.g. `R5-260013`), matched against the meeting's `start_doc`/`end_doc` range;
- empty value is ignored;
- malformed value returns a 400 `invalid_filter` response.

- [ ] **Step 2: Commit**

```bash
git add docs/web-server.md
git commit -m "docs: document TDoc filter on meetings page"
```
