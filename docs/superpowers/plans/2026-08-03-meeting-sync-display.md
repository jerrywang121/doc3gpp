# Meeting Sync Display + One-Click Sync (web) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On the web surface, show each meeting's last TDoc-list sync state — plus `start_doc`/`end_doc` — on the list and detail pages, make the list's sync symbol one-click-trigger the TDoc-list sync, and link the detail page to the meeting's TDocs.

**Architecture:** Pure template + CSS + Jinja-filter changes in `src/doc3gpp/web/`. Two new Jinja filters (`dt_short`, `sync_state`) format the aware-UTC `Meeting.tdoc_list_last_sync` and classify freshness against `datetime.now(timezone.utc)`. The meeting list partial gains three columns (Start doc / End doc / Sync); the Sync cell is an HTMX form posting the hidden `meeting_id` to the existing `POST /jobs/sync_tdocs` job route (`hx-swap="none"` swallows the 202 JSON body). The detail page gains k-v rows and a tdoc-list link. No route, service, model, DB, JSON, MCP, or CLI behavior changes.

**Tech Stack:** Python 3.10+, FastAPI, Jinja2 (single shared `Jinja2Templates` instance), HTMX, pytest (unit tests, `pythonpath=src`).

## Global Constraints

- **Source of truth spec:** `docs/superpowers/specs/2026-08-03-meeting-sync-display-design.md` (approved).
- **No auto-commit.** Repo convention (`docs/conventions.md` §Commits): "The project does **not** auto-commit… User-driven commits happen in **one** commit each." Therefore the per-task `git commit` steps in this plan are **omitted**; the user commits once at the end (Conventional Commits style `feat(web): …`). Do not run `git commit` during implementation.
- **JSON/MCP byte-parity is frozen.** Do NOT touch `_MEETING_DEFAULT_FIELDS` (`src/doc3gpp/web/routes/meetings.py:40`), `output.fields.meeting`, `web/render.py:meeting_rows`, or `web/mcp_server.py`. The web `?format=json` list payload must stay identical to the CLI's.
- `Meeting.tdoc_list_last_sync` is always **aware UTC** (see `_as_utc` in `src/doc3gpp/storage/repositories/meeting_sql.py:180`). Always compare against `datetime.now(timezone.utc)`.
- `Meeting` is `@dataclass(slots=True)` — no monkey-patching.
- Lint: `ruff check .` must pass. Tests: `pytest -q tests/unit` then `./scripts/test_sqlite.sh` (unit + integration, sqlite-only) must pass. Online tests are opt-in and skipped.
- Jinja autoescaping is ON: the literal `&` in an `href` renders as `&amp;` in HTML (assertions must use the escaped form).
- Do not add code comments unless the surrounding code style already justifies them.

---

### Task 1: Jinja filters `dt_short` + `sync_state`

**Files:**
- Modify: `src/doc3gpp/web/templates_setup.py`
- Test: `tests/unit/test_web_template_filters.py` (new)

**Interfaces:**
- Produces (used by Task 2 and Task 3 templates):
  - `dt_short(value: datetime | None) -> str | None` — `value.strftime("%Y-%m-%d %H:%M")`, `None` in → `None` out.
  - `sync_state(value: datetime | None) -> str` — `"never"` for `None`; `"fresh"` when `value >= utcnow - 24h`; else `"stale"`.
- Both registered on the shared `templates.env.filters` (so templates use `| dt_short` / `| sync_state`).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_web_template_filters.py`:

```python
"""Unit tests for the shared Jinja template filters used by the web UI."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from doc3gpp.web.templates_setup import dt_short, sync_state


def test_dt_short_formats_aware_datetime() -> None:
    dt = datetime(2026, 8, 3, 14, 5, 30, tzinfo=timezone.utc)
    assert dt_short(dt) == "2026-08-03 14:05"


def test_dt_short_none_returns_none() -> None:
    assert dt_short(None) is None


def test_sync_state_fresh_within_24h() -> None:
    value = datetime.now(timezone.utc) - timedelta(hours=1)
    assert sync_state(value) == "fresh"


def test_sync_state_fresh_exactly_24h() -> None:
    value = datetime.now(timezone.utc) - timedelta(hours=24)
    assert sync_state(value) == "fresh"


def test_sync_state_stale_older_than_24h() -> None:
    value = datetime.now(timezone.utc) - timedelta(hours=25)
    assert sync_state(value) == "stale"


def test_sync_state_never_for_none() -> None:
    assert sync_state(None) == "never"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest -q tests/unit/test_web_template_filters.py`
Expected: FAIL with `ImportError: cannot import name 'dt_short' from 'doc3gpp.web.templates_setup'`.

- [ ] **Step 3: Implement the filters**

In `src/doc3gpp/web/templates_setup.py`, change the import block (currently lines 11-15):

```python
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
```

After the `templates.env.globals["url_for"] = _url_for` line (line 38), add:

```python
def dt_short(value: datetime | None) -> str | None:
    """Format a (UTC) datetime as ``YYYY-MM-DD HH:MM``; ``None`` stays ``None``."""
    if value is None:
        return None
    return value.strftime("%Y-%m-%d %H:%M")


def sync_state(value: datetime | None) -> str:
    """Classify sync freshness: ``never`` / ``fresh`` (<=24h) / ``stale`` (>24h)."""
    if value is None:
        return "never"
    if value >= datetime.now(timezone.utc) - timedelta(hours=24):
        return "fresh"
    return "stale"


templates.env.filters["dt_short"] = dt_short
templates.env.filters["sync_state"] = sync_state
```

Leave the existing `_url_for` / `static_files` / `mount_static` blocks untouched.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest -q tests/unit/test_web_template_filters.py`
Expected: 6 passed.

---

### Task 2: Meeting list — Start doc / End doc / Sync columns + CSS

**Files:**
- Modify: `src/doc3gpp/web/templates/partials/meeting_results.html`
- Modify: `src/doc3gpp/web/static/style.css`
- Modify: `tests/unit/test_web_routes.py`

**Interfaces:**
- Consumes: `dt_short`, `sync_state` (Task 1).
- Produces: the `sync-btn` / `sync-fresh` / `sync-stale` / `sync-never` CSS contract and the sync-button HTML contract (used for the list; the detail page reuses `dt_short` only).

- [ ] **Step 1: Extend the fake data + write the failing tests**

In `tests/unit/test_web_routes.py`:

(a) Change the import on line 15 (`from datetime import date`) to:

```python
from datetime import date, datetime, timedelta, timezone
```

(b) Replace the body of `FakeMeetingService.__init__` (lines 63-82) with:

```python
    def __init__(self) -> None:  # noqa: D401 - intentional override
        self._now = datetime.now(timezone.utc)
        self._meetings = [
            Meeting(
                meeting_id=1,
                name="RAN5#99-e",
                title="RAN WG5 Meeting #99-e",
                location="Athens, Greece",
                start_date=date(2026, 5, 1),
                end_date=date(2026, 5, 5),
                start_doc="R5-260001",
                end_doc="R5-260500",
                tsg="R5",
            ),
            Meeting(
                meeting_id=2,
                name="SA2#150-e",
                title="SA WG2 Meeting #150-e",
                location="Online",
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 5),
                start_doc="S2-260001",
                end_doc="S2-260400",
                tsg="S2",
                tdoc_list_last_sync=self._now - timedelta(hours=1),
            ),
            Meeting(
                meeting_id=3,
                name="CT1#140-e",
                title="CT WG1 Meeting #140-e",
                location="Online",
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 3),
                start_doc="C1-260001",
                end_doc="C1-260200",
                tsg="C1",
                tdoc_list_last_sync=self._now - timedelta(hours=48),
            ),
        ]
```

  Meeting 1 = never synced (`None`), meeting 2 = fresh (~1h ago), meeting 3 = stale (~48h ago).

(c) After `test_meetings_list_htmx_returns_partial` (line ~321), add:

```python
def test_meetings_list_shows_sync_columns(client: TestClient) -> None:
    """The list table carries Start doc / End doc / Sync columns and values."""
    html = client.get("/meetings").text
    assert "<th>Start doc</th>" in html
    assert "<th>End doc</th>" in html
    assert "<th>Sync</th>" in html
    assert "R5-260001" in html
    assert "R5-260500" in html
    assert "S2-260001" in html
    assert "S2-260400" in html


def test_meetings_list_sync_button_freshness_classes(client: TestClient) -> None:
    """Each row's sync button carries the freshness class (grey/green/orange)."""
    html = client.get("/meetings").text
    assert 'class="sync-btn sync-never"' in html
    assert 'class="sync-btn sync-fresh"' in html
    assert 'class="sync-btn sync-stale"' in html


def test_meetings_list_sync_button_posts_meeting_id(client: TestClient) -> None:
    """The sync button is an HTMX form posting the meeting_id to the job route."""
    html = client.get("/meetings").text
    assert 'hx-post="/jobs/sync_tdocs"' in html
    assert 'hx-swap="none"' in html
    assert "&#8635;" in html
    for meeting_id in (1, 2, 3):
        assert f'name="meeting_id" value="{meeting_id}"' in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q tests/unit/test_web_routes.py -k "sync_column or sync_button" -v`
Expected: the three new tests FAIL (columns / classes / forms missing).

- [ ] **Step 3: Implement the partial template**

In `src/doc3gpp/web/templates/partials/meeting_results.html`, replace lines 5-13 (header row) with:

```html
        <tr>
          <th>ID</th>
          <th>Name</th>
          <th>TSG</th>
          <th>Start</th>
          <th>End</th>
          <th>Location</th>
          <th>Start doc</th>
          <th>End doc</th>
          <th>Sync</th>
          <th></th>
        </tr>
```

Replace the `<td>{{ meeting.location or '-' }}</td>` line (line 23) with:

```html
            <td>{{ meeting.location or '-' }}</td>
            <td>{{ meeting.start_doc or '-' }}</td>
            <td>{{ meeting.end_doc or '-' }}</td>
            <td>
              <form hx-post="/jobs/sync_tdocs" hx-swap="none"
                    title="Last sync: {{ meeting.tdoc_list_last_sync | dt_short or 'never' }}">
                <input type="hidden" name="meeting_id" value="{{ meeting.meeting_id }}">
                <button type="submit" class="sync-btn sync-{{ meeting.tdoc_list_last_sync | sync_state }}"
                        aria-label="Sync TDocs for meeting {{ meeting.meeting_id }}">&#8635;</button>
              </form>
            </td>
```

- [ ] **Step 4: Implement the CSS**

Append to the end of `src/doc3gpp/web/static/style.css`:

```css
.sync-btn {
  background: none;
  border: none;
  padding: 0;
  font-size: 1.1rem;
  line-height: 1;
  cursor: pointer;
  text-decoration: none;
}

.sync-fresh {
  color: #1a7f37;
}

.sync-stale {
  color: #bc4c00;
}

.sync-never {
  color: #8c959f;
}

.sync-btn:hover {
  opacity: 0.7;
}

.muted {
  color: var(--muted);
}
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `pytest -q tests/unit/test_web_routes.py -k "sync_column or sync_button" -v`
Expected: 3 passed.

- [ ] **Step 6: Run the whole meeting+web suite for regressions**

Run: `pytest -q tests/unit/test_web_routes.py tests/unit/test_web_template_filters.py`
Expected: all pass, including the pre-existing `test_meetings_list_json_matches_cli_rows`, `test_meetings_list_htmx_returns_partial`, `test_meeting_show_json`, `test_meeting_show_renders_html`.

---

### Task 3: Meeting detail — Start doc / End doc / Last sync / TDocs link

**Files:**
- Modify: `src/doc3gpp/web/templates/meeting_show.html`
- Modify: `tests/unit/test_web_routes.py`

**Interfaces:**
- Consumes: `dt_short` (Task 1) and the `muted` CSS class (Task 2, Step 4).
- Produces: detail-page k-v rows + the `View TDocs for this meeting` anchor.

- [ ] **Step 1: Write the failing tests**

After `test_meeting_show_json` (line ~338), add:

```python
def test_meeting_show_renders_sync_fields(client: TestClient) -> None:
    """The detail page shows Start doc / End doc / Last sync rows."""
    html = client.get("/meetings/1").text
    assert "<dt>Start doc</dt>" in html
    assert "<dt>End doc</dt>" in html
    assert "<dt>Last sync</dt>" in html
    assert "<dd>Never</dd>" in html


def test_meeting_show_renders_formatted_last_sync(client: TestClient) -> None:
    """A synced meeting shows YYYY-MM-DD HH:MM UTC, never 'Never'."""
    html = client.get("/meetings/2").text
    last_sync = FakeMeetingService().get_by_id(2).tdoc_list_last_sync
    assert last_sync is not None
    formatted = last_sync.strftime("%Y-%m-%d %H:%M")
    previous_minute = (last_sync - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
    # The app's fake was built a moment before this service instance; tolerate
    # a wall-clock minute rollover between the two constructions.
    assert formatted in html or previous_minute in html
    assert "UTC" in html
    assert "Never" not in html


def test_meeting_show_links_to_tdocs(client: TestClient) -> None:
    """The detail page links to the meeting's TDocs pre-filtered by meeting id."""
    html = client.get("/meetings/1").text
    assert "View TDocs for this meeting" in html
    assert 'href="/tdocs?meeting_id=1&amp;limit=200"' in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -q tests/unit/test_web_routes.py -k "meeting_show_renders_sync_fields or meeting_show_renders_formatted_last_sync or meeting_show_links_to_tdocs" -v`
Expected: 3 FAIL (rows / formatted value / link missing).

- [ ] **Step 3: Implement the template**

In `src/doc3gpp/web/templates/meeting_show.html`, replace the `<dl class="kv">` block (lines 10-15) with:

```html
  <dl class="kv">
    {% if meeting.start_date %}<dt>Start</dt><dd>{{ meeting.start_date.isoformat() }}</dd>{% endif %}
    {% if meeting.end_date %}<dt>End</dt><dd>{{ meeting.end_date.isoformat() }}</dd>{% endif %}
    {% if meeting.start_doc %}<dt>Start doc</dt><dd>{{ meeting.start_doc }}</dd>{% endif %}
    {% if meeting.end_doc %}<dt>End doc</dt><dd>{{ meeting.end_doc }}</dd>{% endif %}
    {% if meeting.location %}<dt>Location</dt><dd>{{ meeting.location }}</dd>{% endif %}
    {% if meeting.ftp_url %}<dt>FTP URL</dt><dd><code>{{ meeting.ftp_url }}</code></dd>{% endif %}
    <dt>Last sync</dt>
    <dd>{% if meeting.tdoc_list_last_sync %}{{ meeting.tdoc_list_last_sync | dt_short }} <span class="muted">UTC</span>{% else %}Never{% endif %}</dd>
  </dl>

  <p><a href="/tdocs?meeting_id={{ meeting.meeting_id }}&amp;limit=200">View TDocs for this meeting</a></p>
```

Note the `&amp;` is intentional: it renders the literal `&` in the URL while keeping the HTML well-formed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -q tests/unit/test_web_routes.py -k "meeting_show_renders_sync_fields or meeting_show_renders_formatted_last_sync or meeting_show_links_to_tdocs" -v`
Expected: 3 passed.

- [ ] **Step 5: Run the full meeting+web suite for regressions**

Run: `pytest -q tests/unit/test_web_routes.py tests/unit/test_web_template_filters.py`
Expected: all pass.

---

### Task 4: Docs sync + full validation

**Files:**
- Modify: `docs/web-server.md`

- [ ] **Step 1: Update the web-server docs**

In `docs/web-server.md`, update the `/meetings` route row (line 159) to mention the sync indicator, and add a sentence after the routes table (after line 180):

Row change:

```markdown
| GET | `/meetings` | List meetings (`?format=json`); shows `start_doc`/`end_doc` and a coloured, clickable sync symbol (`↻`) per meeting. |
```

After line 180 ("Append `?format=json` …"), add:

```markdown
The meeting list shows a `↻` sync symbol per meeting coloured by TDoc-list
sync freshness — green (`≤ 24h` ago), orange (`> 24h` ago), grey (never
synced). Clicking it enqueues the TDoc-list sync for that meeting (same job
as the detail page's sync button). The meeting detail page shows
`start_doc`/`end_doc`, the last-sync timestamp (`YYYY-MM-DD HH:MM UTC`), and
a link to the meeting's TDocs.
```

- [ ] **Step 2: Lint**

Run: `ruff check .`
Expected: no errors (you have not introduced new files with unused imports).

- [ ] **Step 3: Full unit suite**

Run: `pytest -q tests/unit`
Expected: all unit tests pass.

- [ ] **Step 4: Full sqlite suite**

Run: `./scripts/test_sqlite.sh`
Expected: script exits 0 (unit + integration, sqlite-only).

## Self-Review

1. **Spec coverage** — list columns ✓ (Task 2), sync freshness classes ✓ (Task 2), clickable sync via `/jobs/sync_tdocs` ✓ (Task 2), detail `start_doc`/`end_doc`/`last sync` + `YYYY-MM-DD HH:MM` ✓ (Task 3), `View TDocs` link `?meeting_id=<id>&limit=200` ✓ (Task 3), filters ✓ (Task 1), CSS ✓ (Task 2), tests ✓ (Tasks 1-3), JSON/MCP untouched ✓ (Global Constraints). No gaps.
2. **Placeholder scan** — all steps contain concrete code/commands; no TBD/TODO. ✓
3. **Type consistency** — `dt_short`/`sync_state` defined in Task 1 with the exact names/signatures consumed by the Task 2/3 templates and asserted in tests; `.muted` introduced in Task 2 Step 4 and used in Task 3. Fake meeting ids 1/2/3 are consistent across Tasks 2-3 and their tests. ✓
