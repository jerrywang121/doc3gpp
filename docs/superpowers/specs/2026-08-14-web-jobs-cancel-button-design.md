# Web jobs page cancel button — design

**Date:** 2026-08-14
**Status:** Approved
**Author:** opencode + user
**Branch:** `web-jobs-cancel-button`

## Goal

Add a per-row **Cancel** button on the web `/jobs` list page so a user can
cooperatively cancel any RUNNING or QUEUED job in one click, with the row
updating in place to show the new terminal status.

Out of scope (explicit user ruling):

- Cancel button on the job detail page (`/jobs/{id}`). The user chose
  list-page-only.
- Bulk cancel / multi-select.
- A "Retry" or "Rerun" action on terminal rows.
- Any new JS file or `bindJobPolling`-style helper — the action is a
  one-shot POST, not a poll-then-refresh cycle.
- Auto-sync, auto-refresh, or any change to `JobWorkerHandle.cancel`'s
  semantics. The cancel endpoint is already idempotent on terminal jobs
  (returns the refreshed envelope without erroring) per the
  [idempotent-cancel spec](2026-08-13-idempotent-cancel-design.md).

## Background

Today the web jobs list page (`templates/job_status.html`) renders a
5-column table (ID, Kind, Status, Created, Action) where the action cell
contains only an `<a href="/jobs/{id}?format=html">show</a>` link. There
is no way to cancel a running job from the web — the only way to cancel
is the MCP `cancel_job` tool or the CLI's `doc3gpp jobs cancel` (which
both `POST /jobs/{id}/cancel`).

The underlying machinery is already complete:

- `POST /jobs/{id}/cancel` (`web/routes/jobs.py:485`) is the cancel route.
  It is idempotent: terminal jobs return the refreshed envelope with a
  `200` and the new status; RUNNING/QUEUED jobs set the worker's cancel
  event and return the current envelope.
- `JobWorkerHandle.cancel(job_id)` (`web/state.py:80`) sets the
  per-job `asyncio.Event` that the worker's `_claim_and_run` honours
  between handler iterations.
- HTMX is already loaded globally (see
  `templates/base.html` — `htmx.org` is the standard markup helper used by
  the sync hub and tdoc detail pages).

What is missing is the UI: a button on each RUNNING/QUEUED row that POSTs
to the cancel endpoint and refreshes the row in place.

## Approach

Use HTMX's `hx-post` + `hx-swap="outerHTML"` to swap the row in place.
Extract a shared row partial so the cancel button is defined once and
rendered by both the list table and (optionally, today) the detail
partial. Add a `?format=html` shape to `POST /jobs/{id}/cancel` that
returns the refreshed row HTML for HTMX to swap.

### Components

#### 1. New shared row partial — `web/templates/partials/_job_row.html`

Renders one `<tr id="job-row-{id}">` for the jobs list table. The action
cell conditionally renders:

- **RUNNING** or **QUEUED**:
  ```html
  <button class="btn small danger"
          hx-post="/jobs/{id}/cancel?format=html"
          hx-swap="outerHTML"
          hx-target="#job-row-{id}"
          hx-confirm="Cancel this job?">
      Cancel
  </button>
  ```
  alongside the existing `show` link.
- **SUCCEEDED / FAILED / CANCELLED**: only the `show` link (current
  behaviour).

The partial is rendered with a `Job` model and a `request` (for
`url_for` if needed in future). The row `id` attribute is the swap
target; HTMX replaces the entire `<tr>` after the POST returns the
refreshed row HTML.

#### 2. List page — `web/templates/job_status.html`

Replaces its inline `<tr>` loop with
`{% include 'partials/_job_row.html' %}` per row, passing the `Job` and
the Jinja `request`. Net change: ~6 lines removed, 1 `{% include %}`
added. The "next →" pagination link stays at the bottom (it is page
state, not row state).

#### 3. Cancel route — `web/routes/jobs.py:485`

`cancel_job` gains an optional `format: str | None = Query(default=None)`
parameter. Behaviour:

- `format=json` (or `format` unset): return the JSON envelope as today.
- `format=html`: render `partials/_job_row.html` with the refreshed
  `Job` and return `HTMLResponse`. Used by the list page's HTMX row
  swap.

The route renders the partial via `templates.TemplateResponse(
request=request, name="partials/_job_row.html", context={"job": job, ...})`
— the same shape used by `get_job(format="html")` at `jobs.py:391`.

The route keeps its current idempotency contract: a cancel on a
SUCCEEDED / FAILED / CANCELLED job sets no event (no-op) and returns
the refreshed envelope / row with the same terminal status.

#### 4. Detail page — `web/templates/partials/job_status.html`

**Not changed** (out of scope per user). The detail page still polls
every 2 s and will pick up the cancelled status naturally when the
worker transitions. If we want a Cancel button there later, it is a
trivial follow-up — same `hx-post` shape, but `hx-target="closest
.job-status"` to swap the whole detail panel with a terminal-state
render.

### Data flow

```
User clicks "Cancel" on row {id}
        |
        v
HTMX hx-post → POST /jobs/{id}/cancel?format=html
        |
        v
cancel_job route:
  - job = job_repo.get(id)
  - if status not terminal: handle.cancel(id)  # set cancel event
  - job = job_repo.get(id)  # refreshed envelope
  - if format == "html":
        return HTMLResponse(render_template("partials/_job_row.html", {"job": job, "request": request}))
  - else:
        return JSONResponse(_envelope(job))
        |
        v
HTMX outerHTML swap replaces the original <tr id="job-row-{id}">
with the new row (now showing CANCELLED, no Cancel button).
```

The worker independently observes the cancel event between handler
iterations and transitions the job to CANCELLED in the DB. The next
list refresh / next poll on the detail page picks up the new state. The
row swap immediately shows the cancel was *requested* (the envelope
captures status at the moment of the request — for QUEUED/RUNNING jobs
this is still QUEUED/RUNNING until the worker transitions, but the
Cancel button has disappeared because the row was re-rendered with the
post-cancel envelope). On most runs the worker transitions within a
few hundred ms; the next page refresh or detail poll picks it up.

### Error handling

- **Unknown `job_id`** (404): the row came from the list, so this is
  essentially unreachable in practice. HTMX surfaces the 404 to the
  user; no special handling.
- **Idempotent cancel on terminal job**: clicking Cancel on a row that
  has already reached SUCCEEDED/FAILED/CANCELLED is a no-op at the DB
  level (no cancel event set) and the route returns the refreshed row
  with the same terminal status. The Cancel button vanishes because
  the row template only renders it for RUNNING/QUEUED.
- **`hx-confirm`**: the browser shows a native confirm dialog
  ("Cancel this job?") before issuing the POST. This is the only
  confirmation prompt — no extra toast / modal / loading state.

### Testing

#### Unit tests (`tests/unit/test_web_jobs_routes.py`)

- `test_cancel_job_format_html_renders_row_for_running`: build a
  RUNNING job, mock `handle.cancel`, `POST /jobs/{id}/cancel?format=html`,
  assert 200, `Content-Type: text/html`, the response body contains
  `<tr id="job-row-{id}">` and a `cancelled` button rendered nowhere
  (the row still shows RUNNING at the moment of the request — that is
  fine). The test pins the HTMX wire shape: the `hx-post` attribute
  the row would re-emit targets the cancel URL.
- `test_cancel_job_format_html_renders_row_for_terminal_job`: build a
  CANCELLED job, `POST /jobs/{id}/cancel?format=html`, assert the
  rendered row has NO Cancel button and the status badge says
  `cancelled`. Idempotency check.
- `test_cancel_job_format_html_returns_404_for_unknown`: assert
  `JobNotFoundError` becomes a 404 (matches existing JSON route test).

#### Unit tests (`tests/unit/test_job_row_partial.py`, new file)

- `test_job_row_renders_cancel_button_for_running`: render the partial
  with a RUNNING job, assert "Cancel" button is present with the
  expected HTMX attributes.
- `test_job_row_renders_cancel_button_for_queued`: same with a QUEUED
  job.
- `test_job_row_omits_cancel_button_for_terminal_jobs`: render with
  SUCCEEDED, FAILED, CANCELLED jobs; assert no Cancel button.

#### Integration tests (`tests/integration/test_web_end_to_end.py`)

- `test_cancel_button_in_jobs_list_swaps_row`: in-memory sqlite, create
  a RUNNING job, `GET /jobs`, assert the row contains the Cancel
  button. `POST /jobs/{id}/cancel?format=html`, assert the response
  swaps successfully.

#### Manual verification

- `doc3gpp server` (or the running web UI): visit `/jobs`, click Cancel
  on a long-running job, confirm the row updates within a second and
  the status flips to `cancelled`.

### Files touched

- `src/doc3gpp/web/templates/partials/_job_row.html` (new)
- `src/doc3gpp/web/templates/job_status.html` (swap inline `<tr>` for
  `{% include %}`)
- `src/doc3gpp/web/routes/jobs.py` (add `format` param to
  `cancel_job`; render the row partial when `format=html`)
- `tests/unit/test_job_row_partial.py` (new)
- `tests/unit/test_web_jobs_routes.py` (add 3 tests)
- `tests/integration/test_web_end_to_end.py` (add 1 test)
- `docs/web-server.md` (document the Cancel button)
- `docs/code-map.md` (note the new partial)

No new JS file, no new dependency, no new endpoint shape beyond the
`format=html` query param on the existing cancel route.