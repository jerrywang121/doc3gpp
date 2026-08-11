# Spec list: rich-text `rapporteurs` filter + display consistency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a rich-text `rapporteurs` filter to `spec list` end-to-end (repo → service → CLI → web → MCP) and add a visible Rapporteurs column to the web results table.

**Architecture:** A vertical slice following the existing `wis` filter precedent. Each layer adds a `rapporteurs: str | None = None` filter parameter and threads it through to the layer below, reusing the shared `apply_text_filter` rich grammar (`null` / `not-null` / `!pattern` / plain LIKE). The web HTML results table gains a Rapporteurs column for display consistency.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0, Typer, FastAPI, Jinja2, pytest.

## Global Constraints

- Ruff is the only linter; line length 100; target py310. Run `ruff check .` after each task.
- No comments unless non-obvious.
- Reuse the existing shared rich-filter grammar via `apply_text_filter` (from `doc3gpp.storage.repositories.rich_filters`) — do not invent a new grammar.
- `rapporteurs` is `String(128)`, nullable, comma-joined company names (e.g. `Ericsson LM`).
- Follow the existing `wis` filter pattern exactly (param name, ordering, help text style).
- Update `docs/cli.md` and `docs/code-map.md` in the same change set (documentation-sync convention).
- Test command for the full suite: `./scripts/test_sqlite.sh`. Per-file: `pytest <file> -v`.

---

### Task 1: Repository — add `rapporteurs` filter to `SpecRepository.list`

**Files:**
- Modify: `src/doc3gpp/repository/protocols.py:356-374` (`SpecRepository.list`)
- Modify: `src/doc3gpp/storage/repositories/spec_sql.py:99-132` (`SQLAlchemySpecRepository.list`)
- Test: `tests/integration/test_spec_sql.py`

**Interfaces:**
- Consumes: `apply_text_filter(stmt, column, value)` from `doc3gpp.storage.repositories.rich_filters` (already imported in `spec_sql.py`); `SpecORM.rapporteurs` column.
- Produces: `SpecRepository.list(..., rapporteurs: str | None = None)` and `SQLAlchemySpecRepository.list(..., rapporteurs: str | None = None)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_spec_sql.py`:

```python
def test_list_rapporteurs_filter(session_factory) -> None:
    repo = SQLAlchemySpecRepository(session_factory)
    repo.upsert(Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5", rapporteurs="Ericsson LM"))
    repo.upsert(Spec(spec_id="38.760-1", type="TR", title="Study", tsg="R5", rapporteurs="Nokia"))
    repo.upsert(Spec(spec_id="38.761-1", type="TR", title="Other", tsg="R5"))
    # LIKE
    assert [s.spec_id for s in repo.list(rapporteurs="%Ericsson%")] == ["36.579-5"]
    # negated
    assert [s.spec_id for s in repo.list(rapporteurs="!%Nokia%")] == ["36.579-5", "38.761-1"]
    # not-null
    assert [s.spec_id for s in repo.list(rapporteurs="not-null")] == ["36.579-5", "38.760-1"]
    # null
    assert [s.spec_id for s in repo.list(rapporteurs="null")] == ["38.761-1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_spec_sql.py::test_list_rapporteurs_filter -v`
Expected: FAIL — `TypeError: list() got an unexpected keyword argument 'rapporteurs'`.

- [ ] **Step 3: Add the param to the Protocol**

In `src/doc3gpp/repository/protocols.py`, add `rapporteurs: str | None = None,` to `SpecRepository.list` after the `wis` param (line 367):

```python
        wis: str | None = None,
        rapporteurs: str | None = None,
    ) -> list[Spec]:
```

- [ ] **Step 4: Add the param + filter to the SQL impl**

In `src/doc3gpp/storage/repositories/spec_sql.py`, add `rapporteurs: str | None = None,` to `list` after `wis` (line 110), and add the filter block after the `wis` block (line 129):

```python
            if wis:
                stmt = apply_text_filter(stmt, SpecORM.wis, wis)
            if rapporteurs:
                stmt = apply_text_filter(stmt, SpecORM.rapporteurs, rapporteurs)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/integration/test_spec_sql.py -v`
Expected: PASS (all tests in the file, including the new one).

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/repository/protocols.py src/doc3gpp/storage/repositories/spec_sql.py tests/integration/test_spec_sql.py
git commit -m "feat(spec): add rapporteurs filter to SpecRepository.list"
```

---

### Task 2: Service — thread `rapporteurs` through `SpecService.list_recent`

**Files:**
- Modify: `src/doc3gpp/services/spec_service.py:313-330` (`SpecService.list_recent`)
- Test: `tests/integration/test_spec_sql.py` (reuse; no new test file needed — service is a thin passthrough)

**Interfaces:**
- Consumes: `SpecRepository.list(..., rapporteurs=...)` from Task 1.
- Produces: `SpecService.list_recent(..., rapporteurs: str | None = None)`.

- [ ] **Step 1: Add the passthrough param**

In `src/doc3gpp/services/spec_service.py`, add `rapporteurs: str | None = None,` to `list_recent` after `wis` (line 324), and forward it in the `_repository.list(...)` call (line 329):

```python
        wis: str | None = None,
        rapporteurs: str | None = None,
    ) -> list[Spec]:
        return self._repository.list(
            limit=limit, offset=offset, tsg=tsg, type=type, spec_id=spec_id,
            title=title, status=status, radio_tech=radio_tech,
            initial_release=initial_release, wis=wis, rapporteurs=rapporteurs,
        )
```

- [ ] **Step 2: Verify no regression**

Run: `pytest tests/integration/test_spec_sql.py tests/integration/test_spec_cli.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add src/doc3gpp/services/spec_service.py
git commit -m "feat(spec): thread rapporteurs filter through SpecService.list_recent"
```

---

### Task 3: CLI — add `--rapporteurs` to `spec list`

**Files:**
- Modify: `src/doc3gpp/cli.py:3933-4023` (`spec_list`)
- Test: `tests/integration/test_spec_cli.py`

**Interfaces:**
- Consumes: `SpecService.list_recent(..., rapporteurs=...)` from Task 2.
- Produces: `spec list --rapporteurs <value>` CLI flag.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_spec_cli.py`:

```python
def test_spec_list_rapporteurs_filter(monkeypatch) -> None:
    svc = MagicMock()
    svc.list_recent.return_value = [
        Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5", rapporteurs="Ericsson LM")
    ]
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)
    result = runner.invoke(app, ["spec", "list", "--rapporteurs", "%Ericsson%", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    svc.list_recent.assert_called_once()
    kwargs = svc.list_recent.call_args.kwargs
    assert kwargs["rapporteurs"] == "%Ericsson%"
    assert "36.579-5" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_spec_cli.py::test_spec_list_rapporteurs_filter -v`
Expected: FAIL — `No such option: --rapporteurs`.

- [ ] **Step 3: Add the `--rapporteurs` option**

In `src/doc3gpp/cli.py`, add after the `wis` option (line 3953):

```python
    rapporteurs: str | None = typer.Option(
        None, "--rapporteurs", help="Rich filter on rapporteurs (comma-joined company names)."
    ),
```

- [ ] **Step 4: Forward it to the service**

In `spec_list`, add `rapporteurs=rapporteurs,` to the `service.list_recent(...)` call (after `wis=wis,` at line 4003).

- [ ] **Step 5: Fix the stale docstring**

In the `spec_list` docstring (lines 3976-3983), the sentence listing output columns currently reads `...``tsg``, and ``wis`` from ``settings.output.fields.spec``.` — change it to include `rapporteurs`:

```text
    ``tsg``, ``wis``, and ``rapporteurs`` from
    ``settings.output.fields.spec``.
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/integration/test_spec_cli.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/cli.py tests/integration/test_spec_cli.py
git commit -m "feat(spec): add --rapporteurs filter to spec list"
```

---

### Task 4: Web route — add `rapporteurs` query param + filters context

**Files:**
- Modify: `src/doc3gpp/web/routes/specs.py:30-98` (`list_specs`)
- Test: `tests/unit/test_web_routes.py`

**Interfaces:**
- Consumes: `SpecService.list_recent(..., rapporteurs=...)` from Task 2; `parse_text_query` from `doc3gpp.web.filters`.
- Produces: `GET /specs?rapporteurs=<value>` query param; `filters.rapporteurs` in the template context.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_web_routes.py`:

```python
def test_get_specs_forwards_rapporteurs_filter(client: TestClient) -> None:
    """``GET /specs?rapporteurs=...`` forwards the filter to the service."""
    from doc3gpp.web.deps import get_spec_service

    captured = {}

    class _RecordingSpecService(FakeSpecService):
        def list_recent(self, **kwargs: Any) -> list[Any]:
            captured.update(kwargs)
            return list(self._specs)

    client.app.dependency_overrides[get_spec_service] = lambda: _RecordingSpecService()
    try:
        response = client.get("/specs?rapporteurs=%25Ericsson%25")
    finally:
        client.app.dependency_overrides.pop(get_spec_service, None)
    assert response.status_code == 200
    assert captured["rapporteurs"] == "%Ericsson%"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_web_routes.py::test_get_specs_forwards_rapporteurs_filter -v`
Expected: FAIL — `KeyError: 'rapporteurs'` (the service is called without it).

- [ ] **Step 3: Add the query param**

In `src/doc3gpp/web/routes/specs.py`, add after the `wis` query param (line 39):

```python
    rapporteurs: str | None = Query(default=None),
```

- [ ] **Step 4: Forward it to the service**

In `list_specs`, add `rapporteurs=parse_text_query(rapporteurs),` to the `service.list_recent(...)` call (after `wis=parse_text_query(wis),` at line 66).

- [ ] **Step 5: Add it to the filters context**

In the `filters` dict (lines 86-96), add after `"wis": wis or "",`:

```python
                "rapporteurs": rapporteurs or "",
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/unit/test_web_routes.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/web/routes/specs.py tests/unit/test_web_routes.py
git commit -m "feat(spec): add rapporteurs filter to /specs route"
```

---

### Task 5: Web templates — Rapporteurs filter input + results column

**Files:**
- Modify: `src/doc3gpp/web/templates/partials/spec_filters.html`
- Modify: `src/doc3gpp/web/templates/partials/spec_results.html`
- Test: `tests/unit/test_web_routes.py`

**Interfaces:**
- Consumes: `filters.rapporteurs` context var (Task 4); `spec.rapporteurs` on each `Spec`.
- Produces: a `Rapporteurs` filter input and a `Rapporteurs` results column.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_web_routes.py`:

```python
def test_get_specs_renders_rapporteurs_column(client: TestClient) -> None:
    """The spec results table shows a Rapporteurs column with cell values."""
    from doc3gpp.web.deps import get_spec_service

    client.app.dependency_overrides[get_spec_service] = lambda: FakeSpecService()
    try:
        response = client.get("/specs", headers={"HX-Request": "true"})
    finally:
        client.app.dependency_overrides.pop(get_spec_service, None)
    assert response.status_code == 200
    assert "<th>Rapporteurs</th>" in response.text
    assert "Ericsson LM" in response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_web_routes.py::test_get_specs_renders_rapporteurs_column -v`
Expected: FAIL — `"<th>Rapporteurs</th>" not in response.text`.

- [ ] **Step 3: Add the filter input**

In `src/doc3gpp/web/templates/partials/spec_filters.html`, add after the `WIs` label block (line 31):

```html
  <label>Rapporteurs
    <input type="text" name="rapporteurs" value="{{ filters.rapporteurs or '' }}">
  </label>
```

- [ ] **Step 4: Add the results column**

In `src/doc3gpp/web/templates/partials/spec_results.html`, add a `<th>` after the `WIs` header (line 13):

```html
          <th>Rapporteurs</th>
```

and a `<td>` after the `wis` cell (line 27):

```html
            <td>{{ spec.rapporteurs or '-' }}</td>
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_web_routes.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/templates/partials/spec_filters.html src/doc3gpp/web/templates/partials/spec_results.html tests/unit/test_web_routes.py
git commit -m "feat(spec): add Rapporteurs filter input and results column"
```

---

### Task 6: MCP — add `rapporteurs` param to `list_specs`

**Files:**
- Modify: `src/doc3gpp/web/mcp_server.py:354-373` (`list_specs`)
- Test: `tests/integration/test_mcp_end_to_end.py`

**Interfaces:**
- Consumes: `SpecService.list_recent(..., rapporteurs=...)` from Task 2.
- Produces: `list_specs(rapporteurs=...)` MCP tool param.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_mcp_end_to_end.py`:

```python
def test_list_specs_rapporteurs_filter(sqlite_env) -> None:
    """``list_specs`` accepts and applies the rapporteurs filter."""
    import asyncio

    _state_and_server()  # runs create_schema()
    _seed_spec_corpus()
    _, server = _state_and_server()

    async def run():
        return await server.call_tool("list_specs", {"rapporteurs": "not-null"})

    result = asyncio.run(run())
    assert result.is_error is False
    import json

    payload = json.loads(result.content[0].text)
    assert payload == []
```

Note: `_seed_spec_corpus()` (lines 293-327) seeds a spec with no `rapporteurs`, so `not-null` returns an empty list. This test pins that the param is accepted and applied.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_mcp_end_to_end.py::test_list_specs_rapporteurs_filter -v`
Expected: FAIL — `TypeError: list_specs() got an unexpected keyword argument 'rapporteurs'`.

- [ ] **Step 3: Add the param**

In `src/doc3gpp/web/mcp_server.py`, add after the `wis` param (line 364):

```python
        rapporteurs: Annotated[str | None, Field(description="Rich filter pattern on rapporteurs.")] = None,
```

- [ ] **Step 4: Forward it to the service**

In `list_specs`, add `rapporteurs=rapporteurs,` to the `services.spec.list_recent(...)` call (after `initial_release=initial_release, wis=wis,` at line 371).

- [ ] **Step 5: Update the tool description**

In the `@server.tool(name="list_specs", ...)` description (line 354), change `...initial release or related WIs.` to `...initial release, related WIs or rapporteurs.` and add `rapporteurs` to the list of rich-filter fields: `The spec_id, title, status, radio_tech, initial_release, wis and rapporteurs filters support Rich filter patterns:...`.

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/integration/test_mcp_end_to_end.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/web/mcp_server.py tests/integration/test_mcp_end_to_end.py
git commit -m "feat(spec): add rapporteurs filter to list_specs MCP tool"
```

---

### Task 7: Documentation sync

**Files:**
- Modify: `docs/cli.md:1858-1905` (spec list section)
- Modify: `docs/code-map.md:107` (SQLAlchemySpecRepository row)

**Interfaces:**
- Consumes: the CLI flag and repo surface from Tasks 1-6.

- [ ] **Step 1: Update `docs/cli.md`**

In the `### doc3gpp spec list` section, add a `--rapporteurs` bullet after the `--wis` bullet (line 1876):

```markdown
- --rapporteurs: rich filter on rapporteurs (comma-joined company names).
```

- [ ] **Step 2: Update `docs/code-map.md`**

In the `SQLAlchemySpecRepository` row (line 107), the text already mentions `list(filters) applies the rich filter grammar`. No change needed unless the row omits `rapporteurs` — verify it reads correctly; if it lists filter fields explicitly, add `rapporteurs`.

- [ ] **Step 3: Verify docs render**

Run: `ruff check .`
Expected: clean (docs are not linted, but confirm no accidental source edits).

- [ ] **Step 4: Commit**

```bash
git add docs/cli.md docs/code-map.md
git commit -m "docs(spec): document rapporteurs list filter"
```

---

### Task 8: Full verification

**Files:**
- None (verification only).

- [ ] **Step 1: Run the full sqlite suite**

Run: `./scripts/test_sqlite.sh`
Expected: all pass except the known pre-existing failure `tests/unit/test_tsg_cli.py::test_tsg_list_with_all_fields` (asserts 5 TSG columns but 6 exist; unrelated to this plan, fails identically at base).

- [ ] **Step 2: Run ruff**

Run: `ruff check .`
Expected: clean.

- [ ] **Step 3: Confirm the new tests pass in isolation**

Run: `pytest tests/integration/test_spec_sql.py tests/integration/test_spec_cli.py tests/unit/test_web_routes.py tests/integration/test_mcp_end_to_end.py -v`
Expected: PASS.

- [ ] **Step 4: Report**

Report the final review verdict and the pre-existing `test_tsg_cli.py` failure to the user.
