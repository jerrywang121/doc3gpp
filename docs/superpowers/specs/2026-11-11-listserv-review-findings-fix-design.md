# LISTSERV review findings remediation

## Status

- Design review: approved by the user, including this written spec.
- Implementation plan: written at `docs/superpowers/plans/2026-11-11-listserv-review-findings-fix.md` and awaiting approval.
- Implementation: **not started**. Do not edit product code until the user has
  reviewed this written spec and then approved a separate implementation plan.

## Purpose

Resolve the two Important and three Minor findings from the independent
whole-branch review of ETSI LISTSERV archives. Preserve the existing public
contracts and the approved LISTSERV design while correcting incomplete deep
semantic/hybrid pages, nondeterministic embedder cleanup, resource-mislabelled
FTS corruption responses, inaccessible rich list-ID filtering in the UI, and
duplicate-page/date-filter interaction.

## User intent and constraints

- The user authorized fixes for all five findings by replying “ok” to the
  proposal to address the two Important and three Minor items.
- Preserve the existing dirty `feature/listserv-archives` checkout, all
  unrelated user work, and user-owned `node_modules/`, `package.json`, and
  `package-lock.json`. Do not stage, commit, reset, change branches, or create a
  worktree.
- No private/authenticated archives, attachment-byte downloads, implicit
  read-time sync/parse, fourth database, or new product dependency.
- Preserve HTTP, MCP, CLI, SDK and UI payloads, result scores/provenance,
  filtering semantics, source-revision freshness, cancellation, and the
  existing job-worker shutdown order.
- No public semantic/hybrid offset ceiling. The previous approved compatibility
  choice keeps hybrid FTS paging internal and permits arbitrary nonnegative
  offsets.
- Existing source URL and encoding restrictions remain unchanged: public ETSI
  host only, identity/gzip/deflate only, decoded response limit 16 MiB, and no
  attachment downloads.
- No schema changes and no additional optional dependency.

## Approved design

### 1. Complete semantic and hybrid vector candidate windows

`sqlite-vec` limits one vec0 KNN request to `k <= 4096`. Keep that request as the
fast path for candidate windows it can satisfy. When a requested semantic or
hybrid candidate window exceeds that limit—or chunk crowding means the KNN
window reaches 4096 rows before enough distinct eligible messages are found—
use an exact scalar-distance fallback based on
`vec_distance_cosine(embedding, query_vector)`.

The fallback must:

1. Use the same SQLite vector read snapshot for the scored vectors and the
   matched chunk text/revision that is attached to each candidate.
2. Apply the same list-ID, sender, subject, TDoc and displayed-local-date filters
   before counting distinct candidate messages. It must not satisfy a deep page
   with an incomplete unfiltered prefix.
3. Rank each message by its best (minimum cosine-distance) eligible chunk, with
   deterministic chunk-ID and message tie-breaks matching current behavior.
4. Return enough distinct vector candidates for the current semantic-only or
   hybrid candidate target. Continue scanning until that target is satisfied
   or the eligible vector set is exhausted.
5. Reuse the existing semantic distance ordering and hybrid RRF merge. Do not
   change mode-specific scores, FTS provenance, snippet selection, result
   ordering, or output shape.

The fast KNN path remains for windows it can satisfy. Scalar scanning is a
deliberate correctness fallback for deep/crowded windows; its CPU and temporary
sort cost can grow with the vector corpus and requested offset. Do not replace
that cost with silent truncation or a new public offset cap. The exact SQL
implementation remains an implementation-plan choice, but must meet all five
invariants above. The in-memory sqlite-vec probe confirmed that
`vec_distance_cosine` can score a `vec0` embedding column; the installed
SQLite-vec documentation confirms the 4096 KNN `k` cap and the supported scalar
distance function.

### 2. Make embedder ownership explicit and deterministic

LISTSERV semantic services must use the web app’s existing shared lazy embedder
provider rather than creating independent HTTP clients for search, indexing,
and each sync/parse job. The app-owned embedder is closed exactly once, after
the job worker drains during lifespan shutdown.

Service ownership rules:

- A semantic service built around an externally supplied/shared embedder
  borrows it and must not close it.
- A builder that creates its own embedder owns it; its service close path closes
  it exactly once.
- `ListservSearchService`, `ListservIndexService`, and sync/parse services
  expose or cascade an idempotent close operation for owned resources. Closing
  a per-job service closes its owned source and owned index/semantic services,
  but not a shared embedder.
- If semantic construction fails after creating an owned embedder, close that
  embedder before returning unavailable or propagating the failure.
- Wire the shared provider into LISTSERV HTTP search/index and worker-built
  sync/parse services. Preserve lazy initialization; web app construction must
  not probe embedding dimensions or make an embedding request.
- CLI/SDK builders that create their own resources retain an explicit cleanup
  path. Closing an external/shared provider from those services is prohibited.

Tests must prove cleanup for successful and failing construction, per-job
success/failure, and web/MCP lifespan shutdown, including that the shared
embedder is closed once and only after jobs have drained.

### 3. Give LISTSERV FTS corruption the correct resource and repair hint

When `/api/listserv/search` encounters `SearchIndexCorruptError`, map it to the
existing HTTP-facing corruption error with `resource="listserv"`. The common
error mapper must return the existing corruption slug/status and recommend
`doc3gpp listserv index --rebuild`, not the spec-doc command.

The MCP `listserv_search` error metadata must likewise identify the resource as
`listserv`; preserve its current error code/slug and other fields. Do not change
error mapping for TDoc, spec-doc, or generic search corruption.

### 4. Expose rich list-ID filters in the web UI

Replace the search form’s known-ID-only `<select>` with a text input backed by
the existing stored-list-ID suggestions. Empty input continues to mean “all
lists.” The input must allow the same existing list-ID rich-filter values as
the API/MCP/CLI, including LIKE wildcards, a leading `!` negation, `null`, and
`not-null`. Do not add a new filter grammar or change repository semantics.

### 5. Resolve page duplicates before date filtering

Within each parsed archive listing page, collapse messages by canonical URL so
the final occurrence is authoritative **before** applying the requested date
filter. Then persist/index only final values whose displayed-local date matches
the requested range. This means an earlier duplicate that matches cannot
override a final occurrence outside the selected dates. Keep page coverage,
pagination URLs, and the atomic repository transaction unchanged. Existing
repository-level final-occurrence handling remains as defense in depth.

## Acceptance criteria

1. A semantic-only query beyond the current KNN cap returns the expected page
   when enough eligible messages exist; a hybrid query at the same deep offset
   includes vector ranks beyond the cap and matches an independent exact-rank
   oracle. Cover multi-chunk messages, filters, stable ties, snippet/provenance,
   and exhaustion at the corpus end.
2. Vector chunks and their matched text are read from the same snapshot on both
   fast and scalar-fallback paths. KNN requests never exceed 4096.
3. Web-created LISTSERV services and worker jobs share the app embedder; service
   close is idempotent, borrowed embedders stay open until app shutdown, and
   owned embedders close on both successful and failing paths. Worker drain
   precedes shared embedder close.
4. HTTP LISTSERV corruption JSON includes the LISTSERV resource and repair hint;
   MCP error data identifies the LISTSERV resource. Existing other-resource
   hints and error codes remain unchanged.
5. The rendered LISTSERV search form accepts arbitrary rich list-ID filters,
   preserves a blank as all lists, and still presents known IDs as suggestions.
   A UI/HTTP integration test proves a pattern can select matching records.
6. Sync duplicate tests cover a final occurrence outside the date filter and a
   final occurrence inside it. In both cases the final row controls persistence;
   source revision, page coverage, next URLs and derived-index updates remain
   correct.
7. Ruff and the complete offline SQLite suite pass. Optional semantic coverage
   must be reported accurately. Existing JavaScript form smoke tests remain
   passing.

## Risks and non-goals

- Exact scalar vector ranking can be expensive for deep offsets and large
  corpora; preserve correctness while keeping the existing KNN fast path for
  ordinary windows. No new public limit is approved.
- Resource ownership across lazy factories is subtle. Do not close borrowed
  resources, close shared resources twice, or leave partially constructed
  owned clients alive after failures.
- The UI change only exposes existing server-side rich-filter semantics; it
  must not change API/CLI/MCP grammar.
- The date fix applies the already-approved final-occurrence rule to the
  filtered sync path. It does not change archive-page coverage or cause reads
  to fetch/parse upstream data.
- This design does not add schema migrations, dependencies, new databases,
  attachment downloads, a full archive crawl, CLI `--output`/`--compact` flags,
  or a total-process-RSS guarantee.
