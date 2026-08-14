# Version Display Across CLI, Web, and MCP Surfaces

**Status:** Draft (awaiting user review)
**Date:** 2026-08-14
**Branch:** main

## Problem

`doc3gpp` is shipped through three user-facing surfaces (the `doc3gpp`
CLI, the local web UI served by `doc3gpp server`, and the MCP server
exposed by the same process), but there is no first-class way to read
the running version from any of them:

- The CLI root app has no `--version` flag. Users have to launch Python
  and `import doc3gpp; print(doc3gpp.__version__)` to find the version.
- The web UI shows the brand `doc3gpp` in the header / footer but never
  the version. Operators running multiple instances over time cannot
  confirm which build a given page is rendering.
- The MCP server populates `serverInfo.version` from the installed
  distribution (via `importlib.metadata.version("doc3gpp")` in
  `web/mcp_server.py::_package_version`) but there is no documented
  path for an MCP client to read it without parsing the handshake.

This design adds a single line of version display to each surface, all
sourced from the existing `doc3gpp.__version__` constant.

## Goals & non-goals

**Goals**

1. A `doc3gpp --version` flag on the root CLI that prints
   `doc3gpp <version>` and exits 0, without firing any sub-command.
2. The same version string rendered in the web UI footer on every page.
3. Documented parity for MCP clients: `serverInfo.version` is
   authoritative, no new tool needed.
4. One source of truth — `doc3gpp.__version__` (already kept in lockstep
   with `pyproject.toml [project] version` by the release flow).

**Non-goals**

- A short flag (`-V`), a per-sub-command `--version`, or a
  `doc3gpp <subcommand> --version` plumbing.
- A new `GET /info` HTTP route or an MCP `get_server_info` tool.
- Any change to the version-string format, the `__version__` constant,
  or the `pyproject.toml` release process.
- Displaying extra metadata (Python version, platform, git hash, etc.).

## Surfaces

### 1. CLI root app — `doc3gpp --version`

**Where:** `src/doc3gpp/cli.py`. The root `app = typer.Typer(...)` at
`cli.py:101` currently has no `@app.callback()` decorator; we add one
with a single eager `--version` option.

**Why eager (`is_eager=True`):** The `tdoc list` and `tdoc parse`
commands already have a per-command `--version` flag that filters on
the TDoc `version` column (cli.py:1019, 1318). Without `is_eager=True`,
Typer would try to resolve `doc3gpp --version meeting list` through
the `meeting` sub-app first and the new top-level flag would either
shadow the per-command one or be ignored depending on arg ordering.
`is_eager=True` short-circuits to the callback before any sub-app
parses its own options.

**Shape:**

```python
def _version_callback(value: bool) -> None:
    if value:
        from doc3gpp import __version__
        typer.echo(f"doc3gpp {__version__}")
        raise typer.Exit()

@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the doc3gpp version and exit.",
    ),
) -> None:
    pass
```

**Output & exit code:**

```
$ doc3gpp --version
doc3gpp 0.1.4
$ echo $?
0
```

When `--version` is passed alongside any other argument, the callback
still fires and `typer.Exit(0)` is raised before the sub-command
resolves. Trailing args are ignored (matches `git --version foo`
behaviour). This is intentional: the flag answers "what version am I
running?", not "what version of <sub-command> is installed?".

**Reuse of `__version__`:** the callback imports `doc3gpp.__version__`
inside the function body rather than at module top level. The CLI
module is imported by other call paths (e.g. `cli_server.py`), and
keeping the import local means an unrelated import failure in the
`__version__` chain (none today, but possible if a future refactor
moves it) cannot take the whole CLI down. The import path is the
public constant exported from `src/doc3gpp/__init__.py` via
`__all__ = ["__version__"]` — do **not** change it to a private
`from doc3gpp._internal import _version` style import, since the
public surface is the contract the web global and the CLI callback
both depend on.

### 2. Web UI footer — every page

**Where:**

- `src/doc3gpp/web/templates_setup.py` — add one Jinja global.
- `src/doc3gpp/web/templates/base.html` — extend the existing footer
  line.

**Why a Jinja global (not a route-level `context=` argument):** 19
routes currently call `templates.TemplateResponse(...)` with their own
`context` dict (landing.py:79, jobs.py × 3, sync.py × 2, search.py × 2,
specs.py × 2, tsgs.py × 2, tdocs.py × 3, wis.py × 1, meetings.py × 2).
Plumbing `version` through every call site is high-churn and easy to
miss. A global is set once at startup, read everywhere, and matches
the existing pattern for `url_for`, `status_color_class`, etc.
(`templates_setup.py:39, 136`).

**Shape:**

```python
# templates_setup.py
from doc3gpp import __version__ as _APP_VERSION
templates.env.globals["app_version"] = _APP_VERSION
```

```html
<!-- base.html footer -->
<footer class="footer">
  <span>doc3gpp web · version {{ app_version }} · {% block footer_text %}read-only interface{% endblock %}</span>
</footer>
```

**Renders as:** `doc3gpp web · version 0.1.4 · read-only interface`

The footer block is inherited by every page that uses
`{% extends "base.html" %}` — i.e. the entire web UI, including the
landing page, all list pages, all show pages, and the job status
partial. Verified: all 15 templates in `src/doc3gpp/web/templates/`
(landing, meetings × 2, tdocs × 3, specs × 2, tsgs × 2, wis × 1,
search × 1, sync × 1, jobs × 1, job_status) extend `base.html`. The
`footer_text` block is defined only in `base.html` and has no
overrides anywhere, so adding `version {{ app_version }}` to the
static portion of that line is safe — no template can clobber it
without explicitly overriding the `footer_text` block, and none do
today.

**JSON routes:** routes that accept `?format=json` (e.g. `landing.py:77`
`return JSONResponse(content={"sections": ...})`) are unchanged. The
JSON output is a contract for machine clients and adding a top-level
`version` key would be a backwards-incompatible shape change. Machine
clients can read the version from the standard MCP `serverInfo`
handshake (see §3) or, if a future task needs it, a dedicated
`GET /info` route (out of scope here).

### 3. MCP `serverInfo.version`

**Where:** `src/doc3gpp/web/mcp_server.py:189`. Already wired:

```python
server = MCPServer(
    "doc3gpp",
    version=_package_version(),  # ← already calls this
    title="doc3gpp MCP",
    description="...",
    website_url="https://github.com/jerrywang121/doc3gpp",
)
```

`_package_version()` (web/mcp_server.py:57) prefers
`importlib.metadata.version("doc3gpp")` and falls back to
`doc3gpp.__version__`. The MCP SDK reflects this into the standard
`serverInfo` block of every `initialize` response, so every MCP
client (Claude Desktop, mcp-cli, custom JSON-RPC clients) can read it
without a tool call.

**No code change on the MCP side.** A short note is added to
`docs/web-server.md` under the "MCP server" section so the contract
is documented. The existing `tests/integration/test_mcp_end_to_end.py`
parity test continues to cover the surface.

## Data flow

```
   pyproject.toml [project] version
             │
             │  (release flow, manual sync)
             ▼
   doc3gpp/__init__.py::__version__     ← single source of truth
             │
             ├──► cli.py::_version_callback ─► stdout (CLI)
             │
             ├──► web/templates_setup.py (app_version global)
             │         │
             │         ▼
             │    base.html footer ─► every web page
             │
             └──► web/mcp_server.py::_package_version ─► MCPServer(...)
                          │
                          ▼
                     serverInfo.version (every MCP initialize)
```

All three surfaces read the same `__version__` constant. The web
Jinja global is read once at startup; the CLI and MCP callbacks read
it on demand.

## Testing

### CLI (`tests/unit/test_cli_version.py`, new file)

Use the `typer.testing.CliRunner` pattern already used in
`tests/unit/test_tdoc_parse_cli.py`:

- `runner.invoke(app, ["--version"])` → `exit_code == 0`,
  `result.stdout == f"doc3gpp {__version__}\n"`.
- `runner.invoke(app, ["--version", "meeting", "list"])` → exit 0,
  same stdout; trailing args ignored.
- `runner.invoke(app, ["--help"])` → exit 0; the help text contains
  the literal string `--version`.
- `runner.invoke(app, [])` → unchanged from today (prints help, exits
  0 or 2 — same as the current behaviour for the bare root app).
- Regression: an existing test that uses
  `tdoc list --version 17%` or `tdoc parse --version 17%` (e.g. in
  `tests/unit/test_tdoc_parse_cli.py:1277, 1308, 1320, 1357`) must
  still pass. The eager callback is gated on the *top-level* `--version`
  only — the per-command flag, which is a different Typer `Option` on
  the sub-command's function, is unaffected.

### Web (`tests/unit/web/test_landing_version.py`, new file)

Mirror the FastAPI `TestClient` patterns used elsewhere in
`tests/unit/web/`:

- `client.get("/")` → 200, body contains `f"version {__version__}"`.
- `client.get("/meetings")` (or any other page) → 200, body contains
  `f"version {__version__}"` (footer is in the base template).
- `client.get("/?format=json")` → 200, JSON body is unchanged (still
  `{"sections": [...]}` only).

### MCP

No new test. The existing
`tests/integration/test_mcp_end_to_end.py` exercises the `initialize`
handshake; the `version` field is already populated and this design
touches no tool bodies. The suite is re-run as part of the regular
verification step to confirm no regression.

## Documentation

- `docs/cli.md` — add to the "Global flags" section (or equivalent):
  - `--version` — print `doc3gpp <version>` and exit. Top-level only;
    takes precedence over sub-command arguments when supplied.
- `docs/web-server.md` — under "MCP server" add a one-liner:
  - The MCP `serverInfo` block returned on every `initialize` handshake
    carries `name` ("doc3gpp"), `version` (from
    `importlib.metadata.version("doc3gpp")`, falling back to
    `doc3gpp.__version__`), `title`, `description`, and `website_url`.
- `AGENTS.md` — no change (project guide, not user-facing reference).
- `README.md` — no change unless the Quick start lists flags; it
  currently does not.

## Failure modes

- **`__version__` removed in a future refactor:** the CLI callback and
  the `templates_setup` import both raise `ImportError` / `AttributeError`
  on startup. This is the same blast radius as today (the constant is
  already exported via `__all__`), so no new failure surface is
  introduced. A guard around the import would hide a real bug.
- **Installed distribution out of sync with `__version__`:** possible
  in a broken sdist build. The MCP path (`_package_version` prefers
  `importlib.metadata`) and the CLI/web path (read `__version__`
  directly) would disagree. Out of scope to detect — the release flow
  already keeps them in lockstep, and the divergence would show up
  immediately in any smoke test.
- **MCP `serverInfo.version` regression:** the `test_mcp_end_to_end.py`
  parity test already asserts tool outputs byte-match the HTTP
  `?format=json` routes. Since no tool body changes, no regression
  risk beyond an unrelated SDK upgrade. Verified by re-running the
  suite.

## Rollout

- No migration. No DB schema change. No settings change.
- One source change to `__init__.py` is **not** part of this design —
  `__version__` stays at `0.1.4` until the next release bumps it.

## Open questions

None.
