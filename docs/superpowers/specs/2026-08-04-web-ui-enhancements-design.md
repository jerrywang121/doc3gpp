# Web UI Enhancements Design

**Date:** 2026-08-04
**Status:** Approved
**Branch:** feat/web-ui-enhancements

## Goal

Four web-UI enhancements to the doc3gpp web server:

1. Add a tdoc filter to the search page (both FTS5 and semantic modes), matching the CLI `search query --tdoc-id` filter.
2. Replace the per-column folding in search results with a single folding block per hit, and add a master toggle above the results table to fold/unfold all results.
3. Add a "Related WIs" field to the TDoc section of the tdoc detail page.
4. Let users select which columns appear in the tdoc list table via a multi-select dropdown.

## 1. Search page tdoc filter

### Behaviour

A "TDoc" text input on the search form, in **both** the FTS5 branch and the semantic branch. Entering a tdoc id (e.g. `S2-1234567`) filters results to that exact tdoc, matching the CLI `search query --tdoc-id` semantics. Empty input → no filter.

The backend plumbing already exists:

- `SearchFilters.tdoc_id` (models/search.py) — used by the FTS5 SQL (`AND t.tdoc_id = :tdoc_id`, search_sql.py) and forwarded by `SemanticSearchService.search` to both the vector KNN and the FTS5 fan-out path.
- CLI `search query --tdoc-id` builds the same field.

### Changes

- **`src/doc3gpp/web/routes/search.py`**
  - `search_query`: add `tdoc_id: str | None = Query(default=None, alias="tdoc-id")`; include in the `SearchFilters` built by `_build_filters()` via `parse_tdoc_id_query`.
  - `search_semantic`: add the same `tdoc_id` param and pass it into `SearchFilters(limit=..., tdoc_id=...)`.
  - `search_semantic`: add a `filters` dict to the template context (it currently has none) so the form input value round-trips.
- **`src/doc3gpp/web/templates/partials/search_form.html`**
  - FTS5 branch: add `TDoc` input with `name="tdoc-id"`, `value="{{ filters.get('tdoc_id', '') }}"`.
  - Semantic branch: add the same input (needs the new `filters` context).
- No model, repository, or service changes. No CLI changes.

## 2. Search results single folding + master toggle

### Behaviour

- Each result hit gets **one** `<details>` block containing all its matching preview fields as `key: value` lines (a `<pre>` per matching column, labelled with the column name). Replaces the current per-column `<details>` loop.
- A **master toggle** switch above the results table folds / unfolds all hits at once.
- Both apply to FTS5 and semantic modes.
- Default state stays collapsed (same as today's per-column behaviour).

### Changes

- **`src/doc3gpp/web/templates/partials/search_results_table.html`** (new)
  - Extract the duplicated table markup from `search_results.html` and `partials/search_results.html` into this shared partial (same pattern as the shared `search_form.html`). Both existing templates include it.
  - Table gets an `id="search-results-table"`.
  - Preview row: single `<details class="hit-details">` per hit; inside, one `<div class="preview-field">` per matching column with `<span class="preview-label">{{ col }}</span>` + `<pre>{{ snippet }}</pre>`.
  - Above the table, a label + `<input type="checkbox" id="fold-toggle">` "Expand all" toggle.
- **`src/doc3gpp/web/static/js/search.js`** (new, first custom JS in the repo)
  - `change` handler on `#fold-toggle`: when checked → open all `details.hit-details`; unchecked → close all.
  - `htmx:afterSwap` listener (scoped to `#results` swaps): re-applies the toggle state to the newly swapped-in details so the choice survives every re-query.
- **`src/doc3gpp/web/templates/search_results.html`** (full page): include the JS file via `<script src="{{ url_for('static', path='js/search.js') }}">`; keep include of the new table partial.
- **`src/doc3gpp/web/templates/partials/search_results.html`** (fragment): include the table partial. JS is already loaded by the page shell, so no script include needed in the fragment.
- **`src/doc3gpp/web/static/style.css`**: styles for `#fold-toggle`, `details.hit-details`, `.preview-label`.

### Why client-side, not server-side

A server-side `?expanded=` round-trip would re-run the FTS5 query purely for presentation. The toggle is pure presentation; a tiny inline script plus an `htmx:afterSwap` listener keeps the state without any route changes.

## 3. TDoc detail page: Related WIs

### Behaviour

The TDoc section of the tdoc detail page shows the `related_wis` field (populated from the XLSX via `TDoc.related_wis`), alongside the existing fields. The Cover page section's `record.cover.related_wis` stays as-is — it is a different source (parsed docx).

### Changes

- **`src/doc3gpp/web/templates/tdoc_show.html`**: add one `dt`/`dd` pair in the TDoc `dl.kv` (after Uploaded, ~line 30): `Related WIs` → `{{ tdoc.related_wis or "-" }}` (match the existing `or "-"` convention used by other nullable fields).
- No model/service/route changes — `TDoc.related_wis` and the JSON record already carry it.

## 4. TDoc list page: selectable columns

### Behaviour

- A multi-select dropdown in the tdoc filter form lists all available columns.
- Default (no `fields` param): **ID, Title, Meeting, Type, Spec, Release, Status** — the current HTML columns with `Uploaded` replaced by `Status` — plus the always-on "show" action column. This preserves today's look except for the Uploaded→Status swap.
- Changing the selection re-queries the list (existing form `hx-trigger="change"`).
- Selection persists in the URL via repeated `fields` params, so it survives pagination (`include_query_params`) and reload.
- Status cells get a row background color derived from the status value (see below). The Status column is a **new** column in the default set, so color coding shows by default; the color applies to the whole row, so it still shows even if the user deselects the Status column.
- `?format=json` and the MCP tool keep `_TDOC_DEFAULT_FIELDS` (10 fields) — unchanged contract.

### Available columns

| Field key | Label | In default |
| --- | --- | --- |
| `tdoc_id` | TDoc ID | yes |
| `meeting_name` | Meeting | yes |
| `title` | Title | yes |
| `type` | Type | yes |
| `spec` | Spec | yes |
| `release` | Release | yes |
| `status` | Status | yes |
| `uploaded_date` | Uploaded | no |
| `source` | Source | no |
| `cr_cat` | CR Category | no |
| `version` | Version | no |
| `related_wis` | Related WIs | no |

### Status color coding

A Jinja filter `status_color_class(status)` maps a status string to a CSS class. Matching is **case-insensitive substring**, first matching entry in the table wins. No match → no class (no background).

| Match string                    | CSS class        | Color           |
| ---------------------------------| ------------------| -----------------|
| `conditionally`, `partially`    | `status-lgreen`  | Lightgreen      |
| `agreed`, `approved`            | `status-green`   | Green           |
| `revised`, `reissued`, `merged` | `status-vanilla` | Vanilla (cream) |
| `rejected`                      | `status-red`     | Red             |
| `withdrawn`                     | `status-grey`    | Grey            |
| `postponed`                     | `status-pink`    | Pink            |
| `noted`, `treated`, `endorsed`  | `status-lblue`   | Light blue      |

Note: because `conditionally` / `partially` match first, a status like "Partially Approved" renders **lightgreen**.

### Changes

- **`src/doc3gpp/web/routes/tdocs.py`**
  - `list_tdocs`: add `fields: list[str] | None = Query(default=None)`.
  - Validate each against the allowlist of field keys; unknown → `InvalidFilterError` (HTTP 400).
  - Default when `fields` is absent/empty: `[tdoc_id, meeting_name, title, type, spec, release, status]`.
  - Pass the resolved field keys + a `(key, label)` list for the dropdown to the template context.
- **`src/doc3gpp/web/render.py`**: add `TDOC_COLUMN_LABELS` mapping (key → display label) next to `_TDOC_DEFAULT_FIELDS`; reuse `tdoc_rows()` for HTML row building.
- **`src/doc3gpp/web/templates_setup.py`**: register `status_color_class` Jinja filter (sibling of `dt_short` / `sync_state`).
- **`src/doc3gpp/web/templates/partials/tdoc_filters.html`**: add `<select name="fields" multiple>` with one `<option>` per available column, `selected` when in the current field list.
- **`src/doc3gpp/web/templates/partials/tdoc_results.html`**: replace hard-coded 8-column markup with a loop over `fields`; header = label; cell = `row[field]`. The background color applies to the **entire row** — `<tr class="{{ status_color_class(row['status']) }}">` — so the color shows regardless of whether the Status column itself is selected (the user may choose to hide it). No match → no class (no background).
- **`src/doc3gpp/web/static/style.css`**: pastel background classes `.status-lgreen`, `.status-green`, `.status-vanilla`, `.status-red`, `.status-grey`, `.status-pink`, `.status-lblue` (soft pastel shades, dark text stays readable).

## Data flow

```
tdoc list page:
  select[fields] (multi) --hx-get /tdocs?fields=...--> list_tdocs route
    -> validate against allowlist -> tdoc_rows(rows, fields) -> template loop
  row class -> status_color_class(row['status']) -> pastel CSS on whole row

search page:
  form[tdoc-id] --hx-get /search|/search/sem--> route
    -> SearchFilters(tdoc_id=...) -> existing FTS5 / semantic search
    -> single <details> per hit + #fold-toggle master switch
  toggle state re-applied on htmx:afterSwap
```

## Error handling

- Unknown `fields` value → `InvalidFilterError` → HTTP 400 with a clear message listing valid keys.
- Malformed/absent `tdoc-id` → treated as no filter (via `parse_tdoc_id_query`).
- No new failure modes elsewhere.

## Testing

- **Unit tests** (`tests/unit/test_web_routes.py` style):
  - `search_query` / `search_semantic` accept `tdoc-id` and forward it into `SearchFilters`.
  - `list_tdocs`: default fields when absent; custom fields honoured; unknown field → 400.
  - `status_color_class`: each mapping entry + no-match → no class; case-insensitivity; "Partially Approved" → lightgreen; row class applied even when Status column is deselected.
- Run `ruff check .` and `./scripts/test_sqlite.sh`.

## Docs

- Update `docs/web-server.md` (search page filters, results folding + toggle, tdoc detail fields, tdoc list column selection + status colors) per the AGENTS.md doc-sync convention.
- No CLI surface changes, so `docs/cli.md` / `README.md` are untouched.
