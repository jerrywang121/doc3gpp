# Version Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the `doc3gpp` version on all three user-facing surfaces (CLI, web UI, MCP) from a single source of truth (`doc3gpp.__version__`).

**Architecture:** A Typer `is_eager=True` root-callback prints `doc3gpp <version>` for `doc3gpp --version`; a Jinja `app_version` global feeds every web page's footer; the MCP `serverInfo.version` block is already populated by `MCPServer(version=_package_version(), ...)` — no code change there, only a doc note. Every surface reads the same `__version__` constant exported from `src/doc3gpp/__init__.py`.

**Tech Stack:** Python 3.10+, Typer 0.12+, FastAPI/Jinja2, MCP SDK v2 (already wired), pytest.

**Spec:** `docs/superpowers/specs/2026-08-14-version-display-design.md`

## Global Constraints

- **Version source of truth:** `doc3gpp.__version__` exported from `src/doc3gpp/__init__.py` (currently `"0.1.4"`). The release flow already keeps it in lockstep with `pyproject.toml [project] version` — do not introduce a second source.
- **Output format for `doc3gpp --version`:** single line `doc3gpp <version>\n` on stdout, exit code 0. No Python/platform/git-hash metadata.
- **Top-level only:** `--version` lives on the root Typer app, not on any sub-command. Must not shadow the existing per-command `--version` flags on `tdoc list` (cli.py:1019) and `tdoc parse` (cli.py:1318), which filter on the TDoc `version` column. Use `is_eager=True` to short-circuit before sub-app parsing.
- **Web footer wording:** the rendered line in `base.html` must be exactly `doc3gpp web · version {{ app_version }} · read-only interface`. The `footer_text` block default text is `read-only interface` and must remain overridable; do not move the version string into a separate block.
- **JSON routes unchanged:** `GET /` and any other `?format=json` response must not include a top-level `version` key. JSON shape is a contract for machine clients; the MCP `serverInfo` block is the documented machine path.
- **MCP `serverInfo.version` is already populated** at `web/mcp_server.py:189` via `_package_version()` (web/mcp_server.py:57). No code change on the MCP side. The existing `tests/integration/test_mcp_end_to_end.py` continues to cover it; re-run it as part of verification.
- **Public import path:** import the version as `from doc3gpp import __version__` (the public constant exported via `__all__ = ["__version__"]`). Do not switch to a private `from doc3gpp._internal import _version` style.
- **Local import inside the CLI callback:** the import is done inside the `_version_callback` function body, not at module top level, so a future refactor that moves `__version__` cannot take the whole CLI down on import.
- **Lint:** `ruff check .` must pass before commit.
- **Tests:** `./scripts/test_sqlite.sh` must pass before commit; re-run `tests/integration/test_mcp_end_to_end.py` as part of the final verification step.

---

### Task 1: Add `--version` flag to root CLI app

**Files:**
- Modify: `src/doc3gpp/cli.py` (the existing `main_callback` at lines 159-163)
- Test: `tests/unit/test_cli_version.py` (new)

**Interfaces:**
- Consumes: `doc3gpp.__version__` (the public string constant from `src/doc3gpp/__init__.py`)
- Produces: a `_version_callback(value: bool) -> None` that prints `doc3gpp <version>\n` and raises `typer.Exit()`; a `version: bool = typer.Option(..., is_eager=True)` parameter on the existing `main_callback` that registers the flag on the root Typer app.

> **Why modify the existing `main_callback` instead of adding a new `@app.callback()`:** Typer allows only one `@app.callback()` per `Typer` instance, and `main_callback` is already registered at `cli.py:159` (it handles logging + bare-app help). The new `--version` option is added as a parameter of that same callback, so the existing logging + help behaviour is preserved. `is_eager=True` on the option causes its `callback` to run *before* `main_callback`'s body, and `typer.Exit()` short-circuits the rest of the CLI.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_cli_version.py` with the following contents:

```python
"""Unit tests for the ``doc3gpp --version`` flag."""
from __future__ import annotations

from typer.testing import CliRunner

from doc3gpp import __version__
from doc3gpp.cli import app


runner = CliRunner()


def test_version_flag_prints_version_and_exits_zero() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout == f"doc3gpp {__version__}\n"


def test_version_flag_ignores_trailing_args() -> None:
    """``doc3gpp --version meeting list`` still prints the version and exits 0."""
    result = runner.invoke(app, ["--version", "meeting", "list"])
    assert result.exit_code == 0
    assert result.stdout == f"doc3gpp {__version__}\n"


def test_help_text_mentions_version_flag() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--version" in result.stdout


def test_root_no_args_still_prints_help() -> None:
    """``doc3gpp`` with no args must keep its current help behaviour (regression guard)."""
    result = runner.invoke(app, [])
    # Typer's bare-app behaviour: exit 0 with --help-like output, or exit 2 with usage.
    # We don't pin the exit code tightly; we just confirm the new flag did not
    # accidentally register as required and break the no-args invocation.
    assert "--version" in result.stdout or "Usage" in result.stdout
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `python -m pytest tests/unit/test_cli_version.py -v`
Expected: 4 failures with messages like `"--version" not in result.stdout` (the flag is not yet registered).

- [ ] **Step 3: Implement the `--version` callback in `src/doc3gpp/cli.py`**

In `src/doc3gpp/cli.py`, locate the existing `main_callback` (currently lines 159-163):

```python
@app.callback(invoke_without_command=True)
def main_callback(ctx: typer.Context) -> None:
    _configure_logging()
    if ctx.invoked_subcommand is None:
        typer.echo(app.get_help(ctx))
```

Replace the entire block (the decorator + function) with:

```python
def _version_callback(value: bool) -> None:
    """Print the doc3gpp version and exit.

    Local-imports ``__version__`` so a future refactor that moves the
    constant cannot take the whole CLI down at import time.
    """
    if value:
        from doc3gpp import __version__

        typer.echo(f"doc3gpp {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the doc3gpp version and exit.",
    ),
) -> None:
    _configure_logging()
    if ctx.invoked_subcommand is None:
        typer.echo(app.get_help(ctx))
```

(Per the project convention in `docs/conventions.md`, the function body carries no comments unless they document non-obvious behaviour. The docstring on `_version_callback` is intentional and is the only comment-like content. The two new function lines preserve the existing `ctx: typer.Context` first-parameter ordering, which Typer requires for `Context` injection.)

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `python -m pytest tests/unit/test_cli_version.py -v`
Expected: 4 passed.

- [ ] **Step 5: Confirm no regression on the existing per-command `--version` flags**

Run: `python -m pytest tests/unit/test_tdoc_parse_cli.py -v -k "version"`
Expected: all existing version-related tests pass (look for `tdoc parse --tdoc ... --version 17%` style invocations in `tests/unit/test_tdoc_parse_cli.py:1277, 1308, 1320, 1357`). If any fail, the eager callback is shadowing them — re-check `is_eager=True` placement and that the option is registered on the root `app`, not the sub-apps.

- [ ] **Step 6: Lint**

Run: `ruff check src/doc3gpp/cli.py tests/unit/test_cli_version.py`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/cli.py tests/unit/test_cli_version.py
git commit -m "feat(cli): add --version flag to root app"
```

---

### Task 2: Add `app_version` Jinja global

**Files:**
- Modify: `src/doc3gpp/web/templates_setup.py` (insert after the existing `templates.env.globals["url_for"] = _url_for` block at line 39)

**Interfaces:**
- Consumes: `doc3gpp.__version__` (imported as `_APP_VERSION`)
- Produces: `templates.env.globals["app_version"]` — a `str` available inside every Jinja2 template that uses the shared `templates` instance.

- [ ] **Step 1: Add the global**

In `src/doc3gpp/web/templates_setup.py`, locate the line `templates.env.globals["url_for"] = _url_for` (around line 39). Immediately after that line, add:

```python
from doc3gpp import __version__ as _APP_VERSION

templates.env.globals["app_version"] = _APP_VERSION
```

Place the `from doc3gpp import ...` at module top with the other imports above the new assignment so it is hoisted out of the function scope. Specifically, move the existing import block to include this line:

```python
from fastapi import Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from doc3gpp import __version__ as _APP_VERSION
```

Then keep the assignment near the other `templates.env.globals[...]` lines:

```python
templates.env.globals["url_for"] = _url_for
templates.env.globals["app_version"] = _APP_VERSION
```

(Do not write a separate `from doc3gpp import __version__ as _APP_VERSION` at the bottom of the file — consolidate with the existing import block at the top.)

- [ ] **Step 2: Smoke-test the import**

Run: `python -c "from doc3gpp.web.templates_setup import templates; assert templates.env.globals['app_version']; print(templates.env.globals['app_version'])"`
Expected: prints the current version string (e.g. `0.1.4`).

- [ ] **Step 3: Lint**

Run: `ruff check src/doc3gpp/web/templates_setup.py`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add src/doc3gpp/web/templates_setup.py
git commit -m "feat(web): expose app_version Jinja global"
```

---

### Task 3: Render version in the base footer

**Files:**
- Modify: `src/doc3gpp/web/templates/base.html` (the `<footer class="footer">` block at lines 28-30)

**Interfaces:**
- Consumes: `app_version` Jinja global (set in Task 2)
- Produces: a footer line that reads `doc3gpp web · version 0.1.4 · read-only interface` (or whatever the current `__version__` is) on every page that extends `base.html`.

- [ ] **Step 1: Update the footer line**

In `src/doc3gpp/web/templates/base.html`, replace the entire `<footer class="footer">` block (currently lines 28-30):

```html
  <footer class="footer">
    <span>doc3gpp web · {% block footer_text %}read-only interface{% endblock %}</span>
  </footer>
```

with:

```html
  <footer class="footer">
    <span>doc3gpp web · version {{ app_version }} · {% block footer_text %}read-only interface{% endblock %}</span>
  </footer>
```

The change is a single `version {{ app_version }} · ` insertion between `web ·` and the `{% block footer_text %}` invocation. Preserve the existing 2-space indentation under `<footer>`.

- [ ] **Step 2: Render-test the landing page**

Run: `python -c "from fastapi.testclient import TestClient; from doc3gpp.web.app import build_app; c = TestClient(build_app()); r = c.get('/'); print(r.status_code); print('version 0.1.4' in r.text); print('read-only interface' in r.text)"`
Expected: status 200, both `True` (the second test confirms the `footer_text` default still works; the first confirms the version is rendered).

- [ ] **Step 3: Lint**

Run: `ruff check src/doc3gpp/web/templates/base.html`
Expected: clean (ruff ignores HTML files but the command is a no-op safety net).

- [ ] **Step 4: Commit**

```bash
git add src/doc3gpp/web/templates/base.html
git commit -m "feat(web): render version in base footer"
```

---

### Task 4: Web footer regression tests

**Files:**
- Create: `tests/unit/web/test_landing_version.py` (new)

**Interfaces:**
- Consumes: FastAPI `TestClient` against `build_app()`; the live Jinja global set up by `templates_setup`.
- Produces: assertions that the footer renders `version <__version__>` on `/` and on a second page (e.g. `/meetings` or `/tsgs`), and that the `?format=json` landing response is unchanged.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/web/test_landing_version.py` with the following contents:

```python
"""The web UI footer must surface the installed ``doc3gpp`` version on every page."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from doc3gpp import __version__
from doc3gpp.web.app import build_app
from doc3gpp.web.deps import get_pending_jobs


@pytest.fixture
def client() -> TestClient:
    app = build_app()
    app.dependency_overrides[get_pending_jobs] = lambda: 0
    return TestClient(app)


def test_landing_html_footer_contains_version(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert f"version {__version__}" in response.text


def test_meetings_html_footer_contains_version(client: TestClient) -> None:
    """Any page that extends base.html must inherit the versioned footer."""
    response = client.get("/meetings")
    assert response.status_code == 200
    assert f"version {__version__}" in response.text


def test_landing_json_shape_unchanged(client: TestClient) -> None:
    """The ``?format=json`` landing response must not include a top-level version key."""
    response = client.get("/?format=json")
    assert response.status_code == 200
    body = response.json()
    assert "version" not in body
    assert "sections" in body
```

- [ ] **Step 2: Run the tests to confirm they pass**

Run: `python -m pytest tests/unit/web/test_landing_version.py -v`
Expected: 3 passed. (If any fail, the most likely cause is that `get_pending_jobs` is required and not overridden — the fixture above handles it.)

- [ ] **Step 3: Lint**

Run: `ruff check tests/unit/web/test_landing_version.py`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/web/test_landing_version.py
git commit -m "test(web): cover version rendering in base footer"
```

---

### Task 5: Docs — CLI and web-server

**Files:**
- Modify: `docs/cli.md` (add a one-line entry to the global-flags / top-of-doc reference)
- Modify: `docs/web-server.md` (add a one-paragraph note under the MCP section)

**Interfaces:**
- Consumes: the spec's "Documentation" section.
- Produces: a single doc line per file that future readers (or a downstream doc-sync reviewer) can grep for.

- [ ] **Step 1: Update `docs/cli.md`**

Find the section in `docs/cli.md` that describes top-level / global flags (search for "Global", "Top-level", "Flags", or the start of the per-sub-command reference). Insert a new line in the first such list (or as a new bullet):

```
- `--version` — print `doc3gpp <version>` and exit. Top-level only; takes precedence over sub-command arguments when supplied.
```

If `docs/cli.md` does not currently have a global-flags section, create one titled `## Global flags` immediately under the `# doc3gpp CLI` heading and add the bullet there. Verify the new bullet renders correctly by reading the surrounding paragraphs to keep the style consistent (one-sentence bullets, backticks around code).

- [ ] **Step 2: Update `docs/web-server.md`**

Find the `MCP server` (or equivalent) section in `docs/web-server.md`. Add a single paragraph immediately after the section's opening description (or at the end of the section, before the next heading):

```
The MCP `serverInfo` block returned on every `initialize` handshake carries `name` ("doc3gpp"), `version` (from `importlib.metadata.version("doc3gpp")`, falling back to `doc3gpp.__version__`), `title`, `description`, and `website_url`. Clients do not need a tool call to read the version.
```

- [ ] **Step 3: Lint**

Run: `ruff check .`
Expected: clean (no Python touched, but the command is a no-op safety net).

- [ ] **Step 4: Commit**

```bash
git add docs/cli.md docs/web-server.md
git commit -m "docs: document --version flag and MCP serverInfo version"
```

---

### Task 6: Full verification

**Files:**
- None modified; pure verification pass.

- [ ] **Step 1: Run the full offline test suite**

Run: `./scripts/test_sqlite.sh`
Expected: all unit + integration tests pass; the new `tests/unit/test_cli_version.py` and `tests/unit/web/test_landing_version.py` are included.

- [ ] **Step 2: Run the MCP end-to-end test**

Run: `python -m pytest tests/integration/test_mcp_end_to_end.py -v`
Expected: all tests pass. The MCP `serverInfo.version` field is exercised by the `initialize` handshake in this suite; the design does not touch the MCP code path, so passing here is the regression guard.

- [ ] **Step 3: Final lint pass**

Run: `ruff check .`
Expected: clean.

- [ ] **Step 4: Confirm the three surfaces end-to-end**

Manual smoke checks (no commit):

```
doc3gpp --version
# expected: "doc3gpp 0.1.4" + exit 0

# start the web server (assumes a dev DB is initialised; otherwise `doc3gpp db init` first)
doc3gpp server start &
SERVER_PID=$!
sleep 2
curl -s http://127.0.0.1:8765/ | grep -F "version 0.1.4"
# expected: matches the footer line
kill $SERVER_PID
```

If the web server port is configured differently, substitute the actual host:port (read it from `doc3gpp config show | grep -A3 '\[server\]'`).

- [ ] **Step 5: No further commit**

If any verification step failed, fix the underlying issue and amend or add a follow-up commit. Do not amend already-pushed history; add a new commit on top.
