# LISTSERV Review Follow-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for inline execution, or superpowers:subagent-driven-development only if the user explicitly authorizes delegation. Steps use checkbox (`- [ ]`) syntax for tracking. Do not start implementation until this plan is approved.

**Goal:** Resolve the outstanding LISTSERV decompression-memory blocker and address or explicitly disposition every minor finding in the final branch review.

**Architecture:** Keep network safety in `ListservSource`, source revision decisions in the SQL repository, index metadata reads in the vector repository and its factory, and failure classification in a shared domain-level helper. Exercise the existing SDK/CLI/HTTP/UI boundaries with offline SQLite and JavaScript tests; do not introduce a new subsystem or database.

**Tech Stack:** Python 3.10+, HTTPX, standard-library `zlib`, SQLAlchemy/SQLite, pytest, Ruff, and Node.js for the existing-style JavaScript smoke test.

**Spec:** `docs/superpowers/specs/2026-11-11-listserv-archives-design.md`; follow-up scope and triage: `.superpowers/sdd/2026-11-11-listserv-archives/final-review.md` and `final-fix-round-1-report.md`. The choices below are follow-up decisions, not modifications to the historical spec.

**User-approved choices:** Support identity, gzip and deflate HTTP responses and explicitly reject Brotli, Zstandard and stacked encodings; for duplicate message URLs in one page, the final occurrence wins and changes the revision only if its final value differs from stored data. This approval does not authorize implementation or approve the rest of the plan.

## Global Constraints

- Public `https://list.etsi.org` only; no authenticated/private archives, attachment downloads, implicit read-time sync/parse, or fourth database.
- Preserve redirect validation, pre-body attachment checks, response closure, retries, safe logging, date and truncation semantics, source/index separation, search provenance, and SDK/CLI/HTTP/MCP payload parity.
- Preserve the 10,000 Unicode-character default for stored content and the independent non-configurable 16 MiB decoded HTTP response limit.
- Keep `ListservRepository.commit_page(...)`, existing service/adapter signatures and JSON payloads compatible unless explicitly stated below. Do not leak ORM entities or put SQL in services.
- Work offline using isolated settings and databases. Do not use live ETSI requests or introduce dependencies without separate approval.
- Preserve unrelated working-tree files, including `node_modules/`, `package.json`, and `package-lock.json`; do not stage, commit, reset, change branches, or overwrite historical reports.
- Previous test results are historical; each completion claim requires fresh verification. Update maintained docs and `AGENTS.md` only where their behavior descriptions need changing.

## Review Focus

- A highly compressible response must be rejected without materializing its full decoded size; Task 1 tests both the error and peak allocation.
- A gzip stream exactly at the limit, including its trailer and multiple members, must be accepted; one extra decoded byte must fail; Task 1 tests both.
- An unsupported or stacked encoding and an attachment must be rejected before their streams are iterated; Task 1 tests both, including caller-supplied clients and redirect hops.
- Duplicate URL entries whose final value equals the previously stored value must not create false revisions or invalidate indexes; Task 2 tests this and transaction rollback.
- Reading index status with missing metadata must not write or call an embedder, but later indexing must still initialize correctly; Task 3 tests both.

## Disposition of all final-review entries

Numbers refer to the 23-entry table in `final-review.md`:

| Finding | Disposition in this plan |
|---|---|
| 1 duplicate revisions | Task 2. |
| 2 no-op timezone code | Task 5. |
| 3 month-only week year/month | Already resolved as displayed-local-date semantics; Task 5 adds a boundary regression, not a behavior change. |
| 4 and 6 historical TDD RED collection errors | Historical evidence cannot be changed; keep reports intact. All new behavior changes use assertion-level RED. |
| 5 encoded paths | Already fixed; Task 1 reruns the existing regressions. |
| 7 escaped quote boundary | Already fixed; Task 5 reruns the existing regression. |
| 8 URL-only attachment TDoc fixture | Task 4. |
| 9 deep-offset hybrid candidates | Already fixed; Tasks 6 and 7 add adapter coverage and rerun the existing regression. |
| 10 stale vector rebuild timestamps | Already fixed; Task 3 reruns lifecycle regressions. |
| 11 source/index failure classification | Already fixed; Task 5 consolidates its duplicated classifier without changing output. |
| 12 missing process/config docs | Already fixed; Task 9 verifies maintained docs. |
| 13 unused service `quiet` parameter | Task 5 documents and tests its current compatibility role; CLI controls whether to supply a callback, while job callbacks remain active. |
| 14 catch-all status errors | Already resolved; Task 3 checks unexpected errors still propagate. |
| 15 status initializes vector metadata | Task 3. |
| 16 CLI rebuild assertion | Task 6. |
| 17 CLI hybrid/offset integration | Task 6. |
| 18 exact-ID CLI coverage | Task 6. |
| 19 duplicated index-failure prefixes | Task 5. |
| 20 CLI `--output`/`--compact` | Existing approved contract is `--format table|json`; do not add flags as an assumed fix. Task 9 verifies the contract and records a separate product decision if those flags are wanted. |
| 21 HTTP successful search modes | Task 7. |
| 22 JavaScript form submission/polling | Task 8. |
| 23 Markdown fragments | Task 9. |

The two original Important findings—attachment/path validation and hybrid deep-offset paging—were addressed in final fix round 1. The **remaining Important blocker** is the HTTPX decompression allocation; Task 1 addresses it. Do not label the branch merge-ready until the final review and fresh verification in Task 10.

**Execution order:** Task 1 is the merge-blocker workstream. Tasks 2–9 are independently testable follow-ups; Task 10 integrates and reviews whichever complete scope the user approves. If executing the entire plan, perform all tasks before Task 10.

---

### Task 1: Bound LISTSERV decompression

**Files:** Modify `src/doc3gpp/scraping/listserv_source.py`, `tests/unit/test_listserv_source.py`, `docs/known-constraints.md`; update `AGENTS.md` only if its transport summary needs the encoding constraint.

**Interfaces:** Preserve `ListservSource.fetch(self, url: str) -> FetchedPage` and `_request_with_retry(self, url: str, request_fn: Callable[[], httpx.Response]) -> tuple[httpx.Response, bytes | None]`; unsupported/malformed bodies raise `ListservSourceError` with no body content in the message.

- [x] **Step 1: Write behavior-level RED tests.** With `httpx.MockTransport` and observable `SyncByteStream`, test gzip bombs expanding to 64 MiB from one raw chunk; assert `ListservSourceError` mentions `16 MiB`, closes the response, and a `tracemalloc` peak measured after preparing the compressed fixture is below 48 MiB. Run the measurement in a fresh subprocess to isolate unrelated allocations; it must exercise `ListservSource.fetch`, not a decoder in isolation. Also assert that `Accept-Encoding` overrides an injected client's conflicting default on every redirect hop.
- [x] **Step 2: Run the RED tests.** Run `.venv/bin/python -m pytest -q -n 0 tests/unit/test_listserv_source.py -k 'bounded_decompression or accept_encoding'`. The current implementation must fail the peak-allocation and request-header assertions. Record the observed failure, not merely a collection error.
- [x] **Step 3: Add boundary and protocol tests.** For identity, gzip and deflate, cover empty, valid small, exactly 16 MiB, and 16 MiB + 1 decoded bytes across fragmented input; verify gzip concatenated members count toward one cumulative limit. Test zlib-wrapped and raw-deflate input including one-byte initial fragments, malformed/truncated streams, invalid gzip checksum, trailing non-member junk, and a full body followed by its trailer. Ensure unsupported (`br`, `zstd`, unknown) and comma-stacked encodings are rejected without iterating the body. Preserve and run attachment, redirect, body-read retry, decoded-path, and response-closure tests.
- [x] **Step 4: Implement bounded decoding.** Send a per-request `Accept-Encoding: gzip, deflate, identity`; inspect status, disposition, MIME type and normalized `Content-Encoding` before consuming a successful body. Use `response.iter_raw()` and a streaming `zlib.decompressobj` for gzip and deflate, requesting at most `min(64 * 1024, remaining_limit + 1)` decoded bytes per `decompress` call and feeding `unconsumed_tail` back until consumed. Never pass `max_length=0` or call unbounded `flush()`. Verify stream EOF/checksum; process concatenated gzip members with the same remaining budget; reject trailing junk, malformed/truncated data, unsupported encodings and stacked encodings. For deflate, allow raw-deflate fallback only if zlib decoding fails before output: retain at most 64 KiB of compressed input for this initial probe, including fragmented headers; if that budget is exhausted without deciding, stop retaining and reject any later decoding error rather than replaying unbounded input or emitted data. Preserve HTTPX transient read retries and close responses on all paths.
- [x] **Step 5: Document and verify.** Explain the accepted encodings and that the limit bounds decoded body/decompressor output, not total process RSS or arbitrary upstream transport-chunk allocations. Run `.venv/bin/python -m pytest -q -n 0 tests/unit/test_listserv_source.py` and `.venv/bin/ruff check src/doc3gpp/scraping/listserv_source.py tests/unit/test_listserv_source.py`; expect all tests and lint to pass. The encoding restriction is a deliberate compatibility change: a server that ignores negotiation and insists on Brotli/Zstandard will now fail explicitly.

### Task 2: Make duplicate-page revisions reflect the final row

**Files:** Modify `src/doc3gpp/storage/repositories/listserv_sql.py`, `tests/integration/test_listserv_repository_sqlite.py`; update `docs/processes.md` if revision semantics are described there.

**Interfaces:** `commit_page(page: ArchivePage, messages: Sequence[ListservMessage], coverage: DateSet, next_urls: Sequence[str]) -> None` remains atomic; each URL's final occurrence in `messages` wins.

- [x] **Step 1: Write RED tests.** Seed URL A at revision 1. Commit `[A_modified, A_original]` and assert the stored record remains original with revision 1; then commit `[A_original, A_modified, A_modified_again]` and assert the last value is stored with revision 2. For a new URL with multiple values assert final value and revision 1. Assert another identical commit is a no-op. Inject a failure after message processing and assert messages, revisions, coverage, and next URLs all roll back.
- [x] **Step 2: Run RED.** Run `.venv/bin/python -m pytest -q -n 0 tests/integration/test_listserv_repository_sqlite.py -k 'duplicate or revision or rollback'`; confirm the new assertions fail on the original code.
- [x] **Step 3: Implement and verify.** Collapse each batch to the final message per URL before comparing with persisted rows; increment at most once only if the final persisted fields differ, keeping coverage and pagination in the same transaction. Run the command from Step 2 and `.venv/bin/python -m pytest -q -n 0 tests/integration/test_listserv_repository_sqlite.py tests/integration/test_listserv_sync_sqlite.py tests/integration/test_listserv_index_sqlite.py`; expect all to pass.

### Task 3: Make vector index status read-only

**Files:** Modify `src/doc3gpp/storage/repositories/listserv_vector_sql.py`, `src/doc3gpp/services/factory.py`, `tests/integration/test_listserv_index_sqlite.py`; update `docs/processes.md` if needed.

**Interfaces:** Add keyword-only `initialize_metadata: bool = True` to `SQLAlchemyListservVectorRepository.__init__`; the status-only factory passes `False`. `stored_dim` continues reporting `DEFAULT_DIM` when the dimension row is missing, without inserting it in read-only mode. Normal constructors and explicit index operations retain initialization.

- [x] **Step 1: Write RED tests.** Using real initialized SQLite/vector schema, delete only `embedding_dim`, snapshot all `vec_listserv_meta` rows, build the index service with semantic status available and call `status()` twice. Assert the snapshots are unchanged, reported dimension matches existing `DEFAULT_DIM` behavior, and an embedder spy receives no encoding/dimension request. Assert an unexpected database/programming error is propagated rather than classified as unavailable.
- [x] **Step 2: Run RED.** Run `.venv/bin/python -m pytest -q -n 0 tests/integration/test_listserv_index_sqlite.py -k 'status and (read_only or metadata)'`; confirm the no-write assertion fails before the change.
- [x] **Step 3: Implement and verify.** Split read from optional initialization within the vector repository; use read-only mode only in the status factory, not semantic/rebuild factories. Assert a later explicit rebuild/index operation initializes metadata and succeeds, and an existing non-default stored dimension/model is preserved. Run the entire `tests/integration/test_listserv_index_sqlite.py` and `tests/integration/test_listserv_semantic_sqlite.py`; expect all to pass, including rebuild-timestamp and freshness tests.

### Task 4: Isolate attachment-URL TDoc indexing

**Files:** Modify `tests/integration/test_listserv_search_sqlite.py`; change `src/doc3gpp/models/listserv_payloads.py` only if the new regression reveals a defect.

**Interfaces:** Use existing `ListservAttachment(name: str, url: str)`, `ListservDetail`, `ListservSearchService.search(...)`, and `listserv_search_payload(...)`.

- [x] **Step 1: Add a focused SQLite test.** Seed a message with an ordinary subject, ordinary body and ordinary attachment name, empty `subject_tdocs` and detail `tdocs`, and an attachment URL containing only `R1-2610793` (also cover a percent-encoded form). Index it. Assert FTS for `R1-2610793` finds the message, returned effective TDocs contain that ID, and the URL itself is not indexed as searchable content. Assert no attachment fetch occurs.
- [x] **Step 2: Verify test sensitivity and behavior.** Run `.venv/bin/python -m pytest -q -n 0 tests/integration/test_listserv_search_sqlite.py -k 'attachment_url'`. This coverage-only test may pass immediately; demonstrate sensitivity by temporarily substituting an empty URL-TDoc extractor within the test, rebuilding that record's FTS entry, and asserting the URL-only lookup no longer finds it. Restore the extractor and reindex before the final positive assertion. If the normal path fails, fix only the extraction/index path and rerun the full file.

### Task 5: Consolidate classifiers and clarify parser/progress behavior

**Files:** Create `src/doc3gpp/models/listserv_failures.py`; modify `src/doc3gpp/cli_listserv.py`, `src/doc3gpp/web/workers/listserv_jobs.py`, `src/doc3gpp/parsers/listserv_archive.py`, `src/doc3gpp/services/listserv_index.py`; test in `tests/unit/test_cli_listserv.py`, `tests/unit/web/test_listserv_jobs.py`, `tests/unit/test_listserv_archive.py`, `tests/integration/test_listserv_index_sqlite.py`.

**Interfaces:** Define `INDEX_FAILURE_PREFIXES: tuple[str, ...]` and `is_index_failure(reason: str) -> bool` in the shared model helper; preserve the job module's existing public exports by importing/re-exporting them. Keep `ListservIndexService.rebuild(..., quiet: bool = False, on_progress: Callable[[str], None] | None = None, cancelled: Callable[[], bool] | None = None)` unchanged.

- [x] **Step 1: Add tests.** For each of `index update failed (`, `fts5 index update failed (` and `vector index update failed (` in mixed case, assert identical CLI/job derived-failure classification; ordinary source errors remain source failures and output wording/counts stay unchanged. Assert a timezone-crossing month-only page derives the week from the displayed local date. Assert service progress is controlled by `on_progress` even when `quiet=True`, while CLI `--quiet` supplies no callback and job callbacks remain enabled.
- [x] **Step 2: Verify then implement.** Add an assertion using `importlib.util.find_spec` that the shared helper exists and run the four files above; that assertion fails before its introduction without a collection error, while existing-behavior regressions may already pass. Move the classifier to the shared helper and have both adapters use it. Delete the no-op RFC 2822 UTC conditional and its now-unused `timezone` import; retain parsed datetime/offset behavior. Document that `quiet` remains for coordinator signature compatibility and the caller controls progress via `on_progress`; do not suppress job progress or change CLI behavior.
- [x] **Step 3: Verify.** Rerun the four test files and `.venv/bin/ruff check` on the touched Python files; expect all to pass. Include existing escaped-`>` quote-boundary tests in the final full-suite run.

### Task 6: Strengthen CLI integration coverage

**Files:** Modify `tests/integration/test_listserv_cli_sqlite.py`, using the deterministic embedder/vector fixtures in `tests/integration/test_listserv_semantic_sqlite.py`; change production files only for a demonstrated regression.

**Interfaces:** CLI remains `listserv search --text TEXT --semantic TEXT --offset N --limit N --format json`, `listserv parse LIST_ID`, and `listserv index --rebuild --quiet`.

- [x] **Step 1: Add independent SQLite tests.** Configure FTS enabled and assert `index --rebuild --quiet` exits exactly 0, then assert indexed records are searchable; add a separate controlled-unavailable case expecting exit 1 and an actionable error. Seed 205 distinct FTS-only matches with deterministic ranking and an offline fake embedder/vector repository; query hybrid with `--offset 170 --limit 7 --format json` and assert the seven URLs, order, scores/provenance and payload fields equal the SDK result. Seed `TEAM_A` and `TEAMXA`; use a fake source that returns valid detail pages, parse the exact ID `TEAM_A`, and assert only its row is selected and saved, despite `_` being a SQL LIKE wildcard. Ensure `%` in a sync/parse ID is rejected before network/database work.
- [x] **Step 2: Verify.** Run `.venv/bin/python -m pytest -q -n 0 tests/integration/test_listserv_cli_sqlite.py tests/integration/test_listserv_semantic_sqlite.py`; all must pass. These are primarily coverage additions and may pass immediately; if an assertion fails, reproduce the adapter defect before fixing it and rerun the focused and full files. Do not relax exact exit-code assertions or fetch upstream.

### Task 7: Exercise successful HTTP search modes

**Files:** Modify `tests/integration/test_listserv_web_sqlite.py`; change `src/doc3gpp/web/routes/listserv.py` or a shared serializer only for a demonstrated defect.

**Interfaces:** GET `/api/listserv/search` shares `listserv_search_payload` and the existing search service with SDK/CLI.

- [x] **Step 1: Add offline SQLite tests.** Index representative metadata and parsed records with real FTS and a deterministic offline vector/embedder fixture. Issue successful FTS-only, semantic-only, hybrid and filter-only requests, including date/list filters, pagination, a parsed and an unparsed record, and a nonzero offset. For each, compare HTTP JSON to the SDK result serialized by `listserv_search_payload`, including URL order, mode-specific scores, matched source/snippet, and both truncation fields. Assert no network fetch is made.
- [x] **Step 2: Verify.** Run `.venv/bin/python -m pytest -q -n 0 tests/integration/test_listserv_web_sqlite.py tests/integration/test_listserv_search_sqlite.py tests/integration/test_listserv_semantic_sqlite.py`; expect all to pass. Diagnose and fix only failures exposed by these tests, retaining existing 400/unavailable behavior.

### Task 8: Exercise actual LISTSERV JavaScript form submissions

**Files:** Create `tests/unit/_listserv_forms_smoke.js` and `tests/unit/test_listserv_forms_smoke.py`, following `tests/unit/_job_polling_smoke.js`; modify `src/doc3gpp/web/static/js/listserv.js` only for a demonstrated regression.

**Interfaces:** The existing `window.bindJobPolling(form, options)` receives `contentType: 'application/json'` and `buildBody(form)` from the LISTSERV script; the existing poller owns submission and job display.

- [x] **Step 1: Add executable JS coverage.** Load the real `listserv.js` and `job_poller.js` into a minimal DOM/fetch mock; trigger actual submit handlers for sync, parse and index forms. Assert fetch targets the forms' existing actions with JSON content type; sync/parse serialize the exact list ID, trimmed nonempty newline-separated dates, and the `force` boolean; index serializes all five booleans, a numeric nonempty batch, and omits an empty batch. Return a fake job ID and assert the poller attaches the job status to the submitting form's own target. Exercise initialization both before and after `DOMContentLoaded`.
- [x] **Step 2: Verify.** Python invokes Node via `subprocess`, using the repository's existing Node-smoke-test skip convention if Node is absent; the JS uses only Node built-ins, not user-owned npm files. Run `.venv/bin/python -m pytest -q -n 0 tests/unit/test_listserv_forms_smoke.py tests/integration/test_listserv_ui.py` and inspect skip reasons. If Node is unavailable, report the coverage as unverified, not passed.

### Task 9: Validate Markdown fragments and documented CLI scope

**Files:** Modify `tests/unit/test_listserv_docs.py`; correct maintained documentation only when a broken link or inaccurate description is demonstrated.

**Interfaces:** Extend the existing relative-link test with a helper resolving same-file and cross-file Markdown fragments using GitHub-compatible heading anchors, duplicate-heading suffixes, explicit HTML `id`/`name` anchors, and percent-decoding. Continue skipping external URLs; do not treat code fences as headings.

- [x] **Step 1: Add helper tests.** Using temporary Markdown files, assert valid same-file, relative-file, percent-encoded, duplicate-heading (`#heading-1`) and explicit HTML anchors resolve; missing paths and missing fragments must fail with source and target in the error. Ensure code-fenced headings do not create anchors. Exercise every local fragment in the maintained `DOCS` set, not just paths.
- [x] **Step 2: Run and correct.** Run `.venv/bin/python -m pytest -q -n 0 tests/unit/test_listserv_docs.py`; fix genuine broken links in their maintained guide or correct the helper when the anchor exists. Assert LISTSERV help/doc examples agree on `--format table|json` and do not claim `--output`/`--compact` exist. Adding those flags requires a separately approved product scope and plan; do not silently add them here.

### Task 10: Final verification and review

**Files:** Update `.superpowers/sdd/2026-11-11-listserv-archives/progress.md` and add a new follow-up report next to the original reports; do not rewrite previous evidence.

- [x] **Step 1: Run fresh checks.** `.venv/bin/ruff check .` passed; `PATH="$PWD/.venv/bin:$PATH" ./scripts/test_sqlite.sh -n 0 -rs` passed with 3,974 passed, 1 skipped, 10 deselected, 13 warnings; `git diff --check` exited 0. The only skip requires `python-docx` to be absent, but it is installed. The Node smoke test ran (no Node-related skip).
- [x] **Step 2: Review all changes.** Compared the implementation and all 23 review dispositions with the original spec and this plan. The author self-review found no new Critical or Important issue; the two prior Important fixes remain covered by regressions, and Task 1 addresses the decompression residual. An independent whole-branch review remains pending because delegation was not authorized; merge readiness is not declared.
- [x] **Step 3: Record handoff.** Added `.superpowers/sdd/2026-11-11-listserv-archives/final-review-follow-up-report.md` and updated the original progress ledger without rewriting prior evidence. `git diff --cached --quiet` exited 0; no staging or commits were made. The final handoff asks the user how to proceed with independent review.
