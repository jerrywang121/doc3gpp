# Web + MCP meeting name filter — design

**Date:** 2026-08-13
**Status:** Approved
**Author:** opencode + user

## Goal

Close the gap where the web `/meetings` filter form and route accept
`tsg / year / location / tdoc` but **not** `name`, even though the CLI's
`meeting list --name` and the MCP `list_meetings` tool's `name` argument
are both already wired through to the same repository layer. After this
change, a user can filter the web meeting list by name (with the same
rich-filter grammar the CLI uses) and an MCP caller of `list_meetings`
gets a covered test that locks the contract in.

Out of scope:

- Auto-sync for the web list route (the CLI's `trigger_auto_sync(meeting_name=…)`
  is **not** mirrored on the web today; adding it is a separate, broader
  conversation about read-side web auto-sync in general).
- Refactor of the meeting filter parse block into a shared helper (defer
  until a second caller materialises).
- New filters (e.g. `start_after`, `start_before`) — removed in a previous
  ruling; not in scope here.

## Background

`doc3gpp meeting list --name <pattern>` is the source of truth. The
`--name` value flows through:

1. `cli.py:meeting_list` (`src/doc3gpp/cli.py:744`) — declared as a
   `typer.Option` with a rich-filter help string.
2. `MeetingService.list_recent(name_like=name, …)`
   (`src/doc3gpp/services/meetings_service.py:108`) — pure pass-through.
3. `MeetingRepository.list(name_like=name_like, …)`
   (`src/doc3gpp/repository/protocols.py:210`,
   `src/doc3gpp/storage/repositories/meeting_sql.py:list`) — the SQL
   `name_like` clause applies the rich-filter grammar
   (`null` / `not-null` / `!pattern` / plain LIKE) against
   `meetings.name`.

The CLI's `meeting_list` also calls
`trigger_auto_sync(meeting_name=name, …)` (`src/doc3gpp/cli.py:828`) so
the auto-sync helper resolves meetings by name and fires per-meeting
TDoc-list syncs before the read. The web route does not trigger
auto-sync for any filter (only the CLI does), and this change does not
introduce that.

The MCP `list_meetings` tool (`src/doc3gpp/web/mcp_server.py:200-218`)
already declares a `name: Annotated[str | None, Field(...)]` argument and
forwards it as `name_like=name` to `services.meeting.list_recent`. So
the MCP server is already complete; the work is to add tests that lock
the behaviour and to fix the web side.

## Approach

Wire the existing `name_like` path through the web route and form, and
lock the MCP behaviour with new tests. No new module, no new helper, no
protocol or service change.

### 1. Web route — `src/doc3gpp/web/routes/meetings.py`

In `list_meetings` (currently at line 59), add a `name` query param
and forward it to the service:

```python
async def list_meetings(
    request: Request,
    tsg: str | None = Query(default=None),
    name: str | None = Query(default=None),
    year: str | None = Query(default=None),
    location: str | None = Query(default=None),
    tdoc: str | None = Query(default=None),
    …
) -> Any:
    …
    parsed_tsg = parse_text_query(tsg)
    parsed_name = parse_text_query(name)
    parsed_location = parse_text_query(location)
    …
    meetings = service.list_recent(
        limit=parsed_limit,
        offset=parsed_offset,
        tsg=parsed_tsg,
        name_like=parsed_name,
        location_like=parsed_location,
        year=parsed_year,
        tdoc_id=parsed_tdoc,
    )
    …
    "filters": {
        "tsg": parsed_tsg or "",
        "name": parsed_name or "",
        "year": year,
        "location": parsed_location or "",
        "tdoc": tdoc or "",
        "limit": parsed_limit,
    },
```

`parse_text_query` is the same helper the route already uses for `tsg`
and `location` (`src/doc3gpp/web/filters.py`). It returns `None` for
`None` and `""` inputs (so `?name=` and absent `?name` are equivalent)
and otherwise returns the value verbatim — the rich-filter grammar is
interpreted downstream by the SQL `name_like` clause, matching how
`?tsg=` and `?location=` already behave.

The `name` query param is ordered between `tsg` and `year` to mirror
the CLI's `meeting list` flag order (`--tsg / --name / --location /
--year / --tdoc` in `src/doc3gpp/cli.py:740-750`).

### 2. Web form — `src/doc3gpp/web/templates/partials/meeting_filters.html`

Insert a new label between the existing `tsg` and `year` inputs:

```html
<label>Name
  <input type="text" name="name" value="{{ filters.name or '' }}">
</label>
```

The HTMX wiring (`hx-get="/meetings" hx-target="#results" hx-trigger="change, submit"`)
is unchanged — the new input is auto-included in the form submission.
The form's existing `class="filters"` styling already accommodates
arbitrary label counts.

### 3. MCP — no code change

`list_meetings` in `src/doc3gpp/web/mcp_server.py:200` already accepts
`name` and forwards it correctly. We add a focused integration test
(see §5) to lock the behaviour.

### 4. Landing copy — `src/doc3gpp/web/routes/landing.py:35`

Update the `Meetings` section description from
`"Browse stored meeting records, optionally filtered by TSG / year."`
to
`"Browse stored meeting records, optionally filtered by TSG / name / year / location."`.

This is a one-line copy change. It does not enumerate every filter
(no `tdoc`, no `format`, no pagination); the goal is to make the
existence of the name filter discoverable from the landing nav
without bloating the description.

### 5. Tests

**`tests/unit/test_web_routes.py`** (next to the existing
`test_meeting_list_filters_form_fields` and
`test_meetings_list_tdoc_filter_returns_200` at line 916+):

1. `test_meeting_list_filters_form_has_name_input` — `GET /meetings`
   HTML contains `name="name"`.
2. `test_meetings_list_name_filter_renders` —
   `GET /meetings?name=SA2%23` returns 200 and the response includes the
   seeded `SA2#156` row.
3. `test_meetings_list_name_filter_rich_grammar` —
   `GET /meetings?name=!%25SA2%25` returns 200 and the response does **not**
   include the `SA2#156` row (NOT LIKE branch).
4. `test_meetings_list_name_filter_empty_returns_200` —
   `GET /meetings?name=` returns 200 (mirrors the existing
   `test_meetings_list_empty_numeric_filter_returns_200` pattern for
   empty form fields).

These tests use the `client: TestClient` fixture already defined in the
test file (line 288 area — `_seed_corpus` analogue is set up by the
shared fixture, which already seeds a single `SA2#156` meeting).

**`tests/integration/test_mcp_end_to_end.py`** (next to the existing
`test_call_list_meetings_empty` at line 90):

5. `test_call_list_meetings_name_filter` — call
   `list_meetings({"name": "%SA2%"})` against the seeded `SA2#156` row,
   assert exactly one row is returned and its `name` is `"SA2#156"`.
6. `test_call_list_meetings_name_no_match` — call
   `list_meetings({"name": "no-match-%"})`, assert the result is `[]`.

### 6. Documentation sync

Per `docs/conventions.md` §"Documentation sync", this change is a
web-layer + MCP-test change (not a CLI surface change), so the bulk
of that rule is N/A. Three small doc updates are still warranted:

1. **`docs/web-server.md`** — after the existing TDoc-sync-freshness
   paragraph at line 194-203, add one sentence: *"The filter form
   supports TSG, name, year, location, and a TDoc id selector, all
   with the same rich-filter grammar as the CLI."*
2. **`docs/cli.md`** — under `### doc3gpp meeting list` (around line
   220), add one line noting web parity: *"The same filter set is
   available on the web at `/meetings` (form-driven, HTMX-powered, JSON
   at `?format=json`)."*
3. **`README.md` / `AGENTS.md` / `docs/architecture.md` /
   `docs/conventions.md`** — no change. The CLI's
   `meeting list --name` is already documented (`docs/cli.md:224`,
   `docs/conventions.md:241`); this change is purely a wire-up
   completion on the web side.

## Data flow

```
GET /meetings?name=%25SA2%25
    │
    ▼  (FastAPI binds `name: str | None`)
parse_text_query(name) ──► parsed_name = "%SA2%"
    │
    ▼
MeetingService.list_recent(
    name_like=parsed_name,
    limit, offset, tsg, location_like, year, tdoc_id,
)
    │
    ▼  (pure pass-through, services/meetings_service.py:122)
SQLAlchemyMeetingRepository.list(name_like="%SA2%", …)
    │
    ▼  (rich-filter grammar → SQL)
WHERE meetings.name LIKE '%SA2%'
    │
    ▼
list[Meeting] → meeting_rows → JSONResponse OR template
```

For an empty `?name=`:
`parse_text_query("")` returns `None` → `name_like=None` → repository's
`name_like` clause is a no-op → unfiltered list.

For `?name=!%25SA2%25` (NOT LIKE):
`parse_text_query` returns `"!%SA2%"` → `name_like="!%SA2%"` → SQL
clause sees the `!` prefix and flips to `NOT LIKE` against the
pattern — same path the existing `tsg` and `location` web filters take.

## Error handling

No new error paths. `parse_text_query` is a no-op for string inputs and
returns `None` for empty / None — no exceptions raised. The repository
SQL `name_like` clause already handles every legal grammar input
(verified by `test_meeting_repository_filters.py` and
`test_meeting_cli_filters_combined.py`). A 400 / 422 only happens for
`?tdoc=…` bad shapes (existing behaviour) — `name` is a string, so
FastAPI's validation never rejects it.

## Parity with the CLI

| Surface | `name` filter status |
| --- | --- |
| CLI `meeting list --name` | already works |
| Web `GET /meetings?name=` | **this change** |
| Web `partials/meeting_filters.html` form | **this change** |
| MCP `list_meetings(name=)` | already works; **test added in this change** |
| Web auto-sync for `?name=` | out of scope (not added in this change) |

The MCP `list_meetings` tool's parity with the HTTP `?format=json`
route is locked by `test_read_tools_parity_with_http_json`
(`tests/integration/test_mcp_end_to_end.py:452`, which compares
empty-args bytes). Adding the `name` query param to the route does not
affect that test: both sides go through
`MeetingService.list_recent(name_like=None, …)` for empty args, so the
byte-for-byte equality holds.

## Out of scope (explicit)

- Web auto-sync for the meeting list. The CLI's `trigger_auto_sync`
  orchestration (`src/doc3gpp/cli_auto_sync.py`) is intentionally
  CLI-only today; bundling its web equivalent into this change would
  conflate the gap-fix with a broader policy decision about read-side
  web syncs. Worth a follow-up.
- Refactor of the meeting filter parse block into a shared helper.
  Only one caller today (the route); defer until a second caller
  materialises.
- The landing-page `Meetings` description does not mention the `tdoc`
  filter or pagination; the new copy follows the same brevity style
  (one line, three of the four filters).
