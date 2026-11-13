# LISTSERV Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not commit unless the user explicitly authorizes it.

**Goal:** Add a prepopulated, relational LISTSERV catalog, safely upgrade existing databases, register valid discoveries, and expose list metadata through CLI, HTTP/REST, and MCP.

**Architecture:** Main-schema initialization inserts missing TSG and catalog references, and a targeted transactional SQLite upgrade backfills legacy IDs and adds list foreign keys. Sync buffers unknown-list empty pages until an atomic message-bearing commit registers the list; repository-backed catalog records feed a shared HTTP/MCP serializer and CLI display.

**Tech Stack:** Python 3.10+, SQLAlchemy 2, SQLite, Typer, FastAPI, existing MCP server, pytest.

**Spec:** `docs/superpowers/specs/2026-11-12-listserv-catalog-design.md`.

## Global Constraints

- Copy all 97 catalog seed tuples verbatim from the approved spec, including Unicode; do not seed `3GPP_TSG_RAN_WG6_DRAFTS`.
- R6 is `RAN WG6`, description `(CLOSED) GSM/EDGE Radio Access Network (GERAN) and UMTS Radio Access (UTRA)`, with null URL.
- Main DB only; preserve existing messages, details, page coverage, pagination URLs, indexes, derived FTS/vector state, and user-maintained metadata.
- Seed initial catalog entries before backfilling legacy message- or page-only IDs; legacy-only rows get null metadata.
- Register unknown IDs only with a message matching requested dates; preserve per-page transactionality and existing cancellation, retry, network-security and index semantics.
- No ORM entities in service or model interfaces; reads do not perform network requests or parsing.
- Do not auto-commit or change unrelated working-tree files; follow `AGENTS.md` validation and documentation rules.

## Review Focus

1. A legacy message has details with `ON DELETE CASCADE`: a failed or successful migration must never erase them (Task 2).
2. Concurrent initializations or discoveries target the same ID: no duplicate, lost metadata, orphan row, or reported success after failure (Tasks 2–3).
3. An unknown list has zero-match pages before a later pagination match: traversal continues, and coverage is persisted only after registration (Task 3).
4. An existing TSG or catalog row has locally edited metadata: automatic initialization must not overwrite it (Task 1).
5. CLI, HTTP, MCP and the web selector consume the same catalog, including null metadata and no-message entries, without breaking the existing `list_ids` field (Task 4).

---

### Task 1: Reference catalog and fresh schema

**Files:**
- Create: `src/doc3gpp/models/tsg_defaults.py`, `src/doc3gpp/models/listserv_catalog.py`, `src/doc3gpp/storage/db/listserv_catalog.py`
- Modify: `src/doc3gpp/services/tsg_service.py`, `src/doc3gpp/storage/db/models.py`, `src/doc3gpp/storage/db/migrate.py`, `src/doc3gpp/models/schema_info.py`
- Test: create `tests/integration/test_listserv_catalog_sqlite.py`; update `tests/unit/test_tsg_service.py`, `tests/integration/test_tsg_sqlite.py`, `tests/unit/test_schema_info.py`, and `tests/integration/test_schema_parity.py` as needed.

**Interfaces:**
- Produces `default_tsgs() -> tuple[Tsg, ...]` and the existing public `build_tsg_url(tsg_name: str) -> str | None`, both backed by the same canonical TSG definitions used for normal explicit TSG seeding.
- Produces `LISTSERV_CATALOG_SEEDS: tuple[tuple[str, str, str], ...]`, `ListservListORM` (`listserv_list`, PK `list_id`, nullable `tsg` FK to `tsgs.short_name`, nullable `description`), and `ensure_listserv_catalog(engine: Engine) -> None`.
- Existing `create_schema(scope: str = "all") -> None` invokes the initializer for main/all; testcase/specdata scopes do not seed the main database.

- [ ] **Step 1: Write failing seed, schema and preservation tests.** Using `sqlite_env`, call `create_schema("main")` and query catalog rows ordered by ID. Pin the complete seed data without maintaining a second 97-row copy:

  ```python
  raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
  assert len(rows) == 97
  assert hashlib.sha256(raw).hexdigest() == "b7a2a990478d6934be221d608ac97c0bf357c406189b7cb4e55e3c09282a8ae7"
  assert "3GPP_TSG_RAN_WG6_DRAFTS" not in {row[0] for row in rows}
  assert r6 == ("RAN WG6", "R6", "(CLOSED) GSM/EDGE Radio Access Network (GERAN) and UMTS Radio Access (UTRA)", None)
  ```

  Here `rows` is a sorted list of three-item tuples read from SQL, and `r6` is the four selected TSG columns. Assert `PRAGMA foreign_keys=1`, catalog/child FKs are declared on a fresh DB, an invalid TSG and orphan child inserts fail, and schema reporting matches ORM columns. Mutate a seeded catalog description and an existing TSG description, call `create_schema("main")` twice, and assert both edits survive. Explicitly seed TSGs twice and assert R6 retains null URL; update old 19-record expectations to 20 without changing other group URLs. Verify main data is not initialized by testcase/specdata-only scope.

- [ ] **Step 2: Run the new tests and see them fail.** Run `./scripts/test_sqlite.sh tests/integration/test_listserv_catalog_sqlite.py tests/unit/test_tsg_service.py tests/integration/test_tsg_sqlite.py`; expected: failures because the catalog/R6/schema do not exist yet.

- [ ] **Step 3: Implement seeds and schema.** Move the canonical TSG definitions and URL composition into `models/tsg_defaults.py`; retain the existing service import path for `build_tsg_url`. Preserve R6's deliberate null URL during explicit refresh. Copy the 97 rows into `models/listserv_catalog.py`. Add the ORM catalog and child FK declarations and schema registry. Implement `ensure_listserv_catalog` with insert-if-missing semantics: ensure the required default TSG rows before catalog rows without replacing existing TSG/catalog metadata, then call it after main `create_all`. Report conflicting existing TSG names/codes rather than overwriting them or creating invalid references. Do not seed for other scopes.

- [ ] **Step 4: Run the task tests.** Repeat the Step 2 command and run `./scripts/test_sqlite.sh tests/unit/test_schema_info.py tests/integration/test_schema_parity.py`; expected: all pass. Note that upgrading pre-existing child tables is Task 2.

### Task 2: Transactional upgrade of legacy SQLite data

**Files:**
- Create: `src/doc3gpp/storage/db/listserv_catalog_migration.py`, `tests/integration/test_listserv_catalog_migration_sqlite.py`
- Modify: `src/doc3gpp/storage/db/migrate.py`, `src/doc3gpp/storage/db/listserv_catalog.py`.

**Interfaces:**
- Consumes Task 1's `ensure_listserv_catalog(engine: Engine) -> None` and ORM metadata.
- Produces `upgrade_listserv_catalog(engine: Engine) -> None`, called by main initialization after tables/seeds exist, before LISTSERV sidecar initialization.

- [ ] **Step 1: Write failing migration tests.** Build a file-backed *legacy* database with the pre-change `listserv_messages`, `listserv_details`, and `listserv_archive_pages` schema, real FK enforcement, indexes, a message plus compressed detail, a distinct page-only ID, coverage/next URLs, and FTS/vector sidecar data where available. Call `create_schema("main")` twice and compare every stored row/blob and index definition before/after; assert parent rows exist for both legacy IDs, seeded IDs keep their predefined metadata, `PRAGMA foreign_key_list` shows both new child FKs, and `PRAGMA foreign_key_check` is empty. Assert invalid child inserts fail and valid detail updates still work.

  Add `test_upgrade_rolls_back_after_old_message_table_drop`: inject an exception after the legacy message table is dropped but before the replacement is fully installed; assert the old schema, message, detail, coverage and indexes remain usable and `foreign_keys` is restored on the returned connection. Add `test_concurrent_catalog_upgrade_serializes` using two independent connections and a barrier; both calls finish or report an explicit failure, and subsequent initialization yields exactly one intact catalog/schema. Add `test_upgrade_rejects_unsupported_legacy_shape_without_data_loss` using an unexpected legacy column/index: preserve it if supported, otherwise fail with a clear error before losing data. Assert non-SQLite legacy tables with missing FKs fail explicitly rather than pretending they were upgraded.

- [ ] **Step 2: Run the migration tests and see them fail.** Run `./scripts/test_sqlite.sh tests/integration/test_listserv_catalog_migration_sqlite.py`; expected: missing FK/backfill or missing upgrade implementation.

- [ ] **Step 3: Implement the upgrade.** Backfill missing exact legacy IDs from both child tables with null metadata, after reference seeds. Under a dedicated SQLite connection, capture and restore the original FK setting; set any required PRAGMAs before beginning a real write transaction (`BEGIN IMMEDIATE`, not just SQLAlchemy's logical transaction). Re-inspect constraints after acquiring the lock. Where needed, create replacement tables with the new FK, copy all columns, safely drop/rename with FK enforcement temporarily controlled so `listserv_details` cannot cascade-delete, and recreate original indexes/triggers. Run integrity/FK checks before committing and roll back every DDL/data operation on any exception. Preserve previously present sidecars and unknown schema data or fail safely. Skip rebuild when FKs already exist. Check non-SQLite support explicitly and raise a useful error if a legacy upgrade is unsupported; fresh schemas still get declared FKs.

- [ ] **Step 4: Run the migration and catalog tests.** Run `./scripts/test_sqlite.sh tests/integration/test_listserv_catalog_migration_sqlite.py tests/integration/test_listserv_catalog_sqlite.py`; expected: all pass, including failure injection, repeated initialization and concurrency.

### Task 3: Atomic discovery and pagination buffering

**Files:**
- Modify: `src/doc3gpp/repository/listserv.py`, `src/doc3gpp/storage/repositories/listserv_sql.py`, `src/doc3gpp/services/listserv_sync.py`, `src/doc3gpp/models/listserv_catalog.py`.
- Test: `tests/integration/test_listserv_sync_sqlite.py`, `tests/integration/test_listserv_repository_sqlite.py`, `tests/unit/web/test_listserv_jobs.py`; create `tests/unit/test_listserv_catalog.py`.

**Interfaces:**
- Produces `infer_listserv_tsg(list_id: str) -> str | None` (a candidate code only; SQL validates that the TSG exists), and `ListservRepository.has_list(list_id: str) -> bool`.
- Keeps `commit_page(page: ArchivePage, messages: Sequence[ListservMessage], coverage: DateSet, next_urls: Sequence[str]) -> None`; it creates a missing parent only for a nonempty matching `messages` sequence, in the same SQL transaction as the child rows.
- `ListservSyncService.sync(...) -> ListservBatchResult` keeps its existing public signature.

- [ ] **Step 1: Write failing mapping and sync tests.** Pin the heuristic with parametrized tests:

  ```python
  @pytest.mark.parametrize(("list_id", "candidate"), [
      ("3gpp_tsg_ran_wg6_drafts", "R6"),
      ("3GPP_TSG_SA_WG2_DISCUSSIONS", "S2"),
      ("3GPP_TSG_CT_WG99_X", "C99"),
      ("3GPP_TSG_RAN_ADHOC", "RP"),
      ("3GPP_TSG_SA", "SP"),
      ("3GPP_TSG_CT_DRAFTS", "CP"),
      ("3GPP_TSG_SA4_FS_MED", None),
      ("OTHER_LIST", None),
  ])
  def test_infer_listserv_tsg(list_id, candidate):
      assert infer_listserv_tsg(list_id) == candidate
  ```

  Using the existing real SQLite sync fixture and fake HTTP source, assert: a matching message registers a normalized unknown ID with a known WG; an unknown `WG99` registers with null TSG; non-3GPP IDs have null metadata; and a seeded non-heuristic mapping such as `3GPP_TSG_RAN_ADHOC -> R2` is never overwritten. For an unknown list, arrange a successful zero-match page linking to a later matching page: both URLs are fetched, the parent/message exist and both coverage/links persist; with no matching page, neither parent nor coverage persists and a second run refetches them. Repeat with messages outside the date filter, malformed/failed pages, cancellation before any match, and forced sync. Inject `commit_page` failure after parent insertion and assert no parent, message, coverage or pagination row remains. Check `processed` counts only committed pages, failed buffered-page commits remain retryable, and seeded-list empty coverage is still committed. Race two successful syncs for one unknown ID and assert one parent, valid child rows and unchanged catalog metadata. Cover existing cancellation and job-handler tests.

- [ ] **Step 2: Run the tests and see them fail.** Run `./scripts/test_sqlite.sh tests/unit/test_listserv_catalog.py tests/integration/test_listserv_sync_sqlite.py tests/integration/test_listserv_repository_sqlite.py tests/unit/web/test_listserv_jobs.py`; expected: absent heuristic/API and unknown-list FK failures.

- [ ] **Step 3: Implement the discovery flow.** Normalize IDs and match family/WG segment boundaries as specified; map absent TSG candidates to null using the current SQL transaction. Implement conflict-safe insert-if-absent inside `commit_page` before message/coverage upserts; never update an existing parent. `has_list` queries the catalog. In sync, buffer successful empty results for unknown IDs in memory while continuing pagination; when the first matching page commits, flush each buffered page through `commit_page` and report each success/failure independently. Discard buffers if no matching page commits. Keep failed pages retryable, count only committed pages, and retain existing in-flight cancellation and index-update behavior. Update fake repository implementations to satisfy the protocol without weakening tests.

- [ ] **Step 4: Run the task tests.** Repeat the Step 2 command; expected: all pass, including direct repository rollback and concurrent discovery tests.

### Task 4: Catalog listing surfaces and maintained documentation

**Files:**
- Modify: `src/doc3gpp/models/listserv.py`, `src/doc3gpp/models/listserv_payloads.py`, `src/doc3gpp/repository/listserv.py`, `src/doc3gpp/storage/repositories/listserv_sql.py`, `src/doc3gpp/services/listserv_service.py`, `src/doc3gpp/cli_listserv.py`, `src/doc3gpp/web/routes/listserv.py`, `src/doc3gpp/web/mcp_server.py` and any affected web selector template.
- Test: `tests/unit/test_cli_listserv.py`, `tests/integration/test_listserv_cli_sqlite.py`, `tests/integration/test_listserv_web_sqlite.py`, `tests/integration/test_listserv_mcp.py`, `tests/integration/test_listserv_ui.py`, and existing SDK/list tests.
- Docs: `docs/cli.md`, `docs/processes.md`, `docs/architecture.md`, `docs/3gpp-knowledge.md`, `docs/web-server.md`, `docs/mcp.md`, `docs/known-constraints.md`, `docs/code-map.md`, and `AGENTS.md` as affected.

**Interfaces:**
- Produces immutable domain `ListservList(list_id: str, tsg: str | None, description: str | None)`; repository and service expose `list_lists() -> list[ListservList]`, ordered by `list_id`.
- Keeps `list_ids() -> list[str]` as a compatibility method returning IDs from the catalog, not distinct messages.
- Produces `listserv_lists_payload(lists: Sequence[ListservList]) -> dict[str, Any]`, returning `{"list_ids": ["..."], "lists": [{"list_id": "...", "tsg": "...", "description": "..."}]}`. HTTP and MCP call the same serializer; nullable fields are JSON null.

- [ ] **Step 1: Write failing interface tests.** On a fresh database assert `list_lists()` and `list_ids()` include sorted seeded entries without messages; insert a null-metadata legacy/discovered entry and assert its values survive. Check CLI column headers `list_id`, `tsg`, `description`, representative exact descriptions including Unicode, and `-` for null. Update HTTP and MCP tests to assert identical parsed JSON, both ordered arrays, exact preset and null fields, and retained `list_ids`. Assert the `/listserv` HTML selector contains list IDs rather than object representations and works with a selected ID. Forbid network access in read tests. Update existing exact `[LIST_ID]` assertions to account for catalog seeds while still testing all prior behaviors.

- [ ] **Step 2: Run the interface tests and see them fail.** Run `./scripts/test_sqlite.sh tests/unit/test_cli_listserv.py tests/integration/test_listserv_cli_sqlite.py tests/integration/test_listserv_web_sqlite.py tests/integration/test_listserv_mcp.py tests/integration/test_listserv_ui.py`; expected: missing metadata/list fields or old ID-only output.

- [ ] **Step 3: Implement the listing surfaces.** Query catalog columns once per listing and convert to domain records; expose compatibility IDs from the same authoritative catalog. Format the CLI table using existing project conventions. Share the HTTP/MCP serializer exactly; retain `list_ids` for the web selector and existing consumers. Update type annotations, help/tool descriptions and test doubles.

- [ ] **Step 4: Update maintained documentation.** Replace statements that listings only derive from messages; document seeded and discovered entries, matching-date registration, empty-page buffering, three output fields/JSON shape, nulls, R6, and SQLite legacy upgrade/limitations without presenting it as a general migration system. Update schema inventory, code map, and `AGENTS.md` where behavior is summarized. Check examples and relative links.

- [ ] **Step 5: Run the interface tests and full verification.** Repeat Step 2; then run `ruff check .` and `./scripts/test_sqlite.sh`. Expected: all pass; report any skipped tests or missing optional dependencies accurately. Run `doc3gpp listserv list --help`, check the changed Markdown links against their targets, and inspect `git diff --check` plus `git status --short` without committing.
