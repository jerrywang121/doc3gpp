# Web UI Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add four web-UI enhancements: a tdoc filter on the search page (FTS5 + semantic), single-folding search results with a master expand/collapse toggle, a Related WIs field on the tdoc detail page, and user-selectable tdoc list columns with status-colored rows.

**Architecture:** All changes live in `src/doc3gpp/web/` (routes, templates, static JS/CSS) plus `src/doc3gpp/web/render.py` (column labels). Backend plumbing for the tdoc search filter already exists (`SearchFilters.tdoc_id` is honoured by the FTS5 SQL, the vector KNN, and the semantic FTS5 fan-out); the plan only wires it into the routes and forms. No model, repository, service, or CLI changes.

**Tech Stack:** FastAPI, Jinja2 (server-rendered templates), HTMX (vendored, htmx.min.js), vanilla JS (first custom script in the repo), pytest + TestClient.

**Spec:** [`docs/superpowers/specs/2026-08-04-web-ui-enhancements-design.md`](../specs/2026-08-04-web-ui-enhancements-design.md)

## Global Constraints

- Do not change `?format=json` payloads — they must stay byte-identical to the CLI (spec Ruling B). The JSON path of `list_tdocs` keeps `_TDOC_DEFAULT_FIELDS`; the MCP tool is untouched.
- The status color applies to the **entire row** (`<tr class="...">`), not just the Status cell, so it shows even when the Status column is deselected.
- Status matching: case-insensitive substring, first match in the ordered table wins. Order: `conditionally`/`partially` → lgreen, then `agreed`/`approved` → green, `revised`/`reissued`/`merged` → vanilla, `rejected` → red, `withdrawn` → grey, `postponed` → pink, `noted`/`treated`/`endorsed` → lblue. No match → no class.
- Default tdoc list HTML columns (no `fields` param): `[tdoc_id, meeting_name, title, type, spec, release, status]` — current look with `Uploaded` replaced by `Status`. The "show" action column is always on.
- Search tdoc filter uses exact-match semantics identical to CLI `search query --tdoc-id` (raw string, not `parse_tdoc_id_query`'s tuple — that is for meeting-range matching only).
- The HTMX partials contract must be preserved: list/search fragments are a single `<div id="results">` block with no `<!DOCTYPE>`/`<html>`.
- Docs: update `docs/web-server.md` in the same change set.
- Verify: `ruff check .` and `./scripts/test_sqlite.sh` must pass.

---

### Task 1: Search page tdoc filter (routes + form)

**Files:**
- Modify: `src/doc3gpp/web/routes/search.py` (`_build_filters` ~line 43, `search_query` ~line 115, `search_semantic` ~line 178)
- Modify: `src/doc3gpp/web/templates/partials/search_form.html`
- Test: `tests/unit/test_web_routes.py`

**Interfaces:**
- Consumes: `SearchFilters.tdoc_id` (already exists, `models/search.py`), `parse_text_query` (`web/filters.py`, `None`/`""` → `None`).
- Produces: `search_query` and `search_semantic` accept `tdoc_id: str | None = Query(default=None, alias="tdoc-id")`; `search_semantic` template context gains a `filters` dict; the search form gains a `tdoc-id` text input in both branches.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_web_routes.py`. First make the fakes record what they receive:

Modify `FakeSearchService` (line ~190) — record filters:

```python
class FakeSearchService(SearchService):
    def __init__(self) -> None:  # noqa: D401
        self.last_filters = None
        self._hits = [
            SearchHit(
                tdoc_id="R5-260001",
                score=-1.234,
                previews={"title": "<<NR>> measurement"},
                title="CR on NR measurement",
                meeting="RAN5#99-e",
                tsg="R5",
                uploaded_date="2026-05-02",
                ftp_url="R5/26.001/R5-260001.zip",
                wis=None,
            ),
        ]

    def search(self, _query: str, _filters: Any) -> list[SearchHit]:
        self.last_filters = _filters
        return list(self._hits)
```

Modify `FakeSemanticSearchService` (line ~210) — record kwargs:

```python
    def __init__(self) -> None:  # noqa: D401
        self.last_kwargs: dict[str, Any] = {}
        ...existing hits list unchanged...
        self._hits = [...]  # keep the existing SemanticSearchHit block

    def search(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        self.last_kwargs = dict(_kwargs)
        return list(self._hits)
```

Add new tests at the end of the Search section (after `test_search_sem_json`):

```python
def test_search_query_tdoc_id_filter_forwarded(client: TestClient) -> None:
    """``GET /search?tdoc-id=<id>`` forwards tdoc_id into SearchFilters."""
    from doc3gpp.web.deps import get_search_service

    service = FakeSearchService()
    client.app.dependency_overrides[get_search_service] = lambda: service
    try:
        response = client.get("/search?q=foo&tdoc-id=R5-260001")
    finally:
        client.app.dependency_overrides.pop(get_search_service, None)
    assert response.status_code == 200
    assert service.last_filters is not None
    assert service.last_filters.tdoc_id == "R5-260001"


def test_search_query_empty_tdoc_id_is_no_filter(client: TestClient) -> None:
    """``GET /search?q=foo&tdoc-id=`` is 200 and tdoc_id stays None."""
    from doc3gpp.web.deps import get_search_service

    service = FakeSearchService()
    client.app.dependency_overrides[get_search_service] = lambda: service
    try:
        response = client.get("/search?q=foo&tdoc-id=")
    finally:
        client.app.dependency_overrides.pop(get_search_service, None)
    assert response.status_code == 200
    assert service.last_filters is not None
    assert service.last_filters.tdoc_id is None


def test_search_sem_tdoc_id_filter_forwarded(client: TestClient) -> None:
    """``GET /search/sem?tdoc-id=<id>`` forwards tdoc_id into SearchFilters."""
    from doc3gpp.web.deps import get_semantic_search_service

    service = FakeSemanticSearchService()
    client.app.dependency_overrides[get_semantic_search_service] = lambda: service
    try:
        response = client.get("/search/sem?q=foo&tdoc-id=R5-260001")
    finally:
        client.app.dependency_overrides.pop(get_semantic_search_service, None)
    assert response.status_code == 200
    filters = service.last_kwargs.get("filters")
    assert filters is not None
    assert filters.tdoc_id == "R5-260001"


def test_search_form_renders_tdoc_input_fts5(client: TestClient) -> None:
    """The FTS5 search form carries a tdoc-id input with the round-tripped value."""
    html = client.get("/search?q=foo&tdoc-id=R5-260001").text
    assert 'name="tdoc-id"' in html
    assert 'value="R5-260001"' in html


def test_search_form_renders_tdoc_input_sem(client: TestClient) -> None:
    """The semantic search form carries a tdoc-id input with the round-tripped value."""
    html = client.get("/search/sem?q=foo&tdoc-id=R5-260001").text
    assert 'name="tdoc-id"' in html
    assert 'value="R5-260001"' in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_web_routes.py -k "tdoc_id_filter or tdoc_input" -v`
Expected: FAIL — `_build_filters` / routes don't accept `tdoc-id`, form lacks the input.

- [ ] **Step 3: Wire the routes**

In `src/doc3gpp/web/routes/search.py`:

Add `tdoc_id` to `_build_filters`:

```python
def _build_filters(
    *,
    tsg: str | None,
    meeting: str | None,
    release: str | None,
    spec: str | None,
    since: str | None,
    until: str | None,
    tdoc_id: str | None,
    limit: int,
) -> SearchFilters:
    """Compose a :class:`SearchFilters` from raw query params.

    ``since`` / ``until`` are validated as date filters first so a
    malformed value surfaces as HTTP 400 (``invalid_filter``) rather
    than being swallowed by the query path.
    """
    return SearchFilters(
        tsg=parse_text_query(tsg),
        meeting=parse_text_query(meeting),
        release=parse_text_query(release),
        spec=parse_text_query(spec),
        since=parse_date_query(since),
        until=parse_date_query(until),
        tdoc_id=parse_text_query(tdoc_id),
        limit=limit,
    )
```

In `search_query`, add the param after `until`:

```python
    until: str | None = Query(default=None),
    tdoc_id: str | None = Query(default=None, alias="tdoc-id"),
    limit: str | None = Query(default="20"),
```

and pass it through (the `filters` context dict also gains the round-trip value):

```python
    filters = _build_filters(
        tsg=tsg, meeting=meeting, release=release,
        spec=spec, since=since, until=until, tdoc_id=tdoc_id,
        limit=parsed_limit,
    )
```

and in the `filters` context dict add:

```python
            "filters": {
                "tsg": tsg or "",
                "meeting": meeting or "",
                "release": release or "",
                "spec": spec or "",
                "since": since or "",
                "until": until or "",
                "tdoc_id": tdoc_id or "",
            },
```

In `search_semantic`, add the param and pass it:

```python
async def search_semantic(
    request: Request,
    q: str | None = Query(default=None),
    tdoc_id: str | None = Query(default=None, alias="tdoc-id"),
    fts5_query: str | None = Query(default=None),
    ...
```

```python
        hits = service.search(
            q,
            fts5_query=fts5_query,
            filters=SearchFilters(
                limit=parsed_limit,
                tdoc_id=parse_text_query(tdoc_id),
            ),
            limit=parsed_limit,
            fts5_weight=fts5_weight,
        )
```

and add a `filters` dict to the sem template context (after `"error": error,`):

```python
            "filters": {
                "tdoc_id": tdoc_id or "",
            },
```

- [ ] **Step 4: Add the form inputs**

In `src/doc3gpp/web/templates/partials/search_form.html`, add a TDoc input to **both** branches. In the FTS5 branch (after the Meeting label):

```html
    <label>TDoc
      <input type="text" name="tdoc-id" value="{{ filters.tdoc_id or '' }}">
    </label>
```

In the sem branch (after the Query label):

```html
    <label>TDoc
      <input type="text" name="tdoc-id" value="{{ filters.tdoc_id or '' }}">
    </label>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_web_routes.py -k "tdoc_id_filter or tdoc_input" -v`
Expected: PASS (all 5)

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/routes/search.py src/doc3gpp/web/templates/partials/search_form.html tests/unit/test_web_routes.py
git commit -m "feat(web): add tdoc filter to search page"
```

---

### Task 2: Search results single folding + master toggle

**Files:**
- Create: `src/doc3gpp/web/templates/partials/search_results_table.html`
- Create: `src/doc3gpp/web/static/js/search.js`
- Modify: `src/doc3gpp/web/templates/search_results.html`
- Modify: `src/doc3gpp/web/templates/partials/search_results.html`
- Modify: `src/doc3gpp/web/static/style.css`
- Test: `tests/unit/test_web_routes.py`

**Interfaces:**
- Consumes: the `hits` / `mode` context keys already passed by both search routes; `hit.previews` dict (column → snippet with `<<...>>` markers).
- Produces: a shared table partial `partials/search_results_table.html` included by both search result templates; a `details.hit-details` block per hit with `.preview-field` children; a `#fold-toggle` checkbox inside the results fragment; `/static/js/search.js` loaded by the full page; CSS classes `#fold-toggle`, `details.hit-details`, `.preview-field`, `.preview-label`.

- [ ] **Step 1: Write the failing tests**

Add to the Search section of `tests/unit/test_web_routes.py`:

```python
def test_search_results_single_details_per_hit(client: TestClient) -> None:
    """One details.hit-details block per hit (single folding), not per column."""
    response = client.get("/search?q=foo")
    assert response.status_code == 200
    body = response.text
    assert body.count('<details class="hit-details">') == 1
    assert '<span class="preview-label">title</span>' in body


def test_search_results_has_master_toggle(client: TestClient) -> None:
    """The results fragment carries the fold/unfold-all toggle."""
    response = client.get("/search?q=foo", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert 'id="fold-toggle"' in response.text


def test_search_results_toggle_absent_without_hits(client: TestClient) -> None:
    """No hits -> no toggle and no details."""
    response = client.get("/search")
    assert response.status_code == 200
    assert 'id="fold-toggle"' not in response.text


def test_search_full_page_loads_search_js(client: TestClient) -> None:
    """The full search page includes the fold-toggle script."""
    html = client.get("/search?q=foo").text
    assert 'src="/static/js/search.js"' in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_web_routes.py -k "search_results or search_full_page" -v`
Expected: FAIL — old markup has per-column `<details>` with no class, no toggle, no script.

- [ ] **Step 3: Create the shared table partial**

Create `src/doc3gpp/web/templates/partials/search_results_table.html`:

```html
{% if hits %}
  <table class="grid" id="search-results-table">
    <thead>
      <tr>
        <th>TDoc</th>
        <th>Title</th>
        <th>Meeting</th>
        <th>TSG</th>
        {% if mode == 'sem' %}<th>RRF</th><th>FTS5 rank</th><th>vec rank</th>{% else %}<th>Score</th>{% endif %}
      </tr>
    </thead>
    <tbody>
      {% for hit in hits %}
        <tr>
          <td><a href="/tdocs/{{ hit.tdoc_id }}"><code>{{ hit.tdoc_id }}</code></a></td>
          <td>{{ hit.title or '-' }}</td>
          <td>{{ hit.meeting or '-' }}</td>
          <td>{{ hit.tsg or '-' }}</td>
          {% if mode == 'sem' %}
            <td>{{ '%.4f' | format(hit.rrf_score) }}</td>
            <td>{{ hit.rank_fts5 if hit.rank_fts5 is not none else '-' }}</td>
            <td>{{ hit.rank_vec if hit.rank_vec is not none else '-' }}</td>
          {% else %}
            <td>{{ '%.4f' | format(hit.score) }}</td>
          {% endif %}
        </tr>
        {% if hit.previews %}
          <tr class="preview-row">
            <td colspan="6">
              <details class="hit-details">
                <summary>Matching fields</summary>
                {% for col, snippet in hit.previews.items() %}
                  <div class="preview-field">
                    <span class="preview-label">{{ col }}</span>
                    <pre>{{ snippet }}</pre>
                  </div>
                {% endfor %}
              </details>
            </td>
          </tr>
        {% endif %}
      {% endfor %}
    </tbody>
  </table>
{% else %}
  <p class="empty">No matches.</p>
{% endif %}
```

- [ ] **Step 4: Rewrite the two result templates to use the partial**

Replace the table markup in `src/doc3gpp/web/templates/partials/search_results.html` (keep the outer `#results` div, error + meta blocks) so it becomes:

```html
<div id="results" hx-target="this" hx-swap="outerHTML">
  {% if error %}
    <p class="error">{{ error }}</p>
  {% endif %}

  {% if query %}
    <p class="meta">
      Query <code>{{ query }}</code> ·
      {% if mode == 'sem' %}
        semantic rerank
      {% else %}
        {{ total }} FTS5 hit{{ '' if total == 1 else 's' }}
      {% endif %}
    </p>

    {% if hits %}
      <label class="fold-toggle">
        <input type="checkbox" id="fold-toggle">
        Expand all matching fields
      </label>
    {% endif %}

    {% include "partials/search_results_table.html" %}
  {% endif %}
</div>
```

Replace `src/doc3gpp/web/templates/search_results.html` so the `#results` block is identical to the partial above (the toggle lives inside `#results` so it swaps in/out with the results), and add the script include at the end of the content block:

```html
{% extends "base.html" %}
{% block title %}doc3gpp · search{% endblock %}
{% block content %}
  <h1>Search</h1>

  {% include "partials/search_form.html" %}

  <div id="results" hx-target="this" hx-swap="outerHTML">
    {% if error %}
      <p class="error">{{ error }}</p>
    {% endif %}

    {% if query %}
      <p class="meta">
        Query <code>{{ query }}</code> ·
        {% if mode == 'sem' %}
          semantic rerank
        {% else %}
          {{ total }} FTS5 hit{{ '' if total == 1 else 's' }}
        {% endif %}
      </p>

      {% if hits %}
        <label class="fold-toggle">
          <input type="checkbox" id="fold-toggle">
          Expand all matching fields
        </label>
      {% endif %}

      {% include "partials/search_results_table.html" %}
    {% endif %}
  </div>

  <script src="/static/js/search.js" defer></script>
{% endblock %}
```

- [ ] **Step 5: Create the toggle script**

Create `src/doc3gpp/web/static/js/search.js`:

```js
/* Fold / unfold all search-result matching-fields blocks.
 *
 * The toggle lives inside the #results fragment, so it is recreated on
 * every HTMX swap. The chosen state is persisted in localStorage and
 * re-applied after each swap so the preference survives re-queries.
 */
(function () {
  'use strict';
  var KEY = 'doc3gpp-search-expand';

  function applyState(container) {
    var toggle = container.querySelector('#fold-toggle');
    if (!toggle) return;
    var want = localStorage.getItem(KEY) === '1';
    toggle.checked = want;
    container.querySelectorAll('details.hit-details').forEach(function (d) {
      d.open = want;
    });
  }

  document.addEventListener('change', function (e) {
    if (e.target && e.target.id === 'fold-toggle') {
      localStorage.setItem(KEY, e.target.checked ? '1' : '0');
      document.querySelectorAll('details.hit-details').forEach(function (d) {
        d.open = e.target.checked;
      });
    }
  });

  document.body.addEventListener('htmx:afterSwap', function (e) {
    var target = e.detail && e.detail.target;
    if (target && target.querySelector) applyState(target);
  });

  document.addEventListener('DOMContentLoaded', function () {
    var results = document.getElementById('results');
    if (results) applyState(results);
  });
})();
```

- [ ] **Step 6: Add the CSS**

Append to `src/doc3gpp/web/static/style.css`:

```css
/* Search results: fold toggle + per-hit matching-fields blocks */
.fold-toggle {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  margin-bottom: 0.5rem;
  font-size: 0.875rem;
  color: var(--muted);
  cursor: pointer;
}

details.hit-details summary {
  cursor: pointer;
  font-size: 0.875rem;
}

.preview-field {
  margin: 0.5rem 0 0.5rem 1rem;
}

.preview-field .preview-label {
  display: block;
  font-size: 0.8rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.preview-field pre {
  margin: 0.25rem 0 0;
  overflow-x: auto;
}
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/unit/test_web_routes.py -k "search_results or search_full_page or search_query_htmx or search_sem_htmx" -v`
Expected: PASS — the HTMX-partial and JSON parity tests (`test_search_query_json` etc.) must still pass: the JSON path is untouched and the fragments still start with `<div id="results"`.

- [ ] **Step 8: Commit**

```bash
git add src/doc3gpp/web/templates/partials/search_results_table.html src/doc3gpp/web/static/js/search.js src/doc3gpp/web/templates/search_results.html src/doc3gpp/web/templates/partials/search_results.html src/doc3gpp/web/static/style.css tests/unit/test_web_routes.py
git commit -m "feat(web): single-folding search results with master toggle"
```

---

### Task 3: TDoc detail page — Related WIs in the TDoc section

**Files:**
- Modify: `src/doc3gpp/web/templates/tdoc_show.html` (TDoc `dl.kv`, after the Uploaded pair ~line 30)
- Test: `tests/unit/test_web_routes.py`

**Interfaces:**
- Consumes: `record.tdoc.related_wis` (already on the `TDoc` dataclass, `models/tdoc.py:45`, populated from the XLSX; the show route already composes it into `TDocShowRecord`).
- Produces: a `Related WIs` dt/dd pair in the TDoc section. The Cover page section's own `record.cover.related_wis` is untouched (different source).

- [ ] **Step 1: Write the failing test**

Add to the TDocs section of `tests/unit/test_web_routes.py` (after `test_tdoc_show_ttcn_changed_functions`):

```python
def test_tdoc_show_related_wis_in_tdoc_section(
    client: TestClient, sqlite_env: Any,
) -> None:
    """The TDoc section shows the related_wis field."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(
            tdoc_id="R5-260001",
            title="CR on NR measurement",
            ftp_url="R5/26.001/R5-260001.zip",
            related_wis="890001, 890002",
        ),
    )
    response = client.get("/tdocs/R5-260001")
    assert response.status_code == 200
    assert "<dt>Related WIs</dt>" in response.text
    assert "<dd>890001, 890002</dd>" in response.text


def test_tdoc_show_related_wis_dash_when_absent(
    client: TestClient, sqlite_env: Any,
) -> None:
    """No related_wis -> the field renders '-'."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260001", ftp_url="R5/26.001/R5-260001.zip"),
    )
    response = client.get("/tdocs/R5-260001")
    assert response.status_code == 200
    assert "<dt>Related WIs</dt>" in response.text
    assert "<dd>-</dd>" in response.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_web_routes.py -k "related_wis" -v`
Expected: FAIL — `Related WIs` not rendered (only the cover-page section has it).

- [ ] **Step 3: Add the dt/dd pair**

In `src/doc3gpp/web/templates/tdoc_show.html`, after the Uploaded pair (line 30):

```html
      <dt>Uploaded</dt><dd>{{ record.tdoc.uploaded_date.isoformat() if record.tdoc.uploaded_date else '-' }}</dd>
      <dt>Related WIs</dt><dd>{{ record.tdoc.related_wis or '-' }}</dd>
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_web_routes.py -k "related_wis" -v`
Expected: PASS (2)

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/web/templates/tdoc_show.html tests/unit/test_web_routes.py
git commit -m "feat(web): show related WIs in the tdoc detail section"
```

---

### Task 4: TDoc list — selectable columns + status-colored rows

**Files:**
- Modify: `src/doc3gpp/web/render.py` (add `TDOC_COLUMN_LABELS` + `TDOC_HTML_DEFAULT_FIELDS` after `_TDOC_DEFAULT_FIELDS`, line ~67)
- Modify: `src/doc3gpp/web/templates_setup.py` (add `status_color_class` filter, line ~59)
- Modify: `src/doc3gpp/web/routes/tdocs.py` (`list_tdocs`, line ~96; HTML branch ~line 169)
- Modify: `src/doc3gpp/web/templates/partials/tdoc_filters.html` (add multi-select)
- Modify: `src/doc3gpp/web/templates/partials/tdoc_results.html` (dynamic columns + row class)
- Modify: `src/doc3gpp/web/static/style.css` (status classes)
- Test: `tests/unit/test_web_routes.py`

**Interfaces:**
- Consumes: `tdoc_rows()` (`web/render.py:177`), `InvalidFilterError` (`web/errors.py`), `TDocWithMeeting` rows from `service.list_recent_with_meeting`.
- Produces:
  - `render.TDOC_COLUMN_LABELS: dict[str, str]` (key → label), `render.TDOC_HTML_DEFAULT_FIELDS: list[str]`.
  - `templates_setup.status_color_class(value: str | None) -> str` returning a CSS class name or `""`.
  - `list_tdocs` accepts `fields: list[str] | None = Query(default=None)`; HTML context gains `"fields"` (resolved keys), `"column_labels"`, and `"tdocs"` is now a list of row dicts (`tdoc_rows` output with `tdoc_id` guaranteed).
  - Template `tdoc_results.html` iterates `fields`; `<tr class="{{ status_color_class(row['status']) }}">`.

- [ ] **Step 1: Write the failing tests**

First extend `FakeTDocService` (line ~117) so rows carry statuses:

```python
    def __init__(self) -> None:  # noqa: D401
        self._rows = [
            TDocWithMeeting(
                tdoc=TDoc(
                    tdoc_id="R5-260001",
                    title="CR on NR measurement",
                    meeting_id=1,
                    ftp_url="R5/26.001/R5-260001.zip",
                    spec="38.523-3",
                    release="Rel-18",
                    type="CR",
                    status="Approved",
                    uploaded_date=date(2026, 5, 2),
                ),
                meeting_name="RAN5#99-e",
            ),
            TDocWithMeeting(
                tdoc=TDoc(
                    tdoc_id="R5-260002",
                    title="Another CR",
                    meeting_id=1,
                    ftp_url="R5/26.002/R5-260002.zip",
                    spec="38.523-3",
                    release="Rel-18",
                    type="CR",
                    status="Revised",
                    uploaded_date=date(2026, 5, 3),
                ),
                meeting_name="RAN5#99-e",
            ),
        ]
```

Add tests to the TDocs section:

```python
def test_tdoc_list_default_columns_use_status(client: TestClient) -> None:
    """Default HTML columns: Status replaces Uploaded; no fields param needed."""
    html = client.get("/tdocs").text
    assert "<th>Status</th>" in html
    assert "<th>Uploaded</th>" not in html


def test_tdoc_list_status_row_colors(client: TestClient) -> None:
    """Rows carry the status-derived class on the <tr>."""
    html = client.get("/tdocs").text
    assert '<tr class="status-green">' in html
    assert '<tr class="status-vanilla">' in html


def test_tdoc_list_custom_fields(client: TestClient) -> None:
    """?fields=tdoc_id&fields=related_wis renders only those columns + action."""
    html = client.get(
        "/tdocs?fields=tdoc_id&fields=related_wis",
    ).text
    assert "<th>TDoc ID</th>" in html
    assert "<th>Related WIs</th>" in html
    assert "<th>Status</th>" not in html


def test_tdoc_list_unknown_field_returns_400(client: TestClient) -> None:
    """?fields=bogus is 400 with the invalid_filter envelope."""
    response = client.get("/tdocs?fields=bogus")
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_filter"


def test_tdoc_list_fields_select_renders(client: TestClient) -> None:
    """The filter form carries the multi-select with all column options."""
    html = client.get("/tdocs").text
    assert 'name="fields"' in html
    assert 'value="related_wis"' in html
    assert 'value="status"' in html


def test_tdoc_list_fields_persist_in_pagination(
    app_with_fakes: FastAPI,
) -> None:
    """The fields selection is preserved in pagination links."""
    from doc3gpp.web.deps import get_tdoc_service

    class _ManyRowsTDocService(FakeTDocService):
        def list_recent_with_meeting(
            self, *, limit: int = 50, offset: int = 0, **_kwargs: Any,
        ) -> list[TDocWithMeeting]:
            return [
                TDocWithMeeting(
                    tdoc=TDoc(
                        tdoc_id=f"R5-{260001 + offset + i:06d}",
                        title=f"row {offset + i}",
                        meeting_id=1,
                        ftp_url=f"R5/26.{(offset + i):03d}/R5-{260001 + offset + i:06d}.zip",
                        spec="38.523-3",
                        release="Rel-18",
                        type="CR",
                        status="Agreed",
                        uploaded_date=date(2026, 5, 2),
                    ),
                    meeting_name="RAN5#99-e",
                )
                for i in range(limit)
            ]

    app_with_fakes.dependency_overrides[get_tdoc_service] = (
        lambda: _ManyRowsTDocService()
    )
    with TestClient(app_with_fakes) as c:
        response = c.get("/tdocs?limit=50&fields=tdoc_id&fields=status")
    assert response.status_code == 200
    assert "fields=tdoc_id" in response.text
    assert "fields=status" in response.text
```

Add a unit-test block for the filter (new section after the TDocs section):

```python
# ---------------------------------------------------------------------------
# status_color_class
# ---------------------------------------------------------------------------


def test_status_color_class_mapping() -> None:
    """Each status needle maps to its class, case-insensitively."""
    from doc3gpp.web.templates_setup import status_color_class

    cases = {
        "Conditionally Approved": "status-lgreen",
        "Partially Approved": "status-lgreen",
        "Agreed": "status-green",
        "approved": "status-green",
        "Revised": "status-vanilla",
        "Reissued": "status-vanilla",
        "Merged": "status-vanilla",
        "Rejected": "status-red",
        "Withdrawn": "status-grey",
        "Postponed": "status-pink",
        "Noted": "status-lblue",
        "Treated": "status-lblue",
        "Endorsed": "status-lblue",
    }
    for value, expected in cases.items():
        assert status_color_class(value) == expected, value


def test_status_color_class_no_match_and_empty() -> None:
    """No matching needle (or None/empty) -> no class."""
    from doc3gpp.web.templates_setup import status_color_class

    assert status_color_class("Submitted") == ""
    assert status_color_class("") == ""
    assert status_color_class(None) == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_web_routes.py -k "tdoc_list or status_color" -v`
Expected: FAIL — no `fields` param, hard-coded 8-column table, no filter, no row classes. (Note: `test_tdoc_list_json` must still pass — JSON path unchanged.)

- [ ] **Step 3: Add the column constants and filter**

In `src/doc3gpp/web/render.py`, after `_TDOC_DEFAULT_FIELDS` (line ~67):

```python
# HTML column catalogue for the tdoc list page. The keys mirror the
# field names used by ``tdoc_rows``; the values are the table headers.
TDOC_COLUMN_LABELS: dict[str, str] = {
    "tdoc_id": "TDoc ID",
    "meeting_name": "Meeting",
    "title": "Title",
    "type": "Type",
    "spec": "Spec",
    "release": "Release",
    "status": "Status",
    "uploaded_date": "Uploaded",
    "source": "Source",
    "cr_cat": "CR Category",
    "version": "Version",
    "related_wis": "Related WIs",
}

# Default HTML table columns: the previous hard-coded set with
# ``uploaded_date`` replaced by ``status`` (spec 2026-08-04).
TDOC_HTML_DEFAULT_FIELDS: list[str] = [
    "tdoc_id",
    "meeting_name",
    "title",
    "type",
    "spec",
    "release",
    "status",
]
```

Extend `__all__` in `render.py`:

```python
__all__ = [
    "TDOC_COLUMN_LABELS",
    "TDOC_HTML_DEFAULT_FIELDS",
    "meeting_rows",
    "tdoc_rows",
    "to_jsonable",
    "tsg_rows",
    "wi_rows",
]
```

In `src/doc3gpp/web/templates_setup.py`, after `sync_state` (line ~55):

```python
# Status colour rules for the tdoc list page: ordered, case-insensitive
# substring matches; the first matching entry wins (spec 2026-08-04).
_STATUS_COLOR_RULES: list[tuple[str, str]] = [
    ("conditionally", "status-lgreen"),
    ("partially", "status-lgreen"),
    ("agreed", "status-green"),
    ("approved", "status-green"),
    ("revised", "status-vanilla"),
    ("reissued", "status-vanilla"),
    ("merged", "status-vanilla"),
    ("rejected", "status-red"),
    ("withdrawn", "status-grey"),
    ("postponed", "status-pink"),
    ("noted", "status-lblue"),
    ("treated", "status-lblue"),
    ("endorsed", "status-lblue"),
]


def status_color_class(value: str | None) -> str:
    """Map a tdoc status string to a pastel row-background CSS class.

    Matching is case-insensitive substring; the first rule whose needle
    appears wins. ``None`` / empty / unmatched values yield ``""`` so
    the row renders with no background.
    """
    if not value:
        return ""
    lowered = value.lower()
    for needle, cls in _STATUS_COLOR_RULES:
        if needle in lowered:
            return cls
    return ""
```

and register it (after `templates.env.filters["sync_state"] = sync_state`):

```python
templates.env.filters["status_color_class"] = status_color_class
```

- [ ] **Step 4: Wire the route**

In `src/doc3gpp/web/routes/tdocs.py`:

Update imports:

```python
from doc3gpp.web.render import (
    TDOC_COLUMN_LABELS,
    TDOC_HTML_DEFAULT_FIELDS,
    to_jsonable,
    tdoc_rows,
)
```

Add a module constant after `_TDOC_DEFAULT_FIELDS`:

```python
_TDOC_ALLOWED_FIELDS = frozenset(TDOC_COLUMN_LABELS)
```

Add the query param to `list_tdocs` (after `offset`):

```python
    limit: str | None = Query(default="50"),
    offset: str | None = Query(default="0"),
    fields: list[str] | None = Query(default=None),
    format: str | None = Query(default=None, alias="format"),
```

Add resolution logic after `parsed_uploaded_date = parse_date_query(uploaded_date)` (line ~138):

```python
    if fields:
        unknown = [f for f in fields if f not in _TDOC_ALLOWED_FIELDS]
        if unknown:
            raise InvalidFilterError(
                "unknown fields: "
                + ", ".join(sorted(unknown))
                + f"; valid: {', '.join(sorted(_TDOC_ALLOWED_FIELDS))}"
            )
        html_fields = [f for f in fields if f]
    else:
        html_fields = list(TDOC_HTML_DEFAULT_FIELDS)
    if not html_fields:
        html_fields = list(TDOC_HTML_DEFAULT_FIELDS)
```

Replace the HTML branch (lines ~166-205). The `?format=json` path stays exactly as-is (`tdoc_rows(rows, _TDOC_DEFAULT_FIELDS)`). New HTML branch:

```python
    next_offset = (
        parsed_offset + len(rows) if len(rows) == parsed_limit else None
    )
    table_rows = tdoc_rows(rows, html_fields)
    for r, item in zip(table_rows, rows):
        r.setdefault("tdoc_id", item.tdoc.tdoc_id)
    template_name = (
        "partials/tdoc_results.html" if is_htmx_request(request) else "tdoc_list.html"
    )
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "active_nav": "tdocs",
            "tdocs": table_rows,
            "total": len(rows),
            "limit": parsed_limit,
            "offset": parsed_offset,
            "next_offset": next_offset,
            "pending_jobs": pending_jobs,
            "fields": html_fields,
            "column_labels": TDOC_COLUMN_LABELS,
            "filters": {
                "tdoc_id": tdoc_id or "",
                "meeting": meeting or "",
                "meeting_id": parsed_meeting_id,
                "title": title or "",
                "type": type or "",
                "source": source or "",
                "spec": spec or "",
                "wi": wi or "",
                "cr_cat": cr_cat or "",
                "status": status or "",
                "revision_of": revision_of or "",
                "revised_to": revised_to or "",
                "ftp_url": ftp_url or "",
                "release": release or "",
                "version": version or "",
                "cr_num": cr_num or "",
                "cr_pack": cr_pack or "",
                "uploaded_date": uploaded_date or "",
                "limit": parsed_limit,
            },
        },
    )
```

(Note: `filters` is unchanged except it is untouched by `fields` — the multi-select name does not collide with any filter.)

- [ ] **Step 5: Update the filter form**

In `src/doc3gpp/web/templates/partials/tdoc_filters.html`, add the multi-select before the Limit label (line ~62):

```html
  <label>Columns
    <select name="fields" multiple size="5">
      {% for key, label in column_labels.items() %}
        <option value="{{ key }}" {% if key in fields %}selected{% endif %}>{{ label }}</option>
      {% endfor %}
    </select>
  </label>
```

- [ ] **Step 6: Rewrite the results partial**

Replace `src/doc3gpp/web/templates/partials/tdoc_results.html` with:

```html
<div id="results" hx-target="this" hx-swap="outerHTML">
  {% if tdocs %}
    <table class="grid">
      <thead>
        <tr>
          {% for f in fields %}
            <th>{{ column_labels[f] }}</th>
          {% endfor %}
          <th></th>
        </tr>
      </thead>
      <tbody>
        {% for row in tdocs %}
          <tr class="{{ status_color_class(row['status']) }}">
            {% for f in fields %}
              <td>
                {% if f == 'tdoc_id' %}
                  <code><a href="/tdocs/{{ row['tdoc_id'] }}">{{ row[f] }}</a></code>
                {% else %}
                  {{ row[f] }}
                {% endif %}
              </td>
            {% endfor %}
            <td><a href="/tdocs/{{ row['tdoc_id'] }}">show</a></td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
    {% include "partials/pagination.html" %}
  {% else %}
    <p class="empty">No TDocs match these filters.</p>
  {% endif %}
</div>
```

The `tdoc_id` cell keeps the code+link styling the old template had; every other cell renders the coerced string from `tdoc_rows`. `row['tdoc_id']` is always present (the route `setdefault`s it) so the links work even when the column is deselected.

- [ ] **Step 7: Add the status CSS**

Append to `src/doc3gpp/web/static/style.css`:

```css
/* TDoc list: status-derived row backgrounds (soft pastels, dark text) */
tr.status-lgreen td { background: #e2f5d8; }
tr.status-green td { background: #c8e6c9; }
tr.status-vanilla td { background: #fff3cd; }
tr.status-red td { background: #f8d7da; }
tr.status-grey td { background: #e2e3e5; }
tr.status-pink td { background: #fce4ec; }
tr.status-lblue td { background: #d1ecf1; }
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `pytest tests/unit/test_web_routes.py -k "tdoc_list or status_color" -v`
Expected: PASS. Then the full file to catch regressions:

Run: `pytest tests/unit/test_web_routes.py -v`
Expected: PASS — in particular `test_tdoc_list_json`, `test_tdoc_list_htmx_returns_partial`, `test_tdoc_list_pagination_renders_without_500` (the `_ManyRowsTDocService` in that test builds rows without a `status` attribute — `row['status']` will be `"-"` from `_coerce_cell`, `status_color_class("-")` → `""`, no class — still 200).

- [ ] **Step 9: Commit**

```bash
git add src/doc3gpp/web/render.py src/doc3gpp/web/templates_setup.py src/doc3gpp/web/routes/tdocs.py src/doc3gpp/web/templates/partials/tdoc_filters.html src/doc3gpp/web/templates/partials/tdoc_results.html src/doc3gpp/web/static/style.css tests/unit/test_web_routes.py
git commit -m "feat(web): selectable tdoc list columns with status row colors"
```

---

### Task 5: Documentation sync

**Files:**
- Modify: `docs/web-server.md`

**Interfaces:**
- Consumes: the four completed features.

- [ ] **Step 1: Update docs/web-server.md**

Add/update the following, matching the file's existing prose style:

1. **Search page section** (the `GET /search` / `GET /search/sem` documentation):
   - Both search modes accept a `tdoc-id` query param — exact-match filter identical to CLI `search query --tdoc-id`; the form carries a TDoc input in both the FTS5 and semantic branches.
   - Search results render one collapsible "Matching fields" block per hit (single folding, replacing per-column folding), with an "Expand all matching fields" toggle above the table that persists across re-queries (localStorage + `htmx:afterSwap` re-apply; script at `/static/js/search.js`).
2. **TDoc detail section** (`GET /tdocs/{tdoc_id}`): the TDoc section now shows `Related WIs` (from the XLSX-derived `tdoc.related_wis`); the Cover page section's own Related WIs (parsed docx) is separate.
3. **TDoc list section** (`GET /tdocs`): the list table accepts repeated `fields` query params selecting the visible columns (validated against the column catalogue; unknown → 400 `invalid_filter`); default columns are TDoc ID, Title, Meeting, Type, Spec, Release, Status (Uploaded replaced by Status); a multi-select in the filter form drives it; row background colors derive from the status value (case-insensitive substring, ordered rules: conditionally/partially → light green, agreed/approved → green, revised/reissued/merged → vanilla, rejected → red, withdrawn → grey, postponed → pink, noted/treated/endorsed → light blue; no match → no background). The color applies to the whole row so it shows even when the Status column is hidden. `?format=json` and the MCP tool keep their fixed 10-field output.

- [ ] **Step 2: Verify lint + full test suite**

Run: `ruff check .`
Expected: no errors.

Run: `./scripts/test_sqlite.sh`
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add docs/web-server.md
git commit -m "docs: document web UI search tdoc filter, folding toggle, related WIs, column selection"
```

---

## Self-review notes

- **Spec coverage:** tdoc filter (Task 1), single folding + toggle (Task 2), Related WIs (Task 3), column selection + status colors + row-level class (Task 4), docs (Task 5). All four spec sections covered; the lgreen group added by the user is in the ordered rule table in Task 4.
- **Type consistency:** `TDOC_COLUMN_LABELS` / `TDOC_HTML_DEFAULT_FIELDS` defined in Task 4 Step 3 and consumed in Steps 4-6 of the same task; `status_color_class` defined in templates_setup.py and used in tdoc_results.html + tests; `html_fields` variable name used consistently in the route.
- **Known caveat:** `_ManyRowsTDocService` instances in pre-existing pagination tests build rows without `status`; the row class renders as `""` via `_coerce_cell` → `"-"` → no match. No test change needed there.
