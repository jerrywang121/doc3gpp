# TDoc Search Namespace Migration Design

## Status

Approved design. The existing search implementation remains unchanged; its
public CLI, HTTP, and MCP names become explicitly TDoc-scoped.

## Motivation

The current full-text and semantic search indexes are TDoc-centered:

- the FTS5 projection is `tdoc_search`;
- vector rows are keyed by TDoc and return TDoc metadata;
- CR cover, body-change, and TTCN details are indexed as TDoc sidecar text.

The generic `search` namespace therefore does not match the resource-oriented
CLI convention already used by `spec`, especially the `spec doc search`
commands. Search should move under the TDoc resource everywhere it is exposed.

## Public Contract

This is a hard namespace migration. The old public names are removed; no
aliases, redirects, or deprecation shims are provided.

### CLI

The commands become:

```text
doc3gpp tdoc search query QUERY [filters]
doc3gpp tdoc search sem QUERY [filters]
doc3gpp tdoc search index [options]
```

The existing command options, output formats, validation, error handling,
semantic reranking, FTS5 rebuild, embedding rebuild, resume, stale-only, and
quiet behavior are preserved. The top-level `doc3gpp search` group and all of
its commands are removed.

### HTTP

The read routes become:

```text
GET  /tdocs/search
GET  /tdocs/search/sem
POST /jobs/tdocs/search/rebuild
```

Query parameters, JSON payloads, HTML templates, HTMX fragment behavior, and
the rebuild job body remain unchanged. The old `/search`, `/search/sem`, and
`/jobs/search/rebuild` routes are not registered and do not redirect.

The exact `/tdocs/search` routes must be registered before the existing
`/tdocs/{tdoc_id}` dynamic route, or mounted in a way that gives the exact
routes precedence. A regression test must prove that `/tdocs/search` reaches
the search handler rather than TDoc detail handling.

### MCP

The TDoc search tools become:

```text
search_tdoc
semantic_search_tdoc
rebuild_tdoc_search_index
```

The tool arguments, serialized results, error mapping, and queued job
parameters remain unchanged. The old `search_tdocs`, `semantic_search_tdocs`,
and `rebuild_search_index` tools are not registered.

Spec-document search is unaffected and remains exposed through:

- CLI: `doc3gpp spec doc search query|sem|...`;
- HTTP: `/spec-docs/search` and `/spec-docs/search/sem`;
- MCP: `search_spec_docs` and `semantic_search_spec_docs`.

## Architecture And Data Flow

Only public adapters and their references change.

### CLI

- Add a `tdoc_search_app` Typer group.
- Mount it as `search` on `tdoc_app`.
- Move the existing `query`, `sem`, and `index` command registrations under
  that group without changing their implementations.
- Remove the top-level `search_app` registration.
- Update help text, examples, and command references to use the new path.

### HTTP

- Keep the existing search route module and service dependencies.
- Change its route prefix from `/search` to `/tdocs/search`.
- Update landing-page links, templates, forms, JavaScript, and internal links
  to the new paths.
- Change only the rebuild route path to `/jobs/tdocs/search/rebuild`.
- Keep `JobKind.REBUILD_SEARCH`, worker dispatch, and job payload structure
  unchanged because those are internal behavior, not public resource names.
- Ensure router mounting/order preserves `/tdocs/search` before
  `/tdocs/{tdoc_id}`.

### MCP

- Rename the three tool registrations and their local function names to the
  new singular TDoc names.
- Leave the search services, repository protocols, index tables, vector
  tables, and JSON helper behavior unchanged.

## Non-Goals

- No FTS5 schema or vector schema migration.
- No reindexing or data rewrite.
- No change to search ranking, snippets, filters, semantic fusion, or output
  payloads.
- No change to spec-document search names.
- No compatibility alias, redirect, or deprecation period.

## Verification

### CLI

- Verify `tdoc search query`, `tdoc search sem`, and `tdoc search index` expose
  the existing options and dispatch to the existing service paths.
- Verify top-level help omits `search`.
- Verify invoking `doc3gpp search` is rejected.

### HTTP

- Verify `/tdocs/search` and `/tdocs/search/sem` return the existing JSON,
  full-page HTML, and HTMX fragment forms.
- Verify `/jobs/tdocs/search/rebuild` creates the existing
  `REBUILD_SEARCH` job with the same body mapping.
- Verify old search routes return not-found responses.
- Verify `/tdocs/search` is not captured as a TDoc ID.

### MCP

- Verify discovery includes `search_tdoc`, `semantic_search_tdoc`, and
  `rebuild_tdoc_search_index`.
- Verify each renamed tool preserves the old result and error behavior.
- Verify the old tool names are absent.

### Documentation and quality

Update the current README, `AGENTS.md`, CLI reference, architecture guide,
web-server guide, templates, JavaScript, and relevant test descriptions. Keep
historical design documents unchanged unless they claim to describe the
current public contract.

Run the focused CLI/web/MCP tests, the full offline SQLite suite, `ruff
check .`, and `git diff --check` before completion.
