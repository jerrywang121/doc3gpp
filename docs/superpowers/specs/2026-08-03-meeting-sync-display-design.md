# Meeting sync display + one-click sync (web) — Design

**Date:** 2026-08-03
**Status:** Approved (user "ok" on the presented design)
**Related:** `docs/web-server.md`, `src/doc3gpp/web/`

## Problem

On the web surface:

- The meeting **list** page (`GET /meetings`) gives no visibility into when each
  meeting was last synced, and no way to trigger a sync from the list.
- The meeting **detail** page (`GET /meetings/{meeting_id}`) omits the
  document-range fields (`start_doc` / `end_doc`), the last-sync timestamp, and
  a link into the meeting's TDoc list.

## Goals / Non-goals

Goals:

- Show `start_doc`, `end_doc`, and a sync indicator on the meeting list.
- Give the sync indicator a colour derived from sync freshness.
- Make the sync indicator clickable so one click enqueues the TDoc-list sync for
  that meeting.
- On the detail page show `start_doc`, `end_doc`, last-sync (`YYYY-MM-DD HH:MM`),
  and a link to the meeting's TDocs filtered by meeting id.

Non-goals:

- No DB schema / model changes (the data already exists).
- No change to the `?format=json` / MCP / CLI output shapes (byte-parity with
  `doc3gpp meeting list --format json` is preserved; the detail JSON already
  carries `tdoc_list_last_sync` via `to_jsonable`'s omit-when-null rule).
- No synchronous sync — clicking enqueues a background job through the existing
  job worker, exactly like the detail page's existing "Sync this meeting's TDocs"
  button.

## Data source

The meeting-level "last sync" timestamp is `Meeting.tdoc_list_last_sync`
(`src/doc3gpp/models/meeting.py:42`), written by
`SQLAlchemyMeetingRepository.update_tdoc_list_last_sync` on each successful TDoc
list sync. In the domain model it is normalised to **aware UTC**
(`_as_utc` in `src/doc3gpp/storage/repositories/meeting_sql.py:180`), so all
freshness comparisons below compare aware-UTC-to-aware-UTC.

## Design

### 1. Meeting list — `src/doc3gpp/web/templates/partials/meeting_results.html`

Add three columns to the existing table (both the full page and the HTMX
partial, since the partial is the table):

- **Start doc** — `meeting.start_doc` (fallback `-`).
- **End doc** — `meeting.end_doc` (fallback `-`).
- **Sync** — per row, a small HTMX form that enqueues the sync job:

  ```html
  <form hx-post="/jobs/sync_tdocs" hx-swap="none"
        title="Last sync: {{ meeting.tdoc_list_last_sync | dt_short or 'never' }}">
    <input type="hidden" name="meeting_id" value="{{ meeting.meeting_id }}">
    <button type="submit" class="sync-btn sync-{{ meeting.tdoc_list_last_sync | sync_state }}"
            aria-label="Sync TDocs for meeting {{ meeting.meeting_id }}">&#8635;</button>
  </form>
  ```

  - `&#8635;` is the `↻` glyph.
  - `hx-swap="none"` discards the 202 JSON envelope body while the POST still
    enqueues the job (route: `POST /jobs/sync_tdocs` →
    `post_sync_tdocs_flat` in
    `src/doc3gpp/web/routes/jobs.py:222`, form-encoded `meeting_id`).

Colour contract (`sync_state` filter):

| State | Condition | CSS class | Colour |
| --- | --- | --- | --- |
| fresh | `last_sync >= utcnow - 24h` | `sync-fresh` | green `#1a7f37` |
| stale | `last_sync < utcnow - 24h` | `sync-stale` | orange `#bc4c00` |
| never | `last_sync is None` | `sync-never` | grey `#8c959f` |

### 2. Meeting detail — `src/doc3gpp/web/templates/meeting_show.html`

Append to the existing `<dl class="kv">`:

- `Start doc` / `End doc` rows (present-only values, matching the existing
  conditional-row style; `-` fallback keeps the row aligned).
- `Last sync` — `{{ meeting.tdoc_list_last_sync | dt_short }}` rendered as
  `YYYY-MM-DD HH:MM` plus a muted `UTC` marker; "Never" when null.
- A link row (below the `dl`): `View TDocs for this meeting` →
  `/tdocs?meeting_id={{ meeting.meeting_id }}&limit=200`
  (the tdoc list route already filters on `meeting_id`; `limit=200` shows the
  whole meeting instead of the 50 default).

### 3. Jinja filters — `src/doc3gpp/web/templates_setup.py`

Register two filters on the shared `templates.env`:

- `dt_short(value)` → `value.strftime("%Y-%m-%d %H:%M")` if not None else `None`.
- `sync_state(value)` → `"never"` if None else
  `"fresh" if value >= datetime.now(timezone.utc) - timedelta(hours=24) else "stale"`.

Both are pure functions (testable as plain Python).

### 4. CSS — `src/doc3gpp/web/static/style.css`

```css
.sync-btn {
  background: none; border: none; padding: 0;
  font-size: 1.1rem; line-height: 1; cursor: pointer; text-decoration: none;
}
.sync-fresh { color: #1a7f37; }
.sync-stale { color: #bc4c00; }
.sync-never { color: #8c959f; }
.sync-btn:hover { opacity: 0.7; }
```

## Data flow

1. List/detail route loads `Meeting` rows (unchanged).
2. Template renders the new columns/rows; `dt_short` / `sync_state` filters do
   the formatting and freshness classification.
3. Clicking `↻` POSTs `meeting_id` to `/jobs/sync_tdocs`, which enqueues a
   `SYNC_TDOCS` job on the existing worker — the same path as the detail page's
   existing sync button. The DOM is untouched (`hx-swap="none"`); the job shows
   up in `/jobs`.

## Testing

In `tests/unit/test_web_routes.py` (the existing web-route suite; fake services
already provide `Meeting` objects):

- Extend the fake meetings with one never-synced, one fresh
  (`utcnow - 1h`), and one stale (`utcnow - 48h`) `tdoc_list_last_sync`.
- List page: assert the HTML contains `start_doc` / `end_doc` cells and that the
  per-row `sync-fresh` / `sync-stale` / `sync-never` classes render correctly.
- List page: assert each sync form carries `hx-post="/jobs/sync_tdocs"` and the
  right hidden `meeting_id`.
- Detail page: assert `Start doc` / `End doc` / `Last sync` rows, the
  `YYYY-MM-DD HH:MM` formatted value, and the `View TDocs` link containing
  `meeting_id=<id>`.
- New `tests/unit/test_web_filters.py` (or alongside existing filter tests):
  unit-test `dt_short` and `sync_state` edge cases (None, exact 24h boundary,
  sub-24h, older-than-24h).

Existing tests that must keep passing: `test_meetings_list_json_matches_cli_rows`
(JSON shape unchanged), `test_meetings_list_htmx_returns_partial`,
`test_meeting_show_json`, `test_meeting_show_renders_html`.

## Files touched

- `src/doc3gpp/web/templates/partials/meeting_results.html`
- `src/doc3gpp/web/templates/meeting_show.html`
- `src/doc3gpp/web/templates_setup.py` (register filters)
- `src/doc3gpp/web/static/style.css`
- `tests/unit/test_web_routes.py` (+ new filter unit tests)

## Verification

- `ruff check .`
- `./scripts/test_sqlite.sh`
- Manual: serve and check `/meetings` (columns + clickable coloured `↻`) and
  `/meetings/{id}` (new k-v rows + link) via `doc3gpp server start`.
