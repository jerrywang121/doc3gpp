# Design: tdoc list — dropdown checkboxes, wider layout, wider meeting column

**Date:** 2026-08-05
**Status:** Approved (user: "ok")

## Goal

Three UI refinements on the tdoc list page (`/tdocs`), building on the
already-implemented selectable-columns feature (`feat/web-ui-enhancements`):

1. Replace the native `<select multiple size="5">` column selector with a
   **dropdown checkboxes** control.
2. Increase the tdoc list page's max table width by **30%** (1100px → 1430px)
   — tdoc list page only, other pages unchanged.
3. Set the **Meeting** column width to a fixed **180px** (replacing its
   current auto width).

## Current state

- `src/doc3gpp/web/templates/partials/tdoc_filters.html:56-61` — `<select name="fields" multiple size="5">` with one `<option>` per column key, `selected` when `key in fields`. The form already carries `hx-trigger="change, submit"` → any checkbox change in a replacement control auto-submits the same repeated `fields` params.
- `src/doc3gpp/web/templates/partials/tdoc_results.html` — `<table class="grid">`, header/body cells rendered by a `{% for f in fields %}` loop (dynamic columns, no fixed positions).
- `src/doc3gpp/web/templates/base.html:22` — `<main class="content">` (max-width 1100px, shared by every page).
- `src/doc3gpp/web/templates/tdoc_list.html` — extends base, includes the filters + results partials.
- `src/doc3gpp/web/static/style.css:101-129` — `table.grid` rules (shared by search/meetings/WIs tables).
- `src/doc3gpp/web/static/js/search.js` — precedent for a deferred page script.
- Tests: `tests/unit/test_web_routes.py:1247-1253` `test_tdoc_list_fields_select_renders` asserts `name="fields"`, `value="related_wis"`, `value="status"`.

## Design

### 1. Dropdown-checkbox column selector

**`src/doc3gpp/web/templates/partials/tdoc_filters.html`** — replace the
`<select>` block with a wrapper + button + panel of checkboxes:

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

Behavior (user decision: **instant auto-submit on toggle**): the checkboxes
are inside the `<form>`, so the existing `hx-trigger="change"` fires the
same `/tdocs` re-query as the old select on every toggle. Wire format
identical: repeated `fields` query params. The button is `type="button"`
so it cannot submit.

`hidden` attribute for the panel means no-JS users still get the form but
the dropdown is collapsed; `columns-count` shows the selected count.

**`src/doc3gpp/web/static/js/tdoc_columns.js`** (new, deferred on the full
page only, mirroring `search.js`):

```js
document.addEventListener('DOMContentLoaded', () => {
  const trigger = document.querySelector('.columns-trigger');
  if (!trigger) return;
  const panel = trigger.closest('.columns-dropdown').querySelector('.columns-panel');
  const count = trigger.closest('.columns-dropdown').querySelector('.columns-count');

  function updateCount() {
    const n = panel.querySelectorAll('input[type="checkbox"]:checked').length;
    if (count) count.textContent = n;
  }

  trigger.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = panel.hidden;
    panel.hidden = !open;
    trigger.setAttribute('aria-expanded', String(open));
  });
  panel.addEventListener('change', updateCount);
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.columns-dropdown')) {
      panel.hidden = true;
      trigger.setAttribute('aria-expanded', 'false');
    }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !panel.hidden) {
      panel.hidden = true;
      trigger.setAttribute('aria-expanded', 'false');
    }
  });
});
```

The form sits outside `#results`, so it is never HTMX-swapped; no
`htmx:afterSwap` handling needed.

**`src/doc3gpp/web/templates/tdoc_list.html`** — add
`<script src="/static/js/tdoc_columns.js" defer></script>` (full page only;
the fragment `partials/tdoc_results.html` never carries the script —
same pattern as `search.js`).

**CSS** (append to `style.css`):

```css
.columns-dropdown { position: relative; display: inline-block; }
.columns-panel {
  position: absolute; top: 100%; left: 0; z-index: 10;
  min-width: 14rem; max-height: 15rem; overflow-y: auto;
  background: var(--card-bg); border: 1px solid var(--border);
  border-radius: 6px; padding: 0.5rem; display: grid; gap: 0.25rem;
}
.columns-option {
  display: flex; align-items: center; gap: 0.4rem;
  font-size: 0.875rem; cursor: pointer; white-space: nowrap;
}
```

Note: the `hidden` attribute's default `display: none` wins over the
`display: grid` rule only if the CSS rule does not override it — the panel
uses `display: grid`, which WOULD beat the UA `[hidden]` rule. Fix: use
the `[hidden]` attribute selector to keep `hidden` authoritative:

```css
.columns-panel[hidden] { display: none; }
```

### 2. Page width — tdoc list only

**`src/doc3gpp/web/templates/base.html:22`** — make the main's class
overridable:

```html
<main class="content{% block main_class %}{% endblock %}">
```

**`src/doc3gpp/web/templates/tdoc_list.html`** — add `{% block main_class %} content-wide{% endblock %}` (note the leading space inside the block so it joins as `content content-wide`).

**CSS:**

```css
.content-wide { max-width: 1430px; }
```

All other pages keep 1100px.

### 3. Meeting column 180px

Columns are dynamic, so position-based selectors (`nth-child`) are wrong.
Tag the meeting cell in the template loop:

**`src/doc3gpp/web/templates/partials/tdoc_results.html`**:

```html
<table class="grid tdoc-grid">
  <thead>
    <tr>
      {% for f in fields %}
        <th{% if f == 'meeting_name' %} class="col-meeting"{% endif %}>{{ column_labels[f] }}</th>
      {% endfor %}
      <th></th>
    </tr>
  </thead>
  <tbody>
    {% for row in tdocs %}
      <tr class="{{ status_color_class(row['status']) }}">
        {% for f in fields %}
          <td{% if f == 'meeting_name' %} class="col-meeting"{% endif %}>
            ...
```

**CSS:**

```css
table.tdoc-grid .col-meeting { width: 180px; }
```

Scoped to `tdoc-grid` so search/meetings/WIs grids are untouched.

## Files

- Modify: `src/doc3gpp/web/templates/partials/tdoc_filters.html`
- Modify: `src/doc3gpp/web/templates/partials/tdoc_results.html`
- Modify: `src/doc3gpp/web/templates/base.html`
- Modify: `src/doc3gpp/web/templates/tdoc_list.html`
- Create: `src/doc3gpp/web/static/js/tdoc_columns.js`
- Modify: `src/doc3gpp/web/static/style.css`
- Test: `tests/unit/test_web_routes.py`
- Docs: `docs/web-server.md`

## Testing

- Update `test_tdoc_list_fields_select_renders` — assert checkbox markup
  instead of the `<select>` (`type="checkbox"`, `name="fields"`, the
  `columns-count` span).
- New assertions in the default-columns test: `<table class="grid tdoc-grid">`,
  `class="col-meeting"` present when `meeting_name` is a default field,
  `content content-wide` on the tdoc list page, and a non-tdoc page (e.g.
  `/meetings`) still renders plain `class="content"`.
- Existing `test_tdoc_list_custom_fields`, `test_tdoc_list_fields_persist_in_pagination`,
  `test_tdoc_list_unknown_field_returns_400`, `test_tdoc_list_status_row_colors`
  must keep passing (wire format unchanged).
- Run: `ruff check .`, targeted pytest, `./scripts/test_sqlite.sh`.

## Docs

`docs/web-server.md`: update the tdoc-list section — "Columns" selector is
a dropdown with checkboxes (instant re-query on toggle); tdoc list page is
wider (max-width 1430px); Meeting column fixed at 180px.

## Out of scope

- No changes to the JSON/MCP fixed 10-field contract.
- No changes to search/meetings/WIs table styling.
- No persistence of the columns selection in localStorage (URL query
  params remain the source of truth).
