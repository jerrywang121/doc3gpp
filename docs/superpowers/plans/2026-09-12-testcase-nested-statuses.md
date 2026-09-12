# Testcase nested statuses payload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reshape `testcase show` and `testcase list` JSON to one flat object per `(testcase_id, group)` with `statuses` nested inside as `[{path, gcf_ptcrb, ttcn_status}]`, byte-identical on CLI, web, and MCP.

**Architecture:** Model type change (`TestCaseWithStatuses.statuses` dict → `list[TestCaseStatus]`) propagates outward: service passes rows through, CLI/web/MCP serializers emit the nested list, templates iterate objects, then docs and the full gate.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0 (untouched), Typer CLI, FastAPI + Jinja2, MCP SDK v2, pytest.

## Global Constraints

- Python `>=3.10`; no new packages.
- Ruff line-length 100, target `py310` (`ruff check .` must pass).
- Layering: `services/` reach storage only through `repository/` Protocols; `cli.py` / `web/` never instantiate SQL repos directly; `models/` are `@dataclass(slots=True)`, never leak ORM attrs.
- `table` output is tab-separated via `_emit_table`/`_emit_records`; `json`/`markdown` via shared emitters; `--compact` semantics unchanged.
- MCP `_to_json` uses compact separators + `ensure_ascii=False`, byte-matching HTTP `?format=json` (Starlette `JSONResponse`).
- TDD: failing test → run → minimal impl → run → commit per task. Offline by default; `./scripts/test_sqlite.sh` is the full offline gate.
- Breaking JSON change is accepted (pre-release feature branch): no compat shim, no deprecation window.
- Status row field order is `path, gcf_ptcrb, ttcn_status` (no `group` key anywhere in output).

---

## File map

| File | Responsibility |
|---|---|
| `src/doc3gpp/models/testcase.py` | `TestCaseWithStatuses.statuses` type: `dict` → `list[TestCaseStatus]` |
| `src/doc3gpp/services/testcase_service.py` | `list_recent` passes full status rows through |
| `src/doc3gpp/cli.py` | flat+nested JSON for show/list; table/markdown status cells; drop `group` from show status cols |
| `src/doc3gpp/web/render.py` | `testcase_rows` emits nested status lists |
| `src/doc3gpp/web/routes/testcases.py` | show JSON flat+nested; `_TESTCASE_STATUS_FIELDS` drops `group` |
| `src/doc3gpp/web/mcp_server.py` | `get_testcase`/`list_testcases` flat+nested; `_TESTCASE_STATUS_FIELDS` drops `group` |
| `src/doc3gpp/web/templates/testcase_show.html` | drop `Group` column from status table |
| `src/doc3gpp/web/templates/partials/testcase_results.html` | iterate status objects |
| Tests (6 files) | reshape assertions to the nested payload |
| Docs (`cli.md`, `architecture.md`, `web-server.md`, `code-map.md`, `AGENTS.md`) | document the nested shape |

---

### Task 1: Model + service — statuses as a row list

**Files:**
- Modify: `src/doc3gpp/models/testcase.py:38-41`
- Modify: `src/doc3gpp/services/testcase_service.py:156-164`
- Test: `tests/unit/test_testcase_model.py`

**Interfaces:**
- Consumes: `TestCaseStatus(testcase_id, group, path, gcf_ptcrb, ttcn_status)` (unchanged).
- Produces: `TestCaseWithStatuses(testcase: TestCase, statuses: list[TestCaseStatus])` — consumed by CLI list (Task 2), `render.testcase_rows` (Task 3), and all fakes/stubs updated in Task 4.

- [ ] **Step 1: Write the failing test**

```python
def test_with_statuses_holds_status_rows():
    from doc3gpp.models.testcase import TestCaseStatus
    tc = TestCase(testcase_id="TC_1", group="5G", title="T", spec="38.523-1")
    rows = [
        TestCaseStatus(testcase_id="TC_1", group="5G", path="FR1", gcf_ptcrb="Approved", ttcn_status="Approved"),
        TestCaseStatus(testcase_id="TC_1", group="5G", path="FR2", gcf_ptcrb=None, ttcn_status=None),
    ]
    assert TestCaseWithStatuses(testcase=tc, statuses=rows).statuses == rows
```

Append to `tests/unit/test_testcase_model.py` (keep the existing `test_dataclass_shapes`, but change its `TestCaseWithStatuses(testcase=tc, statuses={"FR1": "Approved"})` construction to a one-row list and assert the list round-trips).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_testcase_model.py -v`
Expected: PASS on the new test if the model is untyped at runtime (dataclasses don't enforce), so instead assert the service still collapses: add a service-level check in the same run — `TestCaseService.list_recent` with a stub repo returning one header + one `TestCaseStatus` must return `statuses == [row]`, not `{"FR1": ...}`. That service assertion FAILS before the fix.

Service stub for the check:

```python
def test_list_recent_passes_status_rows():
    from doc3gpp.models.testcase import TestCase, TestCaseStatus
    from doc3gpp.services.testcase_service import TestCaseService
    row = TestCaseStatus(testcase_id="TC_1", group="5G", path="FR1", gcf_ptcrb="Approved", ttcn_status="Approved")
    class Repo:
        def list(self, **kw): return [TestCase(testcase_id="TC_1", group="5G", title="T")]
        def list_statuses(self, tid, group=None): return [row]
    out = TestCaseService(Repo()).list_recent()
    assert out[0].statuses == [row]
```

Put this test in `tests/unit/test_testcase_service.py` (new test, keep existing sync tests untouched).

- [ ] **Step 3: Write minimal implementation**

In `src/doc3gpp/models/testcase.py`, change the field type:

```python
@dataclass(slots=True)
class TestCaseWithStatuses:
    testcase: TestCase
    statuses: list[TestCaseStatus]
```

In `src/doc3gpp/services/testcase_service.py`, replace the dict collapse:

```python
        out: list[TestCaseWithStatuses] = []
        for case in cases:
            rows = self._repository.list_statuses(case.testcase_id, case.group)
            out.append(TestCaseWithStatuses(testcase=case, statuses=list(rows)))
        return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_testcase_model.py tests/unit/test_testcase_service.py -v`
Expected: PASS (all tests in both files).

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/models/testcase.py src/doc3gpp/services/testcase_service.py tests/unit/test_testcase_model.py tests/unit/test_testcase_service.py
git commit -m "refactor(testcase): carry list statuses as row lists"
```

---

### Task 2: CLI — flat+nested JSON for show and list

**Files:**
- Modify: `src/doc3gpp/cli.py:4390` (`TESTCASE_SHOW_STATUS_FIELDS`), `:4404-4420` (`_format_testcase_statuses`), `:4567-4613` (list JSON + table cells), `:4652-4733` (show docstring + payload + table blocks)
- Test: `tests/integration/test_testcase_cli.py`

**Interfaces:**
- Consumes: `TestCaseWithStatuses.statuses: list[TestCaseStatus]` (Task 1).
- Produces: CLI JSON payloads that web (Task 3) and MCP (Task 5) must byte-match: show element = `{testcase_id, title, ats, feature, release, wis, spec, group, statuses: [{path, gcf_ptcrb, ttcn_status}]}`; list element = `{<selected header fields...>, statuses: [...]}`.

- [ ] **Step 1: Write the failing tests**

In `tests/integration/test_testcase_cli.py`, rewrite the three shape tests (keep sync/missing/validation tests untouched):

```python
def test_testcase_list_json_shape(monkeypatch) -> None:
    from doc3gpp.models.testcase import TestCaseStatus
    svc = MagicMock()
    svc.list_recent.return_value = [
        TestCaseWithStatuses(
            testcase=TestCase(testcase_id="TC_1", title="T", spec="38.523-1", group="5G", release="Rel-17"),
            statuses=[TestCaseStatus(testcase_id="TC_1", group="5G", path="FR1", gcf_ptcrb="Approved", ttcn_status="Approved")],
        )
    ]
    monkeypatch.setattr("doc3gpp.cli.build_testcase_service", lambda: svc)
    result = runner.invoke(app, ["testcase", "list", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload[0]["testcase_id"] == "TC_1"
    assert payload[0]["group"] == "5G"
    assert payload[0]["statuses"] == [{"path": "FR1", "gcf_ptcrb": "Approved", "ttcn_status": "Approved"}]
    assert "testcase" not in payload[0]
```

```python
def test_testcase_show_multi_group_json(monkeypatch) -> None:
    """``testcase show`` without ``--group`` emits one flat entry per group."""
    from doc3gpp.models.testcase import TestCase, TestCaseDetail, TestCaseStatus
    svc = MagicMock()
    svc.get_all.return_value = [
        TestCaseDetail(
            testcase=TestCase(testcase_id="TC_D", group="IMS", title="I"),
            statuses=[TestCaseStatus(testcase_id="TC_D", group="IMS", path="default", ttcn_status="Approved")],
        ),
        TestCaseDetail(
            testcase=TestCase(testcase_id="TC_D", group="UTRA", title="U"),
            statuses=[TestCaseStatus(testcase_id="TC_D", group="UTRA", path="default", ttcn_status="Rejected")],
        ),
    ]
    monkeypatch.setattr("doc3gpp.cli.build_testcase_service", lambda: svc)
    result = runner.invoke(app, ["testcase", "show", "--testcase", "TC_D", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert isinstance(payload, list) and len(payload) == 2
    assert [entry["group"] for entry in payload] == ["IMS", "UTRA"]
    assert payload[0]["statuses"] == [{"path": "default", "gcf_ptcrb": None, "ttcn_status": "Approved"}]
    assert all("testcase" not in entry and "group" not in entry["statuses"][0] for entry in payload)
```

```python
def test_testcase_show_group_scoped(monkeypatch) -> None:
    """``testcase show --group`` scopes to a single flat ``(id, group)`` object."""
    from doc3gpp.models.testcase import TestCase, TestCaseDetail
    svc = MagicMock()
    svc.get.return_value = TestCaseDetail(
        testcase=TestCase(testcase_id="TC_D", group="UTRA", title="U"),
        statuses=[],
    )
    monkeypatch.setattr("doc3gpp.cli.build_testcase_service", lambda: svc)
    result = runner.invoke(app, ["testcase", "show", "--testcase", "TC_D", "--group", "UTRA", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert isinstance(payload, list) and len(payload) == 1
    assert payload[0]["group"] == "UTRA"
    assert payload[0]["statuses"] == []
    _, kwargs = svc.get.call_args
    assert kwargs.get("group") == "UTRA" or svc.get.call_args.args[1:] == ("UTRA",)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_testcase_cli.py -v -k "json_shape or multi_group or group_scoped"`
Expected: FAIL — `AttributeError` on `.items()` / missing `testcase` key (old envelope code vs new stubs).

- [ ] **Step 3: Write minimal implementation**

(a) `TESTCASE_SHOW_STATUS_FIELDS` (cli.py:4390):

```python
TESTCASE_SHOW_STATUS_FIELDS: list[str] = ["path", "gcf_ptcrb", "ttcn_status"]
```

(b) `_format_testcase_statuses` — retarget to status rows, rendering `path=gcf/ttcn` pairs (dash for `None`):

```python
def _format_testcase_statuses(statuses: list[TestCaseStatus]) -> str:
    """Render status rows as compact ``path=gcf/ttcn;…`` pairs.

    Rows arrive pre-sorted by ``PATH_RANK`` from the repository;
    ``None`` values render as ``-`` so table/markdown cells stay
    non-empty.
    """
    return ";".join(
        f"{s.path}={s.gcf_ptcrb or '-'}/{s.ttcn_status or '-'}"
        for s in statuses
    )
```

Needs `TestCaseStatus` import — already imported at cli.py:42.

(c) List JSON payload (replaces the `item.statuses`-dict branch): build flat header fields + nested list via `_serialise_show_value`:

```python
        payload = [
            {
                **{
                    f: str(getattr(item.testcase, f, None) or "-")
                    for f in out_fields
                    if f != "statuses"
                },
                **(
                    {
                        "statuses": [
                            {
                                f: _serialise_show_value(getattr(s, f))
                                for f in ("path", "gcf_ptcrb", "ttcn_status")
                            }
                            for s in item.statuses
                        ]
                    }
                    if "statuses" in out_fields
                    else {}
                ),
            }
            for item in records
        ]
```

(d) Show `_detail_payload`: flat merge instead of envelope:

```python
    def _detail_payload(detail: TestCaseDetail) -> dict:
        return {
            **{
                f: _serialise_show_value(getattr(detail.testcase, f))
                for f in TESTCASE_SHOW_HEADER_FIELDS
            },
            "statuses": [
                {
                    f: _serialise_show_value(getattr(status_row, f))
                    for f in TESTCASE_SHOW_STATUS_FIELDS
                }
                for status_row in detail.statuses
            ],
        }
```

(e) Show docstring: replace the envelope paragraph with: "Without ``--group`` and several stored groups, every matching group is rendered: JSON emits an array with one flat object per ``(testcase_id, group)`` (a single-element array when exactly one group matches); table/markdown emit one header block per group separated by blank lines, then that group's status rows (``path, gcf_ptcrb, ttcn_status``)."

(f) List docstring: replace "carries a ``statuses`` projection mapping ``path → ttcn_status``" with "carries a nested ``statuses`` list of ``{path, gcf_ptcrb, ttcn_status}`` objects".

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_testcase_cli.py -v`
Expected: PASS (all 8 tests).

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/cli.py tests/integration/test_testcase_cli.py
git commit -m "feat(testcase): nest statuses inside the testcase object"
```

---

### Task 3: Web — render helper, routes, templates

**Files:**
- Modify: `src/doc3gpp/web/render.py:270-291` (`testcase_rows`)
- Modify: `src/doc3gpp/web/routes/testcases.py:33-43`, `:160-191` (field lists + show JSON)
- Modify: `src/doc3gpp/web/templates/testcase_show.html` (drop Group column)
- Modify: `src/doc3gpp/web/templates/partials/testcase_results.html` (object iteration)
- Test: `tests/unit/test_web_routes.py`

**Interfaces:**
- Consumes: CLI payload shapes from Task 2 (must byte-match); `TestCaseWithStatuses.statuses: list[TestCaseStatus]` (Task 1).
- Produces: `testcase_rows(rows, fields)` flat+nested output consumed by `list_testcases` route + MCP `list_testcases` (Task 5).

- [ ] **Step 1: Write the failing tests**

Update in `tests/unit/test_web_routes.py` (keep all other tests untouched):

1. `FakeTestCaseService._rows`: `statuses={"FR1": "Approved"}` →
```python
                statuses=[
                    TestCaseStatus(
                        testcase_id="TC_1",
                        group="5G",
                        path="FR1",
                        gcf_ptcrb="Approved",
                        ttcn_status="Approved",
                    ),
                ],
```
(`TestCaseStatus` already imported in the fake's method scope.)

2. `test_testcases_json_parity` body:
```python
    assert response.status_code == 200
    assert response.json()[0]["statuses"] == [
        {"path": "FR1", "gcf_ptcrb": "Approved", "ttcn_status": "Approved"}
    ]
```
and update its docstring first line to "preserves the statuses list." plus the Lock line to "(the CLI emits it as a list of per-path objects)".

3. `test_testcase_show_json` body:
```python
    assert isinstance(body, list) and body
    assert body[0]["testcase_id"] == "TC_1"
    assert body[0]["statuses"][0]["path"] == "FR1"
    assert "group" not in body[0]["statuses"][0]
```

4. `test_testcase_show_group_scoped_json` body:
```python
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["group"] == "5G"
```

5. `test_testcase_rows_preserves_statuses_dict` → rename to `test_testcase_rows_nests_status_objects`:
```python
def test_testcase_rows_nests_status_objects() -> None:
    """``testcase_rows`` nests ``statuses`` as objects; other fields coerce."""
    from doc3gpp.models.testcase import TestCase, TestCaseStatus, TestCaseWithStatuses
    from doc3gpp.web.render import testcase_rows

    rows = [
        TestCaseWithStatuses(
            testcase=TestCase(testcase_id="TC_1", title="T", group="5G"),
            statuses=[
                TestCaseStatus(testcase_id="TC_1", group="5G", path="FR1", gcf_ptcrb="Approved", ttcn_status="Approved"),
            ],
        ),
    ]
    out = testcase_rows(rows, ["testcase_id", "title", "group", "statuses"])
    assert out[0]["statuses"] == [{"path": "FR1", "gcf_ptcrb": "Approved", "ttcn_status": "Approved"}]
    assert out[0]["title"] == "T"
    assert out[0]["testcase_id"] == "TC_1"
```

6. `test_testcase_status_rows_coerce_cells`: field list `["group", "path", "gcf_ptcrb", "ttcn_status"]` → `["path", "gcf_ptcrb", "ttcn_status"]`, expected `[{"path": "FR1", "gcf_ptcrb": "Approved", "ttcn_status": "-"}]`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_web_routes.py -v -k "testcase"`
Expected: FAIL — dict `.items()` iteration / `body[0]["testcase"]` KeyError on the new shape.

- [ ] **Step 3: Write minimal implementation**

(a) `render.testcase_rows`:

```python
def testcase_rows(
    rows: list[Any],
    fields: list[str],
) -> list[dict[str, Any]]:
    """Build ``testcase list --format json``-shaped rows.

    Each row is one flat object per ``(testcase_id, group)``: the
    selected header fields plus a nested ``statuses`` list of
    ``{path, gcf_ptcrb, ttcn_status}`` objects. ``statuses`` must not
    go through :func:`_coerce_cell`. Every other field is coerced
    exactly like the CLI's ``testcase_list`` JSON cell loop (``None``
    renders as ``"-"``).
    """
    out: list[dict[str, Any]] = []
    for item in rows:
        row: dict[str, Any] = {}
        for f in fields:
            if f == "statuses":
                row[f] = [
                    {
                        sf: _coerce_cell(getattr(s, sf, None))
                        for sf in ("path", "gcf_ptcrb", "ttcn_status")
                    }
                    for s in item.statuses
                ]
            else:
                row[f] = _coerce_cell(getattr(item.testcase, f, None))
        out.append(row)
    return out
```

(b) routes `testcases.py`: `_TESTCASE_STATUS_FIELDS = ["path", "gcf_ptcrb", "ttcn_status"]`; show JSON comprehension:

```python
    if format == "json":
        return JSONResponse(
            content=[
                {
                    **{
                        f: getattr(item.testcase, f, None)
                        for f in _TESTCASE_SHOW_FIELDS
                    },
                    "statuses": testcase_status_rows(
                        item.statuses, _TESTCASE_STATUS_FIELDS
                    ),
                }
                for item in details
            ]
        )
```

Also update the show route docstring: "JSON emits an array with one flat object per ``(testcase_id, group)`` (one element when a single group matches)".

(c) `testcase_show.html`: drop the `<th>Group</th>` header and the `<td><code>{{ s.group }}</code></td>` cell.

(d) `partials/testcase_results.html`: replace the dict loop:

```html
              {% if row.statuses %}
                {% for s in row.statuses %}
                  <code>{{ s.path }}={{ s.gcf_ptcrb or '-' }}/{{ s.ttcn_status or '-' }}</code>{% if not loop.last %} {% endif %}
                {% endfor %}
              {% else %}
                -
              {% endif %}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_web_routes.py -v -k "testcase"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/web/render.py src/doc3gpp/web/routes/testcases.py src/doc3gpp/web/templates/testcase_show.html src/doc3gpp/web/templates/partials/testcase_results.html tests/unit/test_web_routes.py
git commit -m "feat(testcase): nest statuses in web list/show payloads"
```

---

### Task 4: E2E + MCP-adjacent stubs — reshape assertions

**Files:**
- Modify: `tests/integration/test_web_end_to_end.py` (testcase list/show JSON asserts)
- Modify: `tests/integration/test_testcase_sql.py` (only if it pins the dict shape — check `list_recent`/service-level asserts)
- Modify: `tests/unit/test_job_worker.py` (`_FakeTestcaseService`, only if it builds `TestCaseWithStatuses`)
- Test: same files (no new tests; reshape existing asserts)

**Interfaces:**
- Consumes: Task 1–3 shapes. No production code in this task.

- [ ] **Step 1: Locate every stale assertion**

Run: `rg -n '"statuses"|\[.testcase.\]|"testcase" not in|statuses == \{' tests/integration/test_web_end_to_end.py tests/integration/test_testcase_sql.py tests/unit/test_job_worker.py tests/integration/test_mcp_end_to_end.py`
Expected: a list of lines pinning `{"FR1": "Approved"}`, `body[0]["testcase"]["testcase_id"]`, or `payload[0]["testcase"]["group"]`.

- [ ] **Step 2: Rewrite the assertions to the nested shape**

Rules (apply mechanically, no new test logic):
- `X["statuses"] == {"FR1": "Approved"}` → `X["statuses"] == [{"path": "FR1", "gcf_ptcrb": <value>, "ttcn_status": "Approved"}]` (use the seeded `gcf_ptcrb`; check the seed — `test_web_end_to_end` seeds `gcf_ptcrb="Approved"`).
- `body[0]["testcase"]["testcase_id"]` → `body[0]["testcase_id"]`; `payload[0]["testcase"]["group"]` → `payload[0]["group"]`; add `assert "testcase" not in body[0]` and `assert "group" not in body[0]["statuses"][0]` to each reshaped show assert.
- Any `TestCaseWithStatuses(..., statuses={<dict>})` construction → one-row `statuses=[TestCaseStatus(...)]` (import `TestCaseStatus` where missing).
- Do NOT touch MCP tests here (Task 5) or docs (Task 6).

- [ ] **Step 3: Run the touched files**

Run: `pytest tests/integration/test_web_end_to_end.py tests/integration/test_testcase_sql.py tests/unit/test_job_worker.py -q`
Expected: PASS. If `test_testcase_sql.py` has no dict-shape asserts, it passes unchanged — note that in the commit message body.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_web_end_to_end.py tests/integration/test_testcase_sql.py tests/unit/test_job_worker.py
git commit -m "test(testcase): reshape e2e asserts to nested statuses"
```

---

### Task 5: MCP — flat+nested tool payloads

**Files:**
- Modify: `src/doc3gpp/web/mcp_server.py:53-55` (field lists), `:521-548` (`get_testcase` + description), `:519` (`list_testcases` — no code change, inherits `testcase_rows`)
- Test: `tests/integration/test_mcp_end_to_end.py`

**Interfaces:**
- Consumes: `render.testcase_rows` (Task 3) for `list_testcases`; `render.testcase_status_rows` + `_TESTCASE_SHOW_FIELDS` for `get_testcase`.

- [ ] **Step 1: Write the failing tests**

Update in `tests/integration/test_mcp_end_to_end.py` (find the `list_testcases` parity test asserting `payload[0]["statuses"] == {"FR1": "Approved"}` and the `get_testcase` test asserting `payload[0]["testcase"]["testcase_id"]` / `payload[0]["statuses"][0]["path"]`):

```python
    assert payload[0]["statuses"] == [{"path": "FR1", "gcf_ptcrb": "Approved", "ttcn_status": "Approved"}]
```

```python
    assert payload[0]["testcase_id"] == "TC_1"
    assert payload[0]["statuses"][0]["path"] == "FR1"
    assert "group" not in payload[0]["statuses"][0]
```

(Check the seeded `gcf_ptcrb` in `_seed_testcase_corpus` — it seeds `"Approved"`; use exactly that.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_mcp_end_to_end.py -q -k "testcase"`
Expected: FAIL — dict vs list mismatch / `testcase` key missing.

- [ ] **Step 3: Write minimal implementation**

```python
_TESTCASE_STATUS_FIELDS = ["path", "gcf_ptcrb", "ttcn_status"]
```

`get_testcase` return (keep the guard logic untouched):

```python
        return _to_json([
            {
                **{f: getattr(item.testcase, f) for f in _TESTCASE_SHOW_FIELDS},
                "statuses": render.testcase_status_rows(item.statuses, _TESTCASE_STATUS_FIELDS),
            }
            for item in details
        ])
```

Update the tool description: `"Get a testcase by id, including its nested status rows (path, gcf_ptcrb, ttcn_status). Without group, every stored group is returned as an array of flat objects; with group, a single-element array."` Also update the `list_testcases` description if it mentions the dict projection (check current text; only edit if it does).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_mcp_end_to_end.py -q -k "testcase"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/web/mcp_server.py tests/integration/test_mcp_end_to_end.py
git commit -m "feat(testcase): nest statuses in MCP tool payloads"
```

---

### Task 6: Docs + full gate

**Files:**
- Modify: `docs/cli.md` (`testcase list`/`show` JSON shapes)
- Modify: `docs/architecture.md` (testcase workflow one-liners)
- Modify: `docs/web-server.md` (route table + MCP payload shapes)
- Modify: `docs/code-map.md` (`TestCaseWithStatuses` row)
- Modify: `AGENTS.md` (only the lines naming the old envelope, if any)
- Test: full gate `./scripts/test_sqlite.sh` + `ruff check .`

**Interfaces:**
- Consumes: final as-built shapes from Tasks 2–5. Verify each doc claim against the real `--help`/JSON output — no paraphrase drift.

- [ ] **Step 1: Update `docs/cli.md`**

Find the `testcase list` section ("projection mapping `path → ttcn_status`") → "nested `statuses` list of `{path, gcf_ptcrb, ttcn_status}` objects (field-selectable via `--fields`)". Find the `testcase show` section ('array of `{"testcase", "statuses"}`') → "array with one flat object per `(testcase_id, group)`: header fields inline + nested `statuses`; status rows carry no `group`". Include the `20.7` example shape from the spec.

- [ ] **Step 2: Update `docs/architecture.md`, `docs/web-server.md`, `docs/code-map.md`, `AGENTS.md`**

`architecture.md` workflow one-liners: `statuses`-dict projection → nested status-row list; show "array of `"testcase"` / `"statuses"` objects" → "array of flat per-`(id, group)` objects with nested `statuses`". `web-server.md` route table (`/testcases/{id}` row) + MCP `get_testcase` paragraph: same wording. `code-map.md` `TestCaseWithStatuses` row: `statuses: list[TestCaseStatus]`. `AGENTS.md`: only touch lines that name the envelope/dict (leave the rest).

- [ ] **Step 3: Run the full gate**

Run: `./scripts/test_sqlite.sh 2>&1 | tail -3`
Expected: `2238 passed, 1 skipped` (baseline after the coordinator date fix), zero failures.

Run: `ruff check .`
Expected: clean (empty output).

- [ ] **Step 4: Fix any fallout, then commit**

```bash
git add docs/cli.md docs/architecture.md docs/web-server.md docs/code-map.md AGENTS.md
git commit -m "docs(testcase): document nested statuses payload"
```

---

## Self-Review

**1. Spec coverage:**
- Flat per-`(id, group)` object + nested `statuses` without `group` → Tasks 2 (CLI), 3 (web), 5 (MCP).
- `list` same nested treatment with full `{path, gcf_ptcrb, ttcn_status}` rows → Tasks 2–3 (`testcase_rows`), Task 5 (`list_testcases` inherits).
- Table/markdown: drop Group status column (show), `path=gcf/ttcn` cells (list) → Tasks 2–3.
- `TestCaseWithStatuses.statuses` type change → Task 1; `TestCaseDetail` untouched (already nested).
- Byte-identical CLI/HTTP/MCP → Task 3 reuses Task 2 shapes via shared `render` helpers; Task 5 `_to_json` unchanged.
- Docs + spec accordingly → Task 6.
- Gate + lint → Task 6 Step 3.

**2. Placeholder scan:** no TBD/TODO/"similar to"/unshown code — every step carries exact file:line anchors, verbatim code blocks, and exact run commands. Task 4's mechanical rewrite rules name the exact `rg` to run first so no assert is missed.

**3. Type consistency:** `statuses: list[TestCaseStatus]` in Task 1 matches every later construction (`[TestCaseStatus(...)]` in Tasks 2–5 tests, `item.statuses` iteration in CLI/web/MCP impls). Status field order `path, gcf_ptcrb, ttcn_status` identical in Tasks 2 (`("path", "gcf_ptcrb", "ttcn_status")`), 3, 5. `testcase_rows(rows, fields)` signature unchanged throughout.
