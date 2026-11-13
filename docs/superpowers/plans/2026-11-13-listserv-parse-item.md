# LISTSERV Single-Item Parsing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for inline execution, or superpowers:subagent-driven-development only if the user explicitly chooses delegation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Explicitly parse one public ETSI LISTSERV item, including one whose message and list have never been synced, from CLI, REST, MCP and the web portal.

**Architecture:** Validate and canonicalize an A2 item URL, fetch the detail page once, derive missing message metadata from its headers, and reuse existing body/detail parsing. Atomically persist new catalog, message and detail data, then update indexes best-effort; adapters reuse the existing job infrastructure and ownership conventions.

**Tech Stack:** Python 3.10+, SQLAlchemy 2, BeautifulSoup, httpx, Typer, FastAPI, Jinja2, MCP, pytest, SQLite.

**Spec:** `docs/superpowers/specs/2026-11-13-listserv-filters-parse-item-design.md` (single-item sections).

## Global Constraints

- Preserve unrelated working-tree changes; do not auto-commit.
- Accepted input: `https://list.etsi.org/scripts/wa.exe?A2=<list_id>;<item_id>.<year_month_wk_group_id>` with optional empty `&S=`; validate before any network or database work.
- Existing detail URLs with and without `&S=` represent the same item; do not duplicate or destructively rewrite existing records.
- Only a fully successful parse registers an unknown list; never overwrite existing catalog or known-message metadata, and do not write archive coverage.
- Already parsed items are skipped unless forced; a failed forced parse preserves the previously committed detail.
- Preserve public-host-only validated redirects, no attachment downloads, content limits, TDoc extraction, index failure separation, and SQL-only reads.
- Web/MCP mutations are queued; jobs use `_to_thread`, cooperative cancellation, shared embedder ownership, and worker-thread closure.
- Update maintained CLI, web, MCP, process, constraints and contributor documentation; do not change the schema.
- Test with mocked transports and xdist when available; sequential runs are reserved for targeted failing tests.

## Review Focus

1. A previously stored URL without `&S=` must be found when input has it, and vice versa, without a duplicate (Tasks 1 and 3).
2. Duplicate, missing or malformed metadata headers must not produce a newly registered list (Tasks 1 and 3).
3. If the displayed message date differs from the URL's archive group, preserve the displayed date; do not invent archive coverage (Tasks 1 and 3).
4. An invalid URL or attachment target must be rejected before transport and before job creation (Tasks 1 and 4).
5. A derived-index failure after source commit must leave the new list and item readable while marking the job failed with an index-repair hint (Tasks 3 and 4).

## File Structure

- `scraping/listserv_source.py`: strict item URL validation, normalized canonical spelling and alias handling; no HTTP behavior changes.
- `parsers/listserv_detail.py` and, as needed, `parsers/listserv_archive.py`: pure detail-header metadata extraction and reuse of date/subject-TDoc helpers.
- `repository/listserv.py`, `storage/repositories/listserv_sql.py`: atomic source persistence of discovery and detail without touching archive coverage.
- `services/listserv_parse.py`: service entry point, reuse of fetched detail and indexing; existing factory and owned-resource contracts remain.
- `models/jobs.py`, `web/workers/{listserv_jobs,handlers,job_worker}.py`: job kind, lifecycle, error summaries.
- `cli_listserv.py`, `web/routes/listserv.py`, `web/mcp_server.py`, `web/templates/listserv.html`, `web/static/js/listserv.js`: adapters and queue form.
- Extend existing LISTSERV unit, SQLite integration, web UI, worker and MCP tests; update primary maintained docs and `AGENTS.md`.

---

### Task 1: Validated item identity and detail metadata

**Files:** Modify `src/doc3gpp/scraping/listserv_source.py`, `src/doc3gpp/parsers/listserv_detail.py`, `src/doc3gpp/parsers/listserv_archive.py` as needed; test `tests/unit/test_listserv_source.py`, `tests/unit/test_listserv_detail.py`.

**Interfaces:** Produce `validate_listserv_item_url(url: str) -> tuple[str, str]` returning `(canonical_url, normalized_list_id)`, where canonical form ends in `&S=`; `listserv_item_url_aliases(canonical_url: str) -> tuple[str, str]` returns canonical and suffix-less forms. Produce `parse_detail_metadata(html: str, *, url: str, list_id: str) -> ListservMessage`.

- [ ] **Write failing URL tests** using the example `U = "https://list.etsi.org/scripts/wa.exe?A2=3GPP_TSG_RAN_WG5_SIG;5d45a78f.2611B&S="`:

  ```python
  assert validate_listserv_item_url(U) == (U, "3GPP_TSG_RAN_WG5_SIG")
  assert validate_listserv_item_url(U.removesuffix("&S=")) == (U, "3GPP_TSG_RAN_WG5_SIG")
  assert set(listserv_item_url_aliases(U)) == {U, U.removesuffix("&S=")}
  ```

  Parametrize rejection of HTTP, foreign host, credentials, non-default port, wrong path, fragment, absent/multiple A2, empty list/item/group, invalid month, missing semicolon, extra parameters, nonempty or duplicate `S`, attachment target, whitespace/control characters and encoded query delimiters; patch `ListservSource.fetch` to fail if called.
- [ ] **Write failing metadata tests:** use the header table fixture in `tests/integration/test_listserv_parse_sqlite.py` as a model, with subject `Topic R1-2610793`, sender `Alice <[log in to unmask]>`, and displayed date `2026-11-13 12:00:00 +0000`. Assert normalized list ID, exact subject/sender/date, `date_precision == "datetime"`, `subject_tdocs == ("R1-2610793",)` and `week == "2026-11-W2"`; check date-only precision, HTML entities, surrounding whitespace and timezone offset. With a URL group from a different month, assert the displayed date and its monthly day-bucket week are retained. Empty/login pages, missing/empty/duplicate subject/from/date headers and unparseable dates must raise `ListservDetailParseError` without exposing body contents.
- [ ] **Run:** `python -m pytest -n auto tests/unit/test_listserv_source.py tests/unit/test_listserv_detail.py -k 'item_url or detail_metadata' -v`; expect missing-function failures.
- [ ] **Implement URL validation** on top of `validate_listserv_url`: require exact `/scripts/wa.exe`, exactly one A2 value consisting of a list ID accepted by `archive_index_url`, semicolon, item identifier matching `[A-Za-z0-9_-]+`, dot, two-digit year + valid two-digit month + week letter A–E; accept only the optional empty `S`, reject encoded delimiters and fragments; return uppercase normalized list ID and canonical `&S=` spelling. Alias generation must not broaden legal input.
- [ ] **Implement metadata extraction** from the detail-header table, reusing/refactoring the existing archive date and subject-TDoc logic rather than making it diverge. For a newly discovered item, use the displayed date's `(day - 1) // 7 + 1` monthly bucket as `_derive_week` does for monthly archives; never substitute the URL label for the displayed date. Preserve the existing detail parser's login checks and safe error reporting.
- [ ] **Verify:** rerun the test command; expect PASS.

### Task 2: Atomic catalog, message and detail persistence

**Files:** Modify `src/doc3gpp/repository/listserv.py`, `src/doc3gpp/storage/repositories/listserv_sql.py`; test `tests/integration/test_listserv_repository_sqlite.py`.

**Interfaces:** Produce `save_parsed_item(message: ListservMessage, detail: ListservDetail) -> None` in the repository protocol and SQL repository; message and detail URLs must match.

- [ ] **Write failing SQLite tests:** build an unknown list `3GPP_TSG_RAN_WG2_PARSE_TEST`, a message and a detail with the same valid item URL. Assert successful save creates the list, infers `R2` only if that TSG exists, creates the message and detail, advances source revision consistently with `save_detail`, and leaves the archive-page table unchanged. Modify an existing catalog description and existing message metadata and assert they are preserved by item parsing. Inject an exception after list/message insertion but before detail persistence; assert no list, message or detail remains. For an existing detail, inject the same failure and assert old detail and revision survive. Mismatched message/detail URLs must fail before writes. Seed a suffix-less item, then `commit_page` its canonical equivalent and assert the stored item is updated in place without a second message or orphaning its detail; verify other historical URL formats still behave as before.
- [ ] **Run:** `python -m pytest -n auto tests/integration/test_listserv_repository_sqlite.py -k parsed_item -v`; expect missing-method failures.
- [ ] **Implement** one SQL session/transaction using `_register_list_if_absent`, insert-if-absent message metadata, and a session-scoped detail upsert factored from `save_detail`; retain existing revision/change detection, compression and rollback behavior. Do not call `commit_page` or mutate an already-known message's metadata from `save_parsed_item`. In `commit_page`, map valid canonical/suffix-less A2 aliases to an already-stored message key before the normal metadata upsert; do not rewrite stored keys, collapse unrelated legacy URLs or change coverage behavior.
- [ ] **Verify:** rerun those tests and the whole repository test file with xdist; expect PASS.

### Task 3: Single-item service and indexing

**Files:** Modify `src/doc3gpp/services/listserv_parse.py`; test `tests/integration/test_listserv_parse_sqlite.py`.

**Interfaces:** Consume Tasks 1–2. Produce `ListservParseService.parse_item(url: str, *, force: bool = False, on_progress: Callable[[str], None] | None = None, cancelled: Callable[[], bool] | None = None) -> ListservBatchResult`. `processed`, `skipped`, and `failures` follow the existing batch result conventions.

- [ ] **Write failing service tests** using the existing `repository` and `RecordingFakeSource`: an unknown item on an unknown list fetches the A2 page exactly once, optionally fetches its body part, stores subject/attachment/body TDocs, respects content truncation, registers the list and returns `ListservBatchResult(1, 0, {})`; assert no attachment fetch and no archive coverage. Test an unknown item on a known list, an existing unparsed item, and a parsed item returning `ListservBatchResult(0, 1, {})` with no fetch unless `force=True`. Seed both canonical and suffix-less existing records separately, submit the opposite spelling, and assert only the existing row is used; simulate a later normal sync with the canonical spelling and confirm it does not duplicate a new canonical item.
- [ ] **Write failure/index tests:** invalid URL, missing/duplicate header, login page, body fetch failure and injected transaction failure yield no new catalog/message/detail; forced failure keeps old detail; cancellation before fetch prevents work. A mocked index exception after successful commit yields `processed == 1`, an `is_index_failure(...)` failure, and a readable committed message and list. Verify fetched page content is not leaked into failure output and resources are closed according to ownership.
- [ ] **Run:** `python -m pytest -n auto tests/integration/test_listserv_parse_sqlite.py -k parse_item -v`; expect missing-method failures.
- [ ] **Implement** `parse_item`: validate first; check both URL aliases, preferring the already-stored URL for lookup, fetch and persistence; apply skip/force; fetch one validated detail page, use it for missing metadata and existing detail parsing, fetch only a validated body URL if needed, check cooperative cancellation, call the atomic repository method, and update indexes only after source commit. Reuse safe failure formatting and source/index resource lifetime from batch parsing. Refactor `_fetch_and_parse_record` into shared fetched-page and fetch orchestration helpers so batch behavior is unchanged.
- [ ] **Verify:** rerun the new tests and the full `tests/integration/test_listserv_parse_sqlite.py` with xdist; expect PASS.

### Task 4: CLI, queued REST/MCP jobs and portal form

**Files:** Modify `src/doc3gpp/cli_listserv.py`, `src/doc3gpp/models/jobs.py`, `src/doc3gpp/web/workers/listserv_jobs.py`, `src/doc3gpp/web/workers/handlers.py`, `src/doc3gpp/web/workers/job_worker.py`, `src/doc3gpp/web/routes/listserv.py`, `src/doc3gpp/web/mcp_server.py`, `src/doc3gpp/web/templates/listserv.html`, `src/doc3gpp/web/static/js/listserv.js`; test `tests/unit/test_cli_listserv.py`, `tests/unit/web/test_listserv_jobs.py`, `tests/unit/web/test_listserv_mcp.py`, `tests/unit/_listserv_forms_smoke.js`, `tests/integration/test_listserv_web_sqlite.py`, `tests/integration/test_listserv_mcp.py`, `tests/integration/test_listserv_ui.py`.

**Interfaces:** Consume Tasks 1 and 3. Produce `JobKind.LISTSERV_PARSE_ITEM = "listserv_parse_item"`; `enqueue_listserv_parse_item(job_repo: JobRepository, url: str, force: bool = False) -> Job`; REST `POST /api/listserv/parse-item` JSON `{ "url": str, "force": bool }`; MCP `listserv_parse_item(url: str, force: bool = False) -> str`; CLI `doc3gpp listserv parse-item URL [--force]`.

- [ ] **Write failing CLI/adapter tests:** with mocked factory service, CLI passes canonical URL/force and closes its owned service; rejects bad URL before schema/service/network work; emits nonzero exit on source or index failure. REST and MCP accept both legal suffix variants, queue equivalent canonical params, reject invalid URLs before creating a job, and preserve the existing queued payload conventions. Assert job kind, OpenAPI and MCP schema document the URL and force.
- [ ] **Write failing worker/UI tests:** a real queued job invokes `parse_item` on the `_to_thread` worker, passes force/cancellation/progress, closes owned service after completion on that worker and borrows rather than closes the shared embedder. Cancellation is marked cancelled; source failure and index failure produce separated summary fields and failed job status; index failure includes the repair hint and leaves committed data. Portal displays a URL/force form, submits `{url, force}` via job polling, handles queued/error responses, and does not change existing sync/parse/index form behavior. Extend the JS smoke fixture to exercise the new binding.
- [ ] **Run:** `python -m pytest -n auto tests/unit/test_cli_listserv.py tests/unit/web/test_listserv_jobs.py tests/unit/web/test_listserv_mcp.py tests/unit/test_listserv_forms_smoke.py tests/integration/test_listserv_web_sqlite.py tests/integration/test_listserv_mcp.py tests/integration/test_listserv_ui.py -k 'parse_item or forms' -v`; expect missing-command/job/form failures.
- [ ] **Implement** CLI direct call and validate at its boundary before `create_schema`; reuse batch reporting and close the factory service. Add job kind, shared enqueue validator and body model, worker handler and registry; route its summary through `summarize_batch`/`failure_error` using the URL-derived list ID and include item URL in the summary. Add the new kind to `job_worker` failure classification. Build/use/close the parse service inside the `_to_thread` worker and pass the existing shared embedder factory; follow cancellation semantics of existing LISTSERV jobs.
- [ ] **Implement** REST, MCP and portal adapters through the shared enqueue helper; bind JSON `{url, force}` with `bindJobPolling` and retain the other forms and SQL-only page reads.
- [ ] **Verify:** rerun the targeted command and the full listed test files with xdist; expect PASS. Inspect `doc3gpp listserv parse-item --help`, REST OpenAPI and MCP tool schema for the correct contract.

### Task 5: Documentation and full verification

**Files:** Modify `docs/cli.md`, `docs/processes.md`, `docs/web-server.md`, `docs/mcp.md`, `docs/known-constraints.md`, `AGENTS.md`; test `tests/unit/test_listserv_docs.py` and the full SQLite suite.

**Interfaces:** Consume Tasks 1–4; no new API.

- [ ] **Write failing documentation-contract tests:** command, route, MCP tool and portal form examples include the example item URL, optional `&S=`, `--force`, new-list registration only after successful parse, no implicit sync, no attachment download, and source-versus-index failure distinction.
- [ ] **Run:** `python -m pytest -n auto tests/unit/test_listserv_docs.py -v`; expect missing-documentation failures.
- [ ] **Update** maintained guides and contributor entry to describe accepted URL, SQL-only stored reads, no fabricated coverage, atomic discovery, skip/force, queue/ownership and repair behavior. Keep the existing date-matched sync registration rule distinct from the explicit successful item discovery rule; keep examples and links consistent.
- [ ] **Verify:** rerun docs tests; run `ruff check .` and `./scripts/test_sqlite.sh`; expect successful exit. Check help/OpenAPI/MCP names, relative doc links and both plan features together. Report any unavailable dependencies or failing checks rather than claiming success.
