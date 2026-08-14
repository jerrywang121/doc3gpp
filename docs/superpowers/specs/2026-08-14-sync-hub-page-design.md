# Sync Hub Page Design

**Date:** 2026-08-14
**Status:** Draft
**Branch:** feat/sync-hub-page

## Goal

Add a top-level web page at `GET /sync` that exposes every sync-shaped MCP
tool as a stacked panel, mirroring the MCP server's enqueue surface one-to-one
and adding the missing HTTP route for `parse_tdoc_url`. The hub reuses the
existing job-poll + enqueue plumbing; the only new server-side surface is the
`POST /jobs/parse/tdoc-url` route and the `/sync` GET pair (full page +
fragment for HTMX refresh of the recent-jobs table).

## Background

Today the web surface has scattered sync triggers: a per-meeting sync form
on `meeting_show.html`, a per-spec sync form on `spec_show.html`, and a
parse card on `tdoc_show.html`. The MCP server exposes ten sync-shaped tools
that users reach via JSON-over-HTTP, but there is no equivalent control
surface in the web app for any of them except the per-meeting sync form.

The MCP server's tools and their HTTP enqueue routes (in
`src/doc3gpp/web/routes/jobs.py`):

| MCP tool | HTTP enqueue route | Status |
| --- | --- | --- |
| `sync_meetings(tsg)` | `POST /jobs/sync/meetings` | exists |
| `sync_tdocs(meeting_id)` | `POST /jobs/sync/tdocs` | exists |
| `sync_tdocs_by_meeting(meeting)` | `POST /jobs/sync/tdocs` | exists (same route, `meeting_name` param) |
| `sync_all_tdocs()` | `POST /jobs/sync/tdocs/all` | exists |
| `sync_specs(tsg, spec_id, force, per_version_details)` | `POST /jobs/sync/specs` | exists |
| `parse_tdocs(filter, force, full, max_batch)` | `POST /jobs/parse/tdocs` | exists |
| `parse_tdoc_url(url, recursive, max_depth, force, full)` | `POST /jobs/parse/tdoc-url` | **missing** |
| `rebuild_search_index(stale_only, resume)` | `POST /jobs/search/rebuild` | exists |
| `purge_cache(scope, yes)` | `POST /jobs/cache/purge` | exists |

The hub gives the user one URL where every one of these is a stacked panel.

## Approach

A single `GET /sync` page with eight stacked `<section class="card">` panels
(one per enqueue route, plus a ninth section for the recent-jobs table at the
bottom). All panels use the existing `bindJobPolling` helper from
`job_poller.js` so the submit + poll + terminal UX matches every other sync
form on the site. The hub's only behavioural twist is the terminal action:
when a panel's job terminates, the bottom recent-jobs table is refreshed via
an HTMX `outerHTML` swap against `GET /sync?format=fragment` instead of a
full `location.reload()`. The shared helper gets an `onTerminal` callback
option; the two existing call sites (`meeting_sync.js`, `spec_sync.js`) pass
`onTerminal: () => window.location.reload()` so their UX is unchanged.

The TSG field on the Meeting sync and Spec sync panels is a free-text input
(not a dropdown). The TDoc sync panel exposes a radio toggle between
numeric meeting id and meeting name (mirrors the MCP `sync_tdocs(meeting_id)`
and `sync_tdocs_by_meeting(meeting)` shapes). The Parse-from-filter panel
mirrors the tdoc list page's filter form; the Parse-from-URL panel is a
small form for `parse_tdoc_url`.

## Page composition

```text
GET /sync (templates/sync.html)
  <h1>Sync hub</h1>
  <p class="lead">One place to enqueue every sync-shaped job.</p>

  Panel 1 — Meeting sync
    <form action="/jobs/sync/meetings" id="sync-meetings-form">
      TSG [text input]   Force sync [checkbox]
      [Sync meetings]   <span class="sync-queued">queued</span>
      <div id="sync-meetings-form-job-target"></div>

  Panel 2 — TDoc sync (single meeting)
    <form action="/jobs/sync/tdocs" id="sync-tdocs-form">
      (•) Meeting id   ( ) Meeting name
      [text input for the chosen field]
      Force sync [checkbox]
      [Sync TDocs for this meeting]   <span class="sync-queued">queued</span>
      <div id="sync-tdocs-form-job-target"></div>

  Panel 2b — TDoc sync (all tracked meetings)
    <form action="/jobs/sync/tdocs/all" id="sync-tdocs-all-form">
      Force sync [checkbox]
      [Sync TDocs for ALL tracked meetings]   <span class="sync-queued">queued</span>
      <div id="sync-tdocs-all-form-job-target"></div>

  Panel 3 — Spec sync (by TSG)
    <form action="/jobs/sync/specs" id="sync-specs-tsg-form">
      TSG [text input]
      Force sync [checkbox]   Per-version details [checkbox]
      [Sync specs for this TSG]   <span class="sync-queued">queued</span>
      <div id="sync-specs-tsg-form-job-target"></div>

  Panel 3b — Spec sync (by id)
    <form action="/jobs/sync/specs" id="sync-specs-id-form">
      Spec id [text input, e.g. 36.579-5]
      Force sync [checkbox]   Per-version details [checkbox]
      [Sync this spec]   <span class="sync-queued">queued</span>
      <div id="sync-specs-id-form-job-target"></div>

  Panel 4 — Parse TDocs (filter-driven)
    <form action="/jobs/parse/tdocs" id="parse-tdocs-form">
      Filter inputs mirroring partials/tdoc_filters.html
        (tdoc_id, meeting, meeting_id, status, spec, wi,
         release, version, cr_num, cr_pack, source, ftp_url,
         title, revision_of, revised_to, tdoc_type, uploaded_date)
      Force [checkbox]   Full [checkbox]
      Max batch [number, optional]
      [Queue parse]   <span class="sync-queued">queued</span>
      <div id="parse-tdocs-form-job-target"></div>

  Panel 5 — Parse from URL
    <form action="/jobs/parse/tdoc-url" id="parse-tdoc-url-form">
      URL [text input, must be https://www.3gpp.org/ftp/...]
      (•) Recursive [on|off]  ( ) Max depth [number]
      Force [checkbox]   Full [checkbox]
      [Queue parse]   <span class="sync-queued">queued</span>
      <div id="parse-tdoc-url-form-job-target"></div>

  Panel 6 — Rebuild search index
    <form action="/jobs/search/rebuild" id="rebuild-search-form">
      Stale only [checkbox]   Resume [checkbox]
      [Rebuild FTS5 index]   <span class="sync-queued">queued</span>
      <div id="rebuild-search-form-job-target"></div>

  Panel 7 — Purge cache
    <form action="/jobs/cache/purge" id="purge-cache-form">
      Scope [select: markdown | zips | all]
      Confirm purge: yes [checkbox]
      [Purge cache]   <span class="sync-queued">queued</span>
      <div id="purge-cache-form-job-target"></div>

  Panel 8 — Recent sync jobs
    <div id="recent-jobs"
         hx-get="/sync?format=fragment"
         hx-trigger="load"
         hx-swap="outerHTML">
      {# initial render via template include #}
    </div>
```

All eight forms submit JSON bodies via `bindJobPolling`'s `buildBody`
override. The TDoc sync panel uses a radio switch: when "Meeting id" is
selected, `buildBody` emits `{meeting_id: int(text)}`; when "Meeting name" is
selected, `{meeting_name: text}`. The Spec panels are distinct forms (one
per target shape) because the MCP server requires exactly one of `tsg` or
`spec_id` — a single form with a toggle would force the JS to mutate the
body shape on every click and re-bind, which is more code than two static
forms.

## Data flow

```text
User clicks panel submit
  → sync_hub.js submits form via fetch(form.action, {body: JSON.stringify(...), headers: {'Content-Type':'application/json'}})
  → server route returns 202 + {job_id, status, links}
  → sync_hub.js calls attachPolling() from job_poller.js
  → HTMX swap loads /jobs/{id}?format=html → partials/job_status.html
  → polls every 2s until terminal
  → installTerminalObserver detects polling span disappear
  → invokes the onTerminal callback (provided by sync_hub.js):
       htmx.ajax('GET', '/sync?format=fragment',
                 {target:'#recent-jobs', swap:'outerHTML'})
  → Bottom #recent-jobs div swaps to the latest 10 jobs.
  → No full page reload.
```

## Server-side changes

### 1. New route — `GET /sync` and `GET /sync?format=fragment`

`src/doc3gpp/web/routes/sync.py` (new `APIRouter`):

```python
router = APIRouter(prefix="/sync", tags=["sync"])

_LIMIT_RECENT = 10

@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def sync_hub(
    request: Request,
    format: str | None = Query(default=None, alias="format"),
    job_repo: JobRepository = Depends(get_job_repo),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    jobs = job_repo.list(limit=_LIMIT_RECENT) or []
    if format == "fragment":
        return templates.TemplateResponse(
            request=request,
            name="partials/sync_recent_jobs.html",
            context={"jobs": jobs},
        )
    return templates.TemplateResponse(
        request=request,
        name="sync.html",
        context={
            "active_nav": "sync",
            "recent_jobs": jobs,
            "pending_jobs": pending_jobs,
        },
    )
```

### 2. New enqueue route — `POST /jobs/parse/tdoc-url`

`src/doc3gpp/web/routes/jobs.py` (added near `post_parse_tdocs`):

```python
class _ParseTDocURLBody(BaseModel):
    url: str
    recursive: bool = False
    max_depth: int = 2
    force: bool = False
    full: bool = False


@router.post("/parse/tdoc-url", status_code=202)
async def post_parse_tdoc_url(
    body: _ParseTDocURLBody,
    job_repo: JobRepository = Depends(get_job_repo),
) -> JSONResponse:
    from doc3gpp.parsers.direct_extractor import is_3gpp_ftp_url
    if not is_3gpp_ftp_url(body.url):
        raise InvalidFilterError(
            f"url must be a 3GPP FTP URL (https://www.3gpp.org/ftp/...); got {body.url!r}"
        )
    if body.recursive and body.max_depth != 2:
        raise InvalidFilterError(
            "recursive and max_depth are mutually exclusive; set one or the other"
        )
    params: dict[str, JSONValue] = {
        "url": body.url,
        "force": body.force,
        "full": body.full,
        "recursive": body.recursive,
    }
    if not body.recursive:
        params["max_depth"] = body.max_depth
    job = job_repo.create(JobKind.PARSE_TDOC_URL, params)
    return JSONResponse(status_code=202, content=_envelope(job, queued=True))
```

Validation mirrors the MCP tool's `_mcp_error_guard` checks exactly.

### 3. Nav + landing

- `src/doc3gpp/web/templates/base.html`: add a `Sync` link next to the
  `Jobs` link with the same `pending_jobs` badge.
- `src/doc3gpp/web/routes/landing.py`: add `{"label": "Sync", "href": "/sync",
  "description": "Enqueue every sync-shaped job (meetings, tdocs, specs, parse, search, cache)."}`
  to `_SECTIONS`.
- `src/doc3gpp/web/routes/__init__.py`: add `sync_router` to `all_routers()`.

### 4. JavaScript — terminal callback on `bindJobPolling`

`src/doc3gpp/web/static/js/job_poller.js`:

- Add an optional `onTerminal(form, target, jobId, queued)` to
  `bindJobPolling`'s options.
- Default: `function () { window.location.reload(); }`.
- Pass it through to `installTerminalObserver`, which calls it instead of
  `location.reload()` when `pollSeen` flips to false (or the
  `TERMINAL_FALLBACK_MS` fires).
- Update the existing two call sites:
  - `src/doc3gpp/web/static/js/meeting_sync.js`: pass
    `onTerminal: function () { window.location.reload(); }`.
  - `src/doc3gpp/web/static/js/spec_sync.js`: same.

### 5. Page-local JS — `src/doc3gpp/web/static/js/sync_hub.js` (new)

Binds every form on the page with `data-bind="sync-hub"` (defensive —
keeps the script from accidentally binding forms the user adds via
HTMX-loaded partials in the future). For each, call
`window.bindJobPolling(form, {contentType: 'application/json', buildBody:
fn, onTerminal: refreshRecentJobs})`. The `refreshRecentJobs` helper is:

```js
function refreshRecentJobs() {
  if (window.htmx && window.htmx.ajax) {
    window.htmx.ajax('GET', '/sync?format=fragment',
                     {target: '#recent-jobs', swap: 'outerHTML'});
  } else {
    window.location.reload();
  }
}
```

`buildBody` per form:

| Form id | Body |
| --- | --- |
| `sync-meetings-form` | `{tsg, force}` |
| `sync-tdocs-form` | `{meeting_id \| meeting_name, force}` (radio switch) |
| `sync-tdocs-all-form` | `{force}` |
| `sync-specs-tsg-form` | `{tsg, force, per_version_details}` |
| `sync-specs-id-form` | `{spec_id, force, per_version_details}` |
| `parse-tdocs-form` | `{filter: {…from form fields…}, force, full, max_batch?}` |
| `parse-tdoc-url-form` | `{url, recursive, max_depth, force, full}` |
| `rebuild-search-form` | `{stale_only, resume}` |
| `purge-cache-form` | `{scope, yes: true}` |

### 6. Templates

- `src/doc3gpp/web/templates/sync.html` (new): extends `base.html`. Eight
  panels + the `#recent-jobs` div. Initial render of the recent-jobs
  table is a server-side `{% include "partials/sync_recent_jobs.html" %}`
  inside `#recent-jobs` (the `hx-trigger="load"` then swaps it on
  page-load to the same content via `/sync?format=fragment`).
- `src/doc3gpp/web/templates/partials/sync_recent_jobs.html` (new): a
  `<div id="recent-jobs">` wrapping a `<table class="grid">` with columns
  `id (8 chars), kind, status, created_at, show-link`. Reuses the same
  row shape as `job_status.html` for visual consistency.

### 7. CSS

- No new classes. Every panel reuses the existing `.card`, `.filters`,
  `.btn .primary`, `.sync-queued`, `.nav-badge` styles.
- The Parse-from-filter form uses the existing `.filters` row layout for
  its filter inputs so it matches `partials/tdoc_filters.html`.

## Error handling

- Empty required field (TSG / meeting id / spec id / URL) → button still
  POSTs; server returns 400 (`InvalidFilterError`); `bindJobPolling`'s
  existing error path shows "Failed to enqueue job" in the queued span and
  logs to console.
- Unknown TSG / spec_id → worker raises (today the spec service raises
  `typer.BadParameter`, the meeting service logs an upstream error); the
  job transitions to `FAILED` and is surfaced via `partials/job_status.html`.
  Matches today's behaviour for `meeting_show.html`.
- `parse_tdoc_url` with non-3GPP URL → `InvalidFilterError` → 400 (same
  check as the MCP tool).
- `parse_tdoc_url` with both `recursive=true` and a custom `max_depth` →
  `InvalidFilterError` → 400 (XOR, same as MCP).
- `purge_cache` without `yes=true` → 400 (same as today).
- Refresh race (user clicks Sync twice before first refresh lands) → second
  HTMX swap just overwrites the first; no corruption.

## Testing

### Unit (`tests/unit/test_web_routes.py`)

- `test_sync_page_renders_eight_panels`: `GET /sync` returns 200, body
  contains every panel's `<h2>` (Meeting sync, TDoc sync, Spec sync, Parse
  TDocs (filter-driven), Parse from URL, Rebuild search index, Purge
  cache, Recent sync jobs).
- `test_sync_fragment_returns_partial_only`: `GET /sync?format=fragment`
  returns 200 and the body is a `<table>` fragment (no `<html>`/`<body>`).
- `test_sync_nav_link_present`: `GET /` body contains `href="/sync"`.
- `test_parse_tdoc_url_route_validates_url`: `POST /jobs/parse/tdoc-url`
  with a non-3GPP URL → 400 + `invalid_filter`.
- `test_parse_tdoc_url_route_xor_recursive_max_depth`: both set → 400.
- `test_parse_tdoc_url_route_happy_path`: valid body → 202 + `job_id`,
  repo row has `kind=parse_tdoc_url` + expected `params`.
- `test_bindJobPolling_onTerminal_overrides_reload`: with a mocked
  observer, when the polling span disappears, the supplied `onTerminal`
  callback is invoked and `location.reload` is NOT.
- `test_bindJobPolling_defaults_to_reload`: no `onTerminal` passed →
  default behaviour unchanged.

### Integration (`tests/integration/test_web_end_to_end.py` + new
`tests/integration/test_sync_hub_end_to_end.py`)

- `GET /sync` end-to-end renders 200 with all eight panels + the
  recent-jobs table seeded with a fake `Job`.
- `GET /sync?format=fragment` returns only the recent-jobs table fragment.
- Enqueueing from each form produces a `JobRepository` row with the
  expected `kind` and `params` (one assertion per form).
- Mark each enqueued job `SUCCEEDED`; the next `GET /sync?format=fragment`
  shows the new rows in the recent-jobs table.
- Nav badge updates: `pending_jobs` increases when a job is `QUEUED`.

### Regression (`tests/unit/test_web_jobs_routes.py` + existing)

- `meeting_show.html` form still enqueues `sync_tdocs` and triggers
  `location.reload` on terminal (the explicit `onTerminal` is passed).
- `spec_show.html` form still enqueues `sync_specs` and triggers
  `location.reload` on terminal.
- `parse_tdoc_url` route + handler parity: the new route produces the same
  `params` shape as the MCP `parse_tdoc_url` tool.

### Commands

```bash
ruff check .
./scripts/test_sqlite.sh
```

## Out of scope

- Per-TSG dropdown sourced from the `tsgs` table — explicitly declined
  during brainstorming; the hub uses a free-text TSG input to match the
  CLI exactly.
- Collapsible / tabs UI for the panels — explicitly declined; the hub is
  a stacked control surface.
- Any model, repository, service, settings, parser, or scraping change.
- CLI surface (`docs/cli.md`) is unchanged.
- Auto-sync hooks (no behaviour change to `meeting list` / `tdoc list`
  / etc.).
- Server settings (no new TOML keys).
- Job-kind additions — `JobKind.PARSE_TDOC_URL` already exists; no new
  kinds introduced.

## Documentation sync

- `docs/web-server.md` (HTTP routes section): add `/sync` (full page +
  fragment) and `POST /jobs/parse/tdoc-url` to the route tables.
- `AGENTS.md` "Where to look" table: add a row for "Add a sync hub panel
  / sync hub page" pointing at `src/doc3gpp/web/routes/sync.py` +
  `src/doc3gpp/web/templates/sync.html` +
  `src/doc3gpp/web/static/js/sync_hub.js`.
- No `docs/cli.md`, `docs/architecture.md`, or `README.md` changes.

## TL;DR

One new top-level page (`GET /sync`) with eight stacked panels mirroring
every sync-shaped MCP tool; one new enqueue route (`POST /jobs/parse/tdoc-url`)
to close the MCP-vs-HTTP gap; one shared `onTerminal` callback added to the
job-poller helper; page-local JS that swaps the bottom recent-jobs table via
HTMX instead of full reloads. No model / service / repository changes.
