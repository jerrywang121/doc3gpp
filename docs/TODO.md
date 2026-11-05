# Engineering backlog

This is proposed work, not the current interface. Current operational limits
are in [known-constraints.md](known-constraints.md). References use modules and
symbols rather than historical source line numbers.

## Security and deployment

- [ ] Add HTTP/MCP authentication and authorization for reads, jobs, purge,
  cancellation, and mutations; reject unsafe public binding or require an
  explicit deployment policy (`web/app.py`, routes, MCP registration).
- [ ] Enforce a single worker process or introduce ownership/leases/heartbeats
  and stale-only recovery. Guard terminal transitions against stale owners
  (`JobWorker`, `SQLAlchemyJobRepository`).
- [ ] Apply a deliberate dotenv/file-secret policy consistent with process-env
  filtering, with regression tests (`Settings.settings_customise_sources`).
- [ ] Align user-config initialization with XDG-aware discovery; currently
  initialization writes the fixed `~/.config` path even with `XDG_CONFIG_HOME`.
- [ ] Audit persisted cache-key validation/root containment on Windows as well
  as Unix, including both separator styles (`scraping/cache.py`, content readers).

## Data integrity and search

- [ ] Propagate TSG sweep `force` into per-spec interval checks and add recent-spec
  CLI/queued regressions (`SpecService.sync`, `_sync_one_spec`). Single-spec force
  already works; see the documented mismatch in known constraints.
- [ ] Forward CLI single-file URL `--force` to direct extraction and test refresh
  of an already-complete historical URL (`_tdoc_parse_direct`).
- [ ] Make vector initialization/model changes dimension-aware, including clean
  databases with non-384-dimensional embedders (`storage/db/migrate.py`, factories).
- [ ] Improve recovery across SQL, cache, TOC/source ledgers, and index updates.
  Per-TDoc bundles and spec chunks/full presentations already have atomic SQL
  replacements; test the remaining boundaries with failure injection.
- [ ] Replace stale auxiliary TDoc rows only after a confirmed complete successful
  listing (`TDocFileService`, auxiliary repository).
- [ ] Audit raw-vs-compiled FTS query boundaries in hybrid search and retain
  regressions for hyphenated TDoc IDs/dotted spec IDs before changing behavior.
- [ ] Review snapshot/chunk-policy freshness and old-URL retention policies;
  document any cleanup/rebuild contract before introducing automatic deletion.

## Verification

- [ ] Continue fixture/settings/engine-cache isolation audit, including offline
  auto-sync mocks and xdist on Windows.
- [ ] Add a deterministic offline lint/test CI matrix; release publication alone
  is not test coverage.

## Resolved or superseded review items

Do not reopen these based only on an older design/review:

- Queued cancellation is durable and conditional; terminal cancel is idempotent.
- HTTP/MCP TDoc content shares `TDocContentService` and the authoritative readers.
- Markdown/raw upstream HTML is escaped or constrained by shared rendering.
- The web lifespan closes its lazy shared embedder after draining jobs.
- Config writes are atomic/permission-aware, and project secret writes require
  explicit opt-in.
- The vector snapshot-day probe is set-based; old scratch benchmark numbers are
  not a current performance guarantee.
- ZIP/gzip fixture timestamps were made deterministic for xdist collection.
