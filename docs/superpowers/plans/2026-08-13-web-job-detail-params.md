# Web Job Detail Page — Show Params — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render the enqueued `params` JSON on the web job detail page (`GET /jobs/{job_id}?format=html`) so users can see what they actually submitted when they triggered a background job.

**Architecture:** Single template edit to `partials/job_status.html` that drops a new `<section class="card">Params</section>` between the existing status header and the optional error block. The section uses Jinja2's built-in `tojson(indent=2)` filter to pretty-print the `Job.params` mapping. No route, model, service, or storage change. Four unit tests in `tests/unit/test_web_jobs_routes.py` lock the rendered output for happy / nested / empty / XSS-unsafe cases.

**Tech Stack:** Python 3.10+, FastAPI, Jinja2 (built-in `tojson` filter), existing in-memory sqlite `JobRepository` test fixture, pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-13-web-job-detail-params-design.md` (Approved 2026-08-13).
- File: `src/doc3gpp/web/templates/partials/job_status.html` is the only template touched.
- File: `tests/unit/test_web_jobs_routes.py` is the only test file touched.
- Doc: `docs/web-server.md` gets a single-sentence addition at line 230 (after "the job status partial polls inline until the job finishes").
- No new dependencies.
- No CLI / MCP / API contract change.
- All commits are small and scoped to a single file or two.
- Branch: `main` (this is a follow-up to the recently merged `idempotent-cancel` PR #88; no separate feature branch is required for a single-template + 4-tests change).
- Project test command (offline): `./scripts/test_sqlite.sh`.
- Project lint command: `ruff check .`.
- Style: `code style: IMPORTANT: DO NOT ADD ***ANY*** COMMENTS unless asked` — template and test changes do not introduce comments.

---

## File Structure

| File | Role | Change |
| --- | --- | --- |
| `src/doc3gpp/web/templates/partials/job_status.html` | Job detail / htmx-poll fragment | Add a `<section class="card">Params</section>` between the header `<p>` (lines 3-7) and the optional error `<p>` (line 9). |
| `tests/unit/test_web_jobs_routes.py` | Unit tests for the job HTTP routes | Add four `test_get_job_html_*` tests next to the existing `test_get_job_returns_detail` (line 331). |
| `docs/web-server.md` | End-user guide for the web server | Add one sentence about the per-section card layout after the "polls inline" sentence (line 230). |

No other files change. No new files in `src/`.

---

## Task 1: Add the Params section to the template

**Files:**
- Modify: `src/doc3gpp/web/templates/partials/job_status.html` (insert between the closing `</p>` of the header at line 7 and the `{% if job.error %}` block at line 8)

**Interfaces:**
- Consumes: `job.params` — a `Mapping[str, JSONValue]` per `src/doc3gpp/models/jobs.py:106` (always present, never `None`).
- Produces: a new `<section class="card">` with `<h2>Params</h2>` and `<pre><code>{{ job.params | tojson(indent=2) }}</code></pre>`. The `tojson` filter is built-in to Jinja2 (verified available on the FastAPI `Jinja2Templates` instance at `src/doc3gpp/web/templates_setup.py:23`).

- [ ] **Step 1: Open the template and confirm the current line numbers**

Read `src/doc3gpp/web/templates/partials/job_status.html` and confirm:
- Line 7 ends with `</p>` (the header `<p>`).
- Line 8 starts with `{% if job.error %}`.

If the line numbers have drifted, locate the boundary by content rather than number.

- [ ] **Step 2: Insert the new Params section**

Insert these four lines between the header `</p>` and the `{% if job.error %}` line:

```html
    <section class="card">
      <h2>Params</h2>
      <pre><code>{{ job.params | tojson(indent=2) }}</code></pre>
    </section>
```

The leading 4 spaces match the existing indentation in the template (header `<p>` is indented with 4 spaces). The `<section class="card">` matches the convention used by `src/doc3gpp/web/templates/tdoc_show.html` (lines 9, 42, 65, 88, 112, 119). The `<pre><code>` matches the convention used by `src/doc3gpp/web/templates/partials/search_results_table.html:36`.

- [ ] **Step 3: Re-read the file to verify the structure**

The file should now have these sections in order:
1. `<div class="job-status" id="job-{{ job.id }}">`
2. Header `<p>` (Job ID, status, kind)
3. **NEW: `<section class="card">Params</section>`**
4. `{% if job.error %}` block
5. `{% if job.log_lines %}` block
6. `{% if job.status.value not in ('succeeded', 'failed', 'cancelled') %}` htmx poll sentinel
7. Closing `</div>`
8. `{% else %}` / `<p class="error">Job not found.</p>` / `{% endif %}`

- [ ] **Step 4: Commit**

```bash
git add src/doc3gpp/web/templates/partials/job_status.html
git commit -m "feat(web): render params on job detail page"
```

---

## Task 2: Add the happy-path HTML test

**Files:**
- Modify: `tests/unit/test_web_jobs_routes.py` (insert immediately after `test_get_job_returns_404_for_unknown` which ends at line 357)

**Interfaces:**
- Consumes: the existing `client` fixture (line 64-74) which yields `(TestClient, SQLAlchemyJobRepository, _FakeJobWorkerHandle)`.
- Produces: `test_get_job_html_includes_params_section` — calls `GET /jobs/{id}?format=html`, asserts the response is 200, asserts the rendered HTML contains `<h2>Params</h2>`, `<pre><code>`, and the param keys/values inside the `<pre>` block.

- [ ] **Step 1: Write the failing test**

Append the following function to `tests/unit/test_web_jobs_routes.py`, immediately after `test_get_job_returns_404_for_unknown` (the function whose body ends with `assert r.json()["error"] == "job_not_found"`):

```python
def test_get_job_html_includes_params_section(client: Any) -> None:
    """The ?format=html detail page renders a 'Params' section with the params dict.

    Locks the happy path: the new <section class="card">Params</section> is present,
    wraps the params inside a <pre><code> block, and includes the supplied keys
    and values verbatim.
    """
    c, repo, _ = client
    job = repo.create(JobKind.SYNC_MEETINGS, {"tsg": "SA2"})
    r = c.get(f"/jobs/{job.id}?format=html")
    assert r.status_code == 200
    body = r.text
    assert "<h2>Params</h2>" in body
    assert "<pre><code>" in body
    assert "</code></pre>" in body
    pre_start = body.index("<pre><code>")
    pre_end = body.index("</code></pre>")
    pre_block = body[pre_start:pre_end]
    assert '"tsg"' in pre_block
    assert '"SA2"' in pre_block
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `pytest tests/unit/test_web_jobs_routes.py::test_get_job_html_includes_params_section -v`
Expected: FAIL — the assertion `assert "<h2>Params</h2>" in body` fails because Task 1 already landed the section, so the test will **actually pass**. (This is fine: Task 1 is the production change, Task 2 is the test that locks it. Run the test to confirm green; if it fails, re-check Task 1.)

Expected outcome: PASS. If FAIL, re-verify Task 1's template edit landed correctly (4-space indent, no extra whitespace drift).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_web_jobs_routes.py
git commit -m "test(web): cover job detail params section (happy path)"
```

---

## Task 3: Add the nested-filter test

**Files:**
- Modify: `tests/unit/test_web_jobs_routes.py` (insert immediately after the test from Task 2)

**Interfaces:**
- Consumes: the existing `client` fixture.
- Produces: `test_get_job_html_params_for_nested_filter` — enqueues a `parse_tdocs` job with a nested `filter` dict, asserts the nested dict renders.

- [ ] **Step 1: Write the test**

Append:

```python
def test_get_job_html_params_for_nested_filter(client: Any) -> None:
    """A nested params dict (PARSE_TDOCS' filter) renders as nested JSON.

    Locks the case the happy-path test cannot reach: a Mapping whose value is
    itself a Mapping. The outer 'filter' key and its inner 'tdoc_id' key +
    value both surface inside the <pre> block.
    """
    c, repo, _ = client
    job = repo.create(
        JobKind.PARSE_TDOCS,
        {
            "filter": {"tdoc_id": "R5-123456"},
            "force": True,
            "full": False,
        },
    )
    r = c.get(f"/jobs/{job.id}?format=html")
    assert r.status_code == 200
    body = r.text
    pre_start = body.index("<pre><code>")
    pre_end = body.index("</code></pre>")
    pre_block = body[pre_start:pre_end]
    assert '"filter"' in pre_block
    assert '"tdoc_id"' in pre_block
    assert "R5-123456" in pre_block
    assert '"force"' in pre_block
    assert "true" in pre_block  # JSON boolean
    assert '"full"' in pre_block
    assert "false" in pre_block
```

- [ ] **Step 2: Run the test and confirm it passes**

Run: `pytest tests/unit/test_web_jobs_routes.py::test_get_job_html_params_for_nested_filter -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_web_jobs_routes.py
git commit -m "test(web): cover nested filter dict in job detail params"
```

---

## Task 4: Add the empty-params and XSS-safety tests

**Files:**
- Modify: `tests/unit/test_web_jobs_routes.py` (insert immediately after the test from Task 3)

**Interfaces:**
- Consumes: the existing `client` fixture.
- Produces: two tests — `test_get_job_html_empty_params_renders_brace_pair` (the smallest legal params renders as `{}`) and `test_get_job_html_escape_unsafe_value` (HTML in a param value is escaped, no literal `<script>` tag in the response).

- [ ] **Step 1: Write the empty-params test**

Append:

```python
def test_get_job_html_empty_params_renders_brace_pair(client: Any) -> None:
    """The smallest legal params renders as '{}' (no special-case branching)."""
    c, repo, _ = client
    job = repo.create(JobKind.SYNC_TDOCS_ALL, {})
    r = c.get(f"/jobs/{job.id}?format=html")
    assert r.status_code == 200
    body = r.text
    pre_start = body.index("<pre><code>")
    pre_end = body.index("</code></pre>")
    pre_block = body[pre_start:pre_end]
    assert "{}" in pre_block
```

- [ ] **Step 2: Write the XSS-safety test**

Append:

```python
def test_get_job_html_escape_unsafe_value(client: Any) -> None:
    """HTML in a param value is escaped by tojson (no literal <script> in the body)."""
    c, repo, _ = client
    job = repo.create(
        JobKind.SYNC_MEETINGS,
        {"tsg": "<script>alert(1)</script>"},
    )
    r = c.get(f"/jobs/{job.id}?format=html")
    assert r.status_code == 200
    body = r.text
    assert "<script>alert(1)</script>" not in body
```

- [ ] **Step 3: Run both tests and confirm they pass**

Run: `pytest tests/unit/test_web_jobs_routes.py::test_get_job_html_empty_params_renders_brace_pair tests/unit/test_web_jobs_routes.py::test_get_job_html_escape_unsafe_value -v`
Expected: PASS for both.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_web_jobs_routes.py
git commit -m "test(web): cover empty params and XSS escape in job detail"
```

---

## Task 5: Run the full test suite and lint

**Files:** none (read-only verification)

**Interfaces:** none.

- [ ] **Step 1: Run the full sqlite test suite**

Run: `./scripts/test_sqlite.sh`
Expected: all unit + integration tests pass on sqlite. The four new tests are part of the `test_web_jobs_routes.py` module that is collected by default; they should appear in the output as four green tests.

- [ ] **Step 2: Run lint**

Run: `ruff check .`
Expected: no findings. The template change adds 4 lines of HTML (no Python); the test additions are pure-Python and follow the file's existing style (4-space indent, no comments).

- [ ] **Step 3: Confirm no regressions**

If either step fails, fix and re-run before proceeding to Task 6. Do not commit a fix without a passing run.

---

## Task 6: Update the web-server docs

**Files:**
- Modify: `docs/web-server.md` (insert one sentence after line 230, after "the job status partial polls inline until the job finishes")

**Interfaces:** none.

- [ ] **Step 1: Locate the insertion point**

Open `docs/web-server.md` and find the paragraph that ends with "the job status partial polls inline until the job finishes, then the page" (around line 230, inside the TDoc detail page section). The next line is the empty line / closing of that thought.

- [ ] **Step 2: Add one sentence**

Append the following sentence on a new line, immediately after the "polls inline until the job finishes" sentence and before the paragraph break:

```
The job detail page renders each section (Params, optional error, log tail) as a `card`, with the htmx poll sentinel at the bottom while the job is in flight.
```

The sentence uses the same backtick + plain-prose style as the surrounding prose in `docs/web-server.md` (e.g. line 230's "the job status partial polls inline").

- [ ] **Step 3: Re-read the modified section to confirm the prose flows**

Confirm the new sentence does not duplicate any existing sentence and reads naturally next to the preceding "polls inline" sentence. The new sentence should be one line in the markdown source (no line-wrap needed at typical line widths; if your editor wraps at 100 cols, that's fine — the source-level line break is irrelevant).

- [ ] **Step 4: Commit**

```bash
git add docs/web-server.md
git commit -m "docs(web): note per-section card layout on job detail page"
```

---

## Task 7: Final verification

**Files:** none (read-only verification)

**Interfaces:** none.

- [ ] **Step 1: Re-run the full sqlite test suite**

Run: `./scripts/test_sqlite.sh`
Expected: all tests pass (including the four new ones).

- [ ] **Step 2: Re-run lint**

Run: `ruff check .`
Expected: no findings.

- [ ] **Step 3: Inspect the commit log**

Run: `git log --oneline -8`
Expected: a clean linear sequence with the four new commits (Tasks 1, 2, 3, 4, 6) plus the pre-existing merges, no merge commits introduced by this work.

- [ ] **Step 4: Inspect the diff against the spec baseline**

Run: `git diff 14c4625..HEAD -- src/ tests/ docs/web-server.md`
Expected:
- `src/doc3gpp/web/templates/partials/job_status.html`: a single 4-line insertion.
- `tests/unit/test_web_jobs_routes.py`: four new test functions appended; no existing test modified.
- `docs/web-server.md`: one new sentence appended.
- No other file changed by this plan.

- [ ] **Step 5: Manually verify the rendered HTML (optional but recommended)**

Start the server (`doc3gpp server start` or run `uvicorn doc3gpp.web.app:app` in a dev shell) and visit `http://localhost:8765/jobs/<some-job-id>?format=html` in a browser. Confirm the "Params" card renders below the header with pretty-printed JSON.

---

## Self-Review

**1. Spec coverage:**

| Spec section | Plan task |
| --- | --- |
| §"Template — `partials/job_status.html`" insertion | Task 1 |
| §"Position rationale" | Task 1 (insertion point is between header `</p>` and `{% if job.error %}`) |
| §"Polling behaviour" | No code change; the htmx poll sentinel at lines 18-27 of the template is unchanged. Implicitly verified by the existing `test_nav_badge_on_jobs_list_page_counts_running` flow. |
| §"CSS — None" | No CSS task needed. |
| §"Error / edge cases" (params is `Mapping`, never `None`; `tojson` is safe for nested values) | Task 3 (nested) and Task 4 (empty + XSS). |
| §"Testing" — four tests | Tasks 2, 3, 4. |
| §"Documentation sync" — one sentence in `docs/web-server.md` | Task 6. |
| §"Out of scope" items | Not implemented; the spec explicitly excludes them. |

**2. Placeholder scan:** No TBD / TODO / "implement later" in the plan. Every code block is concrete and runnable.

**3. Type / signature consistency:**
- `client` fixture signature is `(TestClient, SQLAlchemyJobRepository, _FakeJobWorkerHandle)` everywhere it's used.
- `repo.create(JobKind, params_dict)` signature matches the existing usage in `test_get_job_returns_detail` (line 333) and the four new tests.
- `r.get(f"/jobs/{job.id}?format=html")` is consistent across all four new tests.
- `body.index("<pre><code>")` and `body.index("</code></pre>")` are the same slice technique used in all four new tests.
- `pre_block` variable name is consistent.

No issues found.
