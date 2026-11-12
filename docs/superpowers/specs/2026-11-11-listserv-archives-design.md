# ETSI LISTSERV archives

## Purpose and scope

Add public ETSI LISTSERV archive ingestion and search to doc3gpp. Users can sync
message listings, parse message details, list known mailing lists, search
messages, show one stored message, and rebuild search indexes. Expose all six
operations through the SDK, CLI, HTTP API, web UI, and both MCP transports.
Long-running HTTP, UI, and MCP operations use the existing background job system.

The user supplied examples of 3GPP TSG/WG mailing-list IDs. They are examples,
not an allowlist: any valid public LISTSERV ID can be supplied. The `list`
operation returns distinct list IDs represented by stored message rows; it does
not return a static catalogue or enumerate all upstream lists. Authenticated or
private archives, downloading attachments, and recovering the complete original
body after truncation are out of scope.

## Architecture and data

Use the main database and existing `Base`/engine, with dedicated LISTSERV
tables and FTS/vector sidecars. Do not mix email data with TDoc or spec-document
tables or introduce a fourth database. Add slotted domain models, repository
protocols and SQL implementations, pure HTML/content parsers, network-only
scraping, orchestrating services, and factory composition in the existing layers.
Adapters share service calls and output renderers. Extend schema bootstrap and
the schema reporting registry in step with ORM columns.

The authoritative records are:

1. **Message listing**, keyed by canonical absolute detail URL: list ID,
   subject, displayed sender, message datetime with available timezone information,
   `yyyy-mm-wk` archive label, and detail URL. Preserve masked senders as
   displayed, rather than inventing an address. Normalize list IDs consistently
   for case-insensitive identity without restricting their vocabulary.
2. **Message detail**, linked by the same URL with a foreign key: ordered
   attachments (`name`, absolute `url`), unique TDoc IDs, compressed Markdown
   content, `content_truncated`, and parse/version metadata. Distinguish an
   unparsed detail (`null`) from a successfully parsed message with empty
   content or no attachments (`""` / `[]`).
3. **Archive coverage**, keyed by list and actual archive-page URL, with
   successful date coverage and status. An archive label is not unique: ETSI
   can split a single displayed week across several page URLs. Coverage cannot
   be inferred merely from the presence of some message rows.
4. **Derived text and vector indexes**, including freshness information needed
   for incremental updates and rebuilds. They are not the source of truth.

Add a validated `[listserv] max_content_chars` setting, default 10,000 Unicode
characters. The setting affects subsequent parses; it does not
silently rewrite existing rows. An explicit forced reparse applies the current
limit. The truncated tail is not stored; the upstream detail URL is the way to
read the full original message.

## Sync

`doc3gpp listserv sync LIST_ID` requires one exact list ID and fetches
`https://list.etsi.org/scripts/wa.exe?A0={list_id}`. Discover archive page URLs
from the index, then parse every subject/from/date/detail-link row from selected
pages. Resolve relative links safely, preserve Unicode, parse and store the full
displayed message datetime and any available timezone offset, and upsert
messages by canonical detail URL without deleting previously stored messages
outside the requested filter.

Preserve the source's local datetime and offset in the returned ISO-8601 value;
do not discard hours, minutes, seconds, or microsecond precision that upstream supplies.
When a timezone is absent, keep the datetime naive rather than inventing a source
offset. If a listing supplies only a date, record day precision and use midnight
as a placeholder, not a claim that the message was sent at that time. Aware
datetimes sort by UTC instant; use the naive wall-clock value as a UTC-like sort
key when no timezone is known, with URL as the deterministic final tie-breaker.
Ordering involving an unknown timezone or day-only value is consequently only a
best-effort approximation, not a guaranteed chronology.

`--date` accepts the project's validated date comparisons, including `=`,
`!=`, `<`, `<=`, `>`, and `>=`; repeat it to intersect comparisons for a range.
With no filter, select messages dated within the rolling preceding calendar year
through the present day, inclusive. Apply comparisons to the displayed calendar date,
not to a UTC-converted date. Sync stores only matching rows. The same date
grammar and semantics apply across surfaces. Invalid expressions fail before
network or database work.

For each historical page, persist only coverage for the date predicate actually
processed, and skip it later only when successful stored coverage encompasses
the requested predicate. Track disjoint predicates correctly, including `!=`;
an empty but successfully parsed page is distinguishable from failure. A
partially filtered week must not be declared wholly complete. Revisit all pages
of the most recent upstream year-month-week on every sync, as that period can
still change. `--force` revisits selected pages irrespective of coverage. Each
page's matching message upserts and coverage update commit atomically; failures
leave that page retryable. If a listing cannot be recognized, do not interpret
it as an empty successful page.

Use archive labels to optimize candidate page discovery only when doing so
cannot discard matching messages: message header dates and archive period labels
are separate facts. Where the source provides no reliable bound, prefer a
conservative scan to silently missing matches. This may make a first filtered
sync expensive. Resume/coverage prevents re-fetching already-covered historical
pages. Month-only historical archives are valid; derive each message's displayed
`yyyy-mm-wk` using its date and LISTSERV's day-of-month week convention, while
coverage remains keyed by the actual month-page URL. Handle split weeks,
pagination, and empty archives without assuming one page per week.

## Parse

`doc3gpp listserv parse LIST_ID` requires one exact list ID on every surface.
Its optional repeatable date filter selects among already-synced messages;
without a date filter it considers all stored messages for that list. Skip
already parsed items unless `--force` is supplied. Sync and parse are separate;
neither a read nor a parse implicitly syncs.

For each selected item, fetch the ETSI detail page. LISTSERV may embed the
message body in an iframe pointing at its `text/html` or `text/plain` part; it
is necessary to fetch that body part, but never download actual attachments.
Treat the listed body alternatives and `multipart/related` containers as body
representations, not attachment entries. Preserve genuine named attachments
with their resolved URLs, without fetching their bytes.

Extract the current message only, excluding recognizable trailing quoted
previous messages using structural HTML quote boundaries and conservative
plain-text reply/forward delimiters. Preserve the current message when the
boundary is uncertain rather than cutting at arbitrary prose. Email quoting is
not standardized, so this is a documented best-effort limitation, not a claim
that every format can be perfectly separated. Convert the resulting body to
Markdown with unsafe embedded HTML excluded.

Before truncating, extract unique, case-insensitive 3GPP TDoc IDs from the
current-message Markdown, subject, and attachment names and URLs. Use the
project's existing TDoc identifier pattern as the baseline, covering six- and
seven-digit IDs such as `R1-2610793`, `RP-2601234`, and `R5s260009`. Never
extract from excluded quoted prior-message content. Store the first configured
number of Markdown characters, gzip-compressed, and set `content_truncated`
only when characters were actually omitted. Persist content, attachments,
TDocs, and parse status as an atomic replacement per message. A failed parse
does not mark that message complete or overwrite its previous good detail.

## Search, list, and show

Search supports optional `--text` (FTS5 text or MATCH expression), `--semantic`
(embedding query), both together (hybrid), or neither (filter-only). Apply the
same SQL LIKE filters to list ID, sender, subject, and TDocs, and the same date
comparisons, in all four modes. Follow the existing shared filter grammar and
bind all SQL parameters. Date filters apply to message calendar dates. Search
metadata-only messages even before detail parsing; detail fields are null until
known. TDocs found in a synced subject are available even before parse.
Filter-only results sort by message datetime newest first, so messages from a
thread retain their relative order; ranked results sort by relevance first and
datetime newest first when scores tie. Use the same timestamp tie-break across
search modes and a deterministic URL tie-break after equal timestamps.

Index list ID, subject, sender, date, week, TDoc IDs, attachment names, and
stored content. Exclude message and attachment URLs and URL strings in message
content from FTS and embedding input, while preserving them in returned metadata
and stored Markdown. An extracted TDoc ID remains searchable even if discovered
inside an attachment URL. The bounded stored body is the content
available for indexing; text beyond the 10,000-character default cannot match
unless represented in extracted TDoc IDs. Reuse existing FTS5/vector search
patterns and reciprocal-rank fusion for hybrid search. Chunk longer content if
needed and deduplicate results by email, retaining matched-source provenance.
Do not silently treat an unavailable semantic index as a successful empty
search. Filter-only search uses source SQL without requiring optional indexes.

Each search result contains the message metadata, attachment metadata and
TDocs when known, detail URL, mode-appropriate score and match snippet when a
text or semantic match provides one, and up to 200 characters of stored content.
Return `content_truncated` for the upstream-to-storage cut and a separate
preview-truncation flag for the storage-to-search-preview cut; represent both
as unknown for unparsed content. Snippets are absent when no match is available,
including filter-only results. Support `--limit` and `--offset` with stable
ordering and deterministic tie-breaks in all modes; equivalent controls exist
on all other surfaces.

`listserv list` returns distinct stored list IDs. `listserv show URL` looks up
the exact stored detail URL and returns all message metadata and all *stored*
content, with the storage truncation flag and upstream URL. If the message is
synced but unparsed, return its metadata and explicit unknown detail fields; if
the URL is absent, return the project's normal not-found error. Search, list,
show, and index rebuild never fetch or parse upstream content implicitly.

## Indexing, failures, and operations

`listserv index` provides text/vector status and rebuild operations consistent
with the project's existing index commands. Rebuild uses stored SQL data only;
vector operation uses the existing optional embedding configuration and
dependencies. FTS indexes cover synced metadata and are updated after sync and
parse when automatic indexing is enabled. Vector updates similarly follow the
configured embedding policy. A source change invalidates both derived forms;
failed index updates leave source data intact and index state visibly stale so
rebuild can repair it. Semantic and hybrid requests fail with actionable errors
when embeddings or vector support are unavailable. FTS-only and filter-only
paths remain independent of vector availability.

Use existing retry and bounded concurrency conventions. Reject private or
authentication-required archives with an actionable error. Restrict fetched
archive, detail, embedded-body, and redirect destinations to the intended public
ETSI host and HTTPS; do not follow arbitrary stored or user-supplied URLs as
network targets. Batch failures are reported per archive page/message, while
successful work remains committed and retryable; results and job status must
make partial failure apparent. No raw email or attachment content is written to
logs. Web jobs use the shared worker's blocking-call helper, cooperative
cancellation, and resource lifecycle, including stdio drain safeguards.

## Surface and documentation contracts

CLI, SDK, HTTP JSON, UI, and MCP expose sync, parse, list, search, show, and
index capabilities with equivalent list/date/filter and truncation semantics.
The HTTP and MCP read surfaces share response payloads; mutation operations
enqueue existing-style jobs. The UI provides forms for operations, search
results, detail display, and links to jobs and upstream messages, escaping
upstream content and using safe Markdown rendering. MCP stdio and HTTP share
the same registered tools and service state.

Update maintained CLI, configuration, process, architecture, code-map, web,
MCP, domain/constraint documentation and `AGENTS.md` as appropriate. Document
public-only access, the partial-body limit and quote-boundary limitation,
coverage/force behavior, filter grammar, optional semantic dependencies, and
index freshness/recovery. Validate schema/render parity, help, settings template,
and links. No unrelated refactoring or automatic commits.

## Verification

Use SDD for the approved implementation plan and TDD for implementation. Normal
tests use offline, sanitized fixtures; live-network tests are explicit only.
Cover at least:

- Weekly, split-week, month-only, empty, malformed, paginated, mixed-timezone,
  and masked-sender archive fixtures; absolute and relative URLs. Preserve full
  datetimes and test same-day message ordering, offsets, absent timezones and
  day-only fallback while retaining displayed-date filter semantics.
- Default and explicit date predicates, disjoint/partial coverage, latest-week
  refresh, force, changed ranges, cancellation, and page transaction failures.
- Embedded HTML/plain bodies, charset handling, body-versus-attachment MIME
  parts, genuine named attachments, reply/forward boundaries, uncertain quotes,
  TDoc deduplication including IDs beyond the storage limit, Unicode length,
  gzip round trips, and failed/forced reparse preservation.
- Metadata-only, parsed, filter-only, FTS5, semantic and hybrid searches;
  URL exclusion from search input, all LIKE/date filters, snippets, both
  truncation flags, deterministic pagination, unavailable backends, index
  freshness, rebuild and recovery.
- SDK/CLI/HTTP/UI/MCP parity, mandatory exact list ID on sync/parse, job
  lifecycle and cancellation, HTML safety, and no implicit network reads.
- Ruff, focused tests, the relevant SQLite integration suite, help/template
  checks, and documentation links; report skips and any live-network limitations.
