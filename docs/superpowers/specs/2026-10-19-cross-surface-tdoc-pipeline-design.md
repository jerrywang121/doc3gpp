# Cross-Surface TDoc Pipeline

**Date:** 2026-10-19  
**Status:** Design approved in conversation; implementation pending

## Problem

TDoc ingestion currently has several execution paths with different
concurrency and error-handling behavior:

- queued web `parse_tdocs` jobs use the bounded preparation pool and a
  serialized persistence lane;
- CLI database-mode batches call `TDocCrService.extract_many()` sequentially;
- CLI local-folder parsing loops over files sequentially;
- CLI and web FTP-folder parsing call `extract_from_url_batch()`, which loops
  over discovered files sequentially;
- MCP `parse_tdocs` inherits the queued web behavior only because it enqueues
  that job.

The existing preparation/persistence split is therefore not a shared
cross-surface contract. A fix in one handler can diverge from the CLI or
direct-folder paths, and large URL-folder or local-folder runs do not use the
available preparation parallelism.

## Goals

1. Use one TDoc preparation/persistence orchestration model across CLI, HTTP,
   MCP, database-ID batches, local folders, and FTP folders.
2. Run network, file, conversion, and parsing work concurrently with bounded
   backpressure to maximize throughput without unbounded memory growth.
3. Route every database mutation for a TDoc item through one serialized writer
   lane per process.
4. Preserve existing public result types, output formats, exit codes, job
   payloads, cancellation semantics, skip rules, cache behavior, and TDoc
   identity rules.
5. Use one exception classifier so expected skips and per-item failures are
   consistent across all TDoc batch modes.
6. Keep the event loop responsive for web jobs and keep search reads
   independent of the persistence queue.

## Non-goals

- Reworking the existing spec-sync or spec-document pipeline semantics. Those
  queued jobs already use the shared runtime and continue using it after the
  runtime is moved below the web adapter.
- Parallel database writes.
- A distributed or cross-process queue. The single-writer guarantee is scoped
  to one running process, matching the current server model. Each CLI process
  owns its own runtime.
- Changing the public CLI, HTTP, or MCP request shape beyond adding shared
  pipeline configuration where required.
- Creating one visible job row per TDoc.
- Parallelizing directory discovery by default. Folder discovery remains a
  deterministic breadth-first phase; file preparation is the parallel phase.

## Current constraints and foundations

The branch already contains:

- `PreparedTDocExtract` and `PreparedDirectTDoc` immutable preparation DTOs;
- `TDocCrService.prepare_extract()` and
  `persist_prepared_extract()` for database-mode TDocs;
- `TDocCrService.prepare_from_url()` and `persist_prepared_url()` for direct
  3GPP URLs;
- a web-only `PipelineRuntime` with bounded preparation and one persistence
  worker;
- SQLite WAL/read-connection behavior intended to keep search reads
  responsive;
- `BatchExtractResult` and `DirectParseBatchResult` public batch result types.

The new work builds on those foundations rather than introducing a second
document parser or a second persistence contract.

## Architecture

### Service-level runtime

Move the generic runtime from `src/doc3gpp/web/workers/pipeline.py` to the
framework-neutral service module:

```text
src/doc3gpp/services/pipeline_runtime.py
```

The runtime must not import FastAPI, job models, CLI/Typer code, or web state.
It owns:

- a bounded preparation executor;
- a fixed one-worker persistence executor;
- capacity accounting for preparation plus submitted persistence work;
- synchronous submission for CLI callers;
- non-blocking/awaitable capacity acquisition for async web callers;
- orderly shutdown and writer draining.

The existing web module may retain a compatibility import/re-export so
existing internal imports and tests do not create a second runtime type.

### Shared TDoc runner

Add the service-level orchestration component:

```text
src/doc3gpp/services/tdoc_pipeline.py
```

The runner accepts ordered items with a stable key and two callbacks:

```text
prepare(item) -> prepared value | already-complete result
persist(prepared value) -> persisted result
```

The persistence callback is optional for local-only parsing. The runner:

1. assigns each item a source sequence and stable identity key;
2. submits preparation work while capacity is available;
3. consumes preparation completions without blocking the event loop;
4. submits prepared values to the shared persistence lane when present;
5. records success, skip, or failure for each item;
6. stops scheduling new work after cancellation;
7. drains persistence commands that were already submitted;
8. returns outcomes in source order, even though preparation and persistence
   may complete in a different order.

The runner is usable from both synchronous CLI code and async web handlers.
The synchronous entry point may block the calling CLI thread while awaiting
futures; the async entry point must never call a blocking future wait from the
event loop.

### Runtime ownership

- **Web server:** create one runtime during FastAPI lifespan startup and place
  it in the existing `WebState`/`JobWorkerHandle` service container. All
  concurrent TDoc jobs and existing queued spec jobs share this runtime and
  its writer lane.
- **CLI:** create one runtime for each batch command invocation, use it for
  the complete operation, then drain and shut it down in `finally`.
- **MCP:** continue to enqueue existing jobs. MCP does not create a second
  runtime or implement a second batch loop.
- **Direct single-file calls:** use the same runner with one item. This is
  primarily a contract-unification path; it does not imply a throughput gain
  for one file.

### Configuration

Add a shared `[pipeline]` settings block with a `prepare_workers` value. The
default is CPU-aware and bounded using the equivalent of:

```python
min(32, (os.cpu_count() or 1) + 4)
```

The pending-work capacity is derived as `prepare_workers * 2` rather than
being unbounded or separately tuned by each surface. `server.max_concurrent_jobs`
continues to control the number of active job handlers; it no longer defines
the per-item preparation pool size. The same pipeline worker setting is used
by CLI and server processes so behavior is predictable across surfaces.

The setting is configuration-only initially. No per-command worker flag is
required for this change; a later CLI override can be added without changing
the runner contract if operational experience shows it is needed.

## TDoc data flows

### Database TDoc IDs

1. Candidate selection remains a repository read and retains all current
   filters, `max_batch`, `force`, and parsed-row rules.
2. Each selected ID enters the shared runner with `prepare_extract()`.
3. A `PreparedTDocExtract` enters the serialized writer through
   `persist_prepared_extract()`.
4. Cover, TTCN, body-change, extract metadata, FTS, and vector updates remain
   within the existing logical item persistence command.
5. The service's synchronous `extract_many()` remains a compatibility wrapper
   over the runner; queued handlers use the async runner against the shared
   web runtime.

### FTP-folder URLs

1. Validate and discover matching file URLs with the existing deterministic
   breadth-first traversal.
2. Submit each discovered URL to the shared runner using
   `prepare_from_url()`.
3. A `PreparedDirectTDoc` uses `persist_prepared_url()` on the writer when
   the URL maps to a stored TDoc and requires database/cache metadata writes.
4. A `DirectParseResult` returned directly for an FK miss, cache hit, or
   in-memory parse is treated as already complete and does not enter the
   writer.
5. `extract_from_url_batch()` remains the synchronous public wrapper and
   returns its existing `DirectParseBatchResult` shape.

Both CLI and web `parse_tdoc_url` folder paths use this same flow. A single
file URL uses the same one-item flow.

### Local folders

1. Collect legitimate files in the existing sorted order and preserve the
   current recursive filtering and output-path rules.
2. Submit file reads and `extract_from_bytes()` calls to the preparation pool.
3. Do not use the database persistence lane because this mode is explicitly
   local/in-memory and has no repository writes.
4. Emit output files and summaries in source order after item completion. File
   parsing can overlap while output ordering and existing counters remain
   deterministic.

## Error and result contract

Add one service-level TDoc exception classifier used by all runner adapters.
It returns a normalized status and reason string:

```text
success: value
skipped: "ExceptionClass: message"
failed:  "ExceptionClass: message"
```

The baseline classification is:

- `TDocNotYetOnFTPError` → `skipped`;
- `TDocTooLargeError` → `skipped`;
- existing recoverable parse, lookup, type, and download errors → `failed`;
- persistence exceptions → `failed` for that item, with later writer
  commands still processed.

The normalized key is the TDoc ID for database-mode inputs and the normalized
FTP URL for URL-folder inputs. Local-folder adapters retain their existing
path-oriented logging and counters while using the same classification
internally.

Adapters preserve existing public contracts:

- `BatchExtractResult` remains the database-mode result type;
- `DirectParseBatchResult` remains the direct URL-folder result type;
- local-folder CLI output and counters remain unchanged;
- queued TDoc job summaries include `failure_details` and
  `skipped_details` maps;
- MCP enqueue responses and HTTP job envelopes remain unchanged;
- item-level failures do not change the existing web job terminal-status
  policy;
- CLI exit-code behavior remains unchanged.

## Persistence, cache, and search safety

- Preparation code may perform network, filesystem, cache, conversion, and
  parser work, but it must not mutate repository tables.
- Each database-backed item is persisted by one writer command that preserves
  current multi-table ordering and post-parse index/embedding behavior.
- Repository reads performed during preparation use the existing per-call
  session behavior and do not acquire the persistence lane.
- Cache writes remain atomic. Cache size enforcement/eviction must be guarded
  so concurrent preparation tasks cannot race while accounting for or
  removing cache files.
- Search reads remain independent of the writer queue and use the existing
  SQLite WAL/read-connection strategy. FTS/vector updates remain inside the
  writer command so readers observe a prior or fully committed item state.
- The runtime must not close the HTTP client, cache, or database engines until
  preparation and submitted writer work have drained.

## Cancellation and shutdown

For a running batch:

1. cancellation prevents new preparation submission;
2. already-running preparation may finish without mutating repositories;
3. persistence commands already submitted finish their current atomic item;
4. unsent prepared values are discarded and included in the existing
   cancellation/result accounting;
5. the runtime drains before process/app shutdown disposes resources.

The web path uses its existing `asyncio.Event`. CLI paths use the runner's
synchronous cancellation hook and ensure shutdown happens in `finally`,
including interruption during a folder or database batch.

## Testing strategy

### Runtime and runner unit tests

- preparation tasks overlap when worker count permits;
- persistence commands never overlap;
- bounded capacity prevents unbounded submitted/prepared work;
- async capacity waits do not block the event loop;
- writer failure is isolated to one item and later items continue;
- cancellation stops unsent preparation and drains submitted writes;
- shutdown waits for the writer before resources are disposed;
- source-order result reconstruction is deterministic.

### TDoc service tests

- database-ID batches use parallel preparation and serialized persistence;
- FTP-folder batches use the same runner and preserve URL-keyed results;
- missing FTP URLs and oversized files are skips in every batch mode;
- ordinary parse/download/lookup errors remain failures;
- cache hits, force behavior, revision-specific cache keys, and FK-miss URL
  behavior remain unchanged;
- concurrent cache writes and eviction do not corrupt or lose artifacts.

### Surface and integration tests

- CLI database, local-folder, and FTP-folder commands reach the shared runner;
- HTTP queued jobs use the same runner and return the same detail maps;
- MCP `parse_tdocs` continues to enqueue the HTTP-equivalent job;
- web FTP-folder jobs no longer process files serially;
- CLI output, counts, exit codes, and result ordering remain compatible;
- search succeeds while a persistence command is deliberately delayed;
- two jobs share one process-wide writer without concurrent mutations.

Run the focused new tests, the full offline SQLite suite, changed-file Ruff,
and `git diff --check`. Online tests remain opt-in.

## Documentation updates

The implementation must update the relevant architecture and operational
documentation, including:

- `docs/architecture.md` for the service-level runner and surface data flow;
- `docs/cli.md` for parallel folder/database behavior and configuration;
- `docs/web-server.md` for queued TDoc job behavior;
- `doc3gpp.toml.example` for `[pipeline]` settings;
- `AGENTS.md` and `README.md` where their surface/architecture summaries need
  synchronization;
- `docs/conventions.md` if the shared concurrency or lifecycle rule belongs
  there.

## Acceptance criteria

The work is complete when:

1. all listed TDoc surfaces use the shared runner;
2. preparation overlap is demonstrated by tests for CLI and web paths;
3. no database mutations occur outside the serialized writer for DB-backed
   TDoc items;
4. expected skips and per-item failure details match across surfaces;
5. URL/local folder processing no longer serializes document preparation;
6. cancellation, shutdown, cache safety, and search responsiveness are
   covered by tests;
7. existing public result/output/status contracts remain compatible; and
8. the full offline test suite and changed-file lint pass.
