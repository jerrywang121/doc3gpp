# Spec-document ZIP Inventory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** List the original outer-ZIP filenames for each spec-document version, collected only during parsing and exposed consistently across the SDK, CLI, web, HTTP JSON, and MCP.

**Architecture:** Store an ORM-free inventory independently in the specdata database, keyed by `(spec_id, version)` and carrying the version's release. Enumerate during parse preparation and persist on the serialized writer, including when later preparation fails; enrich selected `SpecVersion` values with batched inventory reads for all detail surfaces.

**Tech Stack:** Python 3.10+, SQLAlchemy 2, SQLite, Typer, FastAPI/Jinja, MCP, pytest.

**Spec:** `docs/superpowers/specs/2026-10-29-spec-doc-zip-inventory-design.md`

## Global Constraints

- Work on the current branch; do not create a worktree or commit automatically. `docs/conventions.md` requires user-requested commits only.
- Use subagent-driven development and TDD: see each test fail for the intended missing behavior before implementation, then run it green. Review each task before starting the next.
- Use pytest-xdist for test runs: invoke pytest with `-n auto`. If shared-state or resource limits make a particular run impractical, diagnose and report the evidence rather than silently switching to sequential execution.
- ZIP enumeration uses `enumerate_zip_files(bytes) -> tuple[str, ...]`, preserves original decoded order and duplicates, excludes directories, and never opens nested archives or extracts members for inventory.
- Collection occurs only for an actual spec-document parse, not spec sync, schema init, detail reads, or an immutable parsed-version skip. `--force` re-downloads. Reads must not inspect the cache or network.
- `(spec_id, version)` is the existing unique identity; release is a nullable attribute, not a third independent key. No foreign key to the source ledger.
- A missing inventory is `None` / JSON `null`; a known empty archive is `()` / JSON `[]`. Never leak ORM rows. Filenames are untrusted: exact in JSON, inert in text/Markdown, escaped in HTML.
- Missing *inventory table specifically* is an unknown read, not a general database-error suppression. Successful preparation must fail with an actionable `doc3gpp db init --scope specdata` hint if inventory cannot be written; on a preparation failure log a failed inventory write and re-raise the original error.
- Preserve existing release/version resolution, ZIP size/download/skip rules, parsed-marker invalidation, indexing, progress, cancellation, pagination, filters, and `--no-wis-crs` behavior. No public fetch/inventory command, backfill, search filter, or new version-page JSON endpoint.
- Update `README.md`, `AGENTS.md`, `docs/cli.md`, `docs/web-server.md`, and affected architecture/schema/code-map documentation in this change set.

## Review Focus

The following high-risk inputs must each have an explicit test in the owning task:

1. A previously initialized specdata database lacks only the new table: detail reads are unknown; successful writes fail with the upgrade command; repeated `db init --scope specdata` adds the table without losing source rows (Tasks 1 and 2).
2. A ZIP contains duplicate, Unicode, hidden, nested, and unsafe names: inventory keeps their exact order and spelling, while all text/HTML displays remain inert (Tasks 2, 4, and 5).
3. DOCX conversion or selection fails after ZIP enumeration, and the inventory write also fails: preserve the original error and its failed/skipped classification, log the write failure, and never claim parsed content (Task 2).
4. A cancellation occurs between queued preparation and writer submission: no inventory is published and the existing permit/cancellation behavior remains intact (Task 2).
5. A page has multiple releases, unknown and empty inventories, or an evicted ZIP cache: one batched lookup enriches only selected versions, all JSON surfaces agree, and reads never fetch, parse, or access the cache (Tasks 3 and 5).

---

### Task 1: Specdata inventory repository and schema

**Files:**
- Create: `src/doc3gpp/models/spec_doc_zip_inventory.py`, `src/doc3gpp/storage/repositories/spec_doc_zip_inventory_sql.py`, `tests/integration/test_spec_doc_zip_inventory_sqlite.py`
- Modify: `src/doc3gpp/repository/protocols.py`, `src/doc3gpp/storage/db/models.py`, `src/doc3gpp/storage/db/migrate.py`, `src/doc3gpp/models/schema_info.py`, schema registry tests in `tests/unit/test_schema_info.py` and any existing tests with fixed schema counts.

**Interfaces:**
- Consumes: `SpecDataBase`, `get_specdata_session_factory`, `create_schema("specdata")`, and existing TDoc inventory serialization conventions.
- Produces: frozen, slotted `SpecDocZipInventory(spec_id: str, version: str, release: str | None, zip_files: tuple[str, ...])`; `SpecDocInventorySchemaError(RuntimeError)` with the upgrade hint; `SpecDocZipInventoryRepository` Protocol and `SQLAlchemySpecDocZipInventoryRepository(session_factory=None)` with `get(spec_id: str, version: str) -> SpecDocZipInventory | None`, `get_for_versions(spec_id: str, versions: list[str]) -> dict[str, SpecDocZipInventory]`, and `upsert(inventory: SpecDocZipInventory) -> None`.

- [ ] **Step 1: Write failing repository and schema tests.** Use the existing `sqlite_env` fixture and `create_schema`. Add `test_inventory_round_trip_and_isolation`, `test_inventory_empty_and_missing_are_distinct`, `test_inventory_batch_uses_one_query_for_a_normal_page`, `test_missing_table_only_is_unknown_and_writes_explain_upgrade`, `test_other_database_errors_are_not_hidden`, and `test_specdata_upgrade_is_additive_and_idempotent`. Pin these assertions:

  ```python
  assert repo.get("38.331", "18.5.0") == SpecDocZipInventory(
      "38.331", "18.5.0", "Rel-18", ("α.docx", "nested.zip", "α.docx")
  )
  assert repo.get("38.331", "19.0.0").zip_files == ()
  assert repo.get("38.331", "17.0.0") is None
  assert set(repo.get_for_versions("38.331", ["18.5.0", "19.0.0"])) == {
      "18.5.0", "19.0.0"
  }
  ```

  Seed a different spec with the same version; update one pair and assert the other pairs and nullable release survive. Count SQL queries with an engine listener. Build an old specdata schema without the inventory table and assert reads yield `None`/`{}`, upsert raises `SpecDocInventorySchemaError` containing `db init --scope specdata`, and an unrelated `OperationalError` propagates. Run `create_schema("specdata")` twice and verify a pre-existing source row survives and the new table exists only in specdata. Assert the static registry has all four actual ORM columns, proper nullability, and no main-DB `spec_versions.zip_files` column.
- [ ] **Step 2: Run the tests and confirm red.** Run `python -m pytest tests/integration/test_spec_doc_zip_inventory_sqlite.py tests/unit/test_schema_info.py -q -n auto`. Expected: the new inventory symbols/schema are missing, not a fixture or dependency failure.
- [ ] **Step 3: Implement the interfaces.** Add a `SpecDataBase` ORM table `spec_doc_zip_inventory` with primary key `(spec_id, version)`, nullable `release`, and non-null JSON text `zip_files`; serialize losslessly, including empty arrays. Implement one batched select for ordinary pages and safe chunking if a dialect's bind limit requires it. Suppress only the explicitly missing inventory-table error on reads; translate that same specific error on writes to `SpecDocInventorySchemaError`. Do not create tables during reads. Register the table and let the existing metadata bootstrap create it additively; update the bootstrap docstring and fixed registry expectations.
- [ ] **Step 4: Verify green.** Run `python -m pytest tests/integration/test_spec_doc_zip_inventory_sqlite.py tests/integration/test_spec_doc_repo.py tests/unit/test_schema_info.py -q -n auto`. Expected: all pass.

### Task 2: Parse-time collection and serialized failure persistence

**Files:**
- Modify: `src/doc3gpp/models/spec_doc.py`, `src/doc3gpp/services/spec_doc_service.py`, `src/doc3gpp/services/factory.py`, `src/doc3gpp/web/workers/handlers.py`, `tests/integration/test_spec_doc_service.py`, `tests/unit/test_spec_doc_pipeline.py`.
- Test: `tests/unit/test_tdoc_zip.py` for the shared enumerator's existing behavior; add focused cases there only if an input below is not already covered.

**Interfaces:**
- Consumes: Task 1's inventory value, repository, and schema error; `enumerate_zip_files(raw: bytes) -> tuple[str, ...]`; existing spec-doc resolver and pipeline.
- Produces: `PreparedSpecDocParse.inventory: SpecDocZipInventory`; frozen `PreparedSpecDocInventoryFailure(inventory: SpecDocZipInventory, error: Exception)` in `models/spec_doc.py`; optional `inventory_repo` injection on `SpecDocService`. `prepare_parse(...) -> PreparedSpecDocParse | PreparedSpecDocInventoryFailure | SpecDocSource`; `persist_prepared_parse(prepared: PreparedSpecDocParse | PreparedSpecDocInventoryFailure) -> SpecDocSource` re-raises a carried error after its inventory write attempt.

- [ ] **Step 1: Write failing parse and pipeline tests.** Extend the existing `_make_zip_bytes`, `FakeSpecRepo`, `_service`, and `_run_handler` fixtures/helpers. Use ZIPs with `main.docx`, a directory, `readme.txt`, `.hidden`, `__MACOSX/meta`, `nested.zip`, Unicode, a backslash name, and a duplicate; compare to the exact non-directory central-directory order. Add named tests with these assertions:

  ```python
  # test_prepare_carries_inventory_without_writing
  assert prepared.inventory.zip_files == expected_outer_names
  inventory_repo.upsert.assert_not_called()
  # test_no_docx_and_empty_zip_publish_inventory_but_fail
  assert result.failures["38.331"]  # contains the original no-.docx error
  assert inventory_repo.get("38.331", "18.5.0").zip_files == expected_outer_names
  # test_failed_preparation_and_failed_inventory_write_preserves_original
  assert result.failures["38.331"] == "conversion exploded"
  assert "inventory" in caplog.text.lower()
  # test_queued_failure_persists_on_writer_before_reporting_original
  assert result["failure_details"] == {"38.331": "conversion exploded"}
  assert write_thread == writer_thread and prepare_thread != writer_thread
  ```

  Also test normal parsing; force re-download and replace; immutable skip with no download/enumeration/write; an unparsed cached ZIP with no fetch; the existing SDK `fetch` helper alone does not collect an inventory; no-DOCX and empty ZIPs without a source row; a conversion exception after enumeration; a successful parse whose inventory write hits a missing table; invalid ZIP, failed download, and oversized ZIP leaving a previous inventory unchanged; and both forced synchronous and queued parsed-marker semantics. Assert no failure path invents parsed content. Exercise actual queued `PipelineRuntime` writer/prepare threads, queued error classification, progress and permit release; cancel after preparation and before writer submission and assert no inventory write. Inject fake inventory repositories into existing isolated unit fixtures rather than inadvertently using a real database.
- [ ] **Step 2: Run the tests and confirm red.** Run `python -m pytest tests/integration/test_spec_doc_service.py tests/unit/test_spec_doc_pipeline.py -q -n auto`. Expected: new inventory assertions or injection fail for missing behavior.
- [ ] **Step 3: Implement the collection flow.** After the existing resolved-version skip, download/cache selection, and download size check, enumerate the ZIP before `list_spec_docx`; enumeration failures propagate without inventory writes. Build the inventory with the resolved release. Carry it on success or catch subsequent ordinary `Exception` as `PreparedSpecDocInventoryFailure`; do not catch cancellation or other `BaseException`s. Preparation performs no repository writes. In the writer, upsert before reporting success or writing parsed content; for a carried error, best-effort upsert, log any write failure, and re-raise the original exception. Preserve the existing synchronous force invalidation timing and queued behavior. Wire the real repository through service/factory construction and route both successful and failure preparations through the writer in `_parse_spec_docs` without changing skip/cancellation handling.
- [ ] **Step 4: Verify green.** Run `python -m pytest tests/unit/test_tdoc_zip.py tests/integration/test_spec_doc_service.py tests/unit/test_spec_doc_pipeline.py tests/integration/test_spec_doc_docx_pool_equivalence.py -q -n auto`. Expected: all pass.

### Task 3: Batched SDK version enrichment

**Files:**
- Modify: `src/doc3gpp/models/spec.py`, `src/doc3gpp/services/spec_service.py`, `src/doc3gpp/services/factory.py`, `src/doc3gpp/web/app.py` if its composition bypasses the factory, `tests/unit/test_spec_service.py`, `tests/unit/test_services_factory.py`.
- Test: `tests/integration/test_spec_parsed_output.py`, `tests/integration/test_spec_sql.py`.

**Interfaces:**
- Consumes: Task 1's `SpecDocZipInventoryRepository.get_for_versions` and Task 2's persisted records.
- Produces: transient `SpecVersion.zip_files: tuple[str, ...] | None = None`; optional `inventory_repository: SpecDocZipInventoryRepository | None` on `SpecService`; the existing `list_versions(spec_id: str, limit: int = 200, offset: int = 0, version: str | None = None) -> list[SpecVersion]` returns enriched domain rows.

- [ ] **Step 1: Write failing SDK and composition tests.** Seed versions `19.0.0`, `18.5.0`, and `17.0.0`, with inventories `("a.docx", "a.docx")`, `()`, and missing respectively; associate `Rel-19`, `Rel-18`, `Rel-17`. Assert:

  ```python
  assert [row.zip_files for row in rows] == [("a.docx", "a.docx"), (), None]
  inventory_repo.get_for_versions.assert_called_once_with(
      "38.331", ["19.0.0", "18.5.0", "17.0.0"]
  )
  assert [row.version for row in filtered] == ["18.5.0"]
  assert filtered[0].zip_files == ()
  ```

  Assert lookup is batched only for selected rows after pagination/filtering, parsed flags still work with and without a parsed-status collaborator, a missing inventory collaborator leaves `None`, no rows produce no lookup, and factory/web composition injects the real inventory repository. Poison network, cache-read, and parse functions and assert SDK list still works; delete an existing ZIP cache before reading. Verify main ORM/schema has no new version column and sync does not collect inventories.
- [ ] **Step 2: Run the tests and confirm red.** Run `python -m pytest tests/unit/test_spec_service.py tests/unit/test_services_factory.py tests/integration/test_spec_parsed_output.py -q -n auto`. Expected: missing field/enrichment or wiring assertions fail.
- [ ] **Step 3: Implement enrichment.** Add the defaulted transient field at the end of `SpecVersion` to preserve positional callers. Make parsed-status and inventory enrichment independent: the current parsed-status early return must not suppress inventory enrichment. Batch by the already selected version strings, attach exact tuples or `None`, and inject the SQL collaborator in normal CLI/SDK/web service construction without adding it to sync or list-spec rows.
- [ ] **Step 4: Verify green.** Run `python -m pytest tests/unit/test_spec_service.py tests/unit/test_services_factory.py tests/integration/test_spec_sql.py tests/integration/test_spec_parsed_output.py -q -n auto`. Expected: all pass.

### Task 4: CLI spec detail and documentation

**Files:**
- Modify: `src/doc3gpp/cli.py`, `tests/integration/test_spec_cli.py`, `README.md`, `AGENTS.md`, `docs/cli.md`, `docs/architecture.md`, `docs/code-map.md`.

**Interfaces:**
- Consumes: Task 3's enriched `SpecVersion.zip_files`.
- Produces: `spec show` version JSON with `zip_files` appended after existing version fields; table, Markdown, and compact output with a per-version inventory, explicit unavailable/empty states, and a force-reparse plus schema-upgrade hint for unknown.

- [ ] **Step 1: Write failing CLI tests.** Seed the Task 3 three-version example and exercise `spec show --format json`, default table, `--format markdown`, and `--format markdown --compact`. Assert JSON lists are `["a.docx", "a.docx"]`, `[]`, and `null`, in their matching versions/releases; `--version`, `--limit`, `--offset`, and `--no-wis-crs` retain their existing behavior. Assert the existing `spec list --parsed` filter is unchanged; `spec show` has no such flag. For unknown output assert `--force` and `db init --scope specdata` hints; for empty assert a distinct no-files message. Include filename `<script>x</script>\n|[click](https://bad.test)` and terminal escape characters: JSON preserves the exact string, while table/Markdown/compact do not emit active control characters, HTML, Markdown links, injected rows, or terminal escapes.
- [ ] **Step 2: Run the tests and confirm red.** Run `python -m pytest tests/integration/test_spec_cli.py -q -n auto`. Expected: `zip_files` and display assertions fail.
- [ ] **Step 3: Implement the CLI display and documentation.** Append `zip_files` to the version JSON field list, explicitly converting tuples to JSON arrays without collapsing `()` into unknown. Reuse existing `_display_zip_file_name`, `_markdown_zip_file_name`, and `_markdown_code_span` as appropriate; never interpolate raw names into Markdown/table cells. Show inventory with each selected version and preserve all other fields/options. Update the named docs with collection timing, exact JSON semantics, old-database upgrade, `--force` refresh, and cache independence; describe the separate table and update schema counts where relevant.
- [ ] **Step 4: Verify green.** Run `python -m pytest tests/integration/test_spec_cli.py -q -n auto`. Expected: all pass. The existing cross-surface key-order test is updated with both HTTP and MCP adapters in Task 5.

### Task 5: Web, HTTP, MCP parity and whole-change verification

**Files:**
- Modify: `src/doc3gpp/web/render.py`, `src/doc3gpp/web/routes/specs.py`, `src/doc3gpp/web/routes/spec_docs.py`, `src/doc3gpp/web/mcp_server.py`, `src/doc3gpp/web/templates/spec_show.html`, `src/doc3gpp/web/templates/spec_doc_show.html`, `tests/integration/test_spec_parsed_output.py`, `tests/integration/test_spec_doc_web.py`, `tests/unit/web/test_mcp_server.py`, `docs/web-server.md`, `AGENTS.md`.

**Interfaces:**
- Consumes: Task 3's SDK version rows and Task 4's ordered CLI version JSON contract.
- Produces: `GET /specs/{spec_id}?format=json` and `get_spec` per-version `zip_files` matching CLI; spec detail and `/specs/{spec_id}/docs?version=...` HTML show the same inventory even without a successfully parsed source. Existing spec-document version page remains HTML-only.

- [ ] **Step 1: Write failing surface tests.** Extend `tests/integration/test_spec_parsed_output.py` with multiple versions and compare CLI, HTTP, and MCP JSON values and key order. Assert:

  ```python
  assert [v["version"] for v in cli["versions"]] == [v["version"] for v in http["versions"]]
  assert [v["zip_files"] for v in cli["versions"]] == [v["zip_files"] for v in mcp["versions"]]
  assert [v["zip_files"] for v in http["versions"]] == [
      ["a.docx", "a.docx"], [], None
  ]
  assert "&lt;script&gt;" in html and "<script>alert(1)</script>" not in html
  ```

  For matching pagination/filter arguments assert only requested versions appear; compare equivalent HTTP and MCP responses under the existing byte-parity convention without altering other field serialization. Update the existing shared version-key-order expectation to append `zip_files`. Test both HTML pages for known, empty, unknown with reparse/upgrade hints, Unicode, and hostile names; create an inventory without a source row to model failed conversion and assert it remains visible on both pages. Verify existing HTMX version-page updates do not erase or duplicate the inventory. Poison network, parser, and ZIP-cache reads, then remove the cache and verify every detail surface still works. Verify missing-table detail reads remain `null`, and non-inventory DB errors are not disguised as unknown.
- [ ] **Step 2: Run the tests and confirm red.** Run `python -m pytest tests/integration/test_spec_parsed_output.py tests/integration/test_spec_doc_web.py tests/unit/web/test_mcp_server.py -q -n auto`. Expected: mismatched/missing `zip_files` or HTML assertions fail.
- [ ] **Step 3: Implement the surface adapters.** Append `zip_files` in the same position in both `_VERSION_FIELDS` lists, and make `spec_version_rows` preserve `None` or convert tuples to arrays instead of stringifying them. In templates show version-specific inventories independently of source/TOC/chunks, relying on Jinja autoescaping; never mark names safe or turn them into links. Use the existing version lookup and service enrichment for the version page, and include unavailable/reparse/upgrade hints. Update web documentation and the agent guide with the new detail behavior and specdata table.
- [ ] **Step 4: Verify targeted and full regression suites.** Run `python -m pytest tests/integration/test_spec_parsed_output.py tests/integration/test_spec_doc_web.py tests/unit/web/test_mcp_server.py -q -n auto`, then `ruff check .`, then `./scripts/test_sqlite.sh` (which uses `-n auto` when pytest-xdist is installed). Expected: all pass; report actual results and any environment blockers without claiming unrun tests passed. Review `git diff` and `git status --short --branch` to confirm only intended files on the original branch and no automatic commit.
