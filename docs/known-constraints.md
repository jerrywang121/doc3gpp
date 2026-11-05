# Known constraints

Current implementation limits, not historical test results. For remedies and
runtime context see [processes](processes.md); proposed work is in [TODO](TODO.md).

## Configuration and security

- **No HTTP/MCP authentication or authorization.** Host/origin checks are
  transport protection, not identity. Keep loopback-only unless an external
  trusted gateway protects both reads and mutations.
- **Process environment and `.env` differ.** Only process variables are filtered
  through `ALLOWED_ENV_VARS`; the dotenv source can set other recognized fields
  absent from higher-priority TOML. See [precedence](config.md#discovery-and-precedence).
- **Settings/services are process-cached.** Restart after config changes. The
  server does not hot-reload configuration or retroactively resize pools.
- **Config writes reserialize TOML.** Comments/formatting are lost. API keys are
  redacted by `config show`, but database URLs and other fields may be sensitive.
- **User bootstrap and XDG discovery differ.** `config init --target user` writes
  to `~/.config/doc3gpp/config.toml`; discovery instead uses `XDG_CONFIG_HOME`
  when set. See the [bootstrap workaround](config.md#inspect-and-edit).
- **Outbound TLS verification defaults to false.** Enable it where possible;
  it does not secure the inbound server.
- MCP host checks currently allow localhost/127.0.0.1. Changing the bind address
  or origin list alone does not provide a public-host deployment configuration.

## Schema and backfills

- Bootstrap is additive/idempotent for explicitly supported changes, not a
  general Alembic migration history. Older schemas without dedicated migrations
  need individual assessment and backups, not an assumed automatic upgrade.
- Main, testcase, and specdata are separate databases. Legacy testcase tables
  can remain orphaned in an old main file; nothing automatically moves/drops them.
- `db init` creates inventory/presentation tables but does not backfill parsed
  sources. Force reparse to populate old inventories/full presentations.
- Legacy TDoc extractions are incomplete version 1 until reprocessed; an old
  cover or single-member cache cannot prove full ZIP completeness. Very old
  `zip_path`/`markdown_path` schemas are a separate compatibility issue, not
  repaired just by adding the version-2 completeness columns.
- Some sync entry points bootstrap schema for fresh-database ergonomics;
  applications should still initialize required scopes explicitly.

## Upstream and sync

- Sources are coupled to 3GPP portal/DynaReport/FTP layouts. Unexpected HTML
  can produce fewer/empty rows rather than a hard error. Record fixtures when
  upstream layout changes; online tests are opt-in and inherently variable.
- The canonical FTP root is hardcoded in transport/source helpers, while the
  TDoc portal list URL is configurable. Portal lists provide no reliable
  Last-Modified optimization; skip rules use local intervals/windows.
- Missing meetings cannot be bypassed by `tdoc sync`; no-selector sync revisits
  tracked meetings rather than discovering the entire calendar.
- **TSG spec sweep force is not propagated.** `SpecService.sync(force=True)`
  still calls per-spec workers without bypassing their interval gate. This
  affects CLI TSG/no-selector sweeps and queued TSG sync. Single-spec
  `sync_spec(force=True)` does bypass the gate. The advertised sweep flag is a
  known mismatch, not a promise that documentation can repair.
- Auxiliary-file sync upserts discovered rows; a successful listing does not
  necessarily remove entries that disappeared upstream.
- Fallback TDoc ZIP URL synthesis is fixture-backed for R5s/R5w, not a universal
  URL builder for every TSG; prefer a stored/listed source URL.
- Non-3GPP direct-mode auto-sync scans the whole URL for an ID, so a directory
  segment can trigger sync even when the filename itself has no TDoc ID.

## Extraction and content

- Only registered CR/WID/LS/discussion families are eligible. Standalone
  presentations are not direct TDoc inputs; presentation ZIP members are.
- Native Office extraction requires `[extract]`; legacy `.doc`/`.ppt` also needs
  Writer/Impress. PDF, spreadsheet, image, and nested-ZIP content is not converted.
  Presentation extraction has no OCR/chart understanding or inferred metadata.
- A failed supporting member leaves an extraction incomplete and is retried by
  every normal parse, even if the same converter failure is permanent.
- Single-file URL parsing accepts `--force` but dispatches extraction with
  `force=False`. Use `tdoc parse --tdoc <id> --force` for the TDoc's current URL;
  do not rely on the URL flag to refresh an already-complete historical source.
  Single local files reject force instead.
- CR main Markdown is cache-only and **its body is not full-text indexed**.
  Covers/change/TTCN fields and SQL annex/family content are searchable. A
  cache-purged complete CR can be excluded by normal SQL pending selection;
  recover with forced parse or the ID-based raw-extraction path.
- Raw ID reads that need extraction convert in-process rather than using the
  batch conversion pool; URL/HTTP/MCP reads never extract implicitly.
- SQL bundle replacement is atomic, but cache/inventory/TOC/ledger/index updates
  are not one end-to-end transaction. A crash can leave partial derived state.
- ZIP inventory contains untrusted decoded names and all extensions, not just
  converted content. Duplicate names are preserved in inventory, which does not
  imply ambiguous members can be selected as distinct converted documents.

## Search and freshness

- FTS5/vector features are SQLite-specific. Other engine backends do not imply
  equivalent search support; optional sqlite-vec and a remote embedding endpoint
  are required for semantic mode.
- TDoc semantic content is capped across documents. Later content may be
  FTS-searchable without an embedding. Status is not a promise of semantic
  coverage for every word in every source.
- Superseded ZIP URLs remain searchable while their content rows remain stored.
  Rebuilding reads stored URLs; it does not remove historical content.
- LIKE-based vector filters run after bounded KNN windows. Widening stops at
  4096 candidate rows, so selective filters can return fewer than the requested
  number of hits even when other matching corpus rows exist.
- Legacy/stale vector provenance can produce `matched_chunk: null`. A whole-file
  FTS expression may also lack a single satisfying chunk; neither case should
  be presented as a verified chunk match.
- Vector schema bootstrap can pin a default 384-dimensional table before a
  different model is configured. Model/dimension changes need explicit handling;
  a rebuild flag alone is not a general dimension migration guarantee.
- Chunk-policy changes and old snapshot-based `--stale-only` coverage need care.
  Use a full embedding rebuild after changing model/chunk settings or when
  newly synced older-dated metadata is missing; inclusive snapshot-day cutoffs
  may re-embed already indexed rows. Inspect status and provenance separately.
- Status avoids decompressing/chunking all content by using stored signatures
  and token counts. It cannot certify an externally corrupted compressed blob
  just because its stored hash/count remains unchanged. Full rebuilds may log
  and skip corrupt content while retaining older index rows; repair source SQL
  through reparse, then rebuild.

## Pools, jobs, and deployment

- The durable queue is **single-process**, not a distributed worker system.
  Startup fails all leftover running jobs as orphans. Multiple HTTP/stdio worker
  processes sharing the database can interfere with ownership/recovery.
- Cancellation is cooperative, not preemptive and not a rollback. A running
  converter/network call or submitted writer can finish after cancellation.
- Only participating operations use the shared single persistence executor;
  other SQL writes can contend. WAL/busy timeouts reduce, not eliminate, locks.
- A conversion-process death can fail other callers' unfinished conversions
  sharing that executor. Its outstanding conversion window is per call, not a
  process-wide backpressure guarantee.
- Periodic progress is throttled and bounded SSE queues can drop old events.
  SQL job logs are durable, but throttled-away progress is not replayed later.
- The packaged release workflow is not an automated test matrix. Local offline
  tests and fixture/cache isolation remain necessary, including xdist coverage.
