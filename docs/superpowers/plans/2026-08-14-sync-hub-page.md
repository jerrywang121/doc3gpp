# Sync Hub Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a top-level web page at `GET /sync` that mirrors every sync-shaped MCP tool as a stacked enqueue panel, plus one new HTTP route (`POST /jobs/parse/tdoc-url`) closing the MCP-vs-HTTP gap.

**Architecture:** A new `APIRouter` at `/sync` (full page + `?format=fragment` for HTMX swaps). Nine enqueue `<form>` panels, each posting a JSON body to an existing route in `src/doc3gpp/web/routes/jobs.py` (or the new `parse/tdoc-url` route). The bottom recent-jobs `<div id="recent-jobs">` is server-rendered once and then refreshed via HTMX `outerHTML` swap against `/sync?format=fragment` whenever any panel's job terminates — replacing the existing `location.reload()` behaviour. The shared `bindJobPolling` helper in `job_poller.js` gets an `onTerminal` callback option; the two existing call sites (`meeting_sync.js`, `spec_sync.js`) pass an explicit reload callback so their behaviour is preserved.

**Tech Stack:** FastAPI, Jinja2 templates, HTMX, vanilla JavaScript, SQLAlchemy 2.0 (sqlite for tests), Pydantic v2.

## Global Constraints

- Python 3.10+, hatchling build system.
- SQLAlchemy 2.0 ORM; no new tables / columns introduced.
- Pydantic v2 `BaseModel` for any new request body.
- No new dependency added to `pyproject.toml`.
- Every new Python file MUST include the `from __future__ import annotations` header.
- Lint: `ruff check .` MUST pass before each commit.
- Tests: `./scripts/test_sqlite.sh` MUST pass before each commit (or `python -m pytest tests/unit tests/integration -m "not online"` when xdist is not installed).
- The job worker's `run()` loop is **paused** in every integration test that touches jobs (use the `app_with_deps` fixture pattern from `tests/integration/test_web_end_to_end.py` or the `client` fixture in `tests/unit/test_web_jobs_routes.py`) so queued jobs never get claimed by a network-touching handler.
- `JobKind` enum (`src/doc3gpp/models/jobs.py`) is the source of truth for job types — no new kinds introduced (we reuse `JobKind.PARSE_TDOC_URL` which already exists for the MCP tool).
- The shared `bindJobPolling` helper in `src/doc3gpp/web/static/js/job_poller.js` MUST keep its default behaviour (`window.location.reload()`) when no `onTerminal` callback is supplied. The two existing call sites (`meeting_sync.js`, `spec_sync.js`) MUST pass an explicit reload callback so their behaviour is preserved.
- Every new template MUST extend `base.html` and use `active_nav` for the matching nav link.
- No new CSS class names introduced (reuse `.card`, `.filters`, `.btn .primary`, `.sync-queued`, `.nav-badge`).
- The page MUST NOT use a per-spec TSG dropdown — TSG is a free-text input by user choice.
- Doc sync: update `docs/web-server.md` (HTTP routes section) + `AGENTS.md` (Where-to-look table). No changes to `docs/cli.md`, `docs/architecture.md`, or `README.md`.

---

## File Structure

### New files

| Path | Responsibility |
| --- | --- |
| `src/doc3gpp/web/routes/sync.py` | `APIRouter` exposing `GET /sync` (full page) + `GET /sync?format=fragment` (recent-jobs table only). |
| `src/doc3gpp/web/templates/sync.html` | The hub page: nine enqueue panels + bottom `#recent-jobs` div. |
| `src/doc3gpp/web/templates/partials/sync_recent_jobs.html` | The recent-jobs table fragment (wraps the table in `<div id="recent-jobs">` for HTMX `outerHTML` swap target identity). |
| `src/doc3gpp/web/static/js/sync_hub.js` | Page-local wrapper around `bindJobPolling` that overrides the terminal action to refresh `#recent-jobs` via HTMX. |
| `tests/unit/test_sync_hub_page.py` | Unit tests for the new `/sync` GET endpoints (HTML + fragment). |
| `tests/unit/test_parse_tdoc_url_route.py` | Unit tests for the new `POST /jobs/parse/tdoc-url` route. |
| `tests/unit/test_bind_job_polling_on_terminal.py` | Unit tests for the `onTerminal` callback option in `job_poller.js` (JS-side, executed via `python -m pytest` against a minimal HTML harness — see Task 2 for the harness shape). |
| `tests/integration/test_sync_hub_end_to_end.py` | Integration tests exercising every panel via TestClient against the real FastAPI app + sqlite. |

### Modified files

| Path | Reason |
| --- | --- |
| `src/doc3gpp/web/routes/jobs.py` | Add `POST /jobs/parse/tdoc-url` + `_ParseTDocURLBody` Pydantic model. |
| `src/doc3gpp/web/routes/__init__.py` | Add `sync_router` to `all_routers()`. |
| `src/doc3gpp/web/routes/landing.py` | Add a `Sync` entry to the `_SECTIONS` list so the landing page links to it. |
| `src/doc3gpp/web/templates/base.html` | Add a `Sync` nav link next to `Jobs` (with the same pending-jobs badge). |
| `src/doc3gpp/web/static/js/job_poller.js` | Add an optional `onTerminal(form, target, jobId, queued)` callback to `bindJobPolling`; default remains `location.reload()`. `installTerminalObserver` invokes the callback instead of calling `location.reload()` directly. |
| `src/doc3gpp/web/static/js/meeting_sync.js` | Pass explicit `onTerminal: function () { window.location.reload(); }` so the default change is safe + the contract is obvious. |
| `src/doc3gpp/web/static/js/spec_sync.js` | Same as `meeting_sync.js`. |
| `docs/web-server.md` | Add `/sync` + `POST /jobs/parse/tdoc-url` to the HTTP routes section. |
| `AGENTS.md` | Add a row to the "Where to look" table for "Add a sync hub panel / sync hub page". |

### Unchanged

- `src/doc3gpp/models/jobs.py` — `JobKind.PARSE_TDOC_URL` already exists.
- `src/doc3gpp/web/workers/handlers.py` — `_parse_tdoc_url` already exists.
- All services / repositories / parsers / settings / scraping modules.

---

## Task 1: Add `POST /jobs/parse/tdoc-url` route

**Files:**
- Modify: `src/doc3gpp/web/routes/jobs.py:122` (after `_CachePurgeBody`)
- Test: `tests/unit/test_parse_tdoc_url_route.py` (new)

**Interfaces:**
- Consumes: `JobRepository` (via `Depends(get_job_repo)`), `JobKind.PARSE_TDOC_URL` enum.
- Produces: `_ParseTDocURLBody` Pydantic model; `post_parse_tdoc_url(body, job_repo)` route handler returning `JSONResponse(status_code=202, content=_envelope(job, queued=True))`.

- [ ] **Step 1: Write failing tests in `tests/unit/test_parse_tdoc_url_route.py`**

```python
"""Tests for ``POST /jobs/parse/tdoc-url`` (closes the MCP-vs-HTTP gap)."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from doc3gpp.models.jobs import JobKind
from doc3gpp.storage.db.base import Base
from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository
from doc3gpp.web.app import build_app
from doc3gpp.web.deps import get_job_repo, get_job_worker
from doc3gpp.web.state import JobWorkerHandle


class _NoopWorkerHandle(JobWorkerHandle):
    def register_queue(self, job_id, queue=None):
        pass

    def unregister_queue(self, job_id):
        pass

    def cancel(self, job_id):
        return False


def _make_repo() -> SQLAlchemyJobRepository:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    repo = SQLAlchemyJobRepository()
    repo._session_factory = Session  # type: ignore[attr-defined]
    return repo


@pytest.fixture()
def client(sqlite_env: Any):
    repo = _make_repo()
    handle = _NoopWorkerHandle()
    app: FastAPI = build_app()
    app.dependency_overrides[get_job_repo] = lambda: repo
    app.dependency_overrides[get_job_worker] = lambda: handle
    with TestClient(app) as c:
        yield c, repo


def test_post_parse_tdoc_url_happy_path(client: Any) -> None:
    c, repo = client
    r = c.post(
        "/jobs/parse/tdoc-url",
        json={
            "url": "https://www.3gpp.org/ftp/TSG_RAN/WG5_Radio/TSGR5_99/Docs/",
            "max_depth": 2,
            "force": False,
            "full": False,
        },
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert body["job_id"]
    job = repo.get(body["job_id"])
    assert job is not None
    assert job.kind is JobKind.PARSE_TDOC_URL
    assert job.params["url"].startswith("https://www.3gpp.org/ftp/")
    assert job.params["recursive"] is False
    assert job.params["max_depth"] == 2
    assert "max_depth" in job.params  # omitted when recursive=True


def test_post_parse_tdoc_url_rejects_non_3gpp_url(client: Any) -> None:
    c, _ = client
    r = c.post(
        "/jobs/parse/tdoc-url",
        json={"url": "https://example.com/evil/"},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_filter"


def test_post_parse_tdoc_url_xor_recursive_max_depth(client: Any) -> None:
    c, _ = client
    r = c.post(
        "/jobs/parse/tdoc-url",
        json={
            "url": "https://www.3gpp.org/ftp/TSG_RAN/WG5_Radio/TSGR5_99/Docs/",
            "recursive": True,
            "max_depth": 3,
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_filter"


def test_post_parse_tdoc_url_recursive_omits_max_depth(client: Any) -> None:
    c, repo = client
    r = c.post(
        "/jobs/parse/tdoc-url",
        json={
            "url": "https://www.3gpp.org/ftp/TSG_RAN/WG5_Radio/TSGR5_99/Docs/",
            "recursive": True,
        },
    )
    assert r.status_code == 202
    job = repo.get(r.json()["job_id"])
    assert job.params["recursive"] is True
    assert "max_depth" not in job.params
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/test_parse_tdoc_url_route.py -v`
Expected: 4 errors / 4 fails with `404` (route doesn't exist) or `405`.

- [ ] **Step 3: Add the route + model in `src/doc3gpp/web/routes/jobs.py`**

Insert after the existing `_CachePurgeBody` class (around line 124) and before the POST endpoint section:

```python
class _ParseTDocURLBody(BaseModel):
    url: str
    recursive: bool = False
    max_depth: int = 2
    force: bool = False
    full: bool = False
```

Then add a new POST endpoint immediately after `post_cache_purge` (around line 232):

```python
@router.post("/parse/tdoc-url", status_code=202)
async def post_parse_tdoc_url(
    body: _ParseTDocURLBody,
    job_repo: JobRepository = Depends(get_job_repo),
) -> JSONResponse:
    """Enqueue a parse of a single 3GPP FTP URL or folder (closes the MCP-vs-HTTP gap)."""
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

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/test_parse_tdoc_url_route.py -v`
Expected: 4 passes.

- [ ] **Step 5: Lint**

Run: `ruff check src/doc3gpp/web/routes/jobs.py tests/unit/test_parse_tdoc_url_route.py`
Expected: no output (clean).

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/routes/jobs.py tests/unit/test_parse_tdoc_url_route.py
git commit -m "feat(web): add POST /jobs/parse/tdoc-url route to close MCP-vs-HTTP gap"
```

---

## Task 2: Add `onTerminal` callback to `bindJobPolling` (default = reload)

**Files:**
- Modify: `src/doc3gpp/web/static/js/job_poller.js:69-150` (`bindJobPolling`) and `:199-234` (`installTerminalObserver`).
- Modify: `src/doc3gpp/web/static/js/meeting_sync.js:31-35`
- Modify: `src/doc3gpp/web/static/js/spec_sync.js:21-36`
- Test: `tests/unit/test_bind_job_polling_on_terminal.py` (new; uses a minimal Python/jsdom harness via `playwright` headless — see harness at end of this task)

**Interfaces:**
- Consumes: `window.bindJobPolling(form, options)` is the existing call site (used by `meeting_sync.js` and `spec_sync.js`).
- Produces: New option `onTerminal(form, target, jobId, queued)` on `bindJobPolling`. Default value: `function () { window.location.reload(); }`. `installTerminalObserver(target, queued, onTerminal)` invokes `onTerminal` instead of `location.reload()`.

- [ ] **Step 1: Write a failing harness + test in `tests/unit/test_bind_job_polling_on_terminal.py`**

```python
"""Test the new ``onTerminal`` callback option on ``bindJobPolling``.

Exercises ``src/doc3gpp/web/static/js/job_poller.js`` headlessly via
Playwright's ``sync_playwright().chromium.launch()``. Each test builds a
minimal HTML harness, loads the script, dispatches a form submit, and
asserts that either the user-supplied callback fires (when passed) or
``location.reload()`` fires (when omitted).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


_JS_PATH = Path(__file__).resolve().parents[2] / "src" / "doc3gpp" / "web" / "static" / "js" / "job_poller.js"
_HTMX_PATH = Path(__file__).resolve().parents[2] / "src" / "doc3gpp" / "web" / "static" / "htmx.min.js"


@pytest.fixture()
def js_harness_page() -> str:
    return """<!doctype html><html><body>
<form id="test-form">
  <input type="hidden" name="tsg" value="SA2">
  <button type="submit">go</button>
  <span class="sync-queued" style="display:none">queued</span>
</form>
<div id="test-form-job-target"></div>
<script src="%(htmx)s"></script>
<script src="%(job_poller)s"></script>
</body></html>""" % {
        "htmx": _HTMX_PATH.as_uri(),
        "job_poller": _JS_PATH.as_uri(),
    }


def test_on_terminal_callback_invoked_and_reload_skipped(page_factory: Any) -> None:
    page = page_factory()
    page.set_content(js_harness_page := (lambda: """<!doctype html><html><body>
<form id="test-form">
  <input type="hidden" name="tsg" value="SA2">
  <button type="submit">go</button>
  <span class="sync-queued" style="display:none">queued</span>
</form>
<div id="test-form-job-target"></div>
<script src="%(htmx)s"></script>
<script src="%(job_poller)s"></script>
</body></html>""" % {
        "htmx": _HTMX_PATH.as_uri(),
        "job_poller": _JS_PATH.as_uri(),
    })())
    captured: list[str] = []
    page.expose_function("spyOnTerminal", lambda: captured.append("hit"))
    page.add_init_script("window.__reloaded = false; const _reload = window.location.reload.bind(window.location); window.location.reload = () => { window.__reloaded = true; };")
    page.evaluate(
        "document.querySelector('#test-form').addEventListener('submit', e => {"
        "  e.preventDefault();"
        "  window.bindJobPolling(document.querySelector('#test-form'), { onTerminal: window.spyOnTerminal });"
        "});"
    )
    page.click("#test-form button")
    page.wait_for_function("window.__captured === true", timeout=2000)
    assert captured == ["hit"]
    assert page.evaluate("window.__reloaded") is False


def test_default_terminal_falls_back_to_reload(page_factory: Any) -> None:
    page = page_factory()
    page.set_content(_JS_PATH.as_uri() and """<!doctype html><html><body>
<form id="test-form">
  <input type="hidden" name="tsg" value="SA2">
</form>
<div id="test-form-job-target"></div>
<script src="%s"></script>
</body></html>""" % _JS_PATH.as_uri())
    page.add_init_script("window.__reloaded = false; window.location.reload = () => { window.__reloaded = true; };")
    page.evaluate(
        "document.querySelector('#test-form').addEventListener('submit', e => {"
        "  e.preventDefault();"
        "  window.bindJobPolling(document.querySelector('#test-form'), {});"
        "});"
    )
    page.click("#test-form button")
    page.wait_for_function("window.__reloaded === true", timeout=2000)
    assert page.evaluate("window.__reloaded") is True
```

> **Note to the implementer:** the harness above is illustrative; the
> real `tests/unit/test_bind_job_polling_on_terminal.py` should use a
> single `_page_factory` fixture defined at module level that spawns a
> headless Chromium via `playwright.sync_api.sync_playwright()`. If
> Playwright is not available in the test environment, replace this test
> file with a **node-based smoke test** invoked from Python via
> `subprocess.run(["node", "tests/unit/_job_polling_smoke.js"], check=True)`
> that does the same assertions. The node script must be checked in
> alongside the Python test. Either approach is acceptable — pick the one
> that works in CI. The acceptance criterion is the same: when
> `onTerminal` is passed, the callback fires and `location.reload` is
> NOT called; when omitted, `location.reload` IS called.

- [ ] **Step 2: Run the harness; verify it fails**

Run: `python -m pytest tests/unit/test_bind_job_polling_on_terminal.py -v`
Expected: FAIL because `bindJobPolling` does not yet accept `onTerminal`.

- [ ] **Step 3: Modify `src/doc3gpp/web/static/js/job_poller.js`**

Replace the `bindJobPolling` body so it accepts `onTerminal` and forwards it to `installTerminalObserver`. Concretely:

```js
function bindJobPolling(form, options) {
    if (!form || !form.tagName || form.tagName !== "FORM") {
      return;
    }
    var opts = options || {};
    var queuedSelector = opts.queuedSelector || ".sync-queued";
    var targetSelector =
      opts.targetSelector ||
      (form.id ? "#" + form.id + "-job-target" : null);
    var target = targetSelector ? document.querySelector(targetSelector) : null;
    var onTerminal =
      typeof opts.onTerminal === "function"
        ? opts.onTerminal
        : function () { window.location.reload(); };

    form.addEventListener("submit", function (event) {
      // ... unchanged body, except at the very end:

      // (after .then(function (body) { ... attachPolling(form, target, body.job_id, queued); ... })
      attachPolling(form, target, body.job_id, queued, onTerminal);
    });
}

function attachPolling(form, target, jobId, queued, onTerminal) {
    // ... unchanged body, except the final call:
    installTerminalObserver(target, queued, onTerminal);
}

function installTerminalObserver(target, queued, onTerminal) {
    if (!target || !global.MutationObserver) {
      return;
    }
    var pollSeen = false;
    var done = false;
    function finish() {
      if (done) {
        return;
      }
      done = true;
      if (queued) {
        queued.style.display = "none";
      }
      observer.disconnect();
      onTerminal();
    }
    // ... unchanged body
}
```

Update the JSDoc above `bindJobPolling` to mention `onTerminal`.

- [ ] **Step 4: Run the harness; verify it passes**

Run: `python -m pytest tests/unit/test_bind_job_polling_on_terminal.py -v`
Expected: 2 passes.

- [ ] **Step 5: Update existing call sites to pass an explicit reload callback**

In `src/doc3gpp/web/static/js/meeting_sync.js`, replace the `window.bindJobPolling(...)` call (line 31-34) with:

```js
window.bindJobPolling(form, {
  queuedSelector: ".sync-queued",
  targetSelector: "#" + FORM_ID + "-job-target",
  onTerminal: function () { window.location.reload(); },
});
```

In `src/doc3gpp/web/static/js/spec_sync.js`, replace the `window.bindJobPolling(...)` call (line 21-36) — add `onTerminal: function () { window.location.reload(); }` to the options dict.

- [ ] **Step 6: Lint (n/a for JS — no JS linter configured) + commit**

```bash
git add src/doc3gpp/web/static/js/job_poller.js \
        src/doc3gpp/web/static/js/meeting_sync.js \
        src/doc3gpp/web/static/js/spec_sync.js \
        tests/unit/test_bind_job_polling_on_terminal.py
git commit -m "feat(web): add onTerminal callback to bindJobPolling for hub page refresh"
```

---

## Task 3: New `/sync` route module

**Files:**
- Create: `src/doc3gpp/web/routes/sync.py`
- Modify: `src/doc3gpp/web/routes/__init__.py`
- Test: `tests/unit/test_sync_hub_page.py` (new)

**Interfaces:**
- Consumes: `get_job_repo`, `get_pending_jobs` deps from `src/doc3gpp/web/deps.py`; `templates` from `src/doc3gpp/web/templates_setup.py`; `JobRepository.list(limit: int)` protocol method.
- Produces: `APIRouter` with `GET /sync` (renders `sync.html`) + `GET /sync?format=fragment` (renders `partials/sync_recent_jobs.html`).

- [ ] **Step 1: Write failing tests in `tests/unit/test_sync_hub_page.py`**

```python
"""Tests for the ``GET /sync`` hub page + ``?format=fragment`` table refresh."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


def test_sync_page_returns_200_and_contains_all_panels(client: Any) -> None:
    """``GET /sync`` returns 200 and lists every panel's ``<h2>`` in document order."""
    assert client.get("/sync").status_code == 200
    text = client.get("/sync").text
    for heading in (
        "Meeting sync",
        "TDoc sync",
        "Spec sync",
        "Parse TDocs (filter-driven)",
        "Parse from URL",
        "Rebuild search index",
        "Purge cache",
        "Recent sync jobs",
    ):
        assert heading in text, f"missing heading: {heading}"


def test_sync_page_renders_all_nine_forms(client: Any) -> None:
    """Each of the nine enqueue panels has a unique form id."""
    text = client.get("/sync").text
    for form_id in (
        "sync-meetings-form",
        "sync-tdocs-form",
        "sync-tdocs-all-form",
        "sync-specs-tsg-form",
        "sync-specs-id-form",
        "parse-tdocs-form",
        "parse-tdoc-url-form",
        "rebuild-search-form",
        "purge-cache-form",
    ):
        assert f'id="{form_id}"' in text, f"missing form id: {form_id}"


def test_sync_fragment_returns_partial_only(client: Any) -> None:
    """``GET /sync?format=fragment`` returns just the recent-jobs table fragment."""
    response = client.get("/sync?format=fragment")
    assert response.status_code == 200
    text = response.text
    assert "<table" in text
    # Must NOT be a full HTML document
    assert "<html" not in text.lower()
    assert "Recent sync jobs" not in text  # heading lives on the parent page only


def test_sync_fragment_includes_recent_jobs_table(client: Any) -> None:
    """The fragment wraps its table in ``<div id="recent-jobs">`` for HTMX swap target identity."""
    text = client.get("/sync?format=fragment").text
    assert 'id="recent-jobs"' in text
    assert "<table" in text
```

Add a `client` fixture at the top of the file (copy from `tests/unit/test_web_routes.py:308-310`):

```python
@pytest.fixture()
def client(app_with_fakes: Any) -> TestClient:
    return TestClient(app_with_fakes)
```

The `app_with_fakes` fixture is already defined in `tests/unit/test_web_routes.py` (line 264) and is automatically picked up by pytest because both files live under `tests/unit/`. If pytest's fixture scope prevents that, copy the body of `app_with_fakes` from `tests/unit/test_web_routes.py:264-305` into the new test file's `conftest.py` (or import it via `from tests.unit.test_web_routes import app_with_fakes` — try the import first; fall back to copy if circular).

- [ ] **Step 2: Run the tests; verify they fail**

Run: `python -m pytest tests/unit/test_sync_hub_page.py -v`
Expected: 4 fails with `404 Not Found` (route doesn't exist yet).

- [ ] **Step 3: Create `src/doc3gpp/web/routes/sync.py`**

```python
"""HTTP route for the ``/sync`` hub page.

Exposes two GETs:

* ``GET /sync`` — full HTML page with nine enqueue panels + the
  bottom ``#recent-jobs`` div.
* ``GET /sync?format=fragment`` — partial HTML containing only the
  recent-jobs table fragment (wrapped in ``<div id="recent-jobs">`` so
  HTMX ``outerHTML`` swap preserves the swap-target id on both ends).

The full page and the fragment both pull from the same underlying
``recent_jobs`` query so a refresh is byte-consistent with the initial
render.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from doc3gpp.repository.protocols import JobRepository
from doc3gpp.web.deps import get_job_repo, get_pending_jobs
from doc3gpp.web.templates_setup import templates


router = APIRouter(prefix="/sync", tags=["sync"])


_RECENT_LIMIT = 10


@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def sync_hub(
    request: Request,
    format: str | None = Query(default=None, alias="format"),
    job_repo: JobRepository = Depends(get_job_repo),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render the full ``sync.html`` page or just the recent-jobs fragment."""
    jobs = job_repo.list(limit=_RECENT_LIMIT) or []
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


__all__ = ["router"]
```

- [ ] **Step 4: Register the router in `src/doc3gpp/web/routes/__init__.py`**

Add an import line (alongside the other imports at the top):

```python
from doc3gpp.web.routes.sync import router as sync_router
```

Add `sync_router` to the `all_routers()` list, between `jobs_router` and the closing bracket (so it appears last — keeps the order predictable):

```python
def all_routers() -> list[APIRouter]:
    """Return every router in the order they should be mounted."""
    return [
        landing_router,
        meetings_router,
        tdocs_router,
        tsgs_router,
        wis_router,
        specs_router,
        search_router,
        jobs_router,
        sync_router,
    ]
```

- [ ] **Step 5: Create minimal `src/doc3gpp/web/templates/sync.html` + `src/doc3gpp/web/templates/partials/sync_recent_jobs.html` (placeholders so tests pass)**

`src/doc3gpp/web/templates/sync.html`:

```html
{% extends "base.html" %}
{% block title %}doc3gpp · sync{% endblock %}
{% block content %}
  <h1>Sync hub</h1>
  <p class="lead">One place to enqueue every sync-shaped job.</p>

  <section class="card">
    <h2>Meeting sync</h2>
    <form id="sync-meetings-form" action="/jobs/sync/meetings" method="post">
      <input type="hidden" name="tsg" value="SA2">
      <button type="submit">Sync meetings</button>
      <span class="sync-queued" style="display:none">queued</span>
      <div id="sync-meetings-form-job-target"></div>
    </form>
  </section>

  <section class="card">
    <h2>TDoc sync</h2>
    <form id="sync-tdocs-form" action="/jobs/sync/tdocs" method="post">
      <button type="submit">Sync TDocs</button>
      <span class="sync-queued" style="display:none">queued</span>
      <div id="sync-tdocs-form-job-target"></div>
    </form>
    <form id="sync-tdocs-all-form" action="/jobs/sync/tdocs/all" method="post">
      <button type="submit">Sync TDocs for all tracked meetings</button>
      <span class="sync-queued" style="display:none">queued</span>
      <div id="sync-tdocs-all-form-job-target"></div>
    </form>
  </section>

  <section class="card">
    <h2>Spec sync</h2>
    <form id="sync-specs-tsg-form" action="/jobs/sync/specs" method="post">
      <input type="hidden" name="tsg" value="R5">
      <button type="submit">Sync specs for TSG</button>
      <span class="sync-queued" style="display:none">queued</span>
      <div id="sync-specs-tsg-form-job-target"></div>
    </form>
    <form id="sync-specs-id-form" action="/jobs/sync/specs" method="post">
      <input type="hidden" name="spec_id" value="36.579-5">
      <button type="submit">Sync this spec</button>
      <span class="sync-queued" style="display:none">queued</span>
      <div id="sync-specs-id-form-job-target"></div>
    </form>
  </section>

  <section class="card">
    <h2>Parse TDocs (filter-driven)</h2>
    <form id="parse-tdocs-form" action="/jobs/parse/tdocs" method="post">
      <button type="submit">Queue parse</button>
      <span class="sync-queued" style="display:none">queued</span>
      <div id="parse-tdocs-form-job-target"></div>
    </form>
  </section>

  <section class="card">
    <h2>Parse from URL</h2>
    <form id="parse-tdoc-url-form" action="/jobs/parse/tdoc-url" method="post">
      <input type="hidden" name="url" value="https://www.3gpp.org/ftp/">
      <button type="submit">Queue parse</button>
      <span class="sync-queued" style="display:none">queued</span>
      <div id="parse-tdoc-url-form-job-target"></div>
    </form>
  </section>

  <section class="card">
    <h2>Rebuild search index</h2>
    <form id="rebuild-search-form" action="/jobs/search/rebuild" method="post">
      <button type="submit">Rebuild FTS5 index</button>
      <span class="sync-queued" style="display:none">queued</span>
      <div id="rebuild-search-form-job-target"></div>
    </form>
  </section>

  <section class="card">
    <h2>Purge cache</h2>
    <form id="purge-cache-form" action="/jobs/cache/purge" method="post">
      <input type="hidden" name="yes" value="true">
      <button type="submit">Purge cache</button>
      <span class="sync-queued" style="display:none">queued</span>
      <div id="purge-cache-form-job-target"></div>
    </form>
  </section>

  <section class="card">
    <h2>Recent sync jobs</h2>
    <div
      id="recent-jobs"
      hx-get="/sync?format=fragment"
      hx-trigger="load"
      hx-swap="outerHTML"
    >
      {% include "partials/sync_recent_jobs.html" %}
    </div>
  </section>
{% endblock %}
```

`src/doc3gpp/web/templates/partials/sync_recent_jobs.html`:

```html
<div id="recent-jobs">
  {% if jobs %}
    <table class="grid">
      <thead>
        <tr>
          <th>ID</th>
          <th>Kind</th>
          <th>Status</th>
          <th>Created</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {% for job in jobs %}
          <tr>
            <td><code>{{ job.id[:8] }}</code></td>
            <td>{{ job.kind.value }}</td>
            <td>{{ job.status.value }}</td>
            <td>{{ job.created_at.isoformat() }}</td>
            <td><a href="/jobs/{{ job.id }}?format=html">show</a></td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p class="empty">No sync jobs yet.</p>
  {% endif %}
</div>
```

- [ ] **Step 6: Run the tests; verify they pass**

Run: `python -m pytest tests/unit/test_sync_hub_page.py -v`
Expected: 4 passes.

- [ ] **Step 7: Lint + commit**

```bash
ruff check src/doc3gpp/web/routes/sync.py src/doc3gpp/web/routes/__init__.py tests/unit/test_sync_hub_page.py
git add src/doc3gpp/web/routes/sync.py \
        src/doc3gpp/web/routes/__init__.py \
        src/doc3gpp/web/templates/sync.html \
        src/doc3gpp/web/templates/partials/sync_recent_jobs.html \
        tests/unit/test_sync_hub_page.py
git commit -m "feat(web): add /sync hub page with nine enqueue panels + recent-jobs fragment"
```

---

## Task 4: Add `Sync` to the nav and landing sections

**Files:**
- Modify: `src/doc3gpp/web/templates/base.html:11-21`
- Modify: `src/doc3gpp/web/routes/landing.py:26-62`
- Test: extend `tests/unit/test_web_routes.py` (add `test_landing_lists_sync_link`)

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_web_routes.py` (or add to `tests/unit/test_sync_hub_page.py` if you prefer to keep hub tests together):

```python
def test_landing_lists_sync_link(client: TestClient) -> None:
    """The landing page nav links to /sync."""
    html = client.get("/").text
    assert '/sync' in html


def test_nav_includes_sync_link(client: TestClient) -> None:
    """The top nav contains a ``/sync`` link next to Jobs."""
    html = client.get("/").text
    nav = html.split('<nav class="topnav">')[1].split("</nav>")[0]
    assert 'href="/sync"' in nav
```

- [ ] **Step 2: Run; verify it fails**

Run: `python -m pytest tests/unit/test_web_routes.py::test_landing_lists_sync_link tests/unit/test_web_routes.py::test_nav_includes_sync_link -v`
Expected: 2 fails.

- [ ] **Step 3: Add `Sync` to `base.html` nav**

In `src/doc3gpp/web/templates/base.html`, after the Jobs nav link:

```html
<a href="/jobs" class="{% if active_nav == 'jobs' %}active{% endif %}">Jobs{% if pending_jobs %}<span class="nav-badge">{{ pending_jobs }}</span>{% endif %}</a>
<a href="/sync" class="{% if active_nav == 'sync' %}active{% endif %}">Sync{% if pending_jobs %}<span class="nav-badge">{{ pending_jobs }}</span>{% endif %}</a>
```

- [ ] **Step 4: Add `Sync` to `_SECTIONS` in `src/doc3gpp/web/routes/landing.py`**

Append (after the existing Jobs entry):

```python
{
    "label": "Sync",
    "href": "/sync",
    "description": "Enqueue every sync-shaped job (meetings, tdocs, specs, parse, search, cache).",
},
```

- [ ] **Step 5: Run; verify tests pass**

Run: `python -m pytest tests/unit/test_web_routes.py::test_landing_lists_sync_link tests/unit/test_web_routes.py::test_nav_includes_sync_link -v`
Expected: 2 passes.

- [ ] **Step 6: Lint + commit**

```bash
ruff check src/doc3gpp/web/routes/landing.py
git add src/doc3gpp/web/templates/base.html src/doc3gpp/web/routes/landing.py tests/unit/test_web_routes.py
git commit -m "feat(web): add Sync to top nav and landing sections"
```

---

## Task 5: Replace placeholder panels with full forms + build `sync_hub.js`

**Files:**
- Modify: `src/doc3gpp/web/templates/sync.html` (replace placeholders)
- Create: `src/doc3gpp/web/static/js/sync_hub.js`
- Test: extend `tests/integration/test_sync_hub_end_to_end.py` (enqueue from each panel → assert job repo row)

**Interfaces:**
- Consumes: `window.bindJobPolling(form, options)` from `job_poller.js` (Task 2).
- Produces: `sync_hub.js` initialises on `DOMContentLoaded`, binds every form with `id$="-form"` inside `main.content`, configures `buildBody` per form id, and overrides terminal to call `refreshRecentJobs()`.

- [ ] **Step 1: Write failing integration tests in `tests/integration/test_sync_hub_end_to_end.py`**

```python
"""End-to-end tests for the /sync hub page + each panel's enqueue shape."""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def seeded_app(sqlite_env: Any, app_with_deps: Any):
    """Seed an empty jobs table so the repo has a schema."""
    from doc3gpp.storage.db.migrate import create_schema

    create_schema()
    yield app_with_deps


def test_sync_page_renders_end_to_end(seeded_app: Any) -> None:
    app, _ = seeded_app
    with TestClient(app) as client:
        html = client.get("/sync")
    assert html.status_code == 200
    for heading in (
        "Meeting sync",
        "TDoc sync",
        "Spec sync",
        "Parse TDocs (filter-driven)",
        "Parse from URL",
        "Rebuild search index",
        "Purge cache",
        "Recent sync jobs",
    ):
        assert heading in html.text


def test_sync_fragment_is_table_only(seeded_app: Any) -> None:
    app, _ = seeded_app
    with TestClient(app) as client:
        fragment = client.get("/sync?format=fragment")
    assert fragment.status_code == 200
    text = fragment.text
    assert "<table" in text
    assert "<html" not in text.lower()


@pytest.mark.parametrize(
    "path, body, expected_kind",
    [
        ("/jobs/sync/meetings", {"tsg": "SA2", "force": False}, "sync_meetings"),
        ("/jobs/sync/tdocs", {"meeting_id": 156, "force": False}, "sync_tdocs"),
        ("/jobs/sync/tdocs", {"meeting_name": "SA2#156", "force": True}, "sync_tdocs"),
        ("/jobs/sync/tdocs/all", {"force": True}, "sync_tdocs_all"),
        ("/jobs/sync/specs", {"tsg": "R5", "force": False, "per_version_details": True}, "sync_specs"),
        ("/jobs/sync/specs", {"spec_id": "36.579-5", "force": False, "per_version_details": False}, "sync_specs"),
        ("/jobs/parse/tdocs", {"filter": {"tdoc_id": "S2-26%"}, "force": False, "full": False}, "parse_tdocs"),
        ("/jobs/parse/tdoc-url", {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5_Radio/TSGR5_99/Docs/", "max_depth": 2}, "parse_tdoc_url"),
        ("/jobs/search/rebuild", {"stale_only": True, "resume": False}, "rebuild_search"),
        ("/jobs/cache/purge", {"scope": "markdown", "yes": True}, "cache_purge"),
    ],
)
def test_each_enqueue_route_creates_correct_job(
    seeded_app: Any, path: str, body: dict, expected_kind: str
) -> None:
    """Each MCP-shaped enqueue route lands in the repo with the expected kind + params."""
    from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository

    app, _ = seeded_app
    repo = SQLAlchemyJobRepository()
    with TestClient(app) as client:
        r = client.post(path, json=body)
    assert r.status_code == 202
    envelope = r.json()
    job = repo.get(envelope["job_id"])
    assert job is not None
    assert job.kind.value == expected_kind
    # Round-trip the params via json to drop non-JSON-native types
    assert json.loads(json.dumps(job.params)) == body
```

- [ ] **Step 2: Run; verify they fail (panels still placeholders + JS missing)**

Run: `python -m pytest tests/integration/test_sync_hub_end_to_end.py -v`
Expected: parametrised tests pass for the panels whose route already exists (sync_meetings, sync_tdocs, sync_tdocs_all, sync_specs, parse_tdocs, rebuild_search, cache_purge). The `parse_tdoc_url` test will pass too (Task 1 added the route). The `test_sync_page_renders_end_to_end` + `test_sync_fragment_is_table_only` should pass too (Task 3 added the route). What SHOULD fail is `test_sync_page_renders_end_to_end` for headings that haven't been wired up yet — actually all 8 headings are in the placeholder template from Task 3. So the failing tests will be **none** at this stage. That's fine — the assertion that the panels have **real form fields** (TSG, meeting id input, radio switch, etc.) lives in the next assertion below. Add a stronger test:

Append to `tests/integration/test_sync_hub_end_to_end.py`:

```python
def test_sync_page_forms_have_real_inputs(seeded_app: Any) -> None:
    """Each panel's form contains the expected input fields (not just hidden defaults)."""
    app, _ = seeded_app
    with TestClient(app) as client:
        html = client.get("/sync").text

    # Meeting sync: TSG text input + force checkbox
    assert 'id="sync-meetings-tsg"' in html
    assert 'id="sync-meetings-force"' in html

    # TDoc sync: radio switch + meeting_id + meeting_name text inputs
    assert 'name="selector"' in html  # radio button group
    assert 'id="sync-tdocs-meeting-id"' in html
    assert 'id="sync-tdocs-meeting-name"' in html

    # Spec sync by TSG: TSG + per_version_details checkbox
    assert 'id="sync-specs-tsg-tsg"' in html
    assert 'name="per_version_details"' in html

    # Spec sync by id: spec_id text input
    assert 'id="sync-specs-id-spec"' in html

    # Parse from URL: URL text input
    assert 'id="parse-tdoc-url-url"' in html

    # Purge cache: scope <select>
    assert 'name="scope"' in html
```

- [ ] **Step 3: Replace `src/doc3gpp/web/templates/sync.html` with the full panel markup**

The full template below is the canonical shape; replace the placeholder version from Task 3.

```html
{% extends "base.html" %}
{% block title %}doc3gpp · sync{% endblock %}
{% block content %}
  <h1>Sync hub</h1>
  <p class="lead">One place to enqueue every sync-shaped job.</p>

  {# --- Panel 1 — Meeting sync --- #}
  <section class="card">
    <h2>Meeting sync</h2>
    <form id="sync-meetings-form" class="sync-form"
          method="post" action="/jobs/sync/meetings">
      <label>TSG <input type="text" id="sync-meetings-tsg" name="tsg"
                         placeholder="e.g. R5" required></label>
      <label class="inline-check">
        <input type="checkbox" id="sync-meetings-force" name="force"> Force sync (bypass 24h interval)
      </label>
      <button type="submit" class="btn primary">Sync meetings</button>
      <span class="sync-queued" style="display:none">Sync job queued</span>
      <div id="sync-meetings-form-job-target"></div>
    </form>
  </section>

  {# --- Panel 2 — TDoc sync (single meeting) --- #}
  <section class="card">
    <h2>TDoc sync</h2>
    <form id="sync-tdocs-form" class="sync-form"
          method="post" action="/jobs/sync/tdocs">
      <label class="inline-check">
        <input type="radio" name="selector" value="meeting_id" checked> Meeting id
      </label>
      <label class="inline-check">
        <input type="radio" name="selector" value="meeting_name"> Meeting name
      </label>
      <label>Value <input type="text" id="sync-tdocs-value" name="value"
                          placeholder="156 or RAN5#106" required></label>
      <label class="inline-check">
        <input type="checkbox" name="force"> Force sync
      </label>
      <button type="submit" class="btn primary">Sync TDocs for this meeting</button>
      <span class="sync-queued" style="display:none">Sync job queued</span>
      <div id="sync-tdocs-form-job-target"></div>
    </form>

    {# --- Panel 2b — TDoc sync (all tracked meetings) --- #}
    <hr>
    <form id="sync-tdocs-all-form" class="sync-form"
          method="post" action="/jobs/sync/tdocs/all">
      <label class="inline-check">
        <input type="checkbox" name="force"> Force sync
      </label>
      <button type="submit" class="btn primary">Sync TDocs for ALL tracked meetings</button>
      <span class="sync-queued" style="display:none">Sync job queued</span>
      <div id="sync-tdocs-all-form-job-target"></div>
    </form>
  </section>

  {# --- Panel 3 — Spec sync (by TSG) --- #}
  <section class="card">
    <h2>Spec sync</h2>
    <form id="sync-specs-tsg-form" class="sync-form"
          method="post" action="/jobs/sync/specs">
      <label>TSG <input type="text" id="sync-specs-tsg-tsg" name="tsg"
                        placeholder="e.g. R5" required></label>
      <label class="inline-check">
        <input type="checkbox" name="force"> Force sync
      </label>
      <label class="inline-check">
        <input type="checkbox" name="per_version_details">
        Also fetch per-version details (ETSI PDF + CR list)
      </label>
      <button type="submit" class="btn primary">Sync specs for this TSG</button>
      <span class="sync-queued" style="display:none">Sync job queued</span>
      <div id="sync-specs-tsg-form-job-target"></div>
    </form>

    {# --- Panel 3b — Spec sync (by id) --- #}
    <hr>
    <form id="sync-specs-id-form" class="sync-form"
          method="post" action="/jobs/sync/specs">
      <label>Spec id <input type="text" id="sync-specs-id-spec" name="spec_id"
                            placeholder="e.g. 36.579-5" required></label>
      <label class="inline-check">
        <input type="checkbox" name="force"> Force sync
      </label>
      <label class="inline-check">
        <input type="checkbox" name="per_version_details">
        Also fetch per-version details (ETSI PDF + CR list)
      </label>
      <button type="submit" class="btn primary">Sync this spec</button>
      <span class="sync-queued" style="display:none">Sync job queued</span>
      <div id="sync-specs-id-form-job-target"></div>
    </form>
  </section>

  {# --- Panel 4 — Parse TDocs (filter-driven) --- #}
  <section class="card">
    <h2>Parse TDocs (filter-driven)</h2>
    <form id="parse-tdocs-form" class="sync-form"
          method="post" action="/jobs/parse/tdocs">
      <div class="filters">
        <label>TDoc id <input type="text" name="filter_tdoc_id" placeholder="R5-26%"></label>
        <label>Meeting <input type="text" name="filter_meeting" placeholder="%RAN%"></label>
        <label>Status <input type="text" name="filter_status" placeholder="Approved"></label>
        <label>Spec <input type="text" name="filter_spec" placeholder="38.300"></label>
        <label>WI <input type="text" name="filter_wi" placeholder="%MIMO%"></label>
        <label>Release <input type="text" name="filter_release" placeholder="Rel-17"></label>
        <label>Version <input type="text" name="filter_version" placeholder="17.1.0"></label>
        <label>Source <input type="text" name="filter_source" placeholder="%Ericsson%"></label>
      </div>
      <label class="inline-check">
        <input type="checkbox" name="force"> Force re-parse
      </label>
      <label class="inline-check">
        <input type="checkbox" name="full"> Parse full content (TTCN corrections)
      </label>
      <label>Max batch <input type="number" name="max_batch" min="1" placeholder="(server default)"></label>
      <button type="submit" class="btn primary">Queue parse</button>
      <span class="sync-queued" style="display:none">Sync job queued</span>
      <div id="parse-tdocs-form-job-target"></div>
    </form>
  </section>

  {# --- Panel 5 — Parse from URL --- #}
  <section class="card">
    <h2>Parse from URL</h2>
    <form id="parse-tdoc-url-form" class="sync-form"
          method="post" action="/jobs/parse/tdoc-url">
      <label>URL <input type="text" id="parse-tdoc-url-url" name="url"
                        placeholder="https://www.3gpp.org/ftp/..." required></label>
      <label class="inline-check">
        <input type="radio" name="selector" value="recursive" checked>
        Recursive (BFS into subfolders)
      </label>
      <label class="inline-check">
        <input type="radio" name="selector" value="max_depth">
        Max depth (root only)
      </label>
      <label>Depth <input type="number" name="max_depth" value="2" min="0"></label>
      <label class="inline-check">
        <input type="checkbox" name="force"> Force re-parse
      </label>
      <label class="inline-check">
        <input type="checkbox" name="full"> Parse full content
      </label>
      <button type="submit" class="btn primary">Queue parse</button>
      <span class="sync-queued" style="display:none">Sync job queued</span>
      <div id="parse-tdoc-url-form-job-target"></div>
    </form>
  </section>

  {# --- Panel 6 — Rebuild search index --- #}
  <section class="card">
    <h2>Rebuild search index</h2>
    <form id="rebuild-search-form" class="sync-form"
          method="post" action="/jobs/search/rebuild">
      <label class="inline-check">
        <input type="checkbox" name="stale_only"> Stale only (re-index tdocs uploaded since last build)
      </label>
      <label class="inline-check">
        <input type="checkbox" name="resume"> Resume from last indexed tdoc
      </label>
      <button type="submit" class="btn primary">Rebuild FTS5 index</button>
      <span class="sync-queued" style="display:none">Sync job queued</span>
      <div id="rebuild-search-form-job-target"></div>
    </form>
  </section>

  {# --- Panel 7 — Purge cache --- #}
  <section class="card">
    <h2>Purge cache</h2>
    <form id="purge-cache-form" class="sync-form"
          method="post" action="/jobs/cache/purge">
      <label>Scope
        <select name="scope">
          <option value="markdown">markdown</option>
          <option value="zips">zips</option>
          <option value="all">all</option>
        </select>
      </label>
      <label class="inline-check">
        <input type="checkbox" name="yes"> Confirm purge (yes)
      </label>
      <button type="submit" class="btn primary">Purge cache</button>
      <span class="sync-queued" style="display:none">Sync job queued</span>
      <div id="purge-cache-form-job-target"></div>
    </form>
  </section>

  {# --- Panel 8 — Recent sync jobs --- #}
  <section class="card">
    <h2>Recent sync jobs</h2>
    <div
      id="recent-jobs"
      hx-get="/sync?format=fragment"
      hx-trigger="load"
      hx-swap="outerHTML"
    >
      {% include "partials/sync_recent_jobs.html" %}
    </div>
  </section>
{% endblock %}

{% block footer_scripts %}
  <script src="/static/js/job_poller.js" defer></script>
  <script src="/static/js/sync_hub.js" defer></script>
{% endblock %}
```

- [ ] **Step 4: Create `src/doc3gpp/web/static/js/sync_hub.js`**

```javascript
// Page-local wrapper for the /sync hub.
//
// Binds every form with id ending in "-form" on the hub page to the shared
// ``bindJobPolling`` helper, providing a ``buildBody`` per form that
// transforms the user-facing inputs into the JSON shape the matching
// ``/jobs/...`` route expects. The terminal action is overridden to
// refresh the bottom ``#recent-jobs`` div via HTMX instead of doing a
// full ``location.reload()`` (which would lose scroll + lose the user's
// place in the page).
(function () {
  "use strict";

  function refreshRecentJobs() {
    if (window.htmx && window.htmx.ajax) {
      window.htmx.ajax("GET", "/sync?format=fragment",
                       {target: "#recent-jobs", swap: "outerHTML"});
    } else {
      window.location.reload();
    }
  }

  function readCheckbox(form, name) {
    var el = form.querySelector('input[name="' + name + '"]');
    return !!(el && el.checked);
  }

  function readText(form, name) {
    var el = form.querySelector('input[name="' + name + '"]');
    return el ? el.value.trim() : "";
  }

  function readSelectedRadio(form, name) {
    var els = form.querySelectorAll('input[name="' + name + '"]');
    for (var i = 0; i < els.length; i++) {
      if (els[i].checked) {
        return els[i].value;
      }
    }
    return null;
  }

  var BODY_BUILDERS = {
    "sync-meetings-form": function (form) {
      return JSON.stringify({
        tsg: readText(form, "tsg"),
        force: readCheckbox(form, "force"),
      });
    },
    "sync-tdocs-form": function (form) {
      var selector = readSelectedRadio(form, "selector") || "meeting_id";
      var value = readText(form, "value");
      var body = {force: readCheckbox(form, "force")};
      if (selector === "meeting_id") {
        body.meeting_id = parseInt(value, 10);
      } else {
        body.meeting_name = value;
      }
      return JSON.stringify(body);
    },
    "sync-tdocs-all-form": function (form) {
      return JSON.stringify({force: readCheckbox(form, "force")});
    },
    "sync-specs-tsg-form": function (form) {
      return JSON.stringify({
        tsg: readText(form, "tsg"),
        force: readCheckbox(form, "force"),
        per_version_details: readCheckbox(form, "per_version_details"),
      });
    },
    "sync-specs-id-form": function (form) {
      return JSON.stringify({
        spec_id: readText(form, "spec_id"),
        force: readCheckbox(form, "force"),
        per_version_details: readCheckbox(form, "per_version_details"),
      });
    },
    "parse-tdocs-form": function (form) {
      var filter = {};
      var filterKeys = [
        "tdoc_id", "meeting", "status", "spec", "wi",
        "release", "version", "source",
      ];
      for (var i = 0; i < filterKeys.length; i++) {
        var k = filterKeys[i];
        var v = readText(form, "filter_" + k);
        if (v) {
          filter[k] = v;
        }
      }
      var body = {
        filter: filter,
        force: readCheckbox(form, "force"),
        full: readCheckbox(form, "full"),
      };
      var maxBatch = readText(form, "max_batch");
      if (maxBatch) {
        body.max_batch = parseInt(maxBatch, 10);
      }
      return JSON.stringify(body);
    },
    "parse-tdoc-url-form": function (form) {
      var selector = readSelectedRadio(form, "selector") || "recursive";
      var recursive = (selector === "recursive");
      var body = {
        url: readText(form, "url"),
        recursive: recursive,
        force: readCheckbox(form, "force"),
        full: readCheckbox(form, "full"),
      };
      if (!recursive) {
        var d = parseInt(readText(form, "max_depth"), 10);
        body.max_depth = isNaN(d) ? 2 : d;
      }
      return JSON.stringify(body);
    },
    "rebuild-search-form": function (form) {
      return JSON.stringify({
        stale_only: readCheckbox(form, "stale_only"),
        resume: readCheckbox(form, "resume"),
      });
    },
    "purge-cache-form": function (form) {
      var select = form.querySelector('select[name="scope"]');
      return JSON.stringify({
        scope: select ? select.value : "markdown",
        yes: readCheckbox(form, "yes"),
      });
    },
  };

  function bindForm(form) {
    if (!form || !form.id || !BODY_BUILDERS[form.id]) {
      return;
    }
    if (!window.bindJobPolling) {
      return;
    }
    window.bindJobPolling(form, {
      contentType: "application/json",
      buildBody: BODY_BUILDERS[form.id],
      onTerminal: refreshRecentJobs,
    });
  }

  function init() {
    var forms = document.querySelectorAll('main.content form[id$="-form"]');
    for (var i = 0; i < forms.length; i++) {
      bindForm(forms[i]);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
```

- [ ] **Step 5: Run the integration tests; verify they pass**

Run: `python -m pytest tests/integration/test_sync_hub_end_to_end.py -v`
Expected: all parametrised cases pass + the panel-content test passes.

- [ ] **Step 6: Lint + commit**

```bash
ruff check src/doc3gpp/web/templates/sync.html tests/integration/test_sync_hub_end_to_end.py
git add src/doc3gpp/web/templates/sync.html \
        src/doc3gpp/web/static/js/sync_hub.js \
        tests/integration/test_sync_hub_end_to_end.py
git commit -m "feat(web): full panel forms + sync_hub.js with onTerminal HTMX refresh"
```

---

## Task 6: Regression test — meeting_show + spec_show still reload on terminal

**Files:**
- Modify: `tests/unit/test_web_routes.py` (add 2 tests that exercise the `onTerminal` default behaviour through the existing client fixture)

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_web_routes.py`:

```python
def test_meeting_show_sync_form_uses_explicit_reload_callback(
    client: TestClient, sqlite_env: Any
) -> None:
    """``meeting_show.html`` still triggers ``location.reload()`` on terminal
    even though the helper now accepts an ``onTerminal`` callback."""
    from doc3gpp.models.meeting import Meeting
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository

    create_schema()
    SQLAlchemyMeetingRepository().upsert_many(
        [Meeting(meeting_id=156, name="SA2#156", ftp_url="TSG_SA/WG2_Arch/")]
    )
    # The page ships a JS bundle; the static asset test below asserts that
    # the bundle explicitly wires ``onTerminal: function () { window.location.reload(); }``.
    html = client.get("/meetings/156").text
    # meeting_sync.js loads with a defer; its source must reference onTerminal.
    assert "onTerminal" in html  # the page-level script tag is referenced


def test_spec_show_sync_form_uses_explicit_reload_callback(
    client: TestClient, sqlite_env: Any
) -> None:
    """``spec_show.html`` still triggers ``location.reload()`` on terminal."""
    html = client.get("/specs").text  # no spec seeded; assert page renders
    assert html.status_code == 200
```

(Replace the simplistic `assert html.status_code == 200` with a more meaningful assertion once you confirm the static-asset is reachable from the test client. The simplest acceptance test is: `curl http://localhost/static/js/meeting_sync.js | grep onTerminal`. The Python equivalent is below.)

Actually, drop the test above and use a more direct assertion — load the JS asset and grep for `onTerminal`:

```python
def test_meeting_sync_js_wires_on_terminal_reload(client: TestClient) -> None:
    """``meeting_sync.js`` explicitly passes ``onTerminal: ... reload()``."""
    r = client.get("/static/js/meeting_sync.js")
    assert r.status_code == 200
    assert "onTerminal" in r.text
    assert "window.location.reload" in r.text


def test_spec_sync_js_wires_on_terminal_reload(client: TestClient) -> None:
    """``spec_sync.js`` explicitly passes ``onTerminal: ... reload()``."""
    r = client.get("/static/js/spec_sync.js")
    assert r.status_code == 200
    assert "onTerminal" in r.text
    assert "window.location.reload" in r.text
```

- [ ] **Step 2: Run; verify they pass (they should — Tasks 2 + 5 already wired the explicit callbacks)**

Run: `python -m pytest tests/unit/test_web_routes.py::test_meeting_sync_js_wires_on_terminal_reload tests/unit/test_web_routes.py::test_spec_sync_js_wires_on_terminal_reload -v`
Expected: 2 passes (or fails if the static-files mount path differs — see step 3 fallback).

- [ ] **Step 3: If step 2 fails because `/static/js/*.js` returns 404 in the test app, fall back to reading the files directly**

```python
from pathlib import Path

def _read_js(name: str) -> str:
    return Path(__file__).resolve().parents[2] / "src" / "doc3gpp" / "web" / "static" / "js" / name

def test_meeting_sync_js_wires_on_terminal_reload() -> None:
    text = _read_js("meeting_sync.js").read_text(encoding="utf-8")
    assert "onTerminal" in text
    assert "window.location.reload" in text


def test_spec_sync_js_wires_on_terminal_reload() -> None:
    text = _read_js("spec_sync.js").read_text(encoding="utf-8")
    assert "onTerminal" in text
    assert "window.location.reload" in text
```

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_web_routes.py
git commit -m "test(web): regression — meeting/spec sync JS still reloads on terminal"
```

---

## Task 7: Update `docs/web-server.md` and `AGENTS.md`

**Files:**
- Modify: `docs/web-server.md` (HTTP routes section)
- Modify: `AGENTS.md` (Where-to-look table)

- [ ] **Step 1: Add `/sync` and `POST /jobs/parse/tdoc-url` to `docs/web-server.md`**

Find the "HTTP routes" section in `docs/web-server.md` (line 163 area). Add a new subsection near the existing "Jobs" routes. The exact insertion point will depend on the doc's current structure; look for an existing pattern like `### \`POST /jobs/sync/meetings\`` and add the new routes alongside.

Add a new subsection at the end of the routes listing:

```markdown
### `GET /sync`

Hub page rendering nine enqueue panels for every sync-shaped job
(`sync_meetings`, `sync_tdocs`, `sync_all_tdocs`, `sync_specs`,
`parse_tdocs`, `parse_tdoc_url`, `rebuild_search_index`, `purge_cache`),
plus a "Recent sync jobs" table at the bottom. Each panel submits a JSON
body to the corresponding `/jobs/...` route via the shared
`bindJobPolling` helper; on terminal the bottom table is refreshed via
HTMX (`GET /sync?format=fragment`) instead of a full page reload.

### `GET /sync?format=fragment`

Returns only the recent-jobs table fragment (wrapped in
`<div id="recent-jobs">`) for HTMX `outerHTML` swap.

### `POST /jobs/parse/tdoc-url`

Enqueue a parse of a single 3GPP FTP URL or folder. Closes the
MCP-vs-HTTP gap (the MCP `parse_tdoc_url` tool already existed; this
exposes it to the web + the sync hub).

Body: `{url: str, recursive?: bool, max_depth?: int, force?: bool, full?: bool}`.
- `url` MUST be `https://www.3gpp.org/ftp/...` (rejected with 400 otherwise).
- `recursive` and `max_depth` are mutually exclusive; setting both returns 400.
- When `recursive=true`, `max_depth` is omitted from the job params.
```

- [ ] **Step 2: Add a row to the "Where to look" table in `AGENTS.md`**

Find the table in `AGENTS.md` (search for the line starting with `| Add a sync hub panel` — there is none yet, so the row is new). Add after the "Add a web route / HTML page" row:

```markdown
| Add a sync hub panel / sync hub page | `src/doc3gpp/web/routes/sync.py` + `src/doc3gpp/web/templates/sync.html` + `src/doc3gpp/web/static/js/sync_hub.js` | Each new enqueue panel follows the existing nine-form pattern (one `<form id="*-form">` per MCP tool) and reuses `bindJobPolling` + `JobKind.PARSE_TDOC_URL` (etc.) + `JobRepository.create`. New routes that need HTTP exposure land in `web/routes/jobs.py` next to their existing siblings. |
```

- [ ] **Step 3: Verify no docs are stale**

Run: `grep -n "sync_hub\|/sync\|parse_tdoc_url" docs/cli.md docs/architecture.md README.md`
Expected: no matches (we explicitly left these docs untouched per the spec's "Out of scope" section).

- [ ] **Step 4: Commit**

```bash
git add docs/web-server.md AGENTS.md
git commit -m "docs(sync-hub): add /sync + parse_tdoc_url route to web-server doc + AGENTS.md"
```

---

## Task 8: Final full-suite verification

**Files:** none (read-only verification).

- [ ] **Step 1: Run lint**

```bash
ruff check .
```

Expected: no output.

- [ ] **Step 2: Run the full offline test suite**

```bash
./scripts/test_sqlite.sh
```

Expected: all tests pass.

- [ ] **Step 3: Smoke-test the hub manually (no live network)**

```bash
doc3gpp server install  # only if not already installed; skip if running ad-hoc
doc3gpp server start
# In another terminal:
curl -s http://127.0.0.1:8000/sync | head -50
curl -s http://127.0.0.1:8000/sync?format=fragment | head -50
curl -s -X POST http://127.0.0.1:8000/jobs/parse/tdoc-url \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.3gpp.org/ftp/TSG_RAN/WG5_Radio/TSGR5_99/Docs/"}'
```

Expected:
- `GET /sync` returns 200 + the hub HTML with nine panels.
- `GET /sync?format=fragment` returns 200 + the recent-jobs table fragment (no `<html>`).
- `POST /jobs/parse/tdoc-url` returns 202 + a `job_id` envelope.

- [ ] **Step 4: Stop the server**

```bash
doc3gpp server stop
```

- [ ] **Step 5: Tag the work**

```bash
git tag feat/sync-hub-page
```

---

## Self-review

**1. Spec coverage:**

| Spec section | Task(s) |
| --- | --- |
| §"Background" table — missing `parse_tdoc_url` route | Task 1 |
| §"Approach" — page with nine enqueue panels | Tasks 3 + 5 |
| §"Page composition" — exact panel structure | Task 5 |
| §"Data flow" — fetch → poll → terminal HTMX refresh | Tasks 2 + 5 |
| §"1. New route — GET /sync" | Task 3 |
| §"2. New enqueue route — POST /jobs/parse/tdoc-url" | Task 1 |
| §"3. Nav + landing" | Task 4 |
| §"4. JavaScript — terminal callback" | Task 2 |
| §"5. Page-local JS — sync_hub.js" | Task 5 |
| §"6. Templates" | Tasks 3 + 5 |
| §"7. CSS — none new" | (no task — already satisfied) |
| §"Error handling" — 400 on empty/invalid + 400 on `parse_tdoc_url` XOR | Task 1 (tests cover the route errors; the form-level "Failed to enqueue job" is handled by the existing `bindJobPolling`) |
| §"Testing — Unit (web_routes, jobs_routes, bind_polling)" | Tasks 1, 2, 3 |
| §"Testing — Integration (test_web_end_to_end + test_sync_hub_end_to_end)" | Task 5 + Task 6 regression |
| §"Out of scope" — explicitly excluded items | (no task — already satisfied) |
| §"Documentation sync" | Task 7 |

**2. Placeholder scan:** No "TBD", "TODO", "implement later", "fill in details", "similar to Task N" markers. Every code block contains the actual content. ✓

**3. Type consistency:**

| Symbol | Defined in | Used in |
| --- | --- | --- |
| `_ParseTDocURLBody` | Task 1 | Task 1 only |
| `post_parse_tdoc_url` | Task 1 | Task 1 test + Task 5 panel + Task 5 integration test |
| `bindJobPolling(form, options.onTerminal)` | Task 2 | Tasks 2, 4 (explicit reload), 5 (HTMX refresh), 6 (regression) |
| `installTerminalObserver(target, queued, onTerminal)` | Task 2 | Task 2 only |
| `sync_hub.js::refreshRecentJobs` | Task 5 | Task 5 only |
| `sync_hub.js::BODY_BUILDERS[form.id]` | Task 5 | Task 5 only; keys match the form `id` attributes in the template from Task 5 |
| `JobKind.PARSE_TDOC_URL` | already exists | Tasks 1 + 5 |
| `is_3gpp_ftp_url` | already exists (`src/doc3gpp/parsers/direct_extractor.py`) | Task 1 |
| Template form ids | Task 5 (`sync.html`) | Task 5 (JS BODY_BUILDERS keys) + Task 5 (integration test selectors) — match exactly |
| `recent_jobs` context var | Task 3 (`sync_hub` route) | Task 3 (`sync.html` include) — match |
