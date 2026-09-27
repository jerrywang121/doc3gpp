# TODO

Open improvement backlog from the repository review at commit `c0af0ee`.
The review was read-only; the findings below have not been implemented.

## Critical

- [ ] **Add HTTP and MCP authentication and authorization.** References: `src/doc3gpp/web/app.py:154-185`, `:224-235`; `src/doc3gpp/web/routes/jobs.py:157-341`, `:454-583`; `src/doc3gpp/cli_server.py:207-265`. Protect corpus reads, job inspection, sync, parse, indexing, cache purge, and cancellation. Treat host/origin checks as transport controls, not identity. Refuse non-loopback binding unless an explicit authentication mechanism is configured. Add tests for unauthenticated and authorized read/mutating requests.
- [ ] **Prevent stored XSS in cached TDoc content.** References: `src/doc3gpp/web/routes/tdocs.py:86`, `:439-455`; `src/doc3gpp/web/templates/tdoc_content.html:6`. Disable raw HTML in Markdown rendering or sanitize rendered HTML with a strict allowlist before using `safe`. Add a regression test with `<script>` and event-handler payloads.

## Important

- [ ] **Make queued job cancellation durable.** References: `src/doc3gpp/web/state.py:88-115`; `src/doc3gpp/web/routes/jobs.py:558-583`. Persist cancellation state or atomically transition queued jobs to `CANCELLED`, and ensure the worker claims only still-queued rows. Add restart and worker-unavailable tests.
- [ ] **Make job ownership and orphan recovery safe for multiple processes.** References: `src/doc3gpp/web/workers/job_worker.py:195-223`; `src/doc3gpp/cli_server.py:214-255`; `src/doc3gpp/storage/repositories/jobs_sql.py:162-225`. Add worker ownership, leases, and heartbeats with stale-only recovery, or enforce a process singleton. Require `status = 'running'` in terminal status transitions so an old worker cannot overwrite newer state.
- [ ] **Fix double compilation of hybrid FTS5 queries.** References: `src/doc3gpp/services/tdoc_search_facade.py:59-74`; `src/doc3gpp/services/semantic_search_service.py:172-183`; `src/doc3gpp/services/search_service.py:125-133`. Establish one explicit raw-query versus compiled-expression boundary. Add hybrid regression tests for TDoc IDs such as `R5-123456` and dotted spec IDs.
- [ ] **Make the MCP TDoc content tool use the authoritative cache reader.** References: `src/doc3gpp/web/mcp_server.py:443-469`; `src/doc3gpp/web/routes/tdocs.py:416-437`; `src/doc3gpp/services/tdoc_cr_service.py:153-176`. Read current ZIP Markdown sidecars, legacy gzip entries, and plain UTF-8 through the shared helper, using the persisted `tdoc_extracts.cache_file` key. Add a test for a ZIP Markdown sidecar and direct URL parsing.
- [ ] **Make document parse persistence atomic or recoverable.** References: `src/doc3gpp/services/spec_doc_service.py:277-306`; `src/doc3gpp/storage/repositories/spec_doc_sql.py:114-238`; `src/doc3gpp/services/tdoc_cr_service.py:675-710`. Use a per-document database transaction or unit of work, stage and atomically replace filesystem artifacts, and update search indexes only after durable relational and cache state is committed. Add failure-injection tests for partial TOC/chunk/cache writes and TDoc extraction sidecars.
- [ ] **Enforce the documented settings source allowlist for dotenv files.** References: `src/doc3gpp/settings/schema.py:58-70`, `:73-97`, `:924-963`. Apply the same filtering to dotenv and file-secret sources, or explicitly document the broader precedence policy. Add tests proving that TOML-only server/MCP settings cannot be injected through `.env` if that restriction remains intended.
- [ ] **Make vector schema initialization dimension-aware.** References: `src/doc3gpp/storage/db/migrate.py:298-350`, `:409-458`; `src/doc3gpp/services/factory.py:697-715`. Configure or discover embedding dimension before creating vector tables, record the dimension with the index, and support model/dimension changes during rebuild. Add clean-database and rebuild tests using a non-384-dimensional embedder.
- [ ] **Remove stale auxiliary TDoc rows after confirmed upstream listings.** References: `src/doc3gpp/services/tdoc_file_service.py:29-68`; `src/doc3gpp/storage/repositories/tdoc_file_sql.py:150-163`; `src/doc3gpp/repository/protocols.py:590-598`. Replace rows for affected TDoc IDs in one transaction after a complete successful listing. Do not delete rows after partial or failed network results.
- [ ] **Verify and resolve the `spec sync --tsg --force` contract.** References: `src/doc3gpp/cli.py:4639-4771`; `src/doc3gpp/services/spec_service.py:84-176`, `:299-307`. The CLI advertises `--force`, but the TSG sweep appears not to pass it to the per-spec skip logic. Confirm intended behavior, then either propagate `force` and test a recently synced spec or correct the help and documentation.

## Minor

- [ ] **Centralize cache-key validation and enforce root containment on Windows.** References: `src/doc3gpp/storage/cache.py:13-15`; `src/doc3gpp/models/tdoc_cr.py:266-285`; `src/doc3gpp/web/routes/tdocs.py:111-125`, `:416-431`. Reject both slash styles and unsafe traversal, validate persisted cache names, and verify every resolved path remains under the configured cache root.
- [ ] **Close the shared remote embedder during application shutdown.** References: `src/doc3gpp/web/app.py:49-58`, `:217-222`; `src/doc3gpp/services/embedding/remote_embedder.py:96-97`. Store the embedder in `WebState` or register an explicit shutdown callback that calls `close()`.
- [ ] **Avoid writing embedding bearer tokens into project TOML by default.** References: `src/doc3gpp/cli.py:5914-5977`; `src/doc3gpp/settings/config_writer.py:77-81`. Prefer environment or secret-file configuration, warn when writing sensitive settings, and use restrictive permissions for user configuration files.
- [ ] **Make `config set` writes atomic.** Reference: `src/doc3gpp/settings/config_writer.py:77-81`. Use a temporary file, flush/fsync, and `os.replace`, matching the safer `config init` implementation at `src/doc3gpp/cli.py:5841-5853`.

## Test And CI Follow-Up

- [ ] **Cap xdist workers on Windows and investigate fixture/cache isolation.** `python -m pytest -n 4 -q` passed with `2563 passed, 1 skipped`. `python -m pytest -n auto` used 16 workers and was not reliable: separate runs produced repeated `no such table: meetings` failures in `tests/unit/test_tdoc_cli_fields.py` and a Windows `WinError 1920` multiprocessing failure in `tests/unit/test_cr_parser.py`. The affected CLI test passed serially and under `-n 2`, and the complete `test_tdoc_cli_fields.py` file passed under xdist. Use a platform-aware worker cap such as `-n 4` in scripts/CI, then audit global engine/settings caches and fixture isolation for the missing-table symptom.
- [ ] **Install or configure Ruff in the development environment.** `ruff check .` could not be run during review because `ruff` was not installed or available on `PATH`.

## Suggested Order

1. Keep the server loopback-only until authentication, authorization, and XSS protections are in place.
2. Make job cancellation and worker ownership durable before supporting multiple server processes.
3. Fix the FTS query boundary and MCP cache reader, with regression tests.
4. Make document persistence and cache replacement transactional or recoverable.
5. Make vector schema initialization dimension-aware.
6. Harden cache-key validation and atomic configuration writes.
