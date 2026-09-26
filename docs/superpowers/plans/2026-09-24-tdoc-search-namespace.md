# TDoc Search Namespace Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move TDoc full-text, semantic, and index-maintenance search from generic public namespaces to consistent TDoc-scoped CLI, HTTP, and MCP namespaces without changing search behavior or storage.

**Architecture:** Keep `SearchService`, `SemanticSearchService`, FTS5/vector repositories, database tables, `JobKind.REBUILD_SEARCH`, and worker handlers unchanged. Rename only public adapters and references: mount the existing CLI commands under `tdoc search`, expose the existing web handlers at `/tdocs/search` and `/tdocs/search/sem`, move the rebuild endpoint under `/jobs/tdocs/search/rebuild`, and register singular TDoc MCP tool names. Register exact TDoc search routes before the dynamic `/tdocs/{tdoc_id}` routes.

**Tech Stack:** Python 3.10+, Typer, FastAPI, Jinja2, HTMX, JavaScript, MCP server, pytest, Ruff.

## Global Constraints

- This is a hard namespace migration: remove old CLI, HTTP, and MCP names; add no aliases, redirects, or deprecation shims.
- Preserve existing flags, query parameters, JSON envelopes, HTML/HTMX behavior, errors, job payloads, service calls, ranking, and index behavior.
- Keep spec-document search names unchanged: `doc3gpp spec doc search`, `/spec-docs/search`, `/spec-docs/search/sem`, `search_spec_docs`, and `semantic_search_spec_docs`.
- Do not modify FTS5/vector schemas, storage tables, repository contracts, or reindex data.
- Use `apply_patch` for manual edits and run shell commands with the repository's `rtk` prefix.
- Update current documentation and examples; historical design documents remain historical unless they claim to describe the current public contract.

---

## File Map

### CLI surface

- Modify `src/doc3gpp/cli.py`: replace top-level `search_app` registration with `tdoc_search_app` mounted on `tdoc_app`; update command help and user-facing rebuild hints.
- Modify `tests/unit/test_cli_search.py`: move CLI invocations to `tdoc search` and add top-level-removal assertions.
- Modify `tests/unit/test_cli_search_sem.py`, `tests/unit/test_packaging_semantic.py`, and `tests/integration/test_search_filters.py`: update command invocation paths.

### HTTP surface

- Modify `src/doc3gpp/web/routes/search.py`: change the route prefix to `/tdocs/search` and current-route docstrings.
- Modify `src/doc3gpp/web/routes/__init__.py`: mount the search router before `tdocs_router` or otherwise ensure exact routes precede `/tdocs/{tdoc_id}`.
- Modify `src/doc3gpp/web/routes/jobs.py`: rename only the rebuild endpoint path to `/jobs/tdocs/search/rebuild`.
- Modify `src/doc3gpp/web/routes/landing.py`, `src/doc3gpp/web/templates/base.html`, `src/doc3gpp/web/templates/search_results.html`, `src/doc3gpp/web/templates/partials/search_form.html`, and `src/doc3gpp/web/templates/sync.html`: update links, HTMX actions, navigation, and rebuild form action.
- Modify `tests/unit/test_web_routes.py`, `tests/integration/test_web_search_end_to_end.py`, `tests/unit/test_web_jobs_routes.py`, and `tests/integration/test_sync_hub_end_to_end.py`: update paths and add old-route/precedence checks.

### MCP surface

- Modify `src/doc3gpp/web/mcp_server.py`: rename the three TDoc search tool registrations and local functions without changing signatures or behavior.
- Modify `tests/integration/test_mcp_end_to_end.py`: update discovery and tool-call names; assert old names are absent.

### Documentation and internal user-facing hints

- Modify `README.md`, `AGENTS.md`, `docs/cli.md`, `docs/architecture.md`, `docs/web-server.md`, `docs/code-map.md`, and `docs/conventions.md` to use the new current namespaces.
- Modify current source hints in `src/doc3gpp/models/search.py`, `src/doc3gpp/services/semantic_search_service.py`, `src/doc3gpp/storage/repositories/vector_sql.py`, and any current `src/doc3gpp` docstrings that mention executable old commands.
- Do not rewrite historical files under `docs/superpowers/specs/` except the approved migration spec and implementation plan.

---

### Task 1: Move CLI Search Under `tdoc`

**Files:**
- Modify: `src/doc3gpp/cli.py:114-135, 5696-6187, 6310-6330`
- Test: `tests/unit/test_cli_search.py`
- Test: `tests/unit/test_cli_search_sem.py`
- Test: `tests/unit/test_packaging_semantic.py`
- Test: `tests/integration/test_search_filters.py`

**Interfaces:**
- Consumes the existing `search_command`, `sem_command`, and `index_command` implementations and their existing service/repository behavior.
- Produces a `tdoc_search_app = typer.Typer(...)` mounted by `tdoc_app.add_typer(tdoc_search_app, name="search")`; the command paths become `tdoc search query`, `tdoc search sem`, and `tdoc search index`.

- [ ] **Step 1: Update CLI tests to the new command path and add removal coverage.**

  Replace every `CliRunner` argument prefix `['search', ...]` with `['tdoc', 'search', ...]` in the four listed test modules. In `tests/unit/test_cli_search.py`, add a test that invokes `app --help` and asserts `search` is absent from the top-level command list, and a test that invokes `app search query --help` and asserts a nonzero exit code. Keep the existing flag assertions and service behavior tests unchanged apart from the path.

- [ ] **Step 2: Run the focused CLI tests and verify they fail for the expected registration reason.**

  Run:

  ```bash
  rtk python -m pytest tests/unit/test_cli_search.py tests/unit/test_cli_search_sem.py tests/unit/test_packaging_semantic.py tests/integration/test_search_filters.py -q
  ```

  Expected: the updated tests fail because `tdoc search` is not registered yet and the old path is still present.

- [ ] **Step 3: Replace the top-level Typer registration.**

  In `src/doc3gpp/cli.py`, declare the search group beside the other sub-apps:

  ```python
  tdoc_search_app = typer.Typer(help="search over stored TDocs and TDoc sidecars")
  ```

  Remove:

  ```python
  search_app = typer.Typer(help="full-text search over TDocs, CRs, meetings, and WIs")
  app.add_typer(search_app, name="search")
  ```

  Add immediately after the `tdoc_app` registration:

  ```python
  tdoc_app.add_typer(tdoc_search_app, name="search")
  ```

  Change the decorators only:

  ```python
  @tdoc_search_app.command("query")
  def search_command(...):
      ...

  @tdoc_search_app.command("index")
  def index_command(...):
      ...

  @tdoc_search_app.command("sem")
  def sem_command(...):
      ...
  ```

  Update user-facing error/help strings from `doc3gpp search index ...` to `doc3gpp tdoc search index ...`; do not change internal function names or service calls.

- [ ] **Step 4: Run the focused CLI tests and inspect help output.**

  Run:

  ```bash
  rtk python -m pytest tests/unit/test_cli_search.py tests/unit/test_cli_search_sem.py tests/unit/test_packaging_semantic.py tests/integration/test_search_filters.py -q
  rtk python -m doc3gpp.cli --help
  rtk python -m doc3gpp.cli tdoc search --help
  ```

  Expected: focused tests pass; top-level help has no `search` group; TDoc search help lists `query`, `sem`, and `index`; `doc3gpp tdoc search index --help` shows the existing rebuild flags.

- [ ] **Step 5: Commit the CLI namespace migration.**

  ```bash
  rtk git add src/doc3gpp/cli.py tests/unit/test_cli_search.py tests/unit/test_cli_search_sem.py tests/unit/test_packaging_semantic.py tests/integration/test_search_filters.py
  rtk git commit -m "refactor(cli): scope search commands under tdoc"
  ```

### Task 2: Move HTTP Search Routes And Rebuild Job

**Files:**
- Modify: `src/doc3gpp/web/routes/search.py:1-37`
- Modify: `src/doc3gpp/web/routes/__init__.py:15-42`
- Modify: `src/doc3gpp/web/routes/jobs.py:236-245`
- Modify: `src/doc3gpp/web/routes/landing.py:57-61`
- Modify: `src/doc3gpp/web/templates/base.html:20`
- Modify: `src/doc3gpp/web/templates/search_results.html:7-11`
- Modify: `src/doc3gpp/web/templates/partials/search_form.html:1-6`
- Modify: `src/doc3gpp/web/templates/sync.html:151-165`
- Test: `tests/unit/test_web_routes.py`
- Test: `tests/integration/test_web_search_end_to_end.py`
- Test: `tests/unit/test_web_jobs_routes.py`
- Test: `tests/integration/test_sync_hub_end_to_end.py`

**Interfaces:**
- Consumes the existing `SearchService`, `SemanticSearchService`, and `REBUILD_SEARCH` job behavior.
- Produces `GET /tdocs/search`, `GET /tdocs/search/sem`, and `POST /jobs/tdocs/search/rebuild`; old paths are unregistered.

- [ ] **Step 1: Update HTTP tests to new paths and add route-removal/precedence assertions.**

  Replace `/search` with `/tdocs/search`, `/search/sem` with `/tdocs/search/sem`, and `/jobs/search/rebuild` with `/jobs/tdocs/search/rebuild` in the listed current tests and descriptions. Update the expected landing/nav href from `/search` to `/tdocs/search`. Add assertions that:

  ```python
  assert client.get("/search").status_code == 404
  assert client.get("/search/sem").status_code == 404
  assert client.post("/jobs/search/rebuild", json={}).status_code == 404
  ```

  Add a route-precedence test using the existing web test client and dependency overrides: `GET /tdocs/search?format=json` must be handled by the search route (not TDoc detail) and return the search JSON shape, even though `/tdocs/{tdoc_id}` exists. The existing fake search service in `tests/unit/test_web_routes.py` is sufficient; assert the response is a JSON list and does not contain a TDoc-detail envelope.

- [ ] **Step 2: Run focused HTTP tests to capture the expected failures.**

  Run:

  ```bash
  rtk python -m pytest tests/unit/test_web_routes.py tests/integration/test_web_search_end_to_end.py tests/unit/test_web_jobs_routes.py tests/integration/test_sync_hub_end_to_end.py -q
  ```

  Expected: updated tests fail until the route prefix, router order, job path, and template references are changed.

- [ ] **Step 3: Change the read-route prefix and ensure exact-route precedence.**

  In `src/doc3gpp/web/routes/search.py`, change:

  ```python
  router = APIRouter(prefix="/tdocs/search", tags=["search"])
  ```

  Update the module docstring and CLI-parity docstrings from `/search` and `doc3gpp search ...` to the new TDoc-scoped names. Do not alter handler signatures, query aliases, response shaping, template names, or dependencies.

  In `src/doc3gpp/web/routes/__init__.py`, return `search_router` before `tdocs_router` so the search router is mounted before the dynamic TDoc router. Preserve all other router ordering unless the tests demonstrate a conflict.

- [ ] **Step 4: Change the rebuild endpoint path without changing job behavior.**

  In `src/doc3gpp/web/routes/jobs.py`, change only the decorator:

  ```python
  @router.post("/tdocs/search/rebuild", status_code=202)
  ```

  Keep `post_search_rebuild`, `_SearchRebuildBody`, `JobKind.REBUILD_SEARCH`, and the `{"stale_only": ..., "resume": ...}` payload unchanged.

- [ ] **Step 5: Update current web links, forms, and navigation.**

  Change only TDoc search references:

  ```jinja2
  {# partials/search_form.html #}
  hx-get="{{ '/tdocs/search/sem' if mode == 'sem' else '/tdocs/search' }}"

  {# search_results.html #}
  href="/tdocs/search"
  href="/tdocs/search/sem"

  {# sync.html #}
  action="/jobs/tdocs/search/rebuild"
  ```

  Update `/search` to `/tdocs/search` in `landing.py` and `base.html`. Do not modify `/spec-docs/search` links in spec-document templates. Keep the search JavaScript unchanged because it has no route literals.

- [ ] **Step 6: Run focused HTTP tests and verify route behavior.**

  ```bash
  rtk python -m pytest tests/unit/test_web_routes.py tests/integration/test_web_search_end_to_end.py tests/unit/test_web_jobs_routes.py tests/integration/test_sync_hub_end_to_end.py -q
  ```

  Expected: all focused HTTP tests pass; exact TDoc search routes return their existing HTML/HTMX/JSON behavior; old paths return 404; rebuild posts create `REBUILD_SEARCH` jobs with unchanged params.

- [ ] **Step 7: Commit the HTTP namespace migration.**

  ```bash
  rtk git add src/doc3gpp/web/routes/search.py src/doc3gpp/web/routes/__init__.py src/doc3gpp/web/routes/jobs.py src/doc3gpp/web/routes/landing.py src/doc3gpp/web/templates/base.html src/doc3gpp/web/templates/search_results.html src/doc3gpp/web/templates/partials/search_form.html src/doc3gpp/web/templates/sync.html tests/unit/test_web_routes.py tests/integration/test_web_search_end_to_end.py tests/unit/test_web_jobs_routes.py tests/integration/test_sync_hub_end_to_end.py
  rtk git commit -m "refactor(web): scope search routes under tdocs"
  ```

### Task 3: Rename MCP TDoc Search Tools

**Files:**
- Modify: `src/doc3gpp/web/mcp_server.py:689-741, 895-901`
- Test: `tests/integration/test_mcp_end_to_end.py`

**Interfaces:**
- Consumes the existing `SearchService`, `SemanticSearchService`, `_enqueue`, and `JobKind.REBUILD_SEARCH` behavior.
- Produces MCP tools named `search_tdoc`, `semantic_search_tdoc`, and `rebuild_tdoc_search_index` with the existing arguments and JSON result/error behavior.

- [ ] **Step 1: Update MCP discovery and call tests.**

  Replace the test function names, docstrings, and all current calls to `search_tdocs` with `search_tdoc`, `semantic_search_tdocs` with `semantic_search_tdoc`, and `rebuild_search_index` with `rebuild_tdoc_search_index` in `tests/integration/test_mcp_end_to_end.py`. Change the discovery set to require the new names and explicitly assert:

  ```python
  assert "search_tdocs" not in names
  assert "semantic_search_tdocs" not in names
  assert "rebuild_search_index" not in names
  ```

  Keep the existing normalization, stopword error, semantic rerank, and job-payload assertions.

- [ ] **Step 2: Run the focused MCP tests and verify expected failures.**

  ```bash
  rtk python -m pytest tests/integration/test_mcp_end_to_end.py -q
  ```

  Expected: updated calls fail because the old tool registrations are still active.

- [ ] **Step 3: Rename only the MCP registrations and local function names.**

  In `src/doc3gpp/web/mcp_server.py`, change the decorators and function names:

  ```python
  @server.tool(name="search_tdoc", ...)
  def search_tdoc(...):
      ...

  @server.tool(name="semantic_search_tdoc", ...)
  def semantic_search_tdoc(...):
      ...

  @server.tool(name="rebuild_tdoc_search_index", ...)
  def rebuild_tdoc_search_index(...):
      return _enqueue(
          state,
          JobKind.REBUILD_SEARCH,
          {"stale_only": stale_only, "resume": resume},
          "queued rebuild_tdoc_search_index",
      )
  ```

  Preserve all parameter annotations, service calls, `_mcp_error_guard`, result conversion, and job kind. Update the queue message to the new tool name; the job payload remains unchanged.

- [ ] **Step 4: Run MCP tests and verify old names are absent.**

  ```bash
  rtk python -m pytest tests/integration/test_mcp_end_to_end.py -q
  ```

  Expected: all MCP discovery and call tests pass, and old names are rejected as unknown tools.

- [ ] **Step 5: Commit the MCP namespace migration.**

  ```bash
  rtk git add src/doc3gpp/web/mcp_server.py tests/integration/test_mcp_end_to_end.py
  rtk git commit -m "refactor(mcp): scope search tools under tdoc"
  ```

### Task 4: Update Current Documentation And User-Facing Hints

**Files:**
- Modify: `README.md:324-424`
- Modify: `AGENTS.md:342-375`
- Modify: `docs/cli.md` search sections and command examples
- Modify: `docs/architecture.md:672-708` and current search route/MCP sections
- Modify: `docs/web-server.md:193-212, 296-320, 393-457`
- Modify: `docs/code-map.md:207-284`
- Modify: `docs/conventions.md:239-252`
- Modify: `src/doc3gpp/models/search.py:43-49`
- Modify: `src/doc3gpp/services/semantic_search_service.py:134-137`
- Modify: `src/doc3gpp/storage/repositories/vector_sql.py:105-145`
- Modify: current source docstrings containing executable old search commands

**Interfaces:**
- Consumes the completed CLI, HTTP, and MCP public contracts from Tasks 1–3.
- Produces documentation and runtime hints that name only the new current namespaces.

- [ ] **Step 1: Replace current CLI examples and hints.**

  Use these exact command prefixes in current documentation and user-facing source messages:

  ```text
  doc3gpp tdoc search query
  doc3gpp tdoc search sem
  doc3gpp tdoc search index
  doc3gpp tdoc search index --rebuild-embeddings
  ```

  Update current section headings, command inventories, and prose so generic “search” descriptions say “TDoc search” where the command/resource is meant. Update the current `docs/code-map.md` CLI inventory to list search as nested `tdoc` commands and its `all_routers` inventory to put the search router before the dynamic TDoc router. Do not change `doc3gpp spec doc search ...`.

- [ ] **Step 2: Replace current HTTP and MCP references.**

  In `docs/web-server.md`, `docs/code-map.md`, and current guides, document:

  ```text
  GET  /tdocs/search
  GET  /tdocs/search/sem
  POST /jobs/tdocs/search/rebuild
  search_tdoc
  semantic_search_tdoc
  rebuild_tdoc_search_index
  ```

  Explicitly state that the old routes/tools are removed and that spec-document search remains under its existing names.

- [ ] **Step 3: Search current files for stale public names.**

  Run:

  ```bash
  rtk grep "doc3gpp search|/jobs/search/rebuild|search_tdocs|semantic_search_tdocs|rebuild_search_index" src README.md AGENTS.md docs/cli.md docs/architecture.md docs/web-server.md docs/code-map.md docs/conventions.md
  ```

  Expected: no stale public references outside historical design documents and the approved migration spec/plan. Distinguish internal `SearchService`, `tdoc_search`, and `JobKind.REBUILD_SEARCH` names, which must remain.

- [ ] **Step 4: Run documentation quality checks.**

  ```bash
  rtk git diff --check
  ```

- [ ] **Step 5: Commit current documentation and hints.**

  ```bash
  rtk git add README.md AGENTS.md docs/cli.md docs/architecture.md docs/web-server.md docs/code-map.md docs/conventions.md src/doc3gpp/models/search.py src/doc3gpp/services/semantic_search_service.py src/doc3gpp/storage/repositories/vector_sql.py
  rtk git commit -m "docs: document tdoc search namespaces"
  ```

### Task 5: Full Verification And Final Review

**Files:**
- Test: all files changed by Tasks 1–4

**Interfaces:**
- Consumes the completed public-surface migration and documentation.
- Produces evidence that old names are absent, new names work, spec-document surfaces are unchanged, and search internals remain functional.

- [ ] **Step 1: Run focused regression suites together.**

  ```bash
  rtk python -m pytest tests/unit/test_cli_search.py tests/unit/test_cli_search_sem.py tests/unit/test_packaging_semantic.py tests/integration/test_search_filters.py tests/unit/test_web_routes.py tests/integration/test_web_search_end_to_end.py tests/unit/test_web_jobs_routes.py tests/integration/test_sync_hub_end_to_end.py tests/integration/test_mcp_end_to_end.py -q
  ```

  Expected: all focused tests pass.

- [ ] **Step 2: Verify command and route rejection manually.**

  ```bash
  rtk python -m doc3gpp.cli --help
  rtk python -m doc3gpp.cli tdoc search --help
  rtk python -m doc3gpp.cli search --help
  ```

  Expected: top-level help omits `search`; TDoc help includes `search`; old top-level invocation exits nonzero.

- [ ] **Step 3: Run the full offline test and lint gates.**

  ```bash
  rtk ./scripts/test_sqlite.sh
  rtk ruff check .
  rtk git diff --check
  ```

  Expected: SQLite suite passes, Ruff reports no errors, and diff check is clean.

- [ ] **Step 4: Inspect the final diff and status.**

  ```bash
  rtk git status --short
  rtk git diff HEAD~4..HEAD --stat
  rtk git log --oneline -10
  ```

  Confirm only intended source, tests, and current documentation changed; no database files or unrelated worktree changes are staged.

- [ ] **Step 5: Record the completed migration.**

  Update the session progress record with the final test counts and any environment limitations. Do not claim completion until the fresh verification commands above have returned successfully.
