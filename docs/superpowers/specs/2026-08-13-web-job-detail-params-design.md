# Web job detail page — show params — design

**Date:** 2026-08-13
**Status:** Approved
**Author:** opencode + user

## Goal

The web job detail page at `GET /jobs/{job_id}?format=html` currently
renders `partials/job_status.html`, which shows the job's `id`, `status`,
`kind`, optional `error`, and the log_tail list — but **not** the
`params` the user supplied when the job was enqueued. The job's `params`
field is already on the in-memory `Job` model
(`src/doc3gpp/models/jobs.py:106`), already round-trips through the
repository layer via `json.dumps` / `json.loads`, and is already part
of the JSON envelope at `_envelope(job)`'s `"params"` key
(`src/doc3gpp/web/routes/jobs.py:66`). Only the HTML rendering is
missing the section.

After this change, the job detail page (and the htmx-poll fragment
re-rendered every 2s while a job is in flight) displays the enqueued
`params` as a pretty-printed JSON block in a new "Params" card,
matching the existing `tdoc_show.html` rendering style for
JSON-shaped fields.

Out of scope:

- A kind-aware / definition-list rendering of `params`. Plain JSON is
  sufficient and matches what `?format=json` already returns, so users
  who want either form can copy-paste between them.
- A new filter on the jobs list page (e.g. "show only `parse_tdocs`
  jobs with `filter.cr_cat=A`"). The list page is intentionally a flat
  table; per-job detail is the right place to inspect params.
- Editing or re-submitting `params`. The job is a historical record;
  re-enqueueing always creates a new row.
- Showing `result_summary` / `error` / `log_lines` more prominently.
  The existing sections already cover them.

## Background

`GET /jobs/{job_id}?format=html` is the only entry point to the detail
page; it lives in `src/doc3gpp/web/routes/jobs.py:345-360` and renders
`partials/job_status.html`. The same template is also re-rendered
inside the htmx poll (`hx-trigger="every 2s"` at line 21), so any
change to the fragment automatically applies to both the direct visit
and the in-place polling on the list page.

`params` is a `Mapping[str, JSONValue]` (a `dict[str, JSONValue]` in
practice) that the enqueue route fills per `JobKind`:

| `JobKind` | `params` shape (examples) |
| --- | --- |
| `sync_meetings` | `{"tsg": "SA2"}` |
| `sync_tdocs` | `{"meeting_id": 12, "force": false}` or `{"meeting_name": "SA2#156", "force": true}` |
| `sync_tdocs_all` | `{"force": false}` |
| `sync_specs` | `{"tsg": "R5", "force": false, "per_version_details": false}` or `{"spec_id": "36.579-5", "force": false, "per_version_details": false}` |
| `parse_tdocs` | `{"filter": {"tdoc_id": "R5-123456"}, "force": false, "full": false, "max_batch": 50}` |
| `rebuild_search` | `{"stale_only": true, "resume": false}` |
| `cache_purge` | `{"scope": "markdown"}` |
| `parse_tdoc_url` | `{"url": "https://www.3gpp.org/ftp/...", "force": false, "recursive": false, ...}` |

`params` is always a `Mapping` (never `None`); the smallest legitimate
value is `{}` for a hypothetical future kind with no input. The HTML
rendering must handle all of the above uniformly.

Jinja2's built-in `tojson` filter is available on the FastAPI
`Jinja2Templates` instance (`src/doc3gpp/web/templates_setup.py:23`)
and serialises a `Mapping[str, JSONValue]` to JSON. The `indent=2`
argument produces multi-line, human-readable output, identical in
spirit to `json.dumps(value, indent=2)`.

## Approach

Single template edit. No new module, no new helper, no route or model
change.

### 1. Template — `src/doc3gpp/web/templates/partials/job_status.html`

Insert a new "Params" `<section class="card">` between the existing
header `<p>` block (lines 3-7) and the optional error `<p>` (line 9):

```html
<section class="card">
  <h2>Params</h2>
  <pre><code>{{ job.params | tojson(indent=2) }}</code></pre>
</section>
```

The `card` class is the existing convention used by every
`section class="card"` in `tdoc_show.html` (lines 9, 42, 65, 88, 112,
119). It gives the new section a consistent bordered / padded look
without any CSS work.

The empty-params case (e.g. a future `JobKind` with no input) renders
the literal string `{}` inside the `<pre>` — correct, informative, and
no special-case branching required.

Jinja2's `tojson` filter auto-escapes the result, so even if a user
enqueues a `tsg` value containing HTML / quotes / backslashes, the
rendered text is safe (no XSS via the params display). Verified by
Jinja2's documented `tojson` semantics: `markupsafe.Markup`-aware
output that escapes `<`, `>`, `&`, `'`, `"` and is safe to drop inside
`<pre>`.

### 2. Position rationale

Order: status header → Params → error → log_tail → poll sentinel.
`params` is static, set at enqueue and immutable thereafter, so it
sits at the top of the body — readers see the input before the
output. The optional `error` and `log_tail` blocks stay exactly where
they are (lines 8-17); no other line numbers in the template change.

### 3. Polling behaviour

The htmx poll (`every 2s` at line 21) re-renders the entire fragment
each tick. `params` never changes, so the new section is a no-op on
subsequent polls — same DOM, same content. No `hx-target` / `hx-swap`
changes required. The poll sentinel `<span>polling…</span>` (lines
18-27) keeps firing until the job reaches a terminal state, exactly
as before.

### 4. CSS

None. The `card` class is already styled; `<pre><code>` inherits
monospace from the existing stylesheet (used by
`partials/search_results_table.html:36` for the FTS5 snippet). No new
classes, no new rules.

### 5. Error / edge cases

- `params` is `Mapping[str, JSONValue]`, never `None`. The template's
  `{{ job.params | tojson(indent=2) }}` is safe to call for any legal
  `Job.params` value.
- Unicode in params (e.g. a TSG name with non-ASCII) renders as
  `\uXXXX` by default in `tojson` output. `tojson(ensure_ascii=True)`
  is the Jinja2 default. Acceptable for a debug surface; matches the
  CLI's `json.dumps` behaviour.
- A nested filter dict on `parse_tdocs` (e.g.
  `{"filter": {"cr_cat": "A", "tsg": "R5"}}`) renders as a nested
  JSON object — readable, copy-pasteable. No truncation or summary;
  the full value is shown.
- `job_not_found` (404 on `/jobs/{id}`) is unchanged: the route raises
  `JobNotFoundError` before the template is reached.

## Data flow

```
GET /jobs/{job_id}?format=html
    │
    ▼  (FastAPI binds path + query)
_load_job(job_repo, job_id) ──► Job | raise JobNotFoundError
    │
    ▼
templates.TemplateResponse(
    request, name="partials/job_status.html",
    context={"job": job},
)
    │
    ▼  (Jinja2 renders the template)
    job.params (Mapping[str, JSONValue])
        │
        ▼  tojson(indent=2)
    pretty-printed JSON inside <pre><code>
    │
    ▼
HTML response
```

No DB write, no new service, no new dependency. The route's existing
context (`{"job": job}`) is sufficient — `job.params` is just another
attribute on the `Job` model.

## Testing

All tests live in `tests/unit/test_web_jobs_routes.py`, which already
provides a `client` fixture (lines 64-74) backed by an in-memory
sqlite `jobs` table plus a fake `JobWorkerHandle`. Add four tests next
to the existing `test_get_job_returns_detail` (line 331) and
`test_get_job_returns_404_for_unknown` (line 353):

1. **`test_get_job_html_includes_params_section`** — enqueue a
   `sync_meetings` job with `{"tsg": "SA2"}`, `GET /jobs/{id}?format=html`,
   assert the response is 200 and the HTML body contains:
   - `<h2>Params</h2>`
   - `<pre><code>` and `</code></pre>`
   - The literal substring `"tsg"` and `"SA2"` inside the `<pre>`
     block (loose match — `tojson(indent=2)` may add spaces around
     the colon).

2. **`test_get_job_html_params_for_nested_filter`** — enqueue a
   `parse_tdocs` job with
   `{"filter": {"tdoc_id": "R5-123456"}, "force": True, "full": False}`,
   `GET /jobs/{id}?format=html`, assert the response contains the
   nested `filter` block with `tdoc_id` and `R5-123456`. Locks the
   nested-Mapping case the simple `sync_meetings` test can't reach.

3. **`test_get_job_html_empty_params_renders_brace_pair`** — enqueue
   a `sync_tdocs_all` job with `{}` (the smallest legal params for an
   existing kind; covers the empty-params branch). Assert the response
   contains `{}` inside the `<pre>`. Belt-and-suspenders for the "no
   params" branch.

4. **`test_get_job_html_escape_unsafe_value`** — enqueue a job with
   `{"tsg": "<script>alert(1)</script>"}` (synthetic, constructed via
   `repo.create`), assert the response is 200 and the rendered HTML
   does **not** contain a literal `<script>alert(1)</script>` (XSS
   guard). `tojson` auto-escapes; this test pins that behaviour.

These four tests are sufficient — they cover the happy path, the
nested-dict case, the empty/brace case, and the XSS-safety case.
The existing `test_get_job_returns_detail` (line 331) continues to
cover the JSON `?format=json` envelope shape; this change doesn't
touch it.

The `test_get_job_html_*` tests sit next to the existing JSON-shape
tests in the same file, so anyone reading the file sees both the
JSON and the HTML coverage in one place.

## Out of scope (explicit)

- **Kind-aware rendering.** The list of `JobKind`s grows over time
  (8 today, possibly more next quarter). A `<dl>`-per-kind renderer
  is more code, more tests, and a second place to keep in sync with
  the enqueue routes' param shapes. JSON is parameter-agnostic; it
  ages well.
- **Params filter on the jobs list page.** Out of scope — the list
  is intentionally a flat table; users who want to find "all
  `parse_tdocs` jobs with a given filter" can do so via the CLI's
  `server logs --job <id>` / `get_job` flow or by reading
  `?format=json` from a script.
- **Editing / re-submitting params.** A job is a historical record;
  re-running the work is a new POST to `/jobs/...`, not an edit.
- **Showing `result_summary` more prominently.** The detail page
  already renders `summary` in the JSON envelope; the HTML side
  intentionally leaves the result to a separate `summary` / `error`
  read via `?format=json` (so a UI doesn't accidentally treat
  result-shaped data as a "current status"). No change to the
  JSON envelope's structure.

## Documentation sync

Per `docs/conventions.md` §"Documentation sync", this change is a
web-only template tweak — no CLI surface, no MCP surface, no API
contract, no env-var / TOML knob. The rule's bulk is N/A.

One small doc update is still warranted for discoverability:

- **`docs/web-server.md`** — at line 226-231 the "TDoc detail page"
  section mentions the inline polling partial but not the
  per-section card layout. Add one sentence right after the existing
  "the job status partial polls inline until the job finishes"
  sentence (line 230): *"The job detail page renders each section
  (Params, optional error, log tail) as a `card`, with the htmx
  poll sentinel at the bottom while the job is in flight."* This
  doesn't change behaviour, but it does point future readers at the
  template's structure.

No other docs change. `AGENTS.md` already covers the "Add a
background job kind" workflow; this change doesn't introduce one.
`docs/cli.md` is CLI-only and untouched. `docs/architecture.md`
doesn't enumerate every HTML section. `docs/code-map.md` describes
`partials/job_status.html` at the file level; this change is
inside the file, not a new file.
