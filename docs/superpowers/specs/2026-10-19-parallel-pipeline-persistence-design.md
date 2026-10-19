# Parallel TDoc/Spec Pipelines with Serialized Persistence

**Status:** Design approved in conversation; implementation pending

## Problem

Queued TDoc and specification work currently executes too much of the
pipeline as one unit. The background worker defaults to one whole job at a
time, and the existing service methods combine network access, document
conversion/parsing, and database writes. A slow download or conversion can
therefore reduce throughput, while concurrent database access would risk
SQLite contention and inconsistent multi-table updates.

The required behavior is:

1. Fetching, downloading, conversion, parsing, extraction, and chunking may
   run in parallel.
2. All database save/update work for queued TDoc/spec pipelines must use one
   global persistence lane.
3. Search queries must remain responsive while sync/parse preparation and
   persistence are active.
4. Existing per-item skip, failure, cancellation, and output semantics must
   remain compatible unless explicitly changed by this design.

## Scope

### Included

- Queued server/MCP jobs for TDoc parsing:
  `PARSE_TDOCS` and `PARSE_TDOC_URL`.
- Queued specification jobs:
  `SYNC_SPECS` and `PARSE_SPEC_DOCS`.
- The shared runtime used by those jobs:
  - a bounded preparation pool;
  - one process-wide persistence writer;
  - lifecycle, cancellation, progress, and error propagation.
- Database and connection behavior needed to keep search reads responsive.
- Unit and integration tests for concurrency, persistence ordering, search
  responsiveness, and failure/cancellation behavior.

### Not included

- Making synchronous CLI commands asynchronous or changing their public
  command contracts.
- Splitting every item into a separate visible job row.
- Parallel writes to the database.
- A cross-process distributed queue or a guarantee spanning multiple server
  processes. The guarantee is per running server process, which is the current
  worker model.
- Unrelated meeting, testcase, cache-purge, or index-maintenance jobs except
  where their database interactions must avoid interfering with search.

## Design

### Shared pipeline runtime

The server creates one `PipelineRuntime` during lifespan startup and exposes
it through `WebState` / `JobWorkerHandle`.

The runtime owns:

- `prepare_executor`: a bounded `ThreadPoolExecutor` for blocking network,
  filesystem, conversion, parser, and chunker work;
- `persist_executor`: a `ThreadPoolExecutor(max_workers=1)` for all prepared
  TDoc/spec database mutations;
- submission and shutdown methods that make the persistence lane observable
  and drainable;
- queue/backpressure accounting so an unbounded number of prepared results is
  never retained in memory.

`server.max_concurrent_jobs` remains the upper bound for active job handlers.
The preparation pool has an explicit bounded capacity derived from the same
server setting, with no nested per-item executor that could multiply the
configured concurrency. The persistence executor is always one worker and is
not configurable in this change.

The runtime is created before the worker task starts and is drained before
database engines are disposed during lifespan shutdown. A shutdown must not
close an engine while a persistence future is still running.

### Preparation/persistence contract

Each pipeline item is divided into two explicit phases:

```text
prepare(input) -> immutable prepared result / item failure
persist(prepared result) -> persisted result / item failure
```

Preparation code may use network clients, caches, parsers, converters, and
pure domain helpers. It must not call a repository mutation method. The
prepared result contains all data needed for the persistence operation,
including cache metadata and sidecar rows where applicable.

Persistence code runs only on the single writer thread. It owns repository
mutations, transaction boundaries, and post-parse database work. Repository
objects and SQLAlchemy sessions are not shared between preparation workers.

The writer submits one item as one logical persistence command. A command
must preserve the existing multi-table ordering and atomicity guarantees for
that item. If the existing repository surface cannot express the required
transaction boundary, add a repository/service write method rather than
moving individual writes into the preparation phase.

### TDoc pipeline

For database-mode TDoc parsing, candidate selection remains a read operation.
Each candidate then runs preparation concurrently:

1. resolve the stored FTP URL and cache identity;
2. download/read the ZIP and extract the DOCX;
3. convert the DOCX to Markdown and update filesystem cache artifacts;
4. parse CR, TTCN, and body-change data;
5. construct an immutable prepared TDoc result containing the cover,
   sidecars, extract metadata, and any index/embedding input.

The prepared result is submitted to the global writer, which performs the
cover, TTCN, change-detail, extract-metadata, and post-parse index/embedding
updates. A direct URL folder parse uses the same per-file preparation and
persistence contract after URL discovery. Filesystem cache writes must remain
atomic and must not allow two tasks to corrupt the same cache key.

### Specification pipeline

For `SYNC_SPECS`, list-page discovery and each detail/follow-up fetch remain
parallel preparation work. Detail HTML parsing, version construction, and
ETSI/CR follow-up parsing complete before a prepared spec result is submitted
to the writer. The writer applies the header/version updates and the
`last_synced_at` stamp using the existing partial-sync safeguards.

For `PARSE_SPEC_DOCS`, ZIP fetching, DOCX conversion, file ordering, TOC
extraction, and chunk construction happen in preparation workers. The writer
persists source metadata, TOC rows, chunks, parsed status, and the associated
post-parse index/embedding updates as one logical item command.

The existing recency/identity skip checks occur before expensive preparation
where possible. Checks that depend on current persisted state are repeated on
the writer thread immediately before mutation so overlapping jobs cannot
silently overwrite newer work.

### Job worker integration

The existing `JobWorker` continues to claim jobs and emit job lifecycle
events. The TDoc/spec handlers become orchestration layers that:

1. discover candidates;
2. submit bounded preparation work to `PipelineRuntime`;
3. submit completed prepared results to the global writer;
4. aggregate successes, skips, failures, and persisted counts;
5. emit progress through the existing throttled callback.

Other job kinds retain their current behavior. They must not acquire the
pipeline persistence lane unless a later design explicitly opts them in.

Preparation and persistence futures are awaited from the handler without
blocking the asyncio event loop. Blocking service work is executed in the
runtime executors, not directly in the worker task.

## Keeping search responsive

Search reads must not wait behind preparation or the persistence queue.

The implementation will enforce this through all of the following:

1. **Separate read execution:** HTTP/MCP/CLI-facing search calls remain on a
   read-capable executor/thread path and never submit to the persistence
   queue.
2. **Short writer transactions:** preparation never holds a transaction;
   each persistence command opens the required transaction only for its DB
   mutations and commits promptly.
3. **SQLite WAL/read concurrency:** SQLite engines used by the main and
   specdata databases enable WAL mode and a busy timeout where supported.
   Readers use separate connections, allowing an in-progress writer and a
   consistent read snapshot to coexist. Existing dialect-specific backend
   hooks remain the place for these settings.
4. **No process-wide DB mutex:** the global serialization guarantee is
   provided by the writer lane, not by a lock acquired by search. Search
   queries therefore cannot be queued behind writes.
5. **No event-loop blocking:** search and job handlers never execute blocking
   SQL or parsing directly on the event loop.
6. **Index updates stay in the writer lane:** FTS/vector sidecar updates are
   part of the persistence command, so search sees either the prior committed
   snapshot or the new committed snapshot, never partially written rows.

If a database backend cannot enable WAL, the implementation must still keep
transactions short and use a separate read connection; tests must verify the
backend's observed behavior. Search latency must be tested while the writer
is deliberately held on a slow item. The acceptance criterion is that search
requests continue to execute and are not serialized behind the pipeline
writer; exact latency bounds are environment-dependent and should not be
hard-coded.

## Ordering, backpressure, and cancellation

- Prepared results are submitted to the writer in completion order unless a
  pipeline requires source order for correctness. The job summary preserves
  per-item identity, not completion order.
- The number of outstanding preparation plus persistence items is bounded.
  When the persistence queue is full, preparation submission pauses rather
  than accumulating unbounded DTOs.
- A preparation failure is recorded for that item and does not stop other
  items.
- A persistence failure is recorded for that item; the writer remains alive
  and continues with later commands. A command failure rolls back its own
  transaction.
- Cancellation stops scheduling new preparation work. Work already executing
  may finish its pure preparation phase. Writes already submitted to the
  writer finish their current atomic command; unsent prepared results are
  discarded. The final job summary reports completed, skipped, failed, and
  cancelled work consistently with existing job behavior.
- Server shutdown signals cancellation, stops accepting new pipeline work,
  waits for active preparation tasks to settle, drains submitted persistence
  commands, and only then disposes engines.

## Compatibility and configuration

- `server.max_concurrent_jobs` continues to control job/preparation pressure.
- The persistence worker count is fixed at one to make the global guarantee
  explicit.
- Existing CLI service methods, job kinds, route payloads, MCP payloads, and
  summary field names remain stable.
- Progress messages may gain prepared/persisted counts but retain existing
  terminal status and summary contracts.
- Documentation will update the server/job architecture and concurrency
  configuration notes after implementation.

## Testing strategy

### Unit tests

- preparation tasks overlap using barriers/events;
- two different jobs share one writer and never execute persistence commands
  concurrently;
- writer command failure does not terminate the writer or lose later items;
- bounded queue applies backpressure;
- cancellation prevents unsent writes and allows submitted writes to finish;
- repository mutation methods are called only from persistence commands;
- skip and `last_synced_at` behavior remains unchanged.

### Integration tests

- TDoc parsing persists all cover/sidecar/cache metadata through the writer;
- spec sync persists headers and versions through the writer;
- spec-document parsing persists TOC/chunks and parsed metadata through the
  writer;
- concurrent TDoc/spec jobs use one global writer;
- search succeeds while a writer transaction is deliberately delayed;
- SQLite WAL/read snapshots do not expose partial item writes;
- lifespan shutdown drains the writer before engine disposal.

### Regression verification

Run the complete offline SQLite suite and `ruff check .`. Existing online
tests remain opt-in and are not required for the concurrency unit tests.
