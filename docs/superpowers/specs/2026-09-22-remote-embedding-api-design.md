# Remote-only embedding backend (OpenAI-compatible API)

**Status:** Design (approved in brainstorming 2026-09-22)
**Date:** 2026-09-22
**Branch:** `feature/external-embedding-api`
**Author:** brainstorming session

## Goal

Replace the local `sentence-transformers` embedding backend with a
single remote-only backend speaking the OpenAI embeddings API
(`POST {base_url}/embeddings`, `{"model", "input"}` → `{"data":
[{"embedding"}]}`). This covers OpenAI, Ollama's OpenAI-compatible
endpoint (e.g. `http://localhost:11434/v1`), vLLM, TEI, and any other
OpenAI-compatible server with one client and no new dependencies
(`httpx` is already a core dep).

There is no local fallback. When `embedding_base_url` is not
configured, the whole semantic stack (vector search, `--sem-query`
rerank, parse-time auto-embed) is disabled/skipped through the
existing `None`-service paths.

## Context

- `Embedder` Protocol (`src/doc3gpp/repository/protocols.py:770`)
  already anticipates a "future hosted-API impl".
- `SentenceTransformerEmbedder`
  (`src/doc3gpp/services/embedding/embedder.py:40`) lazy-loads a
  local HF model; `build_embedder` / `build_semantic_search_service`
  / `build_search_service` (`src/doc3gpp/services/factory.py:161,253,303`)
  construct it directly in three places.
- Vector dim is pinned: `DEFAULT_DIM = 384` + hardcoded `FLOAT[384]`
  DDL (`src/doc3gpp/storage/db/migrate.py:290`) + `vec_meta`
  `embedding_dim` check with a `--rebuild-embeddings` hint
  (`src/doc3gpp/storage/repositories/vector_sql.py:75,96`).
- `SemanticSearchSettings`
  (`src/doc3gpp/settings/schema.py:500`) is TOML-only; `embedding_model`
  defaults to the `all-MiniLM-L6-v2` HF id.
- `config show` (`src/doc3gpp/cli.py:5023`) dumps all settings as
  plain JSON with no redaction today.

## Design

### 1. Settings (`[semantic_search]`)

- `embedding_model: str = "nomic-embed-text"` — repurposed as the
  remote model name sent in the `/v1/embeddings` payload. No longer
  a HuggingFace id.
- `embedding_base_url: str | None = None` — e.g.
  `http://localhost:11434/v1` or `https://api.openai.com/v1`.
  **Unset/empty = semantic stack disabled.** No provider enum.
- `embedding_api_key: str | None = None` — bearer token, optional
  (local Ollama needs none). Never logged or echoed.
- `embedding_timeout_s: float = 30.0` (`ge=1, le=300`) and
  `embedding_batch_size: int = 32` (`ge=1, le=512`).
- Env: add `DOC3GPP_SEMANTIC_SEARCH__EMBEDDING_API_KEY` to
  `ALLOWED_ENV_VARS` (key only; URL/model/timeouts stay TOML-only,
  preserving the existing "TOML-only semantic_search" rule; env wins
  per loader precedence).
- `config show` redacts `semantic_search.embedding_api_key` (`***`
  when set). `config set semantic_search.embedding_api_key ...`
  works via the existing dotted-key path.

### 2. Enablement rule

- `build_embedder()` returns `OpenAICompatibleEmbedder | None`;
  `None` when `embedding_base_url` is unset/empty (no network
  attempt, no error).
- `build_semantic_search_service()` returns `None` on a `None`
  embedder, exactly like `enabled=false` today. Downstream behavior
  reuses the existing disabled paths with no new branches: `search
  sem` exits 1, `search query --sem-query` uses `PassthroughReranker`,
  the `tdoc parse` auto-embed hook is skipped (a remote failure
  mid-parse logs a warning and keeps the parse result), `search index` hides
  vector rows, web/MCP surfaces report unavailable. Unavailable
  messages hint at setting `[semantic_search].embedding_base_url`.
- `sentence-transformers` is removed from runtime for this path:
  `SentenceTransformerEmbedder` is deleted, the `[semantic]` extra
  drops `sentence-transformers` (keeps `sqlite-vec`), and no new
  dependency is added.

### 3. API client + factory wiring

- New `OpenAICompatibleEmbedder(base_url, model, api_key=None,
  timeout_s, batch_size)` in
  `src/doc3gpp/services/embedding/remote_embedder.py`, implementing
  the existing `Embedder` Protocol (`encode(texts) -> (N, dim)`
  float32, `dim` property). `SemanticSearchService`,
  `SemanticReranker`, and parse auto-embed call it unchanged.
- Transport: `httpx.Client`, `POST {base_url}/embeddings` with
  `{"model", "input"}` (trailing slashes on `base_url` are stripped
  before appending `/embeddings`); `Authorization: Bearer` only when
  a key is set; inputs chunked per `embedding_batch_size`. `dim` is
  probed once via a single short non-empty string and cached
  (thread-safe, mirroring the current lazy pattern). `encode([])`
  returns an empty `(0, 0)` float32 array without any HTTP call,
  preserving the current edge-case behavior.
- Failures (connect error, 401/404/5xx, malformed payload) are
  wrapped as the existing `EmbedderUnavailableError` → factories
  return `None` → the §2 disabled path. The key is never included in
  any message or log.
- The shared-single-instance pattern in web `build_state` is
  unchanged (one embedder per process).

### 4. Dim + model tracking, rebuild story

- `vec_meta` carries `embedding_dim` (existing) plus a new
  `embedding_model` row. Both are stamped at index-creation time and
  on every `--rebuild-embeddings` (which drops + recreates `vec0` at
  the embedder's live dim, then stamps current dim + model).
- Fresh `db init` with a URL configured stamps both from the live
  embedder; without a URL it keeps today's default (dim 384, no
  model) so init never needs network.
- Existing DBs pre-dating this feature have a dim but no model row
  → treated as **mismatch** (they were built by the now-removed
  local backend, so a rebuild is required anyway).
- Mismatch rule (fail-fast, same shape as today's dim path): on
  upsert/KNN, if `embedder.dim != stored dim` **or**
  `embedder.model != stored model`, raise the existing
  `VectorIndexUnavailableError` with a `run doc3gpp search index
  --rebuild-embeddings` hint. Same-model-same-dim is the only happy
  path — swapping models forces a rebuild even when dims collide.
- Deliberately **not** tracked: `base_url`, timeouts, batch size —
  the same model name on a new URL is treated as the same index
  (operator's responsibility).
- `search index` (no flags) surfaces both stored and configured
  model/dim in the vector status block so a pending mismatch is
  visible before running a query.

### 5. Errors + diagnostics

- No URL: silent disable via §2 — `search sem` exits 1 with the
  base-url hint; `--sem-query` falls back to FTS5 order with the
  existing one-shot warning; parse auto-embed and `search index
  --rebuild-embeddings` report unavailable. No stack traces.
- URL set but unreachable / bad key / bad model / dim-or-model
  mismatch: `EmbedderUnavailableError` / `VectorIndexUnavailableError`
  → the same exit-1 one-liners as today; the key is never echoed;
  the mismatch hint points at `--rebuild-embeddings`.
- `search sem --explain` and `search index` (no flags) also print
  the configured `model` + `base_url` (host only, key redacted) and
  the stored `vec_meta` model/dim.

### 6. Tests + docs

- Unit: mocked-httpx client (batching, auth header present/absent,
  dim cache, error mapping); factory enablement matrix (no URL →
  `None`; URL + unreachable → `None`; legacy DB missing model →
  mismatch); settings redaction + env precedence for the new key.
- Integration (sqlite): rebuild round-trip with a stub embedder
  (stamp dim+model, mismatch → hint, re-stamp after rebuild).
- Docs in the same change set: `doc3gpp.toml.example` (new knobs +
  Ollama/OpenAI examples), `docs/cli.md`, `docs/architecture.md`,
  `AGENTS.md` workflow lines, `README.md` semantic-setup section.

## Non-goals

- No Ollama-native `/api/embed` client (Ollama's OpenAI-compatible
  endpoint is sufficient).
- No provider enum, no per-provider knobs.
- No new runtime dependencies.
- No migration of existing local-backend vector rows (rebuild is
  the upgrade path).
