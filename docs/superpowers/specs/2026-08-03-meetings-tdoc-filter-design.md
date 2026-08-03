# Meetings page TDoc filter — design

**Date:** 2026-08-03
**Status:** approved

## Goal

Add a `TDoc` text filter to the web meetings list page (`GET /meetings`) so a
user can narrow the meeting list to the meetings whose `start_doc` / `end_doc`
range brackets a given TDoc id (e.g. `R5-260013`).

## Background

The full backend plumbing already exists:

- `MeetingService.list_recent` / `SQLAlchemyMeetingRepository.list` accept a
  `tdoc_id: tuple[str, int]` parameter and narrow results to meetings whose
  `start_doc` / `end_doc` range brackets the TDoc (`tdoc_sql`-style semantics;
  prefix matched case-insensitively in SQL, numeric range compared in Python).
- The same parameter powers the CLI `meeting list --tdoc` flag
  (`src/doc3gpp/cli.py` `meeting_list`), which validates via
  `parse_tdoc_id` (`src/doc3gpp/cli_filters.py`).
- `doc3gpp.web.filters.parse_tdoc_id_query` already wraps
  `parse_tdoc_id` and remaps its `ValueError` to `InvalidFilterError`,
  which the web error handlers surface as a 400 `invalid_filter` envelope.

What is missing is the web wiring: the `/meetings` route does not declare or
parse a `tdoc` query param, and the meeting filter form has no TDoc input.

## Design

### 1. Route — `src/doc3gpp/web/routes/meetings.py`

Add a `tdoc: str | None = Query(default=None)` parameter to `list_meetings`.
Parse it only when non-empty:

- empty string (`tdoc=`) → `None` (same "empty form field → 200, not 422"
  convention already applied to `year`, `limit`, `offset`; the field stays
  declared as `str` so FastAPI never 422s on `tdoc=`);
- malformed value (does not match the CR-shape regex) →
  `parse_tdoc_id_query` raises `InvalidFilterError` → existing 400
  `invalid_filter` envelope;
- valid value → `(prefix, number)` tuple passed to
  `service.list_recent(..., tdoc_id=parsed)`.

The `filters` context dict gains `"tdoc": tdoc or ""` so the form input
round-trips. No auto-sync: the web meetings route does not trigger
auto-sync today, and this change keeps that behavior unchanged.

### 2. Template — `src/doc3gpp/web/templates/partials/meeting_filters.html`

Add a `TDoc` text input (`name="tdoc"`, value echoed from
`filters.tdoc`) after the `Location` input, styled like the tdocs page
inputs. The filter form posts via HTMX to `/meetings` and swaps the
`#results` partial, so no other template changes are needed.

### 3. Tests — `tests/unit/test_web_routes.py`

- filter form renders the `tdoc` input (`name="tdoc"` present in
  `GET /meetings` HTML);
- `GET /meetings?tdoc=R5-260013` is 200 (pass-through to the service);
- `GET /meetings?tdoc=` is 200, not 422 (empty form field);
- `GET /meetings?tdoc=not-a-tdoc` is 400 with the `invalid_filter`
  envelope.

### 4. Docs

`docs/web-server.md` documents the meetings page filters; add the TDoc
filter to that section. No CLI surface changes, so `docs/cli.md`,
`README.md`, and `AGENTS.md` are untouched.

## Out of scope

- No repository or service changes (plumbing already present).
- No CLI changes (CLI already has `meeting list --tdoc`).
- No auto-sync trigger on the web route.
