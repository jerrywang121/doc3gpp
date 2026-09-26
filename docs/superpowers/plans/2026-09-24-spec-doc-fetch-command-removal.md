# Spec-Document Fetch Command Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the redundant public `doc3gpp spec doc fetch` command while preserving internal fetch-if-missing behavior and `parse --force` re-download plus re-parse semantics across CLI, web, MCP, tests, and current documentation.

**Architecture:** The public spec-document workflow will begin at `doc3gpp spec doc parse`. `SpecDocService.parse_many()` and `SpecDocService.parse()` remain unchanged as the orchestration path, and `SpecDocService.fetch()` remains an internal cache/download helper called by `parse()`. Web and MCP already expose parse jobs rather than standalone fetch operations; regression tests will make that surface contract explicit.

**Tech Stack:** Python 3.10+, Typer, FastAPI, MCP server, pytest, SQLite integration fixtures, Markdown documentation, Ruff.

## Global Constraints

- Remove the public `doc3gpp spec doc fetch` command with no alias, redirect, or deprecation shim.
- Keep `SpecDocService.fetch()` and `fetch_spec_doc_zip()`; `parse()` continues to use the internal fetch step.
- Preserve `doc3gpp spec doc parse --force` as a re-download followed by re-parse, not a cache-only re-parse.
- Preserve ZIP cache layout, version resolution, source/download recording, size-limit handling, parse job payloads, progress behavior, and error mapping.
- Keep web and MCP parse surfaces unchanged; expose no standalone spec-document fetch route or tool.
- Update current-contract documentation only; historical design and implementation-plan documents remain unchanged.
- Use `apply_patch` for manual edits and prefix repository shell commands with `rtk`.
- Verify focused tests, the full offline SQLite suite, Ruff, and `git diff --check` before completion.

---

## File Map

### CLI adapter and CLI regression coverage

- Modify `src/doc3gpp/cli.py`: delete the `spec_doc_fetch` command implementation and make the remaining parse force option describe its preserved re-download behavior.
- Modify `tests/integration/test_spec_doc_cli.py`: assert `spec doc --help` has no `fetch` command and `spec doc fetch` is rejected.
- Verify `tests/integration/test_spec_doc_service.py`: retain the existing cache-hit and `test_force_reparses_and_redownloads` coverage; do not remove service-level fetch tests merely because the CLI adapter is gone.

### Web and MCP public-surface regression coverage

- Modify `tests/integration/test_spec_doc_web.py`: assert the parse job route remains available and no standalone spec-document fetch route exists.
- Modify `tests/integration/test_mcp_end_to_end.py`: include `parse_spec_docs` in the discovered spec-document job tools and assert no `fetch_spec*` tool is registered.
- Do not modify `src/doc3gpp/web/routes/jobs.py`, `src/doc3gpp/web/workers/handlers.py`, or `src/doc3gpp/web/mcp_server.py`; their current parse-only behavior is the contract under test.

### Current documentation

- Modify `README.md`: remove download-only examples and describe parsing as fetch-if-missing; document that `--force` re-downloads and re-parses.
- Modify `AGENTS.md`: remove `fetch` from the command inventory and describe the internal fetch step under the parse workflow.
- Modify `docs/cli.md`: remove the standalone fetch section and move its relevant cache/version/force details into the parse section.
- Modify `docs/architecture.md`: change the corpus flow from `fetch -> parse` to `parse -> TOC -> search`, while documenting that parse internally fetches the ZIP.
- Do not modify `docs/superpowers/specs/2026-09-23-spec-doc-parse-design.md` or `docs/superpowers/plans/2026-09-23-spec-doc-parse.md`; those are historical implementation records.

---

### Task 1: Remove the public CLI adapter

**Files:**
- Modify: `src/doc3gpp/cli.py:4604-4634` (`spec_doc_fetch`) and `src/doc3gpp/cli.py:4650-4652` (parse force help)
- Test: `tests/integration/test_spec_doc_cli.py`
- Verify: `tests/integration/test_spec_doc_service.py`

**Interfaces:**
- Consumes: existing Typer `spec_doc_app`, `SpecDocService.parse_many()`, and the existing `SpecDocService.fetch()` internal method.
- Produces: `doc3gpp spec doc parse` as the first supported acquisition/processing command; `doc3gpp spec doc fetch` is an unknown command.

- [ ] **Step 1: Add the failing CLI-removal regression test**

Append this test to `tests/integration/test_spec_doc_cli.py`:

```python

def test_spec_doc_fetch_command_is_removed(sqlite_env):
    create_schema("all")
    runner = CliRunner()

    help_result = runner.invoke(app, ["spec", "doc", "--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "fetch" not in help_result.output

    fetch_result = runner.invoke(
        app,
        ["spec", "doc", "fetch", "--spec", "38.331"],
    )
    assert fetch_result.exit_code != 0
    assert "No such command" in fetch_result.output
    assert "fetch" in fetch_result.output
```

- [ ] **Step 2: Run the new test to verify it fails before the implementation change**

Run:

```bash
rtk python -m pytest tests/integration/test_spec_doc_cli.py::test_spec_doc_fetch_command_is_removed -q
```

Expected: FAIL because the current `fetch` command is still listed and registered.

- [ ] **Step 3: Delete the public command and clarify the preserved force behavior**

Remove the complete `spec_doc_fetch` function, including its `@spec_doc_app.command("fetch")` decorator, from `src/doc3gpp/cli.py`.

Change the parse option help from:

```python
help="Re-parse already-parsed pairs.",
```

to:

```python
help="Re-download and re-parse already-parsed pairs.",
```

Do not edit `SpecDocService.fetch()`, `SpecDocService.parse()`, or `SpecDocService.parse_many()`. The existing call chain must remain:

```python
src = self.fetch(spec_id, release=release, version=version, force=force)
```

- [ ] **Step 4: Run the CLI and service regression tests**

Run:

```bash
rtk python -m pytest \
  tests/integration/test_spec_doc_cli.py \
  tests/integration/test_spec_doc_service.py -q
```

Expected: PASS, including the existing `test_force_reparses_and_redownloads` test, which must still observe two downloader calls after one normal parse and one `force=True` parse.

- [ ] **Step 5: Commit the CLI change**

```bash
rtk git add src/doc3gpp/cli.py tests/integration/test_spec_doc_cli.py
rtk git commit -m "refactor(cli): remove public spec doc fetch command"
```

---

### Task 2: Lock the web and MCP parse-only surfaces

**Files:**
- Modify: `tests/integration/test_spec_doc_web.py`
- Modify: `tests/integration/test_mcp_end_to_end.py`
- Verify only: `src/doc3gpp/web/routes/jobs.py:291-310`, `src/doc3gpp/web/workers/handlers.py:447-500`, `src/doc3gpp/web/mcp_server.py:872-894`

**Interfaces:**
- Consumes: `POST /jobs/parse/spec-docs`, `JobKind.PARSE_SPEC_DOCS`, `_parse_spec_docs`, and MCP tool `parse_spec_docs`.
- Produces: regression coverage proving parsing remains public while standalone spec-document fetching is absent from web and MCP.

- [ ] **Step 1: Add web route regression assertions**

Append this test to `tests/integration/test_spec_doc_web.py`:

```python

def test_spec_doc_parse_job_exists_without_fetch_route(sqlite_env):
    create_schema("all")
    with TestClient(build_app(get_settings()), raise_server_exceptions=False) as client:
        parse_response = client.post(
            "/jobs/parse/spec-docs",
            json={"spec_ids": []},
        )
        fetch_response = client.get("/spec-docs/fetch")

    assert parse_response.status_code == 400
    assert "parse/spec-docs" in parse_response.json()["detail"]
    assert fetch_response.status_code == 404
```

The invalid empty list intentionally stops before job creation while proving that the parse route is registered. The fetch URL must not be introduced as a route.

- [ ] **Step 2: Run the web test to verify the regression assertions**

Run:

```bash
rtk python -m pytest tests/integration/test_spec_doc_web.py -q
```

Expected: PASS. No web production edit is required because the existing route set already exposes parse, TOC, search, and schema only.

- [ ] **Step 3: Extend MCP discovery coverage**

In `test_list_tools_exposes_read_and_job_tools` in `tests/integration/test_mcp_end_to_end.py`, add `parse_spec_docs` to the `expected` set and add these assertions after the existing TDoc search absence assertions:

```python
assert "parse_spec_docs" in names
assert not any(name.startswith("fetch_spec") for name in names)
```

This checks both halves of the public contract without changing the existing `parse_spec_docs` tool implementation or its `{spec_ids, release?, version?, force}` job payload.

- [ ] **Step 4: Run the MCP discovery regression**

Run:

```bash
rtk python -m pytest tests/integration/test_mcp_end_to_end.py::test_list_tools_exposes_read_and_job_tools -q
```

Expected: PASS with `parse_spec_docs` discovered and no `fetch_spec*` tool discovered.

- [ ] **Step 5: Commit the surface-coverage tests**

```bash
rtk git add tests/integration/test_spec_doc_web.py tests/integration/test_mcp_end_to_end.py
rtk git commit -m "test: cover spec doc parse-only public surfaces"
```

---

### Task 3: Update current CLI and architecture documentation

**Files:**
- Modify: `README.md:269-289`
- Modify: `AGENTS.md:81,376`
- Modify: `docs/cli.md:2453-2500` and the parse behavior immediately below it
- Modify: `docs/architecture.md:23,599-630`

**Interfaces:**
- Consumes: the final CLI contract from Task 1 and the unchanged parse routes/tools from Task 2.
- Produces: current documentation that starts the spec-document workflow at `parse` and never instructs users to run the removed command.

- [ ] **Step 1: Remove download-only README examples**

Replace the README block beginning with:

```text
# fetch only; defaults to the numerically newest stored version
doc3gpp spec doc fetch --spec 38.331
doc3gpp spec doc fetch --spec 38.523-1 --release Rel-18
```

with a parse-first example and explicit force semantics:

```text
# fetch-if-missing, convert every .docx, chunk, and auto-index
doc3gpp spec doc parse --spec 38.331 --spec 38.523-1
doc3gpp spec doc parse --spec 38.331 --force  # re-download and re-parse
```

Keep the TOC, search, semantic-search, and schema examples unchanged.

- [ ] **Step 2: Rewrite the AGENTS command inventory and workflow note**

Change the command inventory from:

```text
spec doc fetch/parse/toc show/search query/search sem/schema
```

to:

```text
spec doc parse/toc show/search query/search sem/schema
```

Replace the standalone fetch workflow bullet with wording that says `spec doc parse` resolves the numeric-newest or pinned version, fetches the ZIP when absent, records the download, and that `--force` re-downloads before re-parsing. Keep the internal `resolve_spec_doc_version` and `fetch_spec_doc_zip` references because they describe implementation internals.

- [ ] **Step 3: Remove the `docs/cli.md` fetch section and fold behavior into parse**

Change the sub-app inventory to:

```text
The `spec doc` sub-app (`spec doc parse/toc show/search query/search sem/schema`
under `spec`) exposes the spec-document corpus: downloaded spec version zips parsed
into chunk rows + a per-version TOC, searchable via FTS5 or hybrid vector search.
```

Change the version paragraph to say `parse` picks the numeric-newest stored `SpecVersion.version` unless `--release` or `--version` pins one. Delete the complete `### doc3gpp spec doc fetch` section, including its options, behavior, and examples.

In the parse section, add these behavior bullets before the existing parse pipeline details:

```text
- Resolves the version via `resolve_spec_doc_version`; parse fetches the ZIP when
  it is missing and records `spec_doc_sources.downloaded_at` before conversion.
- `--force` re-downloads the resolved ZIP and then re-parses it, including when
  the `(spec_id, version)` pair already has `parsed_at`.
```

- [ ] **Step 4: Update the architecture workflow**

Change the top-level command inventory at `docs/architecture.md` from `spec doc fetch / parse / ...` to `spec doc parse / ...`.

Change the section heading from:

```text
### Spec-document corpus (fetch → parse → TOC → search)
```

to:

```text
### Spec-document corpus (parse → TOC → search)
```

Delete the standalone fetch step and renumber the parse step to `1.`. In that parse step, retain the existing cache/version details as an internal sequence: `parse_many` calls `parse`, `parse` calls internal `fetch-if-missing`, and `--force` causes the internal fetch to re-download before parsing. Keep steps for TOC, FTS5 search, and semantic search unchanged apart from numbering.

- [ ] **Step 5: Check current documentation for removed public references**

Run:

```bash
rtk grep "spec doc fetch" README.md AGENTS.md docs/cli.md docs/architecture.md
```

Expected: no output. References in `docs/superpowers/specs/` and `docs/superpowers/plans/` are historical or design records and are intentionally left unchanged.

- [ ] **Step 6: Commit the documentation update**

```bash
rtk git add README.md AGENTS.md docs/cli.md docs/architecture.md
rtk git commit -m "docs: remove spec doc fetch command references"
```

---

### Task 4: Run the complete verification gate

**Files:**
- Verify: all files changed by Tasks 1-3
- No source or test edits unless a verification failure identifies a concrete regression

**Interfaces:**
- Consumes: the final CLI, web, MCP, service, and documentation contracts.
- Produces: evidence that the public command is removed, parse force behavior is intact, and the repository remains green.

- [ ] **Step 1: Verify CLI behavior directly**

Run:

```bash
rtk python -m doc3gpp spec doc --help
rtk python -m doc3gpp spec doc fetch --spec 38.331
```

Expected: help lists `parse`, `toc`, `search`, and `schema` surfaces but not `fetch`; the second command exits nonzero with Typer's unknown-command error.

- [ ] **Step 2: Run all focused spec-document tests**

Run:

```bash
rtk python -m pytest \
  tests/integration/test_spec_doc_cli.py \
  tests/integration/test_spec_doc_service.py \
  tests/integration/test_spec_doc_web.py \
  tests/integration/test_mcp_end_to_end.py -q
```

Expected: PASS, including CLI removal, web route, MCP discovery, cache-hit, normal parse, and force re-download/re-parse coverage.

- [ ] **Step 3: Run the full offline SQLite suite**

Run:

```bash
rtk ./scripts/test_sqlite.sh
```

Expected: the full offline suite passes; online tests are not required for this public-command removal.

- [ ] **Step 4: Run static and diff checks**

Run:

```bash
rtk ruff check .
rtk git diff --check
rtk git status --short --untracked-files=all
```

Expected: Ruff and diff checks are clean. The only intentional untracked artifact, if the plan is not committed, is this implementation-plan file.

- [ ] **Step 5: Review the final public-reference scan**

Run:

```bash
rtk grep "spec doc fetch" README.md AGENTS.md docs/cli.md docs/architecture.md
rtk grep "spec_doc_fetch" src tests README.md AGENTS.md docs/cli.md docs/architecture.md
```

Expected: no current public implementation or documentation references remain. The internal `SpecDocService.fetch` and `fetch_spec_doc_zip` names must remain and are not part of this scan's removal criterion.
