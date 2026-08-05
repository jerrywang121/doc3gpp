# Web/MCP: tdoc parse trigger + search page updates

**Date:** 2026-08-05
**Status:** Approved design
**Branch:** new branch off `main` (e.g. `web-parse-and-search`)

## Goal

1. On the web tdoc detail page, add a button to trigger tdoc parse (before the cover page section). Same as CLI parse: it shall auto FTS5 index and embedding index (if enabled).
2. For embedding indexing: load the embedding model once, keep it available for later embedding jobs (parse and/or search).
3. On the web search page (`/search`): enlarge the FTS5 query input to span 2 columns, add an optional semantic search input after it spanning the rest of the same line; maps to `--sem-query` for CLI `search query`.
4. On `/search`: add a link to the hybrid search page (`/search/sem`) at top right.
5. On `/search/sem`: enlarge the semantic query input to span 3 columns, the FTS5 query input spans 2 columns on the same line; add the other filter options matching `/search`.
6. On `/search/sem`: add a link to `/search` at top right.

## Section 1 — Parse button on tdoc detail page

**Placement:** a new "Parse" card in `templates/tdoc_show.html` between the TDoc card and the Cover page section, visible only when `record.tdoc.ftp_url` is set (no URL ⇒ parse would fail with `TDocNotYetOnFTPError`).

**Form:** POSTs to the existing `POST /jobs/parse/tdocs` endpoint with JSON body:

```json
{
  "filter": {"tdoc_id": "<tdoc_id>"},
  "force": <checkbox>,
  "full": <checkbox>
}
```

Two checkboxes, both default unchecked:
- **Force re-parse** → `force: true` (mirrors CLI `--force`; re-parses even when a cover page already exists).
- **Full extraction** → `full: true` (mirrors CLI `--full`).

**Progress:** on 202 response, a small JS handler (`static/js/tdoc_parse.js`, loaded only on the tdoc detail page) injects a `<div hx-get="/jobs/{job_id}?format=html" hx-trigger="load" hx-swap="outerHTML">` into the parse card. The existing `partials/job_status.html` takes over: polls every 2s via HTMX until terminal, shows job id/status/kind, error, and log lines. No new job machinery.

**Auto-indexing:** already handled by `TDocCrService._index_after_parse` (FTS5, gated `search.auto_index_on_parse`) and `_embed_after_parse` (embeddings, gated `semantic_search.auto_embed_on_parse`) — both fire on any successful parse regardless of CLI/web origin. The web button inherits this for free; verified by a test.

## Section 2 — Shared embedding model (load once)

**Problem:** one server process can create up to 4 separate `SentenceTransformerEmbedder` instances (in `ServiceContainer.search`'s reranker, `ServiceContainer.semantic_search`, `build_semantic_search_service`'s internal fts5 rebuild, and `TDocCrService`'s semantic_service) — each lazily loads the model on first `encode()`, so parse + search can load the model multiple times.

**Design:**

- Add `factory.build_embedder(settings)` → returns the lazy `SentenceTransformerEmbedder` (does not load the model).
- `build_state` builds **one** instance and injects it into:
  - `build_tdoc_cr_service(embedder=...)` (new optional kwarg, forwarded into its `semantic_service`),
  - `build_search_service(embedder=...)` (new optional kwarg, forwarded into the `SemanticReranker`),
  - `build_semantic_search_service(embedder=...)` (kwarg already exists).
- CLI paths unchanged (per-process, no waste).
- **Concurrency hardening:** add a lock around `SentenceTransformerEmbedder._load_model` so a job-worker thread and a request thread never double-load (first `encode()` wins, others wait).

## Section 3 — `/search` page (FTS5)

**Layout:** the form container becomes a 5-column CSS grid line; the FTS5 query box spans 2 columns; a new optional "Semantic" box spans the remaining 3 columns on the same line. Filters (TSG/Meeting/TDoc/Release/Spec/Since/Until/Limit) wrap below.

**Semantics (CLI parity):** `SearchService.search(query, filters)` gains an optional `sem_query: str | None = None` kwarg:

- `None` ⇒ pure FTS5 (reranker bypassed, matching CLI `search query` without `--sem-query`).
- provided ⇒ fanout `limit * search_fanout_factor` + `SemanticReranker.rerank(sem_query, ..., final_limit=limit)` (exactly the CLI path).

The `/search` route passes `?sem=` through. This also removes the current implicit rerank-by-FTS5-query behavior on the web (the reranker is today always invoked with the raw query).

## Section 4 — `/search/sem` page (hybrid)

**Layout:** the semantic `q` box spans 3 columns, the FTS5 query box spans 2 columns on the same line; below: the full filter set.

**Filters:** the route gains `tsg, meeting, release, spec, since, until` query params (currently only `tdoc-id`). The service's hybrid path already constructs `fts5_filters` with those fields, and `vec.knn` accepts filters — only the route/form plumbing is missing.

Keep `fts5_weight` + `limit` controls.

## Section 5 — Cross links

- Top-right of `/search`: link to `/search/sem` ("Hybrid search").
- Top-right of `/search/sem`: link to `/search` ("FTS5 search").

Implemented via a mode-aware header row in `search_results.html` with a `.page-actions` CSS class.

## Section 6 — MCP

- `search_tdocs` MCP tool gains an optional `sem_query` param (same JSON envelope; parity with web `?sem=`).
- `parse_tdocs` unchanged (already covers single-tdoc via `{"tdoc_id": ...}`). No new MCP tool.

## Section 7 — Tests & docs

**Unit tests:**
- `SearchService.search(sem_query=...)` behavior (None → no rerank; provided → fanout + rerank with `final_limit=limit`).
- Factory embedder sharing (same instance across containers).
- Routes for new params (`/search?sem=`, `/search/sem` new filters).
- `SentenceTransformerEmbedder` concurrent-load lock.

**Integration tests:**
- Web parse button enqueues a single-tdoc job (TestClient, stub job repo).
- Auto FTS5 + embed after web-triggered parse.

**Docs:**
- `docs/web-server.md` (parse button, search page layouts, embedder sharing).
- AGENTS.md web row.
- `docs/cli.md` if needed.

## Out of scope

- New MCP parse tool (reuse `parse_tdocs`).
- Changing CLI behavior.
- Model warm-up at server start (model stays lazy; first use loads it once).
