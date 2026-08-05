# TDoc Columns Dropdown & Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the tdoc list page's native multi-select with a dropdown-checkbox column picker, widen the tdoc list page to 1430px, and fix the Meeting column at 180px.

**Architecture:** Template-only + CSS + a small vanilla-JS dropdown controller (no Python route changes — the checkboxes live inside the existing `hx-trigger="change"` filter form, so the wire format of repeated `fields` query params is untouched). Page width comes from a Jinja `main_class` block on the shared base template; the Meeting column is tagged in the dynamic `{% for f in fields %}` loops because columns are user-selectable (position-based selectors would be wrong).

**Tech Stack:** Jinja2 templates, vanilla JS (deferred, no deps — mirrors existing `search.js`), CSS custom properties, htmx (unchanged).

## Global Constraints

- Checkbox wire format identical to the old select: repeated `fields` query params; `?format=json` and MCP `list_tdocs` keep their fixed 10-field output (no Python changes at all in this plan).
- The column catalogue keys come from `TDOC_COLUMN_LABELS` (`src/doc3gpp/web/render.py:37-50`, passed to the template as `column_labels`); the active selection is the template context list `fields` (already the route's `html_fields`).
- Dropdown behavior: instant auto-submit on toggle — checkboxes must stay **inside** the `<form>` so the existing `hx-trigger="change"` re-queries; the trigger button must be `type="button"` so it never submits.
- The script tag (`tdoc_columns.js`) goes on the **full page only** (`tdoc_list.html`); the HTMX fragment `partials/tdoc_results.html` must never carry it (same pattern as `search.js`). The filter form is outside `#results` and is never HTMX-swapped, so no `htmx:afterSwap` handling.
- Panel must respect the `hidden` attribute: include `.columns-panel[hidden] { display: none; }` because the panel's `display: grid` would otherwise beat the UA `[hidden]` rule.
- Width changes are scoped: `.content-wide { max-width: 1430px }` applies only where the `main_class` block says so (tdoc list page); `table.tdoc-grid .col-meeting { width: 180px }` is scoped to `tdoc-grid` so search/meetings/WIs grids are untouched.
- No localStorage persistence of the columns selection; URL query params remain the source of truth.

---

### Task 1: Dropdown-checkbox column selector

**Files:**
- Modify: `src/doc3gpp/web/templates/partials/tdoc_filters.html:62-68`
- Create: `src/doc3gpp/web/static/js/tdoc_columns.js`
- Modify: `src/doc3gpp/web/templates/tdoc_list.html`
- Modify: `src/doc3gpp/web/static/style.css` (append)
- Test: `tests/unit/test_web_routes.py` (`test_tdoc_list_fields_select_renders`, lines 1228-1233)

**Interfaces:**
- Consumes: template context `fields` (list of selected column keys) and `column_labels` (dict key→header label), both already provided by the route (`src/doc3gpp/web/routes/tdocs.py:209-210`).
- Produces: the `.columns-dropdown` / `.columns-trigger` / `.columns-panel` / `.columns-option` / `.columns-count` markup + CSS and `tdoc_columns.js` behavior. Task 2 relies on the script tag in `tdoc_list.html` being present but is otherwise independent.

- [ ] **Step 1: Update the failing test**

In `tests/unit/test_web_routes.py`, replace `test_tdoc_list_fields_select_renders` (lines 1228-1233) with:

```python
def test_tdoc_list_fields_select_renders(client: TestClient) -> None:
    """The filter form carries the dropdown checkboxes with all column options."""
    html = client.get("/tdocs").text
    assert 'name="fields"' in html
    assert 'type="checkbox"' in html
    assert 'value="related_wis"' in html
    assert 'value="status"' in html
    assert 'class="columns-count"' in html
    assert "<select" not in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_web_routes.py::test_tdoc_list_fields_select_renders -v`
Expected: FAIL — `'type="checkbox"'` and `'class="columns-count"'` not found in the old `<select>` markup.

- [ ] **Step 3: Replace the select markup in the filters partial**

In `src/doc3gpp/web/templates/partials/tdoc_filters.html`, replace lines 62-68 (the `<label>Columns ... </label>` block containing the `<select name="fields" multiple size="5">`) with:

```html
  <label class="columns-dropdown">
    <span class="columns-trigger-wrap">
      <button type="button" class="btn columns-trigger" aria-haspopup="true"
              aria-expanded="false">Columns (<span class="columns-count">{{ fields|length }}</span>)</button>
    </span>
    <span class="columns-panel" hidden>
      {% for key, label in column_labels.items() %}
        <label class="columns-option">
          <input type="checkbox" name="fields" value="{{ key }}"
                 {% if key in fields %}checked{% endif %}>
          {{ label }}
        </label>
      {% endfor %}
    </span>
  </label>
```

The checkboxes are inside the `<form>` (the form wraps the whole partial, `tdoc_filters.html:1-7`), so the existing `hx-trigger="change, submit"` auto-submits the same repeated `fields` params on every toggle. The button is `type="button"` and cannot submit.

- [ ] **Step 4: Create the dropdown JS controller**

Create `src/doc3gpp/web/static/js/tdoc_columns.js`:

```js
/* Dropdown-checkbox column picker for the tdoc list filter form.
 *
 * The form lives outside the #results fragment, so it is never
 * HTMX-swapped; this script runs once on full page load. No-JS users
 * still get the form, with the panel collapsed (hidden attribute).
 */
(function () {
  'use strict';
  document.addEventListener('DOMContentLoaded', function () {
    var trigger = document.querySelector('.columns-trigger');
    if (!trigger) return;
    var root = trigger.closest('.columns-dropdown');
    var panel = root.querySelector('.columns-panel');
    var count = root.querySelector('.columns-count');

    function updateCount() {
      var n = panel.querySelectorAll('input[type="checkbox"]:checked').length;
      if (count) count.textContent = n;
    }

    trigger.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = panel.hidden;
      panel.hidden = !open;
      trigger.setAttribute('aria-expanded', String(open));
    });
    panel.addEventListener('change', updateCount);
    document.addEventListener('click', function (e) {
      if (!e.target.closest('.columns-dropdown')) {
        panel.hidden = true;
        trigger.setAttribute('aria-expanded', 'false');
      }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !panel.hidden) {
        panel.hidden = true;
        trigger.setAttribute('aria-expanded', 'false');
      }
    });
  });
})();
```

- [ ] **Step 5: Load the script on the full page only**

In `src/doc3gpp/web/templates/tdoc_list.html`, add the script tag at the end of the `{% block content %}` (after the `tdoc_results.html` include):

```html
  <script src="/static/js/tdoc_columns.js" defer></script>
```

The fragment `partials/tdoc_results.html` must NOT carry this script (same pattern as `search.js` on the search page).

- [ ] **Step 6: Append the dropdown CSS**

Append to the end of `src/doc3gpp/web/static/style.css`:

```css
.columns-dropdown { position: relative; display: inline-block; }
.columns-panel {
  position: absolute; top: 100%; left: 0; z-index: 10;
  min-width: 14rem; max-height: 15rem; overflow-y: auto;
  background: var(--card-bg); border: 1px solid var(--border);
  border-radius: 6px; padding: 0.5rem; display: grid; gap: 0.25rem;
}
.columns-panel[hidden] { display: none; }
.columns-option {
  display: flex; align-items: center; gap: 0.4rem;
  font-size: 0.875rem; cursor: pointer; white-space: nowrap;
}
```

The `.columns-panel[hidden]` rule is required: `display: grid` on the panel would otherwise beat the browser's default `[hidden]` rule and the panel would stay visible when collapsed.

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_web_routes.py -k "tdoc_list" -v`
Expected: PASS — updated select test plus all existing tdoc-list tests (`test_tdoc_list_default_columns_use_status`, `test_tdoc_list_status_row_colors`, `test_tdoc_list_custom_fields`, `test_tdoc_list_unknown_field_returns_400`, `test_tdoc_list_fields_persist_in_pagination`).

Run: `python -m pytest tests/unit/test_web_routes.py`
Expected: all pass (no Python code changed; regression guard).

Run: `ruff check .`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/doc3gpp/web/templates/partials/tdoc_filters.html src/doc3gpp/web/static/js/tdoc_columns.js src/doc3gpp/web/templates/tdoc_list.html src/doc3gpp/web/static/style.css tests/unit/test_web_routes.py
git commit -m "feat(web): replace tdoc columns select with dropdown checkboxes"
```

---

### Task 2: Wider tdoc list page + fixed Meeting column width

**Files:**
- Modify: `src/doc3gpp/web/templates/base.html:22`
- Modify: `src/doc3gpp/web/templates/tdoc_list.html`
- Modify: `src/doc3gpp/web/templates/partials/tdoc_results.html`
- Modify: `src/doc3gpp/web/static/style.css` (two small additions)
- Test: `tests/unit/test_web_routes.py` (`test_tdoc_list_default_columns_use_status`, lines 1195-1199, plus one new test)

**Interfaces:**
- Consumes: the `main_class` block mechanism from base.html (this task defines it); template context `fields` (list of column keys) and `column_labels` from the route; `status_color_class` global already registered in `templates_setup.py`.
- Produces: `class="content content-wide"` on the tdoc list page's `<main>`, `<table class="grid tdoc-grid">`, and `class="col-meeting"` on Meeting header/cells — all consumed by the CSS added here.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_web_routes.py`, update `test_tdoc_list_default_columns_use_status` (lines 1195-1199) to:

```python
def test_tdoc_list_default_columns_use_status(client: TestClient) -> None:
    """Default HTML columns: Status replaces Uploaded; no fields param needed."""
    html = client.get("/tdocs").text
    assert "<th>Status</th>" in html
    assert "<th>Uploaded</th>" not in html
    assert '<table class="grid tdoc-grid">' in html
    assert 'class="col-meeting"' in html
    assert "content content-wide" in html
```

And add this new test directly after it:

```python
def test_meetings_page_keeps_default_width(client: TestClient) -> None:
    """Non-tdoc pages keep the default 1100px content class."""
    html = client.get("/meetings").text
    assert 'class="content"' in html
    assert "content content-wide" not in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_web_routes.py -k "tdoc_list_default_columns_use_status or meetings_page_keeps_default_width" -v`
Expected: FAIL — no `tdoc-grid`, no `col-meeting`, no `content content-wide` in current markup.

- [ ] **Step 3: Make the main's class overridable in base.html**

In `src/doc3gpp/web/templates/base.html:22`, change:

```html
  <main class="content">
```

to:

```html
  <main class="content{% block main_class %}{% endblock %}">
```

- [ ] **Step 4: Add the block + width class in tdoc_list.html**

In `src/doc3gpp/web/templates/tdoc_list.html`, add the `main_class` block between the `title` block and the `content` block (note the **leading space** inside the block so the rendered class joins as `content content-wide`):

```html
{% extends "base.html" %}
{% block title %}doc3gpp · tdocs{% endblock %}
{% block main_class %} content-wide{% endblock %}
{% block content %}
```

- [ ] **Step 5: Tag the table and the Meeting column in the results partial**

In `src/doc3gpp/web/templates/partials/tdoc_results.html`:

1. Line 3: `<table class="grid">` → `<table class="grid tdoc-grid">`
2. In the header loop (line 7): `<th>{{ column_labels[f] }}</th>` → `<th{% if f == 'meeting_name' %} class="col-meeting"{% endif %}>{{ column_labels[f] }}</th>`
3. In the body loop (line 16): `<td>` → `<td{% if f == 'meeting_name' %} class="col-meeting"{% endif %}>`

Tagging is done inside the dynamic `{% for f in fields %}` loops because the columns are user-selectable — position-based `nth-child` selectors would be wrong.

- [ ] **Step 6: Append the layout CSS**

In `src/doc3gpp/web/static/style.css`, immediately after the `.content` rule (lines 60-64, ending at the closing `}` after `padding: 0 1.5rem;`), add:

```css
.content-wide {
  max-width: 1430px;
}
```

And append to the end of the file:

```css
table.tdoc-grid .col-meeting { width: 180px; }
```

Scoped to `tdoc-grid` so search/meetings/WIs grids are untouched.

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_web_routes.py -k "tdoc_list or meetings_page_keeps_default_width" -v`
Expected: PASS — updated default-columns test, new meetings-width test, and all existing tdoc-list tests.

Run: `python -m pytest tests/unit/test_web_routes.py`
Expected: all pass.

Run: `ruff check .`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/doc3gpp/web/templates/base.html src/doc3gpp/web/templates/tdoc_list.html src/doc3gpp/web/templates/partials/tdoc_results.html src/doc3gpp/web/static/style.css tests/unit/test_web_routes.py
git commit -m "feat(web): widen tdoc list page and fix meeting column width"
```

---

### Task 3: Documentation sync

**Files:**
- Modify: `docs/web-server.md` (tdoc list section, lines 235-248)

**Interfaces:**
- Consumes: the two prior tasks' behavior — dropdown checkboxes with instant re-query, 1430px page width, 180px Meeting column.

- [ ] **Step 1: Update the tdoc list docs**

In `docs/web-server.md`, replace the paragraph at lines 235-239 ("The TDoc list page accepts repeated `fields` query params ... A multi-select in the filter form drives the `fields` param.") with:

```markdown
The TDoc list page accepts repeated `fields` query params selecting the
visible columns; values are validated against the column catalogue and
an unknown field returns a 400 `invalid_filter` response. Default
columns are TDoc ID, Title, Meeting, Type, Spec, Release, Status
(Uploaded was replaced by Status). A dropdown of checkboxes in the
filter form drives the `fields` param — toggling a checkbox re-queries
immediately (the form auto-submits on change). The tdoc list page uses
a wider layout (max-width 1430px) and the Meeting column is fixed at
180px.
```

The rest of the paragraph (status row colors through the fixed 10-field JSON/MCP output, lines 240-248) stays unchanged.

- [ ] **Step 2: Verify**

Run: `ruff check .`
Expected: clean (no Python touched; guard only).

Run: `git diff --stat` and `git diff docs/web-server.md`
Expected: the only change is the one paragraph in `docs/web-server.md`.

- [ ] **Step 3: Commit**

```bash
git add docs/web-server.md
git commit -m "docs: document tdoc dropdown checkboxes and layout widths"
```
