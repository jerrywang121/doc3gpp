# LISTSERV Catalog Filters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for inline execution, or superpowers:subagent-driven-development only if the user explicitly chooses delegation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add rich catalog filters across CLI, REST, MCP and an independent linked catalog panel on the web portal.

**Architecture:** Filter `ListservListORM` in SQL through the existing rich-filter helper and return immutable domain records. Both JSON adapters share the existing serializer; the portal keeps catalog and message-search state separate.

**Tech Stack:** Python 3.10+, SQLAlchemy 2, Typer, FastAPI, Jinja2, MCP, pytest, SQLite.

**Spec:** `docs/superpowers/specs/2026-11-13-listserv-filters-parse-item-design.md` (catalog sections).

## Global Constraints

- Preserve unrelated working-tree changes; do not auto-commit.
- SQL-only catalog reads; no upstream fetch or ORM object leakage.
- `%`, `_`, leading `!`, `null`, `not-null` and exact patterns use the existing shared rich-filter implementation and SQL null semantics.
- Both `lists` and compatibility `list_ids` remain ordered and describe the same rows; unfiltered behavior is unchanged.
- Catalog filters must not alter message-search filters; keep the selectors usable.
- Update maintained CLI, web, MCP, process, and contributor docs and tests alongside the feature.
- Run tests with xdist when available; sequential runs are reserved for targeted failing tests.

## Review Focus

1. Nullable TSG with `!R1` must be excluded by SQL three-valued logic (Task 1).
2. `_` is a single-character wildcard, not a literal underscore (Task 1).
3. Bound patterns containing SQL punctuation must remain data, never SQL (Task 1).
4. A catalog filter submission must retain an already-selected message search and vice versa (Task 3).
5. Catalog IDs containing unsafe markup cannot become executable HTML or attacker-controlled links (Task 3).

## File Structure

- `repository/listserv.py`, `storage/repositories/listserv_sql.py`, `services/listserv_service.py`: catalog filter contract and SQL implementation.
- `cli_listserv.py`, `web/routes/listserv.py`, `web/mcp_server.py`: expose shared filter arguments.
- `web/templates/listserv.html`: separate catalog panel and independent forms; `partials/listserv_results.html` only if query retention requires it.
- Existing `tests/integration/test_listserv_{repository,cli,web}_sqlite.py`, `test_listserv_mcp.py`, and `test_listserv_ui.py`: real SQLite, adapter parity and HTML tests.
- `docs/cli.md`, `docs/processes.md`, `docs/web-server.md`, `docs/mcp.md`, `AGENTS.md`: maintained behavior and discoverability.

---

### Task 1: SQL catalog filtering

**Files:** Modify `src/doc3gpp/repository/listserv.py`, `src/doc3gpp/storage/repositories/listserv_sql.py`, `src/doc3gpp/services/listserv_service.py`; test `tests/integration/test_listserv_repository_sqlite.py`.

**Interfaces:** Produce `list_lists(*, list_id: str | None = None, tsg: str | None = None) -> list[ListservList]` on repository protocol, SQL repository and service. Keep `list_ids()` compatibility unchanged.

- [ ] **Write failing SQLite tests** using the existing `repo` fixture. Insert test rows `CATTEST_A`/`R1`, `CATTEST_B`/`R2`, and `CATTEST_NULL`/NULL; restrict examples with `list_id="CATTEST%"` so seeded catalog rows do not affect assertions. Pin:

  ```python
  assert ids(repo.list_lists(list_id="CATTEST%", tsg="R1")) == ["CATTEST_A"]
  assert ids(repo.list_lists(list_id="CATTEST%", tsg="!R1")) == ["CATTEST_B"]
  assert ids(repo.list_lists(list_id="CATTEST%", tsg="null")) == ["CATTEST_NULL"]
  assert ids(repo.list_lists(list_id="CATTEST%", tsg="not-null")) == ["CATTEST_A", "CATTEST_B"]
  assert ids(repo.list_lists(list_id="CATTEST__", tsg="R_")) == ["CATTEST_A", "CATTEST_B"]
  assert repo.list_lists(list_id="CATTEST%' OR 1=1 --") == []
  ```

  Define `ids(rows)` as each row's `list_id`. Also assert exact-match behavior, both filters intersect, unfiltered ordering and `list_ids()` parity, and `list_id="null"` returns no rows.
- [ ] **Run the new tests:** `python -m pytest -n auto tests/integration/test_listserv_repository_sqlite.py -k catalog_filter -v`; expect failures for unsupported parameters.
- [ ] **Implement** the interface in all three layers using `apply_text_filter` independently for `ListservListORM.list_id` and `.tsg`; preserve SQL ordering and domain conversion.
- [ ] **Verify:** rerun the command and expect PASS.

### Task 2: CLI, REST and MCP parity

**Files:** Modify `src/doc3gpp/cli_listserv.py`, `src/doc3gpp/web/routes/listserv.py`, `src/doc3gpp/web/mcp_server.py`, `docs/cli.md`, `docs/mcp.md`; test `tests/integration/test_listserv_cli_sqlite.py`, `tests/integration/test_listserv_web_sqlite.py`, `tests/integration/test_listserv_mcp.py`.

**Interfaces:** Consume Task 1. Produce CLI options `--list-id` and `--tsg`, REST query `list_id` and `tsg` on `/api/listserv/lists`, and MCP `listserv_list(list_id: str | None = None, tsg: str | None = None) -> str`.

- [ ] **Write failing adapter tests:** use the seeded catalog to compare CLI output, REST `lists`/`list_ids`, and MCP JSON for `list_id="3GPP_TSG_RAN_WG1"`, `tsg="R1"`; assert the arrays have the same IDs and a conflicting `tsg="R2"` returns none. Assert no-filter output remains unchanged, SQL NULL tokens pass through, and network-fetch mocks are never called. Assert CLI help, REST OpenAPI, and MCP tool schema describe both rich filters.
- [ ] **Run:** `python -m pytest -n auto tests/integration/test_listserv_cli_sqlite.py tests/integration/test_listserv_web_sqlite.py tests/integration/test_listserv_mcp.py -k 'list or catalog' -v`; expect failures for unsupported arguments.
- [ ] **Implement** options using existing help constants and shared serializer. REST must use `_optional_search_filter` for blank query values; MCP and CLI follow their existing rich-filter argument conventions. Keep existing unfiltered callers working.
- [ ] **Document** CLI examples and REST/MCP names, grammar, filtering intersection and SQL-only behavior in `docs/cli.md` and `docs/mcp.md`.
- [ ] **Verify:** rerun the test command; expect PASS.

### Task 3: Independent web catalog panel

**Files:** Modify `src/doc3gpp/web/routes/listserv.py`, `src/doc3gpp/web/templates/listserv.html`, `src/doc3gpp/web/templates/partials/listserv_results.html` if needed, `docs/web-server.md`, `docs/processes.md`, `AGENTS.md`; test `tests/integration/test_listserv_ui.py`.

**Interfaces:** Consume Task 1. Produce `/listserv` query keys `catalog_list_id` and `catalog_tsg`; retain existing `list_id` as the independent message-search filter.

- [ ] **Write failing UI tests** with BeautifulSoup: panel heading appears before “Search stored messages”; query `catalog_list_id="3GPP_TSG_RAN_WG1"&catalog_tsg="R1"` renders that catalog link with `href="https://list.etsi.org/scripts/wa.exe?A0=3GPP_TSG_RAN_WG1"` but not other catalog links; `catalog_tsg="R2"` excludes it. Assert the message search results and `list_id` input stay unchanged after catalog submission, and catalog values survive search and pagination. Assert the datalist/sync/parse selectors remain usable even when catalog filters select nothing. Insert an unsafe catalog ID in the test database and assert it is HTML-escaped and cannot create an unsafe link. Patch network to fail as the existing client fixture does.
- [ ] **Run:** `python -m pytest -n auto tests/integration/test_listserv_ui.py -k catalog -v`; expect missing panel/filter failures.
- [ ] **Implement** independent SQL-backed catalog query and panel. Keep an unfiltered ID collection for datalist and job forms. Use `archive_index_url` to build trusted `A0` targets and Jinja escaping for text/attributes; if a corrupt preexisting ID cannot pass that validator, display escaped text without a link rather than raising or generating an unsafe URL. Carry message-search values through catalog GET submissions and catalog values through search/pagination without changing the existing search semantics.
- [ ] **Document** portal names, independent filters and upstream links; update `docs/processes.md` and `AGENTS.md` to reflect the catalog contract.
- [ ] **Verify:** rerun UI tests, then `python -m pytest -n auto tests/integration/test_listserv_repository_sqlite.py tests/integration/test_listserv_cli_sqlite.py tests/integration/test_listserv_web_sqlite.py tests/integration/test_listserv_mcp.py tests/integration/test_listserv_ui.py -v`; expect PASS. Run `ruff check .` and inspect `doc3gpp listserv list --help`; expect successful exit and both options.
