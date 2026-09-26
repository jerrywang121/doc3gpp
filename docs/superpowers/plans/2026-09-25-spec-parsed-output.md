# Spec Parsed Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose derived spec-document parse status on CLI, REST, web, and MCP spec list/show surfaces, including a pre-pagination `parsed` filter, without adding persistent columns or migrations.

**Architecture:** `spec_doc_sources.parsed_at IS NOT NULL` remains the only source of truth. A batch parsed-version lookup is added to the specdata repository, `SpecService` enriches transient `Spec.parsed` and `SpecVersion.parsed` values, and every adapter consumes the enriched service results. The main spec repository gains an unbounded-list mode used only when the parsed filter must be applied before pagination.

**Tech Stack:** Python 3.10+, dataclasses with `slots=True`, SQLAlchemy 2.0, SQLite specdata database, Typer, FastAPI/Jinja2, MCP server, pytest, Ruff.

## Global Constraints

- Parsed status is `spec_doc_sources.parsed_at IS NOT NULL`; missing source rows and null `parsed_at` are unparsed.
- `Spec.parsed` is transient `str | None`; `SpecVersion.parsed` is transient `bool`; neither is persisted or added to schema metadata.
- Parsed version strings are numeric-newest-first, so `18.10.1` sorts after `19.1.0` and before `18.2.1` only according to numeric tuple ordering.
- Spec-list structured JSON emits `parsed: null` when no version is parsed; table and Markdown render that value as `-`.
- Spec-show structured JSON emits native `true`/`false` for every version's `parsed`; table and Markdown render the boolean text.
- `spec list --parsed` accepts only `true` or `false`, case-insensitively; omission means no parsed-status filter.
- REST/web `parsed` accepts `true` or `false`, case-insensitively; an omitted or empty form value means Any, and any other non-empty value is an HTTP 400 filter error.
- Parsed filtering occurs before `limit`/`offset` pagination and does not alter any existing main-database filter grammar.
- No network calls, schema migrations, new runtime dependencies, or changes to unrelated resource renderers are allowed.
- Do not commit automatically; leave each task's diff inspectable until the user explicitly requests commits.

## File Map

### Domain, contracts, and storage

- Modify `src/doc3gpp/models/spec.py` to add transient output fields and the shared numeric version sort-key helper.
- Modify `src/doc3gpp/repository/protocols.py` to add the parsed-status repository contract and allow an unbounded main spec list.
- Modify `src/doc3gpp/storage/repositories/spec_doc_sql.py` to batch-read parsed versions from the specdata database.
- Modify `src/doc3gpp/storage/repositories/spec_sql.py` to support `limit=None` without changing the existing default pagination.
- Modify `tests/integration/test_spec_doc_repo.py` and `tests/integration/test_spec_sql.py` for repository behavior.

### Service composition

- Modify `src/doc3gpp/services/spec_service.py` to enrich list/version results and apply parsed filtering before pagination.
- Modify `src/doc3gpp/services/factory.py` to inject `SQLAlchemySpecDocRepository` as the parsed-status reader.
- Modify `tests/unit/test_spec_service.py` and `tests/unit/test_services_factory.py` for service and wiring seams.

### CLI and configuration

- Modify `src/doc3gpp/settings/schema.py` and `src/doc3gpp/data/doc3gpp.toml.example` so `parsed` is in the default spec-list field set.
- Modify `src/doc3gpp/cli.py` for `--parsed`, native null list JSON, and the per-version show field.
- Modify `tests/integration/test_spec_cli.py` and settings tests for the new option and output contract.

### REST, web, and MCP

- Modify `src/doc3gpp/web/render.py` to preserve native parsed values while keeping legacy string coercion for other fields.
- Modify `src/doc3gpp/web/routes/specs.py` for the list query/form filter and parsed field constants.
- Modify `src/doc3gpp/web/templates/partials/spec_filters.html`, `src/doc3gpp/web/templates/partials/spec_results.html`, and `src/doc3gpp/web/templates/spec_show.html` for controls and columns.
- Modify `src/doc3gpp/web/mcp_server.py` for the `list_specs(parsed=...)` argument and version output field.
- Modify `tests/unit/test_web_routes.py`, `tests/unit/web/test_mcp_server.py`, and `tests/integration/test_mcp_end_to_end.py` for REST/web/MCP parity.

### Documentation

- Modify `docs/cli.md`, `docs/web-server.md`, `README.md`, `AGENTS.md`, and `docs/code-map.md` to document the derived field and filter.
- Do not modify `src/doc3gpp/models/schema_info.py`; `parsed` is not a database column.

---

### Task 1: Add Parsed Status Domain and Repository Contracts

**Files:**
- Modify: `src/doc3gpp/models/spec.py:9-76`
- Modify: `src/doc3gpp/repository/protocols.py:366-421`
- Modify: `src/doc3gpp/storage/repositories/spec_sql.py:99-159,203-216`
- Modify: `src/doc3gpp/storage/repositories/spec_doc_sql.py:29-44`
- Test: `tests/integration/test_spec_doc_repo.py`
- Test: `tests/integration/test_spec_sql.py`
- Test: `tests/unit/test_spec_model.py`

**Interfaces:**
- Produces `spec_version_sort_key(version: str) -> tuple[int, ...]` in `doc3gpp.models.spec`.
- Produces `SpecParsedStatusRepository.list_parsed_versions(spec_ids: Iterable[str] | None = None) -> dict[str, list[str]]`.
- Changes `SpecRepository.list` to accept `limit: int | None = 50`; `None` means no SQL `LIMIT`, while all existing integer callers retain current behavior.
- Adds `Spec.parsed: str | None = None` and `SpecVersion.parsed: bool = False` as non-persisted dataclass fields.

- [ ] **Step 1: Write the failing domain and repository tests**

Add tests that lock the sort and lookup behavior before implementation:

```python
def test_spec_version_sort_key_is_numeric() -> None:
    from doc3gpp.models.spec import spec_version_sort_key

    versions = ["18.2.1", "19.1.0", "18.10.1"]
    assert sorted(versions, key=spec_version_sort_key, reverse=True) == [
        "19.1.0",
        "18.10.1",
        "18.2.1",
    ]


def test_list_parsed_versions_returns_only_parsed_rows_in_numeric_order(sqlite_env) -> None:
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.spec_doc_sql import SQLAlchemySpecDocRepository

    create_schema("specdata")
    repo = SQLAlchemySpecDocRepository()
    for version in ("18.2.1", "19.1.0", "18.10.1"):
        repo.record_parsed("36.579-5", version, chunk_count=1)
    repo.record_download(
        "36.579-5",
        "20.0.0",
        release="Rel-20",
        ftp_url="ftp://unparsed",
        docx_count=1,
    )
    repo.record_parsed("38.331", "18.5.0", chunk_count=1)

    assert repo.list_parsed_versions() == {
        "36.579-5": ["19.1.0", "18.10.1", "18.2.1"],
        "38.331": ["18.5.0"],
    }
    assert repo.list_parsed_versions(["36.579-5"]) == {
        "36.579-5": ["19.1.0", "18.10.1", "18.2.1"]
    }
    assert repo.list_parsed_versions(["99.999"]) == {}
```

Add a SQL repository test that calls `list(limit=None, offset=0)` with more rows than the normal page size and verifies every row is returned. Add a model test that constructs `Spec` and `SpecVersion` without the new arguments and confirms `parsed is None` and `parsed is False`, respectively.

- [ ] **Step 2: Run the focused tests and verify they fail for the missing interfaces**

Run:

```bash
rtk pytest tests/unit/test_spec_model.py tests/integration/test_spec_doc_repo.py tests/integration/test_spec_sql.py -q
```

Expected: failures identify the missing sort helper, parsed-status lookup, transient fields, and unbounded repository path.

- [ ] **Step 3: Implement the domain fields and shared numeric ordering**

In `src/doc3gpp/models/spec.py`:

```python
def spec_version_sort_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for segment in version.split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            parts.append(0)
    return tuple(parts)
```

Add `parsed` after `last_synced_at` on `Spec` and after `wki_id` on `SpecVersion`. Document both fields as transient output values. In `spec_sql.py`, keep `_version_sort_key(SpecVersion)` as the existing private compatibility helper and delegate to `spec_version_sort_key(version.version)`.

- [ ] **Step 4: Implement the parsed-status protocol and SQL lookup**

Add this protocol next to `SpecRepository` in `repository/protocols.py`:

```python
class SpecParsedStatusRepository(Protocol):
    """Read-only parsed-version status from the specdata database."""

    def list_parsed_versions(
        self, spec_ids: Iterable[str] | None = None
    ) -> dict[str, list[str]]:
        """Return parsed versions grouped by spec id, newest first."""
        ...
```

Add `list_parsed_versions` to `SQLAlchemySpecDocRepository`. Materialize a supplied iterable once; return `{}` immediately for an empty restriction. Query only `spec_id` and `version` where `parsed_at.is_not(None)`, group rows by spec id, and sort each list with `spec_version_sort_key(...), reverse=True`. Do not read or write any other specdata table.

Update `SQLAlchemySpecRepository.list` so it applies `offset` and `limit` only when requested:

```python
stmt = stmt.order_by(SpecORM.spec_id)
if offset:
    stmt = stmt.offset(offset)
if limit is not None:
    stmt = stmt.limit(limit)
```

Keep the default `limit=50` and all existing filter clauses unchanged.

- [ ] **Step 5: Run the focused tests and inspect the diff**

Run:

```bash
rtk pytest tests/unit/test_spec_model.py tests/integration/test_spec_doc_repo.py tests/integration/test_spec_sql.py -q
rtk ruff check src/doc3gpp/models/spec.py src/doc3gpp/repository/protocols.py src/doc3gpp/storage/repositories/spec_doc_sql.py src/doc3gpp/storage/repositories/spec_sql.py tests/unit/test_spec_model.py tests/integration/test_spec_doc_repo.py tests/integration/test_spec_sql.py
```

Expected: all focused tests pass and Ruff reports no errors.

### Task 2: Enrich SpecService and Apply Parsed Filtering Before Pagination

**Files:**
- Modify: `src/doc3gpp/services/spec_service.py:69-80,469-502`
- Modify: `tests/unit/test_spec_service.py:18-64`

**Interfaces:**
- `SpecService.__init__(repository, sync_interval=..., max_workers=None, parsed_status_repository=None)` accepts `SpecParsedStatusRepository | None` after the existing arguments.
- `SpecService.list_recent(..., parsed: bool | None = None) -> list[Spec]` enriches every returned spec and filters before slicing when `parsed` is not `None`.
- `SpecService.list_versions(...) -> list[SpecVersion]` sets `parsed` on every returned version without changing repository pagination or ordering.

- [ ] **Step 1: Add service tests for enrichment and pagination order**

Add an in-memory status reader and tests with three main-DB specs, one parsed spec with two versions, one unparsed spec, and one parsed spec whose ID would fall outside the first raw page. Assert all of the following:

```python
def test_list_recent_enriches_parsed_versions_and_uses_null_when_empty() -> None:
    status = _StubParsedStatusRepo({"36.579-5": ["19.2.0", "19.1.0"]})
    service = SpecService(_StubSpecRepoWithRows(), parsed_status_repository=status)

    rows = service.list_recent(limit=50, offset=0)

    assert rows[0].parsed == "19.2.0,19.1.0"
    assert rows[1].parsed is None


def test_list_recent_parsed_filter_happens_before_pagination() -> None:
    status = _StubParsedStatusRepo({"38.331": ["19.2.0"]})
    service = SpecService(_StubSpecRepoWithRows(), parsed_status_repository=status)

    rows = service.list_recent(limit=1, offset=0, parsed=True)

    assert [row.spec_id for row in rows] == ["38.331"]
    assert status.calls == [["36.579-5", "38.331", "39.001"]]


def test_list_versions_sets_native_booleans() -> None:
    status = _StubParsedStatusRepo({"36.579-5": ["19.2.0"]})
    service = SpecService(_StubSpecRepoWithRows(), parsed_status_repository=status)

    rows = service.list_versions("36.579-5")

    assert [row.parsed for row in rows] == [True, False]
```

The fake main repository must accept `limit: int | None` and record calls so the test proves the filtered path requests `limit=None, offset=0` before applying the service slice.

- [ ] **Step 2: Run the service tests to verify the new behavior is absent**

Run:

```bash
rtk pytest tests/unit/test_spec_service.py -q
```

Expected: the new tests fail because the service does not yet accept the status reader or `parsed` argument.

- [ ] **Step 3: Implement service enrichment and filtering**

Import `SpecParsedStatusRepository`, store the optional reader, and add a private helper with this behavior:

```python
def _enrich_specs(self, specs: list[Spec]) -> None:
    if not specs:
        return
    if self._parsed_status_repository is None:
        return
    parsed_by_spec = self._parsed_status_repository.list_parsed_versions(
        [spec.spec_id for spec in specs]
    )
    for spec in specs:
        versions = parsed_by_spec.get(spec.spec_id, [])
        spec.parsed = ",".join(versions) if versions else None
```

Extend `list_recent` with `parsed`. For `parsed is None`, retain the repository's requested limit/offset, enrich the returned page, and return it. For `parsed is True` or `False`, call the repository with `limit=None, offset=0` and the existing field filters, enrich all matching rows, keep rows where `(spec.parsed is not None) is parsed`, then return `filtered[offset : offset + limit]`. A missing status reader acts as an empty map, so all rows are unparsed and `parsed=True` returns no rows.

Extend `list_versions` to obtain the parsed map for the requested spec when a status reader exists and set `version.parsed = version.version in parsed_versions` for every returned row. Leave all repository version filters and pagination unchanged.

- [ ] **Step 4: Run service and regression tests**

Run:

```bash
rtk pytest tests/unit/test_spec_service.py tests/integration/test_spec_sql.py -q
```

Expected: the new enrichment/filter tests and existing sync/list/version tests pass.

### Task 3: Wire the Status Reader and Update Default Spec Fields

**Files:**
- Modify: `src/doc3gpp/services/factory.py:144-150`
- Modify: `src/doc3gpp/settings/schema.py:206-217`
- Modify: `src/doc3gpp/data/doc3gpp.toml.example:73-77`
- Test: `tests/unit/test_services_factory.py`
- Test: `tests/unit/test_settings_config_file.py`

**Interfaces:**
- `build_spec_service()` always injects a configured `SQLAlchemySpecDocRepository` into `SpecService` while preserving its existing return type and sync interval.
- Default `settings.output.fields.spec` becomes `spec_id,type,title,status,radio_tech,initial_release,tsg,rapporteurs,parsed`.
- The packaged TOML example documents `parsed` in the commented spec field list.

- [ ] **Step 1: Add failing settings and factory tests**

Add a settings assertion:

```python
def test_spec_default_fields_include_parsed() -> None:
    from doc3gpp.settings.schema import Settings

    assert Settings().output.fields.spec[-1] == "parsed"
```

Add a factory test that monkeypatches `SQLAlchemySpecRepository`, `SQLAlchemySpecDocRepository`, and `get_settings`, calls `build_spec_service()`, and asserts the returned service carries the patched status reader and configured `spec_sync_interval`.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
rtk pytest tests/unit/test_services_factory.py tests/unit/test_settings_config_file.py -q
```

Expected: the default field assertion and injected-reader assertion fail before implementation.

- [ ] **Step 3: Implement the factory and default field changes**

Import `SQLAlchemySpecDocRepository` and pass it to `SpecService`:

```python
return SpecService(
    SQLAlchemySpecRepository(),
    sync_interval=settings.sync.spec_sync_interval,
    parsed_status_repository=SQLAlchemySpecDocRepository(),
)
```

Append `"parsed"` after `"rapporteurs"` in `OutputFieldsSettings.spec` and in the example TOML list. Do not add `parsed` to ORM models, migration code, or schema registry entries.

- [ ] **Step 4: Run factory, settings, and service regressions**

Run:

```bash
rtk pytest tests/unit/test_services_factory.py tests/unit/test_settings_config_file.py tests/unit/test_spec_service.py -q
```

Expected: all pass, including existing callers that instantiate `SpecService` without a status reader.

### Task 4: Add CLI Parsed Filter and Native List JSON

**Files:**
- Modify: `src/doc3gpp/cli.py:2468-2483,4332-4428,4967-5073`
- Modify: `tests/integration/test_spec_cli.py`
- Test: `tests/unit/test_compact_helpers.py` only if the new spec JSON helper is placed beside compact output helpers.

**Interfaces:**
- Adds `_parse_bool_option(value: str | None, option_name: str) -> bool | None` for strict case-insensitive CLI parsing.
- `spec list --parsed true|false` forwards a native `bool | None` to `SpecService.list_recent`.
- Spec-list JSON preserves `None` for `parsed` while coercing other list fields exactly as before.
- `spec show` version fields include `parsed`; JSON preserves native booleans and table/Markdown renders `True`/`False` rather than converting `False` to `-`.

- [ ] **Step 1: Add failing CLI tests**

Extend `tests/integration/test_spec_cli.py` with tests equivalent to:

```python
def test_spec_list_json_preserves_null_parsed(monkeypatch) -> None:
    service = MagicMock()
    service.list_recent.return_value = [
        Spec(spec_id="36.579-5", type="TS", title="NR", parsed=None)
    ]
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: service)

    result = runner.invoke(app, ["spec", "list", "--format", "json"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)[0]["parsed"] is None


@pytest.mark.parametrize(("flag", "expected"), [("true", True), ("TRUE", True), ("false", False)])
def test_spec_list_parsed_filter_is_forwarded(monkeypatch, flag, expected) -> None:
    service = MagicMock()
    service.list_recent.return_value = []
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: service)

    result = runner.invoke(app, ["spec", "list", "--parsed", flag, "--format", "json"])

    assert result.exit_code == 0, result.stdout
    assert service.list_recent.call_args.kwargs["parsed"] is expected


def test_spec_list_rejects_invalid_parsed_filter(monkeypatch) -> None:
    service = MagicMock()
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: service)

    result = runner.invoke(app, ["spec", "list", "--parsed", "maybe"])

    assert result.exit_code != 0
    assert "true" in result.stdout.lower() and "false" in result.stdout.lower()


def test_spec_show_json_emits_native_parsed_boolean(monkeypatch) -> None:
    service = MagicMock()
    service.get.return_value = Spec(spec_id="36.579-5", type="TS", title="NR")
    service.list_versions.return_value = [
        SpecVersion("36.579-5", "19.2.0", "ftp://x", parsed=False)
    ]
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: service)

    result = runner.invoke(app, ["spec", "show", "36.579-5", "--format", "json"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["versions"][0]["parsed"] is False
```

Add a table assertion that the parsed column contains `False` for an unparsed version, and a `--no-wis-crs` JSON assertion that `parsed` remains present.

- [ ] **Step 2: Run the focused CLI tests and verify the new assertions fail**

Run:

```bash
rtk pytest tests/integration/test_spec_cli.py -q
```

Expected: failures show the missing option, null preservation, and version field.

- [ ] **Step 3: Implement strict CLI parsing and output branches**

Add `_parse_bool_option` near `_resolve_format`:

```python
def _parse_bool_option(value: str | None, option_name: str) -> bool | None:
    if value is None:
        return None
    normalized = value.lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise typer.BadParameter(f"{option_name} must be 'true' or 'false'")
```

Add `parsed: str | None = typer.Option(None, "--parsed", help="Filter by whether any stored spec document is parsed: true or false.")` to `spec_list`. Parse it before building the service call and pass `parsed=parsed_filter` to `list_recent`.

For JSON list output, use a spec-specific payload builder rather than `_emit_json`, because `_emit_json` intentionally stringifies every generic list cell:

```python
def _spec_list_json_rows(records: list[Spec], fields: list[str]) -> list[dict[str, object]]:
    return [
        {
            field: getattr(record, field, None)
            if field == "parsed"
            else str(getattr(record, field, None) or "-")
            for field in fields
        }
        for record in records
    ]
```

When `fmt == "json"`, call `_dump_show_json(_spec_list_json_rows(records, default_fields), output, compact=resolved_compact)` and return. Keep `_emit_records` for table and Markdown rows, where `None` is rendered as `-`.

Append `"parsed"` to `version_fields` in `spec_show`. Add a display helper that returns `str(value)` for booleans and `str(value or "-")` otherwise, then use it for header/version table rows so `False` remains visible.

- [ ] **Step 4: Run CLI tests and output-format regressions**

Run:

```bash
rtk pytest tests/integration/test_spec_cli.py tests/unit/test_compact_helpers.py -q
```

Expected: all CLI parsed-filter, null, boolean, compact, and existing spec sync/show tests pass.

### Task 5: Add REST/Web Parsed Filter and Columns

**Files:**
- Modify: `src/doc3gpp/web/render.py:259-349`
- Modify: `src/doc3gpp/web/routes/specs.py:21-114,149-201`
- Modify: `src/doc3gpp/web/templates/partials/spec_filters.html:1-40`
- Modify: `src/doc3gpp/web/templates/partials/spec_results.html:1-43`
- Modify: `src/doc3gpp/web/templates/spec_show.html:37-88`
- Modify: `tests/unit/test_web_routes.py`

**Interfaces:**
- `_coerce_cell` remains unchanged for all existing resources.
- `spec_rows` returns `list[dict[str, object]]`, preserving `None` only for the `parsed` field.
- `spec_version_rows` returns `list[dict[str, object]]`, preserving native `bool` only for the `parsed` field.
- `GET /specs?parsed=true|false` forwards a native boolean to `SpecService.list_recent`; `parsed=TRUE` is accepted and `parsed=maybe` returns HTTP 400.
- Web pagination keeps `parsed` automatically because pagination links reuse the current request query string.

- [ ] **Step 1: Add failing REST/web tests**

Add route tests that cover JSON values and service calls:

```python
def test_specs_json_preserves_parsed_null_and_forwards_filter(client, spec_service) -> None:
    spec_service.list_recent.return_value = [
        Spec(spec_id="36.579-5", type="TS", title="NR", parsed=None)
    ]

    response = client.get("/specs?format=json&parsed=TRUE")

    assert response.status_code == 200
    assert response.json()[0]["parsed"] is None
    assert spec_service.list_recent.call_args.kwargs["parsed"] is True


def test_specs_rejects_invalid_parsed_query(client, spec_service) -> None:
    response = client.get("/specs?parsed=maybe")

    assert response.status_code == 400


def test_spec_show_json_preserves_version_boolean(client, spec_service) -> None:
    spec_service.get.return_value = Spec(spec_id="36.579-5", type="TS", title="NR")
    spec_service.list_versions.return_value = [
        SpecVersion("36.579-5", "19.2.0", "ftp://x", parsed=False)
    ]

    response = client.get("/specs/36.579-5?format=json")

    assert response.status_code == 200
    assert response.json()["versions"][0]["parsed"] is False
```

Add HTML assertions that the spec filter form contains `name="parsed"` with Any/true/false choices, the list table contains the parsed-version column, the selected true value survives the rendered form, and the show table displays `false`. Add direct renderer tests for `spec_rows([Spec(parsed=None)], ["parsed"]) == [{"parsed": None}]` and `spec_version_rows([SpecVersion(..., parsed=False)], ["parsed"]) == [{"parsed": False}]`.

- [ ] **Step 2: Run the focused web tests and verify the new assertions fail**

Run:

```bash
rtk pytest tests/unit/test_web_routes.py -q
```

Expected: failures identify missing field constants, query forwarding, native value preservation, and template controls.

- [ ] **Step 3: Implement native parsed rendering**

In `web/render.py`, add a narrow helper:

```python
def _spec_cell(field: str, value: Any) -> object:
    if field == "parsed":
        return value
    return _coerce_cell(value)
```

Use it only inside `spec_rows` and `spec_version_rows`; leave meeting, TDoc, TSG, WI, and testcase renderers unchanged.

- [ ] **Step 4: Implement route parsing and field selection**

Add `parsed` to `_SPEC_DEFAULT_FIELDS` after `rapporteurs` and to `_VERSION_FIELDS` after `version`. Add `parsed: str | None = Query(default=None)` to `list_specs`. Normalize a non-empty raw value with `.lower()` before passing it to `parse_bool_query`; pass `None` for an omitted or empty value. Forward the resulting `bool | None` to `service.list_recent`.

Store the canonical filter value in the template context:

```python
"parsed": "" if parsed_filter is None else str(parsed_filter).lower(),
```

This makes Any selected for omitted/empty input and preserves true/false through HTMX refreshes and pagination. `show_spec` automatically receives version booleans from the updated service and includes `parsed` through `_VERSION_FIELDS`.

- [ ] **Step 5: Implement the template controls and columns**

Add this select to `partials/spec_filters.html`:

```html
<label>Parsed
  <select name="parsed">
    <option value="" {% if not filters.parsed %}selected{% endif %}>Any</option>
    <option value="true" {% if filters.parsed == 'true' %}selected{% endif %}>true</option>
    <option value="false" {% if filters.parsed == 'false' %}selected{% endif %}>false</option>
  </select>
</label>
```

Add a `Parsed versions` header/cell to `partials/spec_results.html`, displaying `spec.parsed or '-'` before the show link. Add a `Parsed` header/cell to `spec_show.html`, displaying `true` or `false` explicitly:

```jinja2
<td>{{ 'true' if v.parsed else 'false' }}</td>
```

Keep the existing `no_wis_crs` condition around only the CR column; parsed remains visible in both normal and slim views.

- [ ] **Step 6: Run web and template regressions**

Run:

```bash
rtk pytest tests/unit/test_web_routes.py tests/unit/web/test_mcp_server.py -q
```

Expected: REST JSON, HTML, HTMX fragment, pagination, and existing spec show/list tests pass.

### Task 6: Add MCP Parsed Filter and Output Parity

**Files:**
- Modify: `src/doc3gpp/web/mcp_server.py` spec field constants and `list_specs`/`get_spec` tool definitions
- Modify: `tests/unit/web/test_mcp_server.py`
- Modify: `tests/integration/test_mcp_end_to_end.py`

**Interfaces:**
- `list_specs(..., parsed: bool | None = None) -> str` forwards the optional boolean to `SpecService.list_recent`.
- MCP list output uses `_SPEC_FIELDS` including `parsed` and emits native JSON `null` for no parsed versions.
- MCP `get_spec` uses `_VERSION_FIELDS` including `parsed` and emits native JSON booleans.
- Existing `no_wis_crs` behavior continues to remove only `wis` and `crs`.

- [ ] **Step 1: Add failing MCP tests**

Extend the MCP tool tests with these assertions:

```python
def test_list_specs_accepts_parsed_filter(mcp_tools, spec_service) -> None:
    spec_service.list_recent.return_value = [
        Spec(spec_id="36.579-5", type="TS", title="NR", parsed=None)
    ]

    payload = json.loads(mcp_tools["list_specs"](parsed=True))

    assert payload[0]["parsed"] is None
    assert spec_service.list_recent.call_args.kwargs["parsed"] is True


def test_get_spec_emits_native_version_parsed_boolean(mcp_tools, spec_service) -> None:
    spec_service.get.return_value = Spec(spec_id="36.579-5", type="TS", title="NR")
    spec_service.list_versions.return_value = [
        SpecVersion("36.579-5", "19.2.0", "ftp://x", parsed=False)
    ]

    payload = json.loads(mcp_tools["get_spec"]("36.579-5"))

    assert payload["versions"][0]["parsed"] is False
```

The end-to-end MCP test should also assert the default `list_specs` field set contains `parsed` and `get_spec(..., no_wis_crs=True)` still contains the version `parsed` field while omitting `crs`.

- [ ] **Step 2: Run the focused MCP tests and verify they fail**

Run:

```bash
rtk pytest tests/unit/web/test_mcp_server.py tests/integration/test_mcp_end_to_end.py -q
```

Expected: failures identify the missing MCP argument and output fields.

- [ ] **Step 3: Implement MCP field and argument changes**

Add `parsed` after `rapporteurs` in `_SPEC_FIELDS`, add `parsed` after `version` in `_VERSION_FIELDS`, and leave `_SPEC_SHOW_FIELDS` unchanged. Extend the tool description to mention parsed status. Add:

```python
parsed: Annotated[
    bool | None,
    Field(description="Filter by whether any stored spec document is parsed.")
] = None,
```

to `list_specs`, and pass `parsed=parsed` to `services.spec.list_recent`. The existing `render.spec_rows` and `render.spec_version_rows` changes provide native values to `_to_json`.

- [ ] **Step 4: Run MCP parity tests**

Run:

```bash
rtk pytest tests/unit/web/test_mcp_server.py tests/integration/test_mcp_end_to_end.py -q
```

Expected: MCP list/show JSON matches the REST structured-value contract.

### Task 7: Synchronize User Documentation

**Files:**
- Modify: `docs/cli.md:2326-2416`
- Modify: `docs/web-server.md` spec route/MCP sections
- Modify: `README.md` spec command examples
- Modify: `AGENTS.md` spec workflow/default-fields descriptions
- Modify: `docs/code-map.md` `SpecService` and `SpecRepository` entries

- [ ] **Step 1: Add documentation tests or source assertions where existing documentation tests cover defaults**

If the existing settings/config documentation test checks the commented TOML field list, extend it to require `parsed`. Otherwise, use the settings test from Task 3 as the executable source-of-truth check and keep these changes textual.

- [ ] **Step 2: Update the CLI contract**

In `docs/cli.md`, document:

- `--parsed true|false` under `spec list`.
- `parsed` in the default fields after `rapporteurs`.
- List JSON semantics: comma-separated numeric-newest-first versions, or JSON `null` when none are parsed; table/Markdown `-`.
- `spec show` version JSON semantics: native boolean `parsed` for every version.
- Filtering before pagination.

Add examples:

```bash
doc3gpp spec list --parsed true --format json
doc3gpp spec list --parsed false --limit 20
```

- [ ] **Step 3: Update web/MCP and architecture references**

Document `GET /specs?parsed=true|false`, the Any/true/false web control, the native JSON values, and the MCP `list_specs(parsed=...)` argument in `docs/web-server.md`. Update `README.md`, `AGENTS.md`, and `docs/code-map.md` to state that parsed status is derived from the separate specdata ledger and is not a main-table column. Keep schema documentation unchanged because no schema field was added.

- [ ] **Step 4: Check documentation and formatting**

Run:

```bash
rtk git diff --check
rtk grep 'parsed' docs/cli.md docs/web-server.md README.md AGENTS.md docs/code-map.md src/doc3gpp/data/doc3gpp.toml.example
```

Expected: every public surface documents the same null/boolean/filter semantics.

### Task 8: Full Verification and Review

**Files:**
- Verify all files changed by Tasks 1-7.

- [ ] **Step 1: Run focused feature tests together**

Run:

```bash
rtk pytest \
  tests/unit/test_spec_model.py \
  tests/unit/test_spec_service.py \
  tests/unit/test_services_factory.py \
  tests/unit/test_settings_config_file.py \
  tests/unit/test_web_routes.py \
  tests/unit/web/test_mcp_server.py \
  tests/integration/test_spec_doc_repo.py \
  tests/integration/test_spec_sql.py \
  tests/integration/test_spec_cli.py \
  tests/integration/test_mcp_end_to_end.py -q
```

Expected: all parsed-output and existing spec tests pass.

- [ ] **Step 2: Run the complete offline SQLite suite**

Run the repository-standard command exactly:

```bash
./scripts/test_sqlite.sh
```

Expected: the full offline suite passes with no new failures. Online tests are not required for this derived, database-only feature.

- [ ] **Step 3: Run lint and whitespace checks**

Run:

```bash
rtk ruff check .
rtk git diff --check
```

Expected: Ruff and whitespace checks pass.

- [ ] **Step 4: Inspect the final diff for contract regressions**

Run:

```bash
rtk git status --short
rtk git diff --stat
rtk git diff -- src/doc3gpp/models/spec.py src/doc3gpp/services/spec_service.py src/doc3gpp/cli.py src/doc3gpp/web/routes/specs.py src/doc3gpp/web/render.py src/doc3gpp/web/mcp_server.py
```

Confirm manually:

- `spec list` default fields include `parsed` in settings, CLI, web, MCP, and TOML example.
- `parsed: null` survives CLI/REST/MCP JSON when no source row is parsed.
- `parsed: false` survives CLI/REST/MCP spec-show JSON for unparsed versions.
- `--parsed false` and `?parsed=false` include only specs with no parsed version, then paginate.
- `no_wis_crs` still removes only `wis` and `crs`.
- No ORM, migration, or static schema registry file contains a new `parsed` database column.

Plan complete. After implementation and verification, the user can request commits or branch integration explicitly.
