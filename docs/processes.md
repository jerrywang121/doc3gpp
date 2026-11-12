# Processes and runtime flows

This guide explains runtime behavior and recovery. Use the [CLI reference](cli.md)
for flags, [configuration](config.md) for defaults, and
[architecture](architecture.md) for storage ownership. HTTP and MCP adapters use
the same services, but do not inherit every CLI convenience such as auto-sync.

## Lifecycle at a glance

```text
reference data / upstream listings
  -> sync metadata into SQL
  -> discover eligible documents and apply skip rules
  -> download or reuse ZIP
  -> inventory + convert + parse + chunk in preparation workers
  -> persist prepared aggregate
  -> update derived FTS5/vector indexes when enabled
  -> query SQL/indexes and render CLI / HTTP / MCP output
```

Syncing metadata does not extract document bodies. Parsing does not guarantee
that optional indexes or embeddings are enabled. Index rebuilding generally
reads stored SQL content, not the original ZIPs. Cache files, source SQL,
and derived indexes therefore have separate lifecycles.

## Sync

### Meetings

`meeting sync --tsg R5` calls `MeetingService.sync`:

1. Resolve the TSG and inspect its `meeting_last_sync`.
2. Unless forced, skip if the configured meeting interval has not elapsed.
3. Fetch the DynaReport calendar and parse it without network calls in the parser.
4. Stamp the TSG onto parsed meetings, upsert them, and record the successful sync.

An ordinary skip is a successful no-op, not an error. Explicit sync validates
the TSG reference; bootstrap/schema initialization seeds the reference table.

### TDoc metadata and auxiliary files

`tdoc sync --meeting-id <id>` or `--meeting <name>` uses
`TDocSyncCoordinator`:

1. Resolve a stored meeting. **No meeting row means no TDoc sync.**
2. For a previously synced meeting, first check the closed-meeting window,
   then the recent-sync interval. A never-synced meeting bypasses both checks,
   so old meetings can still be populated for the first time.
3. Fetch the portal export at `sync.tdoc_list_url_template`, parse TDoc rows,
   and upsert the metadata.
4. If the meeting has an FTP URL, scan its auxiliary-file listing. Without an
   FTP URL, keep the metadata sync and report that the auxiliary scan was skipped.

`--force` bypasses both time gates. With no selector, the coordinator visits
distinct meeting IDs already represented in `tdocs`; it does not discover every
3GPP meeting automatically. Auxiliary `tdoc_files` rows are listing metadata,
not extracted content or the outer-ZIP inventory.

### Specifications and work items

`spec sync --tsg R5` fetches a specification list, then prepares detail pages
concurrently. Each spec has its own `specs.last_synced_at` gate; there is no
TSG-wide spec-sync timestamp. The sweep uses a bounded thread pool (default
`min(6, (cpu_count or 4) + 4)`) and shares an HTTP client. Queued spec sync
offloads this same service call to a thread; it does not use the shared pipeline
writer. Currently its `force` argument does not bypass per-spec gates in a
TSG sweep; see [the known mismatch](known-constraints.md#upstream-and-sync).

`spec sync --spec-id <id>` reads the stored header or bootstraps a missing one
from `https://www.3gpp.org/DynaReport/{no_dot}.htm`. The bootstrap normalizes the
responsible group to a seeded TSG; unknown specs or unsupported groups fail
with an actionable error. With no selector, the CLI syncs distinct TSGs already
present in `specs`.

Per-version ETSI PDF and CR-list follow-ups are **off by default**; cached values
are preserved. `--per-version-details` enables them, subject to their individual
availability checks. These requests use a separate executor to avoid workers
waiting on tasks in their own saturated pool. Follow-up errors are tolerated.
Persistence writes the header and versions before stamping `last_synced_at`;
this is not one transaction across every spec in a sweep.

`wi sync --tsg R5` fetches and parses the WI list and upserts records keyed by
`(wi_id, tsg_short)`. See [upstream conventions](3gpp-knowledge.md) for source
URLs and the interpretation of WI/spec fields.

### Testcases

`testcase sync` uses a separate testcase database:

1. List history ZIPs and choose the maximum `(year, week, revision)`.
2. Skip if that exact source file already has `parsed_at`, unless forced.
3. Download the ZIP, record its download, and select/extract its workbook.
4. Parse headers and path statuses; upsert headers and replace the associated
   statuses in bulk, then record parse completion.

This is a file-identity gate, not a time interval. A testcase header is keyed by
`(testcase_id, group)`; public status arrays contain `{path, gcf_ptcrb,
ttcn_status}`. Status filters match any status path and correlate both key
columns. `testcase show` returns all groups unless `--group` selects one.

## Auto-sync

Selected CLI commands run best-effort sync before their main operation when
`sync.auto_sync` is true (the default):

| CLI operation | Auto-sync scope |
| --- | --- |
| `meeting list` | Meeting calendars for inferred TSGs |
| `tdoc list` | Calendars and TDoc lists for inferred meetings |
| `tdoc show --tdoc` | Same selector-based path as a TDoc list |
| DB-mode `tdoc parse` | Same filters used for parse discovery |
| `tdoc parse --from-path/--from-url` | TSG/calendar/TDoc-list preparation for inferred source IDs |

Candidate extraction is deliberately narrow: exact TDoc IDs, recognizable TSG
prefixes, explicit meeting IDs, and matching stored meeting names/ranges.
Broad ambiguous patterns do not imply a full upstream crawl. Direct local mode
derives an ID from the filename. A canonical FTP file URL uses its basename;
folder URLs discover candidates by breadth-first traversal within the selected
depth. Non-3GPP URLs currently scan the whole URL string for an ID, including
directory segments.

Auto-sync never forces a sync or bypasses its skip rules. Reasons are printed
with `[auto-sync]`; failures become warnings and do not abort the read/parse.
Local parsing still does not persist its result even if the preliminary sync
adds metadata. Disable auto-sync for deterministic offline CLI reads.

`tdoc show --ftp-url` does **not** auto-sync: it reads the exact stored URL
snapshot. HTTP/MCP content reads also do not download, extract, or implicitly
invoke CLI auto-sync. Explicit HTTP/MCP mutation tools enqueue jobs instead.

## TDoc parsing

### Discovery and completeness

DB-mode discovery intersects filters with the eligible content-family registry:
CR, WID new/revised, LS in/out, and discussion. An explicit type filter cannot
make an unsupported type eligible. The CLI exception is an exact `--tdoc` with
no `--type`: the item reaches the pipeline and can be reported as unsupported.
Web/MCP always apply family eligibility, even for an exact ID.

Normal discovery excludes complete extractions **before** applying `max_batch`.
A cover row alone is not completeness: the parent extraction version, required
content rows, and skipped-source state matter. `--force` includes complete rows
and reprocesses them. Unsupported types, excessive sizes/member counts, and
not-yet-available FTP sources enter the skip bucket; other extraction errors
enter the failure bucket. Individual failures do not abort the entire batch.

### Prepare, convert, persist

1. Resolve immutable ZIP URL identity and reuse/download the source as allowed
   by cache provenance and force policy.
2. Record every non-directory **outer ZIP** member as ordered inventory,
   preserving decoded names, duplicates, and unsupported extensions. Do not
   descend into nested ZIPs.
3. Enforce archive limits and convert supported `.docx`, `.doc`, `.pptx`, `.ppt`
   members with the shared Office-conversion pool.
4. For a CR, identify exactly one Word main document with the structural cover
   detector. It alone supplies the cover, optional TTCN details, and optional
   body-change details. Presentations cannot become CR-cover candidates.
5. For WID/LS/discussion, use the family main-member selector; store both main
   and supporting documents. A failed main conversion fails the item. Failed
   supporting conversions are retained in `skipped_sources`, making it incomplete
   so a normal parse can retry.
6. Persist the prepared bundle through the serialized writer. The content
   repository atomically replaces the URL's family content, extraction parent,
   and applicable CR sidecars. Inventory and filesystem/index operations have
   their own boundaries; this is not a transaction across SQL and disk.
7. Write a versioned Markdown archive and run enabled index hooks.

TTCN changed-function strings are derived from required changes and stored as
newline-delimited `<module>.<function>` entries. Partial names use `<module>.`
or `.<function>` sentinels. Non-TTCN body changes use tracked-change markers
and the configured gap/context windows.

Presentations are textual only: no OCR, image/chart interpretation, or inferred
domain metadata. Legacy Office conversion uses isolated temporary profiles and
a timeout. Historical `docx_count` and pool names cover all four formats.

### Direct mode and content reads

| Source | Result persistence |
| --- | --- |
| Local `.docx`/`.doc`/`.zip`, including local-folder batches | In memory/output files only |
| Canonical 3GPP FTP source with a matching stored TDoc | FK-aware cache and database persistence |
| Canonical FTP source without a matching TDoc | Output plus a warning; no persisted bundle |
| Non-3GPP URL | In memory only |

Standalone `.pptx`/`.ppt` is not a supported direct TDoc source; presentations
inside ZIPs are supported. Canonical FTP inputs require HTTPS; the bare
`www.3gpp.org/ftp/...` form is normalized. File URLs require TDoc-shaped
`.docx`/`.doc`/`.zip` basenames; other canonical paths are folders with optional
trailing slashes. See [local-mode flags](cli.md#doc3gpp-tdoc-parse-local-mode).

Single-file URL dispatch currently accepts but does not forward `--force`.
It can return an already-complete extraction rather than refresh it. For a
current stored source, use `tdoc parse --tdoc <id> --force`; forcing a historical
URL has no equivalent working CLI guarantee. A single local file rejects force.

Raw `tdoc show --tdoc` can run extraction before reading authoritative content;
a complete explicitly selected presentation can be served SQL-first. URL-keyed
raw reads, HTTP, and MCP are snapshots and never repair content implicitly.
`source_file` selects an exact full ZIP member path, not a local filesystem path.
Multi-document Markdown preserves source labels; CR main Markdown comes from
the derived archive, whereas other stored documents come from SQL.

## Specification-document parsing

`spec doc parse --spec <id> [--release R] [--version V]` resolves one version:
an explicit version is strict; otherwise choose the numeric-newest version
with a downloadable ZIP after release filtering. Paired release/version values
must align. There is no separate public fetch step.

1. Skip an immutable already-parsed version unless forced.
2. Reuse/download `{spec_doc.cache_dir}/zips/<spec_id>/<version>.zip`.
   Force re-downloads and reparses.
3. Inventory the outer ZIP and convert supported Office members. A ZIP without
   supported members fails; an oversized ZIP is skipped.
4. Extract Word TOC/sections/tables, convert presentation text, and build chunks.
   Presentations create neither TOC rows nor section/table metadata.
5. Store TOC, source/download/parsed state, and chunks. Full presentation
   Markdown and chunks are replaced atomically together; the entire parse,
   including TOC/cache/ledgers/indexes, is not one global transaction.
6. Run the enabled spec-document FTS5 and embedding hooks.

`spec doc show --source-file` reads the exact stored presentation from SQL;
it needs no ZIP/cache access. TOC/chunk/content reads do not trigger parsing.
Spec-version `parsed` and `zip_files` enrichments are loaded from specdata, not
columns on main-database `spec_versions`.

## Cache

TDoc cache subtrees under `cache.dir` are `zips/` and `markdown/`. Normal stored
URL keys combine a sanitized source stem with a hash of the normalized URL, so
revisions/paths do not collide. Derived v2 ZIPs contain a manifest and named
Markdown documents. Legacy plain/gzip/single-Markdown artifacts remain readable
only under legacy completeness/identity checks and are pending for reprocessing.

The combined size limit evicts old files using filesystem creation/change-time
ordering. It is not a true access-based LRU. Scoped purge defaults to `markdown`;
`--scope zips` and `--scope all` are explicit alternatives. Purging affects
cache files, not SQL rows or indexes; the separate spec-document cache is not
included. A forced TDoc parse verifies cached ZIP provenance and re-downloads
unverifiable sources rather than blindly trusting a filename match.

| Artifact | Survives cache purge? | Recovery |
| --- | --- | --- |
| TDoc metadata, cover/sidecars, inventories | Yes, SQL | Sync/reparse only when freshness/backfill is needed |
| CR annex and WID/LS/discussion full content | Yes, SQL | Can still read and rebuild text indexes |
| CR main Markdown | No; derived archive only | Re-extract/download the source |
| Spec full presentation, TOC, chunks, inventory | Yes, specdata SQL | Force parse for missing/legacy stored content |

Deleting cache files alone does not clear parsed SQL state or make every item
pending. Use an explicit forced parse to repair a complete-but-cache-missing CR
main; do not assume normal DB discovery will select it.

## Chunking

TDoc content semantic chunks and presentation FTS chunks use sliding windows of
whitespace tokens with overlap; they do not preserve paragraph or table
boundaries. Spec-document chunks use paragraph/sentence boundaries and
Markdown-table handling. Their non-table text can overlap; table rows are kept
atomic and headers are repeated when tables span chunks. A very large atomic
unit can constrain how tightly a budget is observed.

- TDoc metadata chunks cover searchable metadata/cover/change/TTCN material.
- TDoc content chunks cover SQL-stored annex/family documents. Their semantic
  cap is shared across documents in one TDoc, not multiplied per document.
- Presentation FTS chunks are indexed independently of semantic coverage, so
  semantic caps do not truncate presentation full-text indexing.
- Spec chunks also use `max_chunk_chars`; overlap inherits the semantic setting
  unless overridden. Word chunks carry `sections` and `tables`; presentation
  chunks keep source identity without inferred section/table labels.

Chunks retain exact source paths and deterministic identities/indices. An
index can prove a matched chunk only when current source identity, content,
and chunk provenance agree; legacy/stale provenance is not guessed.

## Indexing

FTS5 and vectors are derived, independent components. `tdoc index` and
`spec doc index` without rebuild flags report status. Rebuild text with
`--rebuild`, vectors with `--rebuild-embeddings`, or both with `--rebuild-all`;
`--batch`, `--resume`, and `--stale-only` control maintenance as documented in
the [CLI reference](cli.md).

TDoc FTS currentness checks expected source rows/IDs, content hashes, and
extraction versions, not just upload timestamps. Its headline `row_count` is
a logical TDoc count even though content/presentation sidecars have more rows.
SQL content can rebuild indexes after cache eviction. **The CR main body is not
full-text indexed**; its extracted cover/change/TTCN fields are.

Embedding needs `[semantic]`, SQLite vector support, and a configured remote
endpoint. It stores metadata and chunk vectors with source provenance. A parse
can succeed while an optional index hook fails or is disabled; inspect status
and run maintenance rather than assuming `parsed` means every index is current.
Change models/chunk policy deliberately and rebuild affected embeddings.

## Querying

Both unified search facades select the mode from the supplied inputs:

| `text` | `semantic` | Mode / score |
| --- | --- | --- |
| Present | Absent | FTS5 / text relevance |
| Absent | Present | Vector KNN / distance |
| Present | Present | Hybrid reciprocal-rank fusion / fused score |
| Absent | Absent | Source-SQL filters only / `null` score |

Filters narrow the source corpus; rich filter syntax is covered in
[conventions](conventions.md#filter-grammar). For TDoc semantic search,
candidate fanout compensates for deduplication and post-filtering but is bounded,
not an unlimited exhaustive vector scan. LISTSERV semantic/hybrid search keeps
vec0 KNN as its ordinary fast path and uses exact filtered cosine scoring when a
deep or chunk-crowded candidate window would otherwise be incomplete. That
fallback can score and order much of the eligible vector corpus; arbitrary
nonnegative offsets remain supported. Hybrid weighting is configured, not
supplied per request.

Public rows are flattened, in final rank order, with `search_mode`, `score`, and
FTS-only `previews`. TDoc rows always include `matched_documents` (possibly
empty). Each source match identifies the exact URL/document/path, source score,
previews, and `matched_chunk` (`chunk_id`, `chunk_index`, `text`) or `null`.

FTS and semantic-only searches may return source matches. Hybrid unions distinct
FTS/vector sources, orders vector matches by `(distance, document_id)` before
FTS-only matches, preserves shared FTS previews, and prefers verified vector
chunk provenance with verified FTS fallback. Metadata-only/filter-only hits may
have no source matches. A whole-document FTS expression can match across chunks
without any one chunk satisfying it; `matched_chunk` is then `null`. Stale or
legacy vector provenance also stays `null` rather than inventing a text match.

Spec-document results identify the actual `source_file`, `chunk_id`,
`chunk_index`, `sections`, `tables`, and text. CLI, HTTP JSON, and MCP share
the row-building/rendering contracts.

## Pools and background jobs

### Capacity and ownership

| Mechanism | Role |
| --- | --- |
| `server.max_concurrent_jobs` | Maximum active job handlers; default `3` |
| `PipelineRuntime` preparation threads | Bounded item preparation, shared by participating jobs |
| Pipeline capacity permits | Bound outstanding items to twice `prepare_workers` by default; held through persistence |
| Pipeline persistence executor | One writer for submitted prepared aggregates |
| `DocxConvertPool` processes | Shared Office conversion; `0`/`1` workers runs inline |
| Spec follow-up executor | Independent optional HTTP follow-ups |

The web lifespan owns a shared runtime/conversion pool and a lazily created
shared embedder. LISTSERV search/index facades and worker-built sync/parse
services borrow that provider; per-job services close their owned source/index
on the worker thread, while app shutdown closes its LISTSERV facades after job
drain and before the shared embedder. Standalone service and CLI builders close
only resources they create. CLI operations otherwise own their
invocation-scoped resources. Queued TDoc parse, direct-URL parse, and
spec-document parse use the shared preparation/persistence runtime. This does
**not** serialize every database write in the application: other sync/index/job/log
operations have separate boundaries.

TDoc pipeline output is deterministic in discovery order; progress reflects
completion. Do not infer commit order from output order. Bounded permits apply
backpressure rather than allowing every discovered item to queue in memory.

### Job state and cancellation

HTTP/MCP mutation requests create durable `queued` rows and return immediately.
The worker polls independently of its retention-cleanup cadence, conditionally
claims work, and runs up to the configured active-job limit:

```text
queued -> running -> succeeded | failed | cancelled
queued -> cancelled
```

Progress callbacks append throttled SQL log lines and bounded SSE events;
forced progress bypasses throttling, and oldest queued events may be dropped
when a consumer falls behind. Logs/status in SQL are the durable monitoring
surface. A successful job may still report skipped/failed items in its summary;
inspect counts and failure details, not only the outer status.

Queued cancellation is persisted immediately. Running cancellation is
cooperative: it stops new work and discards preparation that has not entered
persistence where supported, but does not kill a running conversion/thread or
roll back already submitted/committed writes. Shutdown signals cancellation,
drains handlers/pipeline work, closes the conversion pool and LISTSERV facades,
then closes the shared embedder and disposes engines. Cancelling an
already-terminal job returns its envelope.

At startup, stale `running` jobs are failed as `orphaned_after_restart`, not
resumed. Retention cleanup deletes old terminal job rows and their SQL logs;
it does not rotate `server.log`. Use one server process for a database:
multi-process ownership/recovery is not coordinated.

## Schema and recovery

`db init` is additive and idempotent for supported bootstrap additions, not a
general migration framework. Existing databases may need new tables before
using newer features:

```bash
doc3gpp db init --scope main
doc3gpp db init --scope specdata
```

Initialization does not backfill old ZIP inventories or presentation content.
Force-parse affected TDocs/spec versions to populate them. Inventory `null`
means unknown; `[]` means a known empty outer ZIP. Detail reads and metadata
sync never populate inventory from cache/network.

After content is present, rebuild text indexes for newly supported content and
embeddings for updated vector provenance as needed. `db reset` is destructive
and is not the normal upgrade/backfill path. See
[known constraints](known-constraints.md) before deployment or recovery.

## LISTSERV archive flow

### Public sync and date coverage

LISTSERV sync uses the exact supplied list ID to discover public archive pages
from `https://list.etsi.org/scripts/wa.exe?A0=...`. Only HTTPS requests to the
intended public LISTSERV host are followed, including redirects and embedded
body URLs. Authentication-required/private archives and arbitrary external
hosts are rejected; there is no attachment download path. Example IDs such as
`RAN-WG1` illustrate input syntax only and do not define an allowlist.

Sync stores message listing metadata, not message bodies. With no date filters,
it selects the rolling preceding calendar year through today, inclusive (the
anniversary date clamps February 29 to February 28 in non-leap years). Repeatable
`--date` comparisons (`=`, `!=`, `<`, `<=`, `>`, `>=`) intersect. They compare
the message's displayed calendar date, not a UTC-converted date. The returned
ISO-8601 datetime retains the source's displayed time and offset; naive and
date-only values remain distinguishable through `date_precision`. A date-only
value uses midnight as a placeholder. Aware times sort by instant, with a
wall-clock fallback for unknown zones; ordering involving unknown-zone or
day-only values is approximate.

Coverage belongs to the actual archive-page URL, not its week/month label:
ETSI may split one week across multiple pages, and a message date can differ
from the page label. Successful empty pages are covered; unrecognized or failed
pages remain retryable. Historical pages are skipped only when recorded
successful coverage includes the requested date predicate. The newest upstream
period (week, or a month-only latest label) is revisited each sync; `--force`
revisits every selected page. Page
messages, pagination links, and coverage commit atomically, so a failed page
does not imply coverage. A conservative first filtered scan can fetch more
historical pages than the displayed period suggests.

The SDK uses the same service contracts without the web job queue:

```python
from doc3gpp.models.listserv import ListservFilters, parse_date_set
from doc3gpp.services.factory import (
    build_listserv_parse_service,
    build_listserv_search_service,
    build_listserv_service,
    build_listserv_sync_service,
)

sync_service = build_listserv_sync_service()
try:
    sync_result = sync_service.sync(
        "RAN-WG1", dates=[">= 2026-10-01", "<= 2026-10-31"]
    )
finally:
    sync_service.close()

parse_service = build_listserv_parse_service()
try:
    parse_result = parse_service.parse("RAN-WG1")
finally:
    parse_service.close()

messages = build_listserv_service()
record = messages.show(
    "https://list.etsi.org/scripts/wa.exe?A2=RAN-WG1;2026-10-15;EXAMPLE"
)
search_service = build_listserv_search_service()
try:
    hits = search_service.search(
        text="handover",
        filters=ListservFilters(
            list_id="RAN-WG1",
            dates=parse_date_set([">= 2026-10-01", "<= 2026-10-31"]),
            limit=10,
        ),
    )
finally:
    search_service.close()
```

`RAN-WG1` and the URL are illustrative inputs, not an allowlist. Sync and parse
builders own their source and should be closed when finished; list/show/search
builders are SQL-backed reads. Close the search service when finished to release
any lazily created semantic service it owns.

### Separate parse and bounded message detail

`doc3gpp listserv parse LIST_ID` operates on already-synced records and does not
run sync. It skips parsed details unless forced. LISTSERV detail pages may embed
the message body in an HTML or plain-text iframe; that body representation is
fetched and parsed, but named attachments are metadata only and their bytes are
never fetched. `multipart/related` and text alternatives are body
representations, not downloadable attachments.

The parser removes recognizable quoted-reply/forward sections conservatively,
using structural HTML quote boundaries, plain-text delimiters, and groups of
reply headers, including bold labels rendered from HTML and headers separated by
blank lines. If a boundary is uncertain it favors retaining the current body
rather than cutting arbitrary prose. Email quoting is not standardized, so quote
removal is best-effort, not a guarantee for every message format. Unsafe
embedded HTML is excluded from stored Markdown. Parser improvements apply on
subsequent parses; force-reparse already stored messages to update them.

Before applying `[listserv].max_content_chars` (10,000 Unicode characters by
default), parsing extracts unique case-insensitive TDoc IDs from the current
message body, subject, and attachment names/URLs. Recognizable quoted prior
messages are excluded. Thus IDs after the storage boundary can remain in
metadata, but body text after that boundary is not retained or searchable.
Changing the setting affects subsequent parses; force-reparse to replace an
existing detail. Each parsed detail is atomically replaced; a failed parse or
forced reparse leaves its prior good detail intact. A parsed-empty message has
`content=""`; an unparsed message has `content=null`.

### Stored reads, search, and indexes

`listserv list`, search, show, and index status/rebuild read stored SQL and do
not fetch or parse upstream pages. List returns distinct IDs represented by
stored messages, not an archive catalogue. Show requires the exact detail URL
and returns the full **stored** body only; it cannot recover truncated text.

Search modes match the shared facades: `text` uses FTS5, `semantic` uses vector
search, both use hybrid RRF, and neither uses source-SQL filters only. The
filters for list ID, sender, subject, TDocs, and displayed dates apply in each
mode. Listing metadata and TDoc IDs extracted from subjects are searchable
before parsing; body and attachment IDs require parsing. Index input
includes stored message metadata, attachment names, IDs, and the bounded stored
body, but strips message/attachment URL strings from both FTS and embeddings
(URLs remain in output metadata). A TDoc ID extracted from an attachment URL is
retained as an ID field. Filter-only search needs neither index. Scores are
mode-specific; filter-only results have no score.

Search's `content_preview` is at most 200 characters. `content_truncated` marks
the original-to-storage cut; nullable `preview_truncated` marks the
storage-to-preview cut. Both flags are null for unparsed content. `show` and
search share `url` and effective `tdocs`; search never exposes full body content.
Filter-only results sort by newest message datetime. Ranked results sort by
relevance, then newest datetime, with URL as the deterministic final tie-break.
For unknown-zone or date-only records, newest-first order remains a documented
approximation.

FTS and vector indexes have independent freshness and rebuild state derived from
source revisions. Sync resolves each parsed page's duplicate message URLs to
their final occurrence before applying the requested displayed-local-date
filter; an earlier in-range duplicate cannot override a final out-of-range row.
Within the archive-page transaction, duplicate URLs are independently resolved
to their final occurrence before change detection; an existing source revision
advances at most once and only when that final message metadata differs from
stored metadata. Automatic updates follow the four `[listserv]`
hooks and the global search/semantic availability settings. A failed derived
update does not roll back committed source data and leaves the component stale
for rebuild.
Rebuild uses SQL-stored data, not ETSI; check `doc3gpp listserv index` and use
`--rebuild`, `--rebuild-embeddings`, or `--rebuild-all` as appropriate. Vector
support is optional; FTS/filter-only paths are independent of it.

### Background jobs and shutdown

HTTP, UI, and both MCP transports enqueue LISTSERV sync, parse, and index jobs
through the existing single-process worker. Job kinds are `listserv_sync`,
`listserv_parse`, and `listserv_index`; statuses follow `queued`, `running`,
`succeeded`, `failed`, or `cancelled`. Sync/parse summaries report `processed`,
`skipped`, source `failed`/`failures`, derived `index_failed`/`index_failures`,
and `message`. A job with source or index failures is `failed`; an index failure
does not undo source writes. Cancellation is cooperative: in-flight requests and
committed rows may finish and are not rolled back. Blocking handler work uses
the shared `_to_thread` path. Worker-built LISTSERV services close their owned
source/index wrappers on that worker thread, not the app embedder they borrow.
MCP stdio drains in-flight LISTSERV work and its worker executor, then closes
app LISTSERV facades before the shared embedder; see
[shutdown constraints](known-constraints.md#pools-jobs-and-deployment).
