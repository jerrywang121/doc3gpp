# Presentation search provenance and matching chunks

## Goal and scope

Make search results identify the source ZIP member and show the matching
presentation chunk for specification documents and TDocs, consistently in the
CLI, web UI, REST API, and MCP. This applies to `.pptx` and `.ppt` documents
that have already been successfully converted to Markdown. Specification search
must also expose the source of existing Word-document chunks.

Keep TDoc results grouped by TDoc: return at most one best matching chunk per
matching presentation, nested under its source document. Preserve the existing
TDoc semantic indexing cap, including its default of 16 content chunks shared
across a TDoc's documents. There is no new presentation-specific semantic cap.

## Existing behavior

- Specification parsing already splits presentation Markdown into stored,
  searchable chunks. Results contain chunk text but omit the stored
  `source_file` from the public result. Word and presentation chunks share the
  same search paths.
- TDoc FTS5 indexes a whole converted content document per row and groups
  results by TDoc, with matching documents identified by URL and ZIP member
  path. Semantic indexing already splits stored document Markdown into chunks,
  but vector results do not expose their text. CR main-document content is not
  SQL-backed and is not part of this search corpus.
- Presentations are extracted as text, including slide text, tables, and notes;
  images, charts, and OCR are outside the extraction contract. A presentation
  does not add specification TOC, section, or table metadata.

## Result contracts

### Specification documents

Add `source_file` to each `SpecDocSearchResult`, in all four modes: FTS5,
semantic, hybrid, and filter-only. Its value is the exact stored ZIP member path
for the returned chunk, with its case and directory components preserved. The
existing `chunk_id`, `chunk_index`, and `text` remain unchanged. Preserve current
chunk ranking, pagination, filters, and `sections`/`tables` semantics.

Populate the field from the stored chunk in every read path, including FTS5
SQL, vector metadata hydration, hybrid results, and filter-only queries. Existing
stored spec chunks already provide this information; adding the field must not
require a reparse, index rebuild, or network access.

### TDocs

Keep the existing parent fields, ranking, pagination, and `matched_documents`
array. Each matching presentation document keeps its existing `document_id`,
`ftp_url`, `source_file`, `score`, and `previews`, and additionally exposes:

```json
"matched_chunk": {
  "chunk_id": "<stable-chunk-id>",
  "chunk_index": 0,
  "text": "<full matching chunk text>"
}
```

`matched_chunk` is null when no trustworthy matching chunk is available. The
source path is the exact ZIP member path, not the outer archive URL or a
basename. A result matching only TDoc metadata has no invented document or
source; filter-only results retain an empty `matched_documents` array. Existing
non-presentation document matches remain available; their new `matched_chunk`
may be null, so this feature does not require changing Word FTS behavior.

For text search, select the best FTS5-ranked matching chunk per presentation,
with deterministic tie-breaking by chunk position and ID. Keep the existing
whole-document FTS match and parent ranking, including query expressions whose
terms occur in different chunks. If the full document matches but no single
chunk satisfies the complete expression, `matched_chunk` is null; never label a
partial match as satisfying the full query. Text chunk indexing covers the
entire presentation, independently of the semantic cap.

For semantic search, return the actual best vector-matched chunk per
presentation among the vector candidates returned by the existing search, with
its indexed text and identity. The existing shared content-chunk limit and
vector fan-out continue to apply: semantic search may not cover later chunks or
later documents, and it does not promise every matching document is returned.

For hybrid search, retain the existing TDoc-level reciprocal-rank fusion and
union the document matches from both search modes. When both sides match the
same presentation, prefer its best vector chunk; otherwise use the matching
side's chunk. Preserve FTS previews when available. Rank and deduplicate
document entries deterministically without conflating different ZIP URLs that
contain the same member path. If an index entry is stale, incomplete, or cannot
be tied to the exact indexed chunk, return a null chunk rather than unrelated
text.

## Indexing and lifecycle

Keep the existing whole-document TDoc FTS index intact so existing query
semantics, TDoc-level scores, and result counts do not change. Add an FTS5
chunk sidecar for SQL-backed `.ppt` and `.pptx` content, keyed by stable TDoc,
document, source, content-hash, and chunk identity. Use the existing TDoc
content chunking size and overlap; generate all presentation chunks for FTS,
not just the chunks eligible for embeddings. Chunks must have stable positions,
respect the configured whitespace-token limit, and retain enough extracted
text to find content throughout the source. A single unusually long token can
still produce a large chunk. Do not treat slide headings as specification TOC
entries.

Integrate the sidecar with the existing TDoc index repository and transaction
boundaries: indexing, parse-triggered updates, batch rebuilds, removal, and
replacement must keep document and chunk index state coherent. Freshness and
stale-only rebuilds must notice missing or obsolete chunks, changed source
content, and changes to chunking configuration or algorithm version. Existing
logical-TDoc index counts remain logical TDoc counts. Do not change the main
FTS table's layout or destroy an existing index during the additive upgrade.

The vector index must retain or otherwise safely resolve the text actually
embedded for a returned presentation chunk. It must verify the content identity
and indexing-time chunking configuration; reconstructing a chunk using only
current settings can silently return the wrong text after settings change. Treat
older vector entries without sufficient provenance as stale and return null
chunk text until a vector rebuild establishes trustworthy provenance. Update
vector provenance atomically with the corresponding vector entries and clean
it on replacement/removal. Existing vector candidate ranking and the configured
`max_content_chunks_per_tdoc` behavior are unchanged.

Create any new index sidecars additively through the existing main-database
schema bootstrap, gated consistently with the relevant optional search
capabilities. Existing installations can run `doc3gpp db init --scope main`,
then `doc3gpp tdoc index --rebuild` for text chunks and, when semantic search is
configured, `doc3gpp tdoc index --rebuild-embeddings` for vector provenance.
Rebuild from the SQL-stored converted content; it must not download ZIPs,
reparse Office documents, require the Markdown cache, or require LibreOffice.
Semantic rebuilding still requires the configured embedding service. Index
status and existing stale hints must make a required rebuild visible. Missing
optional search capabilities retain their established unavailable behavior.

## Surfaces and documentation

Use the shared result models and serializers so CLI JSON, REST JSON, and MCP
JSON agree on field names, values, nulls, and ordering. The CLI's table,
Markdown, and compact Markdown output and the web search pages must visibly
show source paths and available chunk text without requiring a JSON response.
Render paths and chunk text safely as inert text in HTML. Preserve existing
result limits and avoid loading unrelated documents while decorating hits.

Update `src/doc3gpp/data/skills/doc3gpp/SKILL.md`, `README.md`, `AGENTS.md`,
and the relevant CLI and web documentation. Explain source paths, grouped TDoc
chunks, the distinction between text and capped semantic coverage, null chunks,
the additive upgrade/rebuild steps, and the lack of image/OCR extraction. The
MCP `get_skill` tool continues to return that packaged skill verbatim.

## Verification

Use TDD and subagent-driven development after this spec and its implementation
plan are approved. Tests must cover:

- `.pptx` and `.ppt` presentation paths, nested paths and case preservation,
  multiple presentations per TDoc, and multiple TDocs. Include content beyond
  the first chunk and beyond the semantic content cap; text search must find it.
- Specification source identity and matching chunk text in FTS5, semantic,
  hybrid, and filter-only modes, for both presentations and Word chunks.
- TDoc FTS, semantic, and hybrid best-chunk selection; union of source matches;
  stable ties; metadata-only and filter-only results; whole-document matches
  whose terms cannot all match one chunk; and no fabricated source/chunk.
- Changed content and chunking settings, stale and legacy vector entries,
  missing sidecars, safe null behavior, index freshness, stale-only and full
  rebuilds, atomic replacement/removal, and no ZIP-cache or source-download
  dependency; semantic rebuilds may contact the configured embedding service.
- CLI JSON and human-readable formats, REST, MCP, and safely escaped web HTML;
  shared JSON parity, exact source paths and full chunk text, and unchanged
  parent ranking and pagination.
- Existing index, extraction, search, optional-dependency, and content
  regression suites, followed by lint and the project's full offline SQLite
  test suite.
