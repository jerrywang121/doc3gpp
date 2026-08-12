# `spec show` Version Limiting + `spec list` Default Fields Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `limit`/`offset`/`version`/`--no-wis-crs` controls to `spec show` across CLI, web, and MCP, and drop `wis` from the `spec list` default output fields.

**Architecture:** Extend `SpecRepository.list_versions` (protocol + SQLAlchemy impl + service) with a `version` rich-filter param. The CLI/web/MCP `spec show` surfaces each pass `limit` (default 10), `offset` (default 0), `version`, and a `no_wis_crs` flag. The web detail page paginates the version table. Separately remove `wis` from the four spec default-field mirrors (settings schema, web route, MCP, toml example).

**Tech Stack:** Python 3.10+, Typer, FastAPI, SQLAlchemy 2.0, Jinja2, pydantic-settings, pytest.

## Global Constraints

- Default `limit` for the *service and repository* `list_versions` stays `200` — the sync backfill `SpecService._backfill_pdf_urls` must keep seeing all versions. Only the CLI/web/MCP *call sites* pass `limit=10`.
- The `version` filter uses the existing rich-grammar helper `apply_text_filter` (supports `19.%`, `!19.%`, `null`, `not-null`).
- Ordering stays numeric-DESC via `_version_sort_key`; filtering happens before ordering and the `offset/limit` slice.
- Web route, CLI, and MCP `spec show` JSON output must remain byte-consistent with each other.
- No comments in source code unless required by existing file convention.

---

### Task 1: Repository `version` filter

**Files:**
- Modify: `src/doc3gpp/repository/protocols.py:381-388` (`SpecRepository.list_versions`)
- Modify: `src/doc3gpp/storage/repositories/spec_sql.py:142-156` (`SQLAlchemySpecRepository.list_versions`)
- Test: `tests/integration/test_spec_sql.py`

**Interfaces:**
- Produces: `SpecRepository.list_versions(spec_id: str, limit: int = 200, offset: int = 0, version: str | None = None) -> list[SpecVersion]` and the matching impl. This is the only source of truth; Tasks 2-5 consume it.

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_spec_sql.py`:

```python
def test_list_versions_version_filter_and_paging(session_factory) -> None:
    """``list_versions`` filters by ``version`` (rich LIKE), orders
    numeric-DESC, then applies ``limit``/``offset``."""
    repo = SQLAlchemySpecRepository(session_factory)
    repo.upsert(Spec(spec_id="36.579-5", type="TS", title="T"))
    versions = [
        SpecVersion(spec_id="36.579-5", version="19.2.0", ftp_url="u1"),
        SpecVersion(spec_id="36.579-5", version="19.10.0", ftp_url="u2"),
        SpecVersion(spec_id="36.579-5", version="18.3.0", ftp_url="u3"),
        SpecVersion(spec_id="36.579-5", version="17.1.0", ftp_url="u4"),
    ]
    repo.upsert_versions(versions)

    got = repo.list_versions("36.579-5", version="19.%")
    assert [v.version for v in got] == ["19.10.0", "19.2.0"]

    got = repo.list_versions("36.579-5", version="19.%", limit=1, offset=1)
    assert [v.version for v in got] == ["19.2.0"]

    got = repo.list_versions("36.579-5", version="null")
    assert got == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_spec_sql.py::test_list_versions_version_filter_and_paging -v`
Expected: FAIL with `TypeError: list_versions() got an unexpected keyword argument 'version'`.

- [ ] **Step 3: Update the protocol**

In `src/doc3gpp/repository/protocols.py`, change `SpecRepository.list_versions` signature to:

```python
    def list_versions(
        self,
        spec_id: str,
        limit: int = 200,
        offset: int = 0,
        version: str | None = None,
    ) -> list[SpecVersion]:
        """Return version rows for a spec, ordered by ``version DESC``."""
```

- [ ] **Step 4: Update the SQLAlchemy impl**

In `src/doc3gpp/storage/repositories/spec_sql.py`, replace `list_versions`:

```python
    def list_versions(
        self,
        spec_id: str,
        limit: int = 200,
        offset: int = 0,
        version: str | None = None,
    ) -> list[SpecVersion]:
        with self._session_factory() as session:
            stmt = select(SpecVersionORM).where(SpecVersionORM.spec_id == spec_id)
            if version:
                stmt = apply_text_filter(stmt, SpecVersionORM.version, version)
            rows = session.scalars(stmt).all()
        versions = [_orm_to_version(r) for r in rows]
        # Version strings are ``#.#.#`` (e.g. ``18.10.1``), so a plain
        # string sort would rank ``18.2.1`` above ``18.10.1``. Sort by
        # the numeric tuple instead, newest first.
        versions.sort(key=_version_sort_key, reverse=True)
        return versions[offset : offset + limit]
```

`apply_text_filter` is already imported at the top of the file.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/integration/test_spec_sql.py -v`
Expected: PASS (including the existing `test_list_versions_orders_numerically_desc`).

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/repository/protocols.py src/doc3gpp/storage/repositories/spec_sql.py tests/integration/test_spec_sql.py
git commit -m "feat(spec): add version filter to list_versions"
```

---

### Task 2: Service `version` passthrough

**Files:**
- Modify: `src/doc3gpp/services/spec_service.py:336-339` (`SpecService.list_versions`)
- Test: `tests/integration/test_online_spec_sync.py` (verify existing callers still pass; no new test needed here, but add a passthrough check in `tests/unit/test_spec_service.py`)

**Interfaces:**
- Consumes: `SpecRepository.list_versions(..., version=...)` from Task 1.
- Produces: `SpecService.list_versions(spec_id: str, limit: int = 200, offset: int = 0, version: str | None = None) -> list[SpecVersion]`.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_spec_service.py`:

```python
def test_service_list_versions_forwards_version_filter() -> None:
    """``SpecService.list_versions`` passes ``version`` through to the repo."""
    repo = _StubSpecRepo()
    svc = SpecService(repo)
    repo.list_versions = MagicMock(return_value=[])
    svc.list_versions("36.579-5", limit=10, offset=2, version="19.%")
    repo.list_versions.assert_called_once_with(
        "36.579-5", limit=10, offset=2, version="19.%"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_spec_service.py::test_service_list_versions_forwards_version_filter -v`
Expected: FAIL with `TypeError`.

- [ ] **Step 3: Update the service**

In `src/doc3gpp/services/spec_service.py`, replace `list_versions`:

```python
    def list_versions(
        self,
        spec_id: str,
        limit: int = 200,
        offset: int = 0,
        version: str | None = None,
    ) -> list[SpecVersion]:
        return self._repository.list_versions(
            spec_id, limit=limit, offset=offset, version=version
        )
```

Note: `MagicMock` must be imported in `tests/unit/test_spec_service.py` (it is already, via `from unittest.mock import MagicMock` used in `_StubTsgRepo`). Confirm the import exists; add it if not.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_spec_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/spec_service.py tests/unit/test_spec_service.py
git commit -m "feat(spec): forward version filter through SpecService"
```

---

### Task 3: CLI `spec show` flags

**Files:**
- Modify: `src/doc3gpp/cli.py:4031-4122` (`spec_show`)
- Test: `tests/integration/test_spec_cli.py`

**Interfaces:**
- Consumes: `SpecService.list_versions(spec_id, limit, offset, version)` from Task 2.
- Produces: `doc3gpp spec show` accepts `--limit` (default 10, max 500), `--offset` (default 0), `--version`, `--no-wis-crs`. Header `wis` and version `crs` are dropped when `--no-wis-crs` is set.

- [ ] **Step 1: Write the failing tests**

Add to `tests/integration/test_spec_cli.py`:

```python
def test_spec_show_forwards_limit_offset_version(monkeypatch) -> None:
    """``spec show`` passes ``--limit``/``--offset``/``--version`` to the service."""
    spec = Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5")
    version = SpecVersion(spec_id="36.579-5", version="18.3.0", ftp_url="u", release="Rel-18")
    svc = MagicMock()
    svc.get.return_value = spec
    svc.list_versions = MagicMock(return_value=[version])
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)

    result = runner.invoke(app, ["spec", "show", "36.579-5", "--limit", "5", "--offset", "2", "--version", "19.%"])
    assert result.exit_code == 0, result.stdout
    svc.list_versions.assert_called_once_with("36.579-5", limit=5, offset=2, version="19.%")


def test_spec_show_no_wis_crs_drops_fields(monkeypatch) -> None:
    """``--no-wis-crs`` drops ``wis`` from the header and ``crs`` from versions."""
    spec = Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5", wis="eNB")
    version = SpecVersion(
        spec_id="36.579-5", version="18.3.0", ftp_url="u",
        release="Rel-18", crs="R5-1,R5-2",
    )
    svc = MagicMock()
    svc.get.return_value = spec
    svc.list_versions = MagicMock(return_value=[version])
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)

    result = runner.invoke(app, ["spec", "show", "36.579-5", "--format", "json", "--no-wis-crs"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert "wis" not in payload["spec"]
    assert "crs" not in payload["versions"][0]
```

Add `import json` at the top of the test file if not present.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_spec_cli.py -k "forwards_limit or no_wis_crs" -v`
Expected: FAIL (unexpected arguments / `wis` still present).

- [ ] **Step 3: Update the CLI**

In `src/doc3gpp/cli.py`, add options to `spec_show` (after the `compact` option) and update the body. Signature additions:

```python
    limit: int = typer.Option(10, min=1, max=500, help="Max versions to return."),
    offset: int = typer.Option(0, min=0, help="Number of versions to skip before applying --limit (pagination)."),
    version: str | None = typer.Option(
        None, "--version", help="Rich filter pattern on the version (e.g. 19.%)."
    ),
    no_wis_crs: bool = typer.Option(
        False, "--no-wis-crs",
        help="Drop the 'wis' header field and the per-version 'crs' field from output.",
    ),
```

Update the body: replace the `versions = service.list_versions(spec_id)` call with:

```python
    versions = service.list_versions(spec_id, limit=limit, offset=offset, version=version)
```

And after the `version_fields` definitions, add:

```python
    if no_wis_crs:
        header_fields = [f for f in header_fields if f != "wis"]
        version_fields = [f for f in version_fields if f != "crs"]
```

`header_fields` and `version_fields` are already `list[str]` so the list-comprehension reassignment is valid.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_spec_cli.py -k "spec_show" -v`
Expected: PASS (including existing `test_spec_show_json`, `test_spec_show_table`, etc.).

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/cli.py tests/integration/test_spec_cli.py
git commit -m "feat(spec): add limit/offset/version/no-wis-crs to spec show"
```

---

### Task 4: Web route pagination + `no_wis_crs`

**Files:**
- Modify: `src/doc3gpp/web/routes/specs.py:104-135` (`show_spec`)
- Modify: `src/doc3gpp/web/templates/spec_show.html`
- Test: `tests/unit/test_web_routes.py` (FakeSpecService + new tests)

**Interfaces:**
- Consumes: `SpecService.list_versions(spec_id, limit, offset, version)` from Task 2.
- Produces: `GET /specs/{spec_id}` accepts `limit` (default 10), `offset` (default 0), `version`, `no_wis_crs` (`true`/`false`). Template context gains `limit`, `offset`, `next_offset`, `version`, `no_wis_crs`.

- [ ] **Step 1: Extend the fake service and write failing tests**

Update `FakeSpecService.list_versions` in `tests/unit/test_web_routes.py:2239` to be a recording stub:

```python
    def list_versions(self, spec_id: str, limit: int = 200, offset: int = 0, version: str | None = None, **kwargs: Any) -> list[Any]:
        return [v for v in self._versions if v.spec_id == spec_id][offset : offset + limit]
```

Add tests:

```python
def test_get_spec_show_forwards_limit_offset_version(client: TestClient) -> None:
    """``GET /specs/{id}`` forwards ``limit``/``offset``/``version`` to the service."""
    from doc3gpp.web.deps import get_spec_service

    captured = {}

    class _RecordingSpecService(FakeSpecService):
        def list_versions(self, spec_id: str, limit: int = 200, offset: int = 0, version: str | None = None, **kwargs: Any) -> list[Any]:
            captured.update({"limit": limit, "offset": offset, "version": version})
            return list(self._versions)

    client.app.dependency_overrides[get_spec_service] = lambda: _RecordingSpecService()
    try:
        response = client.get("/specs/36.579-5?limit=5&offset=2&version=19.%25")
    finally:
        client.app.dependency_overrides.pop(get_spec_service, None)
    assert response.status_code == 200
    assert captured == {"limit": 5, "offset": 2, "version": "19.%"}
    assert "next ›" in response.text


def test_get_spec_show_no_wis_crs(client: TestClient) -> None:
    """``no_wis_crs=true`` omits the WIs row and CRs column; false keeps them."""
    from doc3gpp.web.deps import get_spec_service

    client.app.dependency_overrides[get_spec_service] = lambda: FakeSpecService()
    try:
        slim = client.get("/specs/36.579-5?no_wis_crs=true&format=json").json()
        full = client.get("/specs/36.579-5?no_wis_crs=false&format=json").json()
    finally:
        client.app.dependency_overrides.pop(get_spec_service, None)
    assert "wis" not in slim["spec"]
    assert "crs" not in slim["versions"][0]
    assert "wis" in full["spec"]
    assert "crs" in full["versions"][0]
```

Note: `FakeSpecService._versions[0]` has `crs="R5-260001,R5-260002"`; the header spec has no `wis` set (defaults to `None`) but the field key `wis` is still present in full output because `_SPEC_DEFAULT_FIELDS` includes it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_web_routes.py -k "get_spec_show" -v`
Expected: FAIL (unknown query params ignored, no pagination link, `wis`/`crs` still in slim output).

- [ ] **Step 3: Update the route**

In `src/doc3gpp/web/routes/specs.py`, update `show_spec` to:

```python
@router.get("/{spec_id}", include_in_schema=False)
async def show_spec(
    request: Request,
    spec_id: str,
    format: str | None = Query(default=None, alias="format"),
    limit: str | None = Query(default="10"),
    offset: str | None = Query(default="0"),
    version: str | None = Query(default=None),
    no_wis_crs: str | None = Query(default=None),
    service: SpecService = Depends(get_spec_service),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render ``spec_show.html`` or a JSON payload with the spec + versions."""
    parsed_limit = parse_int_query(limit, min=1, max=_LIMIT_CAP) or 10
    parsed_offset = parse_int_query(offset, min=0) or 0
    spec = service.get(spec_id)
    if spec is None:
        raise SpecNotFoundError(f"Spec {spec_id!r} not found")
    versions = service.list_versions(
        spec_id, limit=parsed_limit, offset=parsed_offset,
        version=parse_text_query(version),
    )

    slim = parse_bool_query(no_wis_crs) is True
    header_fields = [f for f in _SPEC_DEFAULT_FIELDS if not (slim and f == "wis")]
    version_fields = [f for f in _VERSION_FIELDS if not (slim and f == "crs")]

    if format == "json":
        return JSONResponse(
            content={
                "spec": {f: getattr(spec, f, None) for f in header_fields},
                "versions": spec_version_rows(versions, version_fields),
            },
        )

    next_offset = parsed_offset + len(versions) if len(versions) == parsed_limit else None
    return templates.TemplateResponse(
        request=request,
        name="spec_show.html",
        context={
            "active_nav": "specs",
            "spec": spec,
            "versions": versions,
            "limit": parsed_limit,
            "offset": parsed_offset,
            "next_offset": next_offset,
            "version": version or "",
            "no_wis_crs": slim,
            "pending_jobs": pending_jobs,
        },
    )
```

Add `parse_bool_query` to the import from `doc3gpp.web.filters` on line 12.

- [ ] **Step 4: Update the template**

In `src/doc3gpp/web/templates/spec_show.html`:

- Wrap the `{% if spec.wis %}` line in the header `<dl>` with `{% if not no_wis_crs %}` so the WIs row is hidden when slim. The existing line is:

```html
    {% if spec.wis %}<dt>WIs</dt><dd class="wrap-csv">{{ spec.wis | wrap_csv(150) }}</dd>{% endif %}
```

Replace it with:

```html
    {% if not no_wis_crs and spec.wis %}<dt>WIs</dt><dd class="wrap-csv">{{ spec.wis | wrap_csv(150) }}</dd>{% endif %}
```

- Remove the CRs column when slim. In the `<thead>`, the line `<th>CRs</th>` becomes `{% if not no_wis_crs %}<th>CRs</th>{% endif %}`. In the `<tbody>`, the whole CR cell:

```html
            <td>
              {% if v.crs %}
                <span class="copy-cell" title="{{ v.crs }}" data-copy="{{ v.crs }}">{{ cr_count }}</span>
              {% else %}
                -
              {% endif %}
            </td>
```

becomes `{% if not no_wis_crs %}` ... `{% endif %}`.

- After the closing `</table>` (inside the `{% if versions %}` block), add the pagination include:

```html
    {% include "partials/pagination.html" %}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_web_routes.py -k "spec" -v`
Expected: PASS (existing spec tests plus new ones).

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/routes/specs.py src/doc3gpp/web/templates/spec_show.html tests/unit/test_web_routes.py
git commit -m "feat(web): paginate and slim spec show versions"
```

---

### Task 5: MCP `get_spec` params

**Files:**
- Modify: `src/doc3gpp/web/mcp_server.py:376-386` (`get_spec` tool)
- Test: `tests/integration/test_mcp_end_to_end.py`

**Interfaces:**
- Consumes: `SpecService.list_versions(spec_id, limit, offset, version)` from Task 2.
- Produces: `get_spec` tool accepts `limit` (default 10), `offset` (default 0), `version`, `no_wis_crs` (bool). Must stay byte-consistent with the HTTP `?format=json` route (Task 4).

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_mcp_end_to_end.py`:

```python
def test_get_spec_tool_version_and_no_wis_crs(sqlite_env) -> None:
    """``get_spec`` accepts ``version`` and ``no_wis_crs``; JSON matches HTTP."""
    import asyncio
    import json

    from fastapi.testclient import TestClient

    from doc3gpp.settings.schema import CacheSettings, MCPSettings, ServerSettings, Settings
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.web.app import build_app

    _state_and_server()  # runs create_schema()
    _seed_spec_corpus()
    state, server = _state_and_server()
    app = build_app(
        Settings(
            server=ServerSettings(enabled=True, port=8765),
            mcp=MCPSettings(enabled=True),
            cache=CacheSettings(dir=state.settings.cache.dir),
        )
    )
    with TestClient(app) as client:

        async def call(name: str, args: dict) -> str:
            result = await server.call_tool(name, args)
            assert result.is_error is False, result
            return result.content[0].text

        mcp_bytes = asyncio.run(call(
            "get_spec",
            {"spec_id": "36.579-5", "no_wis_crs": True},
        ))
        http_resp = client.get("/specs/36.579-5?format=json&no_wis_crs=true")
        assert http_resp.status_code == 200, http_resp.text
        http_bytes = http_resp.content.decode("utf-8")
        assert json.loads(mcp_bytes) == json.loads(http_bytes)
        assert "wis" not in json.loads(mcp_bytes)["spec"]

        asyncio.run(call(
            "get_spec",
            {"spec_id": "36.579-5", "version": "19.%"},
        ))

    get_engine.cache_clear()
    del state.engine
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_mcp_end_to_end.py::test_get_spec_tool_version_and_no_wis_crs -v`
Expected: FAIL (unexpected keyword args `no_wis_crs`/`version`).

- [ ] **Step 3: Update the MCP tool**

In `src/doc3gpp/web/mcp_server.py`, replace the `get_spec` tool:

```python
    @server.tool(name="get_spec", description="Get a single spec by its dotted id, including its version rows. Set no_wis_crs to drop the 'wis' header field and per-version 'crs' field; version is a rich filter pattern (e.g. '19.%').")
    @_mcp_error_guard
    def get_spec(
        spec_id: Annotated[str, Field(description="Dotted spec id (e.g. '36.579-5').")],
        limit: Annotated[int, Field(description="Maximum number of version rows to return.")] = 10,
        offset: Annotated[int, Field(description="Number of version rows to skip for pagination.")] = 0,
        version: Annotated[str | None, Field(description="Rich filter pattern on the version (e.g. '19.%').")] = None,
        no_wis_crs: Annotated[bool, Field(description="Drop the 'wis' header field and per-version 'crs' field.")] = False,
    ) -> str:
        spec = services.spec.get(spec_id)
        if spec is None:
            raise SpecNotFoundError(spec_id)
        versions = services.spec.list_versions(
            spec_id, limit=limit, offset=offset, version=version
        )
        spec_fields = [f for f in _SPEC_FIELDS if not (no_wis_crs and f == "wis")]
        version_fields = [f for f in _VERSION_FIELDS if not (no_wis_crs and f == "crs")]
        return _to_json({
            "spec": {f: getattr(spec, f, None) for f in spec_fields},
            "versions": render.spec_version_rows(versions, version_fields),
        })
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_mcp_end_to_end.py -k "get_spec or spec_tools_parity" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/web/mcp_server.py tests/integration/test_mcp_end_to_end.py
git commit -m "feat(mcp): add limit/offset/version/no-wis-crs to get_spec"
```

---

### Task 6: Drop `wis` from `spec list` default fields

**Files:**
- Modify: `src/doc3gpp/settings/schema.py:203-215` (`OutputFieldsSettings.spec`)
- Modify: `src/doc3gpp/web/routes/specs.py:24` (`_SPEC_DEFAULT_FIELDS`)
- Modify: `src/doc3gpp/web/mcp_server.py:48` (`_SPEC_FIELDS`)
- Modify: `src/doc3gpp/data/doc3gpp.toml.example:60-63`
- Test: `tests/unit/test_settings.py`, `tests/integration/test_spec_cli.py`

**Interfaces:**
- Produces: The default `spec` output-field list is `["spec_id", "type", "title", "status", "radio_tech", "initial_release", "tsg", "rapporteurs"]` in all four mirrors. No downstream code depends on `wis` being present (filtering by `--wis` is unchanged).

- [ ] **Step 1: Write the failing test**

Update `tests/unit/test_settings.py:82-84` to remove `"wis"`:

```python
    assert s.output.fields.spec == [
        "spec_id", "type", "title", "status",
        "radio_tech", "initial_release", "tsg", "rapporteurs",
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_settings.py::test_output_fields_default_spec -v`
Expected: FAIL.

- [ ] **Step 3: Update the settings schema**

In `src/doc3gpp/settings/schema.py`, change the `spec` default to:

```python
    spec: list[str] = Field(
        default_factory=lambda: [
            "spec_id",
            "type",
            "title",
            "status",
            "radio_tech",
            "initial_release",
            "tsg",
            "rapporteurs",
        ]
    )
```

- [ ] **Step 4: Update the web + MCP mirrors and example**

- In `src/doc3gpp/web/routes/specs.py:24`, remove `"wis"` from `_SPEC_DEFAULT_FIELDS`:

```python
_SPEC_DEFAULT_FIELDS = ["spec_id", "type", "title", "status", "radio_tech", "initial_release", "tsg", "rapporteurs"]
```

- In `src/doc3gpp/web/mcp_server.py:48`, remove `"wis"` from `_SPEC_FIELDS`:

```python
_SPEC_FIELDS = ["spec_id", "type", "title", "status", "radio_tech", "initial_release", "tsg", "rapporteurs"]
```

- In `src/doc3gpp/data/doc3gpp.toml.example:60-63`, update the commented default:

```toml
# spec = [
#   "spec_id", "type", "title", "status",
#   "radio_tech", "initial_release", "tsg", "rapporteurs",
#   # "wis" — opt back in via config if you want it in spec list output
# ]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_settings.py tests/integration/test_spec_cli.py tests/unit/test_web_routes.py -k "spec or default_spec" -v`
Expected: PASS. Note any existing test asserting `wis` in spec list JSON must be updated to no longer expect it (search `test_spec_cli.py` for `wis` in `spec list` output assertions; `wis="eNB"` fixtures are fine since they only set the model field, not the output columns).

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/settings/schema.py src/doc3gpp/web/routes/specs.py src/doc3gpp/web/mcp_server.py src/doc3gpp/data/doc3gpp.toml.example tests/unit/test_settings.py
git commit -m "feat(spec): drop wis from spec list default output fields"
```

---

### Task 7: Documentation sync

**Files:**
- Modify: `docs/cli.md` (spec show flags + spec list default fields)
- Modify: `AGENTS.md` (spec-show workflow line)
- Modify: `docs/architecture.md` if the spec workflow line lists fields
- Modify: `docs/superpowers/specs/2026-08-12-spec-show-version-limit-design.md` (no change — already reflects final design)

**Interfaces:**
- Consumes: the final CLI flag surface from Task 3 and default fields from Task 6.

- [ ] **Step 1: Update `docs/cli.md`**

Under the `spec show` section, add the new options. Find the existing spec show reference (around `docs/cli.md`) and add:

```markdown
- --limit N: max versions to return (default 10).
- --offset N: number of versions to skip before applying --limit (pagination).
- --version PATTERN: rich filter pattern on the version string (e.g. `19.%`).
- --no-wis-crs: drop the `wis` header field and the per-version `crs` field.
```

Under the `spec list` default-output-fields block (currently at `docs/cli.md:1882-1886`), change:

```markdown
- `spec_id`, `type`, `title`, `status`, `radio_tech`, `initial_release`,
  `tsg`, `rapporteurs`
```

- [ ] **Step 2: Update `AGENTS.md`**

Find the spec-show workflow bullet and append the version-limiting behavior, and update any mention of spec list default fields to remove `wis`. Concretely, ensure the spec-show workflow line reflects that versions can be limited/filtered and `--no-wis-crs` slims output.

- [ ] **Step 3: Update `docs/architecture.md`**

If the architecture doc lists the spec show / list output fields (e.g. `docs/architecture.md:477` mentions spec columns), remove `wis` from any default-field list for `spec list`.

- [ ] **Step 4: Run lint + full sqlite test suite**

Run: `ruff check .`
Run: `./scripts/test_sqlite.sh`
Expected: no lint errors, all tests pass.

- [ ] **Step 5: Commit**

```bash
git add docs/cli.md AGENTS.md docs/architecture.md
git commit -m "docs: spec show version limiting and spec list default fields"
```
