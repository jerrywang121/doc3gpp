# Unified TDoc and Spec-Document Search Design

## Status

Approved design. This specification supersedes the public-interface portions
of [`2026-09-24-tdoc-search-namespace-design.md`](2026-09-24-tdoc-search-namespace-design.md)
and the current split search command contracts. Historical design documents are
not rewritten.

The change is a hard public API migration. The old split commands, routes, and
paired MCP tools are removed without aliases, redirects, or deprecation shims.

## Goal

Provide one resource-scoped search and index API for TDocs and spec-document
chunks across the CLI, web/REST, and MCP surfaces:

- Move TDoc index maintenance to `tdoc index`.
- Add `spec doc index` with FTS5 and embedding maintenance equivalent to TDoc
  index maintenance.
- Replace separate text and semantic search commands/routes/tools with one
  search operation whose mode is selected by optional text and semantic inputs.
- Return one flattened result shape per resource for every search mode.
- Keep existing field filters, pagination, output formats, snippets, and
  explain/quiet behavior where those options already exist.
- Read the configured `semantic_search.fts5_weight` value for hybrid ranking;
  it is not a request-level argument on any public surface.

## Non-Goals

- No FTS5 or sqlite-vec table format migration beyond metadata needed for the
  new spec-document vector rebuild/status loop.
- No change to BM25 weighting, snippet extraction, embedding model, chunking,
  or RRF mathematics.
- No compatibility aliases, redirects, or deprecation period for removed
  public names.
- No change to unrelated list, parse, sync, or show commands.
- No requirement that field-only search use FTS5 or embeddings. It must remain
  a direct SQL read path.

## Public Search Contract

### Mode selection

Both resources accept two optional text inputs:

| `text` | `semantic` | Mode | Ranking |
|---|---|---|---|
| non-empty | empty | `fts5` | FTS5 BM25 order |
| empty | non-empty | `semantic` | vector distance order |
| non-empty | non-empty | `hybrid` | configured-weight RRF order |
| empty | empty | `filter` | deterministic SQL/source order |

Whitespace-only values are treated as empty. The mode is selected before the
  search service is called, so a filter-only request never initializes or calls
  FTS5 or the embedding stack.

The `semantic_search.fts5_weight` setting is used only for `hybrid` mode.
There is no public `fts5_weight`, `fts5_query`, or `sem_query` request
argument. A hybrid request's `text` input is the FTS5 query and its `semantic`
input is the embedding query.

### Unified TDoc result

Every mode returns a JSON object with these fields and no nested `hit` object:

```json
{
  "tdoc_id": "R1-25-0001",
  "score": -8.42,
  "search_mode": "fts5",
  "previews": {
    "title": "<<handover>> procedure"
  },
  "title": "Handover procedure",
  "meeting": "RAN1#120",
  "tsg": "RAN",
  "uploaded_date": "2025-01-10",
  "ftp_url": "https://www.3gpp.org/ftp/...",
  "wis": "38.331",
  "type": "CR",
  "status": "Agreed",
  "best_chunk_id": null
}
```

Field semantics:

- `score` is the raw FTS5 BM25 score in `fts5` mode, minimum vector distance
  in `semantic` mode, RRF score in `hybrid` mode, and `null` in `filter`
  mode.
- `search_mode` is exactly one of `fts5`, `semantic`, `hybrid`, or `filter`.
- `previews` contains the existing per-column FTS5 snippets only in `fts5`
  mode. It is JSON `null` in `semantic`, `hybrid`, and `filter` modes.
- `best_chunk_id` identifies the closest vector chunk in `semantic` and
  `hybrid` modes. It is `null` in `fts5` and `filter` modes.
- `rank_fts5`, `rank_vec`, `rrf_score`, and `min_chunk_distance` are not
  public fields. Array order is the final rank for the selected mode.
- TDoc `type` and `status` are included in every mode, including vector-only
  results and field-only results.

Score direction is intentionally mode-specific and is made explicit by
`search_mode`: lower is better for FTS5 and vector distance; higher is better
for hybrid RRF; filter order is not a score ranking.

### Unified spec-document result

Every mode returns the same flattened object shape:

```json
{
  "chunk_id": "38.331@18.5.0#0",
  "score": -7.1,
  "search_mode": "fts5",
  "previews": {
    "text": "handover <<procedure>>"
  },
  "spec_id": "38.331",
  "version": "18.5.0",
  "release": "Rel-18",
  "sections": "5.1 Handover",
  "tables": "Table 1 Values",
  "chunk_index": 0,
  "text": "..."
}
```

Field semantics match the TDoc contract: `score` is BM25, minimum vector
distance, RRF, or `null` for `fts5`, `semantic`, `hybrid`, or `filter`
respectively; `previews` is an object only for FTS5 and JSON `null` otherwise;
array order is the final rank. All metadata fields are populated for vector-only
chunks, so a semantic result never exposes the old `hit: null` shape.

The CLI JSON renderer, HTTP `?format=json` response, and MCP tool response use
the same field names, values, ordering, and compact JSON conventions already
used for cross-surface parity. Table and Markdown renderers use the same
unified fields, with `search_mode` and `score` visible in their result rows.

## CLI Contract

### TDoc index

```text
doc3gpp tdoc index [options]
```

With no rebuild action, the command prints the current FTS5/vector index
status. Existing TDoc index maintenance options move unchanged from
`tdoc search index`:

- `--rebuild` rebuilds the FTS5 index.
- `--rebuild-embeddings` rebuilds the TDoc vector index.
- `--rebuild-all` runs both rebuilds in sequence.
- `--batch`, `--resume`, `--stale-only`, and `--quiet` retain their current
  meanings.

The action flags remain mutually validated as they are today. Status output
reports both available subsystems when the corresponding service is enabled.

### TDoc search

```text
doc3gpp tdoc search [--text TEXT] [--semantic TEXT] [filters] [options]
```

Existing TDoc search filters remain available: `--tsg`, `--meeting`,
`--meeting-id`, `--tdoc-id`, `--release`, `--spec`, `--since`, and `--until`.
Existing output controls remain available: `--limit`, `--format`, `--compact`,
`--snippet-tokens`, `--explain`, and `--quiet` where applicable.

The command dispatches to the four modes above. `--text` and `--semantic` are
optional; omitting both is valid and performs field-only SQL filtering. The
old `tdoc search query`, `tdoc search sem`, and `tdoc search index` commands
are not registered. `--fts5-weight`, `--fts5-query`, and `--sem-query` are not
accepted.

### Spec-document index

```text
doc3gpp spec doc index [options]
```

The command mirrors TDoc index maintenance:

- no action flags prints FTS5/vector status;
- `--rebuild` rebuilds the spec-document FTS5 projection;
- `--rebuild-embeddings` rebuilds embeddings for parsed `(spec_id, version)`
  pairs;
- `--rebuild-all` runs both;
- `--batch`, `--resume`, `--stale-only`, and `--quiet` retain the TDoc index
  meanings.

Spec-document embedding rebuilds enumerate parsed source versions from the
specdata database, embed every stored chunk for each pair, and persist a
resume cursor plus parsed-at freshness watermark in specdata vector metadata.
Pairs with no parsed chunks remove stale vector rows. A failed pair is logged
and does not abort the remaining rebuild.

### Spec-document search

```text
doc3gpp spec doc search [--text TEXT] [--semantic TEXT] [filters] [options]
```

Existing spec-document filters remain available: `--spec`, `--release`,
`--version`, `--sections`, and `--tables`, plus `--limit`, `--offset`,
`--fields`, `--format`, `--output`, and `--compact`.

The command uses the same four-mode dispatch and unified result shape as TDoc
search. The old `spec doc search query`, `spec doc search sem`, and any
request-level blend/query flags are removed.

## HTTP/Web Contract

### Search routes

The public read routes are:

```text
GET /tdocs/search
GET /spec-docs/search
```

Both accept `text` and `semantic` query parameters, with empty values treated
as absent. Existing resource filters, pagination, `format=json`, full-page
HTML, and HTMX fragment behavior remain available. The `/tdocs/search/sem` and
`/spec-docs/search/sem` routes are removed and do not redirect.

JSON responses remain bare arrays of unified result objects. HTTP JSON and MCP
JSON must be byte-equivalent after their existing compact serialization rules.
The route adapters call resource-specific unified search facades; they do not
duplicate mode selection or ranking logic.

The web forms become one form per resource with text and semantic inputs on
the same page. The FTS5 weight control and links to the removed `/sem` pages
are deleted. Result tables/cards display `search_mode`, `score`, and the
resource metadata; FTS5 previews are rendered only when non-null.

### Index status and jobs

Index maintenance is asynchronous on the web surface:

```text
GET  /tdocs/index
POST /jobs/tdocs/index
GET  /spec-docs/index
POST /jobs/spec-docs/index
```

The GET routes return current FTS5/vector status. The POST bodies contain the
selected action and shared options (`rebuild`, `rebuild_embeddings`,
`rebuild_all`, `batch`, `resume`, and `stale_only`) but never a blend weight.
They enqueue the corresponding resource-specific job and return the standard
job envelope with status and event links. Existing job polling and SSE
behavior is reused.

The sync hub's existing TDoc FTS5 rebuild control is replaced by the resource
index control; the spec-document index control is added using the same job
polling pattern.

## MCP Contract

### Search tools

The paired mode-specific tools are replaced by one tool per resource:

```text
search_tdoc
search_spec_docs
```

Each tool accepts optional `text` and `semantic` inputs, all existing resource
filters, and existing pagination/output-relevant options. It does not accept
`fts5_weight`, `fts5_query`, or `sem_query`. It returns a compact JSON string
containing the same bare array as the equivalent HTTP `?format=json` route.

The old `semantic_search_tdoc` and `semantic_search_spec_docs` tools are not
registered. Existing error mapping remains at the MCP boundary: malformed
FTS5 input is a client query error, disabled search/embedding infrastructure
is a service-unavailable error, and repository corruption retains its rebuild
hint.

### Index tools

The index/status tools are:

```text
get_tdoc_index
index_tdocs
get_spec_doc_index
index_spec_docs
```

The `get_*_index` tools return the same status payload as the matching HTTP
GET route. The `index_*` tools enqueue a background job and return the standard
job envelope. They accept rebuild action/options but no per-request blend
weight. The old split/rebuild-only MCP index tool is removed.

## Architecture

### Unified facades

Add one facade per resource:

- `TDocSearchFacade` composes the existing FTS5 and semantic services plus the
  TDoc filter repository path.
- `SpecDocSearchFacade` composes the existing FTS5 and semantic services plus
  the spec-document chunk filter path.

Each facade owns mode selection, empty-input normalization, configured hybrid
weight lookup, and conversion into the resource's unified result DTO. The
existing low-level services retain responsibility for FTS5 query execution,
vector KNN, RRF, indexing, and index status. CLI, HTTP, and MCP adapters use
the same facade and serializers so mode/error behavior cannot drift.

The public DTOs are immutable resource-specific result records with explicit
`search_mode`, `score`, `previews`, and metadata fields. Internal FTS5 and
semantic DTOs may retain their current score/rank fields while the facade
normalizes them; no internal ranking diagnostic is exposed unless it is part of
the approved unified shape.

### Metadata completeness

TDoc FTS5 SQL and vector metadata queries select `tdocs.type` and
`tdocs.status` in addition to the existing fields. TDoc metadata stubs carry
those values for vector-only results.

Spec-document vector-only results are enriched by a batched chunk metadata
lookup from the specdata repository. The lookup returns all fields needed for
the unified result (`chunk_id`, spec/version/release, sections, tables, chunk
index, and text). The facade sets `previews` to `null` and uses the vector
distance as `score` in semantic mode. Hybrid rows use the FTS5 metadata when a
chunk is present on the FTS5 side and the metadata lookup otherwise; previews
remain `null` for the entire hybrid result set.

### Filter-only path

Filter mode uses direct SQL repository reads over the source tables, not an
empty FTS5 query and not vector KNN. It preserves the existing filter grammar
and applies `limit`/`offset` at the source query. Rows are mapped to the same
unified DTO with `score=None`, `search_mode="filter"`, and
`previews=None`. The order is deterministic and documented as source/database
order, not relevance order.

### Index maintenance

TDoc FTS5 and vector rebuild loops retain their current repositories and
resume/stale semantics, with the public command and job names moved to the
resource namespace.

Spec-document FTS5 uses the existing version-level rebuild loop. Add a
spec-document vector rebuild loop parallel to it, with:

- parsed source-version enumeration;
- per-pair `index_for_version` calls;
- vector metadata for cursor, last rebuild time, and last indexed parsed-at
  watermark;
- `resume` and `stale_only` handling;
- per-pair failure isolation and progress reporting;
- status reporting alongside FTS5 status.

`rebuild-all` invokes the two resource indexers in a deterministic sequence.
No request path performs a synchronous rebuild.

## Error Handling

- An empty text/semantic pair is valid filter mode.
- FTS5 query parsing errors map to the existing CLI exit/error behavior and
  HTTP/MCP invalid-query errors.
- A requested semantic or hybrid mode with no embedding/vector service maps to
  the existing unavailable-service error.
- A requested FTS5 or hybrid mode with no FTS5 service maps to the existing
  search-unavailable error.
- Index corruption errors retain the current rebuild hint, updated to the new
  `tdoc index` or `spec doc index` command.
- Invalid index action combinations, limits, offsets, and filter values retain
  existing validation behavior.
- Background index failures are recorded in the job result and progress log;
  one resource/version failure does not abort unrelated work unless the
  existing worker treats the entire infrastructure as unavailable.

## Testing

### Search behavior and payloads

- Unit-test dispatch for all four modes for both resources, including empty and
  whitespace-only inputs.
- Assert exact unified DTO/JSON keys, key values, `search_mode`, score source,
  array ordering, and `previews` (`dict` only for FTS5, `null` otherwise).
- Assert semantic and hybrid results are flattened and contain every FTS5
  metadata field; assert vector-only spec chunks are fully enriched.
- Assert TDoc FTS5, semantic, hybrid, and filter results include `type` and
  `status`.
- Assert no `rank_*`, nested `hit`, `rrf_score`, or
  `min_chunk_distance` fields appear in public payloads.
- Assert CLI JSON, HTTP JSON, and MCP JSON serialize equivalent arrays.
- Preserve coverage for FTS5 filters, snippet columns, semantic filters, RRF
  ordering, and field-only pagination.

### Public surface migration

- Verify `tdoc index` and `spec doc index` expose status and rebuild options.
- Verify old `tdoc search query`, `tdoc search sem`, `tdoc search index`, and
  `spec doc search query|sem` invocations are rejected.
- Verify `/tdocs/search` and `/spec-docs/search` support all modes and that
  `/tdocs/search/sem` and `/spec-docs/search/sem` are not registered.
- Verify MCP discovery exposes only the unified search/index tools and omits
  the removed semantic/rebuild-only names.
- Verify index status routes and job routes enqueue the correct job kinds and
  preserve polling/SSE behavior.

### Quality and documentation

Run focused unit/integration tests, the full offline SQLite suite,
`ruff check .`, and `git diff --check`. Update `README.md`, `AGENTS.md`,
`docs/cli.md`, `docs/architecture.md`, `docs/web-server.md`,
`docs/code-map.md`, relevant templates/JavaScript, and current API examples.
