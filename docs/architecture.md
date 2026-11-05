# Architecture

doc3gpp is a layered Python SDK with CLI, HTTP, browser, and MCP adapters.
This document covers structure and ownership, not command syntax or operational
algorithms. See [processes](processes.md), [configuration](config.md), and the
[code map](code-map.md) for those details.

## Layers and dependency rules

```text
CLI / HTTP / MCP adapters
          |
          v
     services + factories
       /       |       \
 scraping   parsers   repository Protocols
 transport  pure data      |
                         SQL implementations
                              |
                         engines / ORM

domain models are shared across these boundaries
```

| Layer | Responsibility / constraint |
| --- | --- |
| `models/` | Slotted domain dataclasses and shared contracts; never return ORM entities to callers |
| `repository/protocols.py` | Persistence and collaborator interfaces |
| `services/` | Orchestration over injected protocols; no concrete ORM operations |
| `scraping/` | HTTP/FTP and download/cache transport; no HTML/Excel domain parsing |
| `parsers/` | Deterministic parsing/conversion/chunking; no network access |
| `storage/db/`, `storage/backends/` | SQLAlchemy metadata, engine lifecycle, bootstrap, dialect options |
| `storage/repositories/` | SQL implementations and compression of stored content |
| `settings/` | TOML discovery, validated settings, cached loading, config writes |
| `cli.py`, `cli_server.py`, `web/` | Thin input/output adapters and process composition |

Services can use both scraping and parsers; these are not a linear chain in
which parsers depend on network transports. Factories are the composition
boundary that imports concrete repositories. Preserve Protocol/implementation
agreement when changing filters or return types.

## Composition and lifetimes

`services/factory.py` builds repositories and services for the CLI/SDK. Settings
and engine getters are cached per process. Tests changing environment/database
configuration must reset the relevant caches; see [conventions](conventions.md).

`web/app.py` builds the service container during lifespan startup, owns the
background worker and shared `PipelineRuntime`, and injects services through
`web/deps.py`. It shares a conversion pool and lazy remote embedder across
search/parse/index services. Shutdown drains work before closing resources and
disposing the main/testcase/specdata engines.

Ordinary CLI commands call factory-built services, not SQL repositories.
Server-management status/log helpers are an explicit adapter-level exception
for job/process inspection. HTTP routes and MCP tools reuse service/render
helpers instead of reimplementing domain queries or conversions.

## Databases and ownership

There are **three logical databases/SQLAlchemy bases**:

| Database | Base / engine | Owns |
| --- | --- | --- |
| Main | `Base` / `get_engine` | TSGs, meetings, TDocs/auxiliary files, WIs, specs/versions, TDoc extraction/content/sidecars/inventory, jobs, TDoc indexes |
| Testcase | `TestCaseBase` / `get_testcase_engine` | `testcases`, `testcase_status`, `testcase_sources` |
| Specdata | `SpecDataBase` / `get_specdata_engine` | `spec_doc_sources`, `spec_doc_tocs`, `spec_doc_chunks`, `spec_doc_zip_inventory`, `spec_doc_presentations`, FTS/vector sidecars |

Secondary SQLite URLs default to siblings of the main file. Main spec headers
and versions remain separate from the extracted spec-document corpus; testcase
rows are not main metadata. SQL relationships cannot be assumed across these
databases. Services batch-enrich output with parsed/inventory information.

SQLite connections use foreign-key enforcement and concurrency settings such
as WAL/busy timeouts where configured. FTS5 and sqlite-vec are SQLite-specific;
the presence of other engine backends does not make every feature portable.
`create_schema` provides additive/idempotent bootstrap additions, not a fully
wired Alembic migration history. See [schema recovery](processes.md#schema-and-recovery).

Use `<resource> schema` or HTTP/MCP schema reads for current columns. The static
registry in `models/schema_info.py` is checked against ORM definitions by tests;
it deliberately excludes transient fields such as spec `parsed`/`zip_files`.
Avoid duplicating exhaustive column inventories or fixed schema counts here.

## Content identity and storage

- TDoc IDs identify metadata rows. A normalized immutable `ftp_url` identifies
  an extraction revision and its cover, TTCN/body sidecars, content, and inventory.
  URL reads can expose historical extracts independently of the current parent.
- `tdoc_files` is auxiliary listing metadata linked by TDoc ID, not content.
- `tdoc_extracts` is the per-URL extraction parent, carrying version, cache
  pointer, stored-document count, and skipped sources.
- `tdoc_cr_annexes` stores CR supporting content only; the CR main is in the
  derived archive. WID, LS, and discussion stores include both main and annexes.
- All four content tables share compressed full Markdown, hashes/token counts,
  exact source paths, and document identity. Atomic replacement is per bundle,
  not across the whole batch or filesystem/index operations.
- `models/tdoc_content_families.py` is the routing/completeness registry. Parse
  selection, retrieval, search, and schema reporting read it; do not invent a
  second list of supported TDoc types in an adapter.
- TDoc/spec ZIP inventories preserve original decoded outer names, ordering,
  and duplicates for every extension. Unknown (`null`) differs from empty (`[]`).
- Spec presentation full Markdown lives in SQL alongside independently
  searchable chunks. Presentation and chunk replacement share a transaction.
  TOC, source ledgers, caches, and index updates are separate operations.

Filesystem caches are derived/download artifacts, not a substitute for source
SQL. The CR main is the important exception to SQL-first full-content reads.
See [cache lifecycle](processes.md#cache).

## Search and output contracts

Unified search facades select text/vector/hybrid/filter-only implementations,
then produce shared public rows. Content identity and matched-chunk provenance
are checked rather than inferred from rank alone. Index services coordinate
independent component status/rebuilds; source parsing and indexing remain
separate concepts. See [query/index flows](processes.md#indexing).

CLI output, HTTP JSON, and MCP read tools use shared rendering/payload helpers.
Matching JSON surfaces use compact Unicode-preserving serialization. HTML
escapes upstream text and renders Markdown with raw HTML disabled. MCP returns
the packaged skill as raw Markdown rather than a JSON read envelope.

The web job queue persists requests and logs in SQL, while in-process handles
own cancellation events, bounded SSE queues, and pipeline resources. It is a
single-process coordinator, not a distributed queue. Capacity and cancellation
guarantees are defined in [pools and jobs](processes.md#pools-and-background-jobs).
