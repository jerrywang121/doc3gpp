# Semantic Search — Design Spec

**Status:** Draft (pending user review)
**Date:** 2026-07-31
**Branch:** `feat/embedding-search`
**Author:** brainstorming session
**Builds on:** [`2026-07-29-fts5-search-design.md`](2026-07-29-fts5-search-design.md)

## Goal

Add a natural-language read path (`doc3gpp search sem QUERY`) to
doc3gpp that combines SQLite FTS5 keyword matching with a local
embedding-based vector search, returning a single merged top-N result
list via reciprocal-rank fusion (RRF). The FTS5 path is the one
shipped in the previous spec; this spec adds the vector path, a
spaCy stopword + lemma pre-processor for natural-language queries,
and a new CLI subcommand dedicated to prose-style search.

The dominant use case is **prose-first search**: a user types a
question in natural English ("what CRs touch NB-IoT power saving?")
and expects the system to surface the relevant TDocs even when the
literal keywords (`touch`, `power`) are absent from the cover page.
The existing identifier-first path (`doc3gpp search query`) is
preserved unchanged.

## Non-goals

- **Hosted embedding APIs (OpenAI, Voyage, Cohere, Ollama).** Local
  `sentence-transformers` only in v1. A pluggable model name is
  exposed via `Settings.semantic_search.embedding_model` so users
  can swap to a different HuggingFace repo id, but no HTTP client
  to a hosted endpoint is in scope.
- **MCP server transport.** Same deferral as the FTS5 spec.
- **Web UI / search box.** Deferred.
- **3GPP-domain keyword extraction / NER.** spaCy's default
  `en_core_web_sm` pipeline is enough for stopword removal +
  lemmatization; telecom-aware NER is out of scope.
- **MySQL / Postgres backend.** sqlite-vec is sqlite-only. On other
  dialects `search sem` reports unavailable and degrades gracefully,
  same as the FTS5 spec.
- **GPU acceleration.** CPU only. The default model
  (`all-MiniLM-L6-v2`) is fast enough on CPU; GPU wiring is a
  separate change.
- **Background embedding worker.** Embedding runs inline during
  parse and during the first `search sem` invocation. A worker
  thread is deferred.
- **Replacing the FTS5 path.** `search query` stays as-is; this
  spec adds a parallel `search sem` subcommand, not a flag on the
  existing one.

## Architecture

The FTS5 spec's layering rules are preserved; a new sibling
subsystem sits alongside the existing search service.

```
CLI subcommand
  │
  ├── search query QUERY [filters]    ──► SearchService ──► SearchIndexRepository
  │                                      (unchanged)            │
  │                                                           └─► FTS5 virtual table
  │
  └── search sem QUERY [filters]      ──► SemanticSearchService
                                          │   ─ stopword strip (spaCy)
                                          │   ─ FTS5 candidate fan-out
                                          │   ─ query embedding + vector KNN
                                          │   ─ RRF merge → top N
                                          │
                                          ├──► SearchService (reuse)
                                          ├──► Embedder (Protocol)
                                          │       └──► SentenceTransformerEmbedder
                                          └──► VectorIndexRepository (Protocol)
                                                  └──► SQLAlchemyVectorIndexRepository
                                                          uses sqlite-vec
```

The two subcommands share `TDocCrService`'s auto-index hook: a
single successful parse now writes the FTS5 row **and** the
chunked vector rows. The FTS5 + vector indexes have the same
"latest revision wins via JOIN" semantics, so they are
automatically consistent.

### New modules

| File | Purpose |
|---|---|
| `models/semantic_search.py` | `SemanticSearchHit` DTO + error hierarchy (`SemanticSearchError`, `SemanticSearchUnavailableError`, `SpacyUnavailableError`, `EmbedderUnavailableError`, `VectorIndexUnavailableError`) |
| `repository/protocols.py` (extend) | `Embedder` Protocol, `VectorIndexRepository` Protocol |
| `services/embedding/chunker.py` | Pure-Python `_chunks(text, size, overlap) -> list[str]` |
| `services/embedding/stopwords.py` | spaCy wrapper `strip_stopwords(text) -> str`; caches the loaded pipeline per process |
| `services/embedding/embedder.py` | `Embedder` Protocol impl `SentenceTransformerEmbedder` (lazy model load); dim + dtype check |
| `services/embedding/__init__.py` | Public re-exports |
| `storage/repositories/vector_sql.py` | `SQLAlchemyVectorIndexRepository` (sqlite-vec DDL/DML; runtime probe) |
| `services/semantic_search_service.py` | `SemanticSearchService` (orchestration: stopword strip → FTS5 fan-out → embed → vector KNN → RRF merge → truncate) |
| `services/factory.py` (extend) | `build_semantic_search_service(settings)`; wire into `build_tdoc_cr_service` |
| `services/tdoc_cr_service.py` (modify) | New `_embed_after_parse(tdoc_id)` helper called from the same two happy-path sites as `_index_after_parse` |
| `cli.py` (extend) | `search sem` command (sibling of `search query`); `search index --rebuild-embeddings` |
| `settings/schema.py` (extend) | `SemanticSearchSettings` |
| `pyproject.toml` (modify) | New `[semantic]` extra: `sentence-transformers`, `spacy`; doc-only model download hint |
| `storage/db/migrate.py` (extend) | `vec_tdoc_embeddings` virtual table DDL + `vec_meta` sidecar; gated on sqlite + sqlite-vec availability |

### Reused, unchanged

- `SearchService` (`src/doc3gpp/services/search_service.py`) — used
  as the FTS5 fan-out engine. `SemanticSearchService` calls
  `SearchService.search(query, filters)` with an enlarged `limit`
  and reuses the existing `SearchHit` shape.
- `SearchIndexRepository` (`storage/repositories/search_sql.py`) —
  same FTS5 row. The vector index is a **separate** table; nothing
  in the FTS5 code path changes.
- `TDocCrService._index_after_parse` from the FTS5 spec — keeps
  firing. The new `_embed_after_parse` is its sibling and fires
  from the same two call sites.
- `cli_filters.py` — same filter grammar; the new subcommand
  reuses `parse_date_filter`, `parse_release_filter`,
  `parse_spec_filter`, `parse_tdoc_filter`, `parse_meeting_filter`.
- `--format` / `--compact` / `--quiet` / `--explain` semantics —
  the new subcommand reuses the same Typer renderer and the same
  `_resolve_compact` helper from the FTS5 spec.

### Data flow — `search sem QUERY [filters]`

```
"what CRs touch NB-IoT power saving"  (raw natural-language input)
        │
        ├──► stopwords.strip(text) → "CR touch NB-IoT power save"  (spaCy)
        │            │                       (lemma + stopword drop + quote-aware)
        │            ▼
        │   SearchService.search(limit = N * fanout_multiplier)
        │            │
        │            ▼
        │     list[SearchHit]                 (top 2N by FTS5 bm25)
        │
        ├──► embedder.encode(["what CRs touch NB-IoT power saving"])
        │            │                       (original query, not stripped)
        │            ▼
        │     np.ndarray, shape (1, D)
        │            │
        │            ▼
        │   VectorIndexRepository.knn(query_vec, limit = N * fanout_multiplier)
        │            │
        │            ▼
        │     list[(tdoc_id, chunk_id, distance)]   (top 2N by cosine distance)
        │
        └──► RRF merge by tdoc_id:
                 tdoc_score = 1/(k + rank_fts5) * (1 - vector_weight)
                            + 1/(k + rank_vec) * vector_weight
                 (chunks reduced to min(distance) per tdoc_id for ranking)
                          │
                          ▼
                  list[SemanticSearchHit]    (top N, default 20)
```

### Data flow — `search index --rebuild-embeddings`

```
search index --rebuild-embeddings [--stale-only] [--batch N] [--quiet]
        │
        ▼
SemanticSearchService.rebuild_embeddings(batch_size, stale_only, quiet)
        │
        ▼
VectorIndexRepository.rebuild_batch(...)    (mirrors FTS5 rebuild_batch)
        │
        ▼
For each tdoc_id:
    embed_text = build_embed_text(tdoc_id)         (join + decompress)
    chunks     = _chunks(embed_text, size, overlap)
    for chunk, vec in zip(chunks, embedder.encode(chunks)):
        repo.upsert_chunk(chunk_id = f"{tdoc_id}#{i}",
                          tdoc_id  = tdoc_id,
                          chunk_index = i,
                          embedding = vec)
    delete any chunks where chunk_index >= len(chunks)  (shortened re-parse)
        │
        ▼
Yield RebuildProgress per batch; update vec_meta (resume + staleness)
```

### Data flow — auto-embed on parse

`TDocCrService._embed_after_parse(tdoc_id)` (sibling of
`_index_after_parse`) fires from the same two call sites (DB-mode
`extract()` happy path + direct-mode `_extract_from_3gpp_url()`
happy path). It is best-effort, gated on
`Settings.semantic_search.auto_embed_on_parse`, and never raises:

```python
def _embed_after_parse(self, tdoc_id: str) -> None:
    if not self._settings.semantic_search.auto_embed_on_parse:
        return
    if self._semantic_service is None:
        return
    try:
        self._semantic_service.index_for_tdoc(tdoc_id)
    except SemanticSearchUnavailableError:
        logger.debug("semantic extra not installed; skipping embed")
    except Exception as exc:
        logger.warning(
            "failed to update embedding index for tdoc_id=%s: %s",
            tdoc_id, exc,
        )
```

`index_for_tdoc(tdoc_id)` is the single-tdoc version of
`rebuild_embeddings`; it is also exposed on the service so
debugging scripts can re-embed one TDoc at a time.

## Vector schema

Two new objects in the sqlite database, created by
`storage/db/migrate.py` (gated on `dialect.name == "sqlite"` and a
runtime sqlite-vec probe):

```sql
-- Per-chunk vector index. One TDoc maps to N rows (N chunks).
CREATE VIRTUAL TABLE IF NOT EXISTS vec_tdoc_embeddings USING vec0(
    chunk_id TEXT PRIMARY KEY,                 -- "{tdoc_id}#{i}"
    tdoc_id TEXT,                              -- joins back to tdocs
    chunk_index INTEGER,                       -- 0..N-1
    embedding FLOAT[384]                       -- dim pinned at create time
);

-- Sidecar meta table (mirrors tdoc_search_meta).
CREATE TABLE IF NOT EXISTS vec_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Rows: 'last_rebuild_at', 'last_indexed_at', 'last_indexed_uploaded_date',
--       'embedding_dim', 'embedding_model'.
```

### Why a separate table (not extending `tdoc_search`)

`vec_tdoc_embeddings` has different shape and lifecycle from
`tdoc_search`:

- `tdoc_search` is one row per `tdoc_id` (FTS5 collapses
  revisions; latest wins via JOIN). `vec_tdoc_embeddings` is
  one row per chunk (revisions collapse identically because the
  `_embed_after_parse` hook always re-walks the joined query
  and replaces every chunk for `tdoc_id`).
- `tdoc_search` is a built-in sqlite FTS5 virtual table; mixing
  it with a sqlite-vec virtual table is not supported by the
  extension loader.
- Indexes serve different queries: FTS5 is `MATCH` + `bm25`;
  vectors are KNN by cosine distance. Different access paths,
  different storage engine.

### Embedding dim

The dim is set at table creation time and stored in `vec_meta` so
the schema matches the model. On a model swap the CLI exits 1
with a one-line `vector index dim mismatch: stored=384 requested=768;
run \`doc3gpp search index --rebuild-embeddings\`` (non-interactive;
no `typer.confirm`). An attempt to upsert a vector with the wrong
dim raises `VectorIndexUnavailableError` with the current vs
requested dim before any SQL runs.

The default model `sentence-transformers/all-MiniLM-L6-v2` is
384-dim. The schema literal in the migration is `FLOAT[384]`; the
runtime probe in `SQLAlchemyVectorIndexRepository.__init__` checks
the model's `get_sentence_embedding_dimension()` and raises if it
does not match the stored dim in `vec_meta`. The
`--rebuild-embeddings` path drops and recreates the table when
the dim changes.

## Chunking

### `services/embedding/chunker.py`

```python
def _chunks(text: str, size: int, overlap: int) -> list[str]:
    """Split ``text`` into chunks of ~``size`` whitespace tokens with ``overlap``.

    Whitespace tokenization (not word-piece): the model has its own
    tokenizer, but our chunk boundary is on whitespace to keep the
    function pure-Python and fast. The model's true token count per
    chunk will be ~1.0-1.3x the whitespace count for English prose.

    ``size`` and ``overlap`` are in whitespace tokens, NOT model
    tokens. Settings: ``chunk_size=800``, ``chunk_overlap=100``.
    """
```

- Pure function; no I/O; corpus-testable.
- Boundary cases pinned: empty string → `[]`; text shorter than
  `size` → `[text]`; trailing whitespace stripped from every chunk.
- `overlap` must be `< size`; `ValueError` otherwise (defense at
  the service layer — settings validation rejects bad values).

### `services/embedding/stopwords.py`

```python
def strip_stopwords(text: str) -> str:
    """Run ``text`` through ``spacy.load("en_core_web_sm")`` and return a
    space-joined string of lemmatized, non-stopword, alpha-numeric tokens.

    The spaCy pipeline is loaded once per process (cached module
    attribute) so the per-call cost is dominated by ``Doc`` creation,
    not model load.
    """
```

- Punctuation, stopwords, and tokens with `is_space` /
  `is_punct` / `is_stop` are dropped.
- Each remaining token's `.lemma_` is emitted lowercase.
- The output is a space-joined string ready to feed into the
  existing `SearchQueryBuilder` (T9 from the FTS5 spec) — the
  builder adds the spec-id normalization (`38.300` → `38_300`)
  and the TDoc-id duplication (`R5-1234567r2` →
  `R5-1234567 R5-1234567r2`).
- Empty / punctuation-only input returns `""`; the CLI surfaces a
  `SemanticSearchQueryError("query has no content after
  stopword stripping")` and exits 2.
- Model load failure raises `SpacyUnavailableError`; the CLI
  tells the user to run `python -m spacy download en_core_web_sm`
  and exits 1.

#### Custom stopword set

The effective stopword set is composed per-call as
`spacy.lang.en.stop_words.STOP_WORDS | user_defined_stop_words -
set(keep_negation_words)`:

- `user_defined_stop_words: list[str]` (default `[]`) — extra
  tokens to drop, used to remove 3GPP-domain noise (e.g.
  `"tdoc"`, `"cr"`, `"3gpp"`, `"spec"`, `"meeting"`, `"agenda"`
  when the user has 100k+ TDocs and those tokens drown out the
  real signal). The list is matched case-insensitively against
  each token's lowercased form. The user composes the default
  list by running the stripper over a real-corpus sample and
  eyeballing the high-frequency low-signal tokens; the setting
  is empty until that exercise is done.
- `keep_negation_words: list[str]` (default `["not"]`) — tokens
  that the user wants to **retain** even though spaCy treats
  them as stopwords. Negation is the canonical case: stripping
  `not` from `"which CRs do not relate to NB-IoT"` leaves
  `"relate NB-IoT"`, which inverts the user's intent. The
  default of `["not"]` is the v1 safe choice; users running
  their own negation-aware rerank can set this to `[]` to
  restore spaCy's full default stopword set.

Implementation:

```python
def _effective_stopwords() -> frozenset[str]:
    from spacy.lang.en.stop_words import STOP_WORDS
    base = set(STOP_WORDS)
    base -= {w.lower() for w in settings.semantic_search.keep_negation_words}
    base |= {w.lower() for w in settings.semantic_search.user_defined_stop_words}
    return frozenset(base)
```

The result is cached at process start (keyed on the resolved
settings hash) and reused on every `strip_stopwords` call.
`strip_stopwords` does the membership check against the cached
frozenset; spaCy's `Doc` is still created per call (the cost is
the `Doc` constructor + tokenizer, not the stopword lookup).

### `services/embedding/embedder.py`

```python
class Embedder(Protocol):
    def encode(self, texts: list[str]) -> np.ndarray: ...
    @property
    def dim(self) -> int: ...


class SentenceTransformerEmbedder(Embedder):
    def __init__(self, model_name: str) -> None:
        # Lazy model load: the SentenceTransformer instance is
        # built on first .encode() call, not at construction,
        # so a process that never reranks never pays the load.

    def encode(self, texts: list[str]) -> np.ndarray:
        # Returns shape (len(texts), dim), dtype float32.
        # Single encode() call with the full batch; the
        # sentence-transformers library batches internally.

    @property
    def dim(self) -> int: ...
```

- Model name is `Settings.semantic_search.embedding_model`
  (default `sentence-transformers/all-MiniLM-L6-v2`).
- Construction does not load the model; `.encode()` does. The
  first rerank query pays the load cost (1-3s warm, 5-10s on first
  download). Subsequent queries are sub-100ms.
- Model load failure (`OSError` from HuggingFace, network error,
  OOM) is wrapped as `EmbedderUnavailableError`; the CLI prints
  the cause and exits 1.

## Storage layer

### `SQLAlchemyVectorIndexRepository`

`storage/repositories/vector_sql.py` — protocol impl.

- Runtime probe: `sqlite-vec` is loaded via
  `sqlite_vec.load(conn)`; missing extension raises
  `VectorIndexUnavailableError` with the install hint
  (`pip install doc3gpp[semantic]`).
- DDL: `CREATE VIRTUAL TABLE IF NOT EXISTS vec_tdoc_embeddings
  USING vec0(...)` with the dim from `vec_meta.embedding_dim`
  (default 384).
- DML:
  - `upsert_chunks(tdoc_id, embeddings: list[np.ndarray])` —
    deletes existing chunks for `tdoc_id`, then inserts the new
    chunk rows in a single transaction.
  - `remove_for_tdoc(tdoc_id)` — `DELETE WHERE tdoc_id = ?`.
  - `knn(query_vec, limit, filters=None)` — KNN by cosine
    distance; joins to `tdocs` / `meetings` for filters.
    `k = limit`; results are `(tdoc_id, chunk_id, chunk_index,
    distance)`.
  - `rebuild_batch(batch_size, after_id, stale_only)` —
    mirrors the FTS5 shape; yields batches of `tdoc_id` strings
    in `ORDER BY tdoc_id ASC`.
  - `count_tdocs_to_index(stale_only)`, `get_resume_cursor()`,
    `set_resume_cursor()`, `status()` — same shape as the FTS5
    repo; `SearchIndexStatus` gains an `embedding_row_count`
    field (or a new `VectorIndexStatus` DTO; spec defers the
    exact shape to impl, with a preference for extending
    `SearchIndexStatus` so the CLI can render both indexes in
    one `search index` call).

## Service layer

### `SemanticSearchService` shape

```python
class SemanticSearchService:
    def __init__(
        self,
        fts5_service: SearchService,
        embedder: Embedder,
        vector_repo: VectorIndexRepository,
        settings: Settings,
    ) -> None:
        ...

    # Write paths
    def index_for_tdoc(self, tdoc_id: str) -> None: ...
    def remove_for_tdoc(self, tdoc_id: str) -> None: ...

    # Read path
    def search(self, query: str, filters: SearchFilters,
               limit: int, vector_weight: float) -> list[SemanticSearchHit]: ...

    # Maintenance
    def rebuild_embeddings(
        self, batch_size: int, stale_only: bool, quiet: bool,
    ) -> Iterator[RebuildProgress]: ...
    def status(self) -> SearchIndexStatus: ...  # extends with vector fields
```

- `search(query, filters, limit, vector_weight)`:
  1. `stripped = stopwords.strip(query)`. If empty after strip,
     raise `SemanticSearchQueryError("query has no content after
     stopword stripping")`.
  2. `fts5_expr = SearchQueryBuilder(stripped).build()` (the
     existing builder from the FTS5 spec, reused).
  3. `internal_limit = limit * settings.semantic_search.fanout_multiplier`.
  4. `fts5_hits = fts5_service.search(fts5_expr, filters)[:internal_limit]`.
  5. `query_vec = embedder.encode([query])[0]` (the **original**
     query, not the stripped one — the embedder has its own
     stopword handling; the strip is for the FTS5 path only).
  6. `vec_hits = vector_repo.knn(query_vec, limit=internal_limit,
     filters=filters)`.
  7. `merged = rrf_merge(fts5_hits, vec_hits, k=settings.semantic_search.rrf_k,
     vector_weight=vector_weight)`.
  8. `return merged[:limit]`.

### RRF merge

A small pure-Python helper at `services/semantic_search_service.py`
top level (corpus-testable, no I/O):

```python
def rrf_merge(
    fts5_hits: list[SearchHit],
    vec_hits: list[tuple[str, str, int, float]],   # (tdoc_id, chunk_id, chunk_index, distance)
    *,
    k: int = 60,
    vector_weight: float = 0.7,
    limit: int = 20,
) -> list[SemanticSearchHit]:
    """Reciprocal-rank fusion across FTS5 and vector rankings.

    Each tdoc_id is ranked by FTS5 position (if present) and by
    the best (lowest-distance) vector chunk. The final score is:

        rrf_score = 1/(k + rank_fts5) * (1 - vector_weight)
                  + 1/(k + rank_vec)  * vector_weight

    A tdoc_id present in only one side contributes 0 from the
    other side's rank. A tdoc_id present in neither side is
    dropped. The output is sorted descending by rrf_score and
    truncated to ``limit``.
    """
```

- `vector_weight=0.0` → pure FTS5 (vector contributes nothing;
  but FTS5 candidates that have no vector row still rank).
- `vector_weight=1.0` → pure vector (FTS5 candidates that have
  no vector row are dropped — the rerank needs a vector score).
- Default `0.7` → vector dominates but FTS5 still matters.

The chunk-to-tdoc reduction for the vector side uses
`min(distance)` across all chunks for the same `tdoc_id`, then
ranks `tdoc_id` by that min. The full chunk list is preserved
in the result DTO so the CLI can show "best matching chunk" in
`--explain` mode.

### Factory wiring

```python
def build_semantic_search_service(
    settings: Settings,
    fts5_service: SearchService | None = None,
    embedder: Embedder | None = None,
    vector_repo: VectorIndexRepository | None = None,
) -> SemanticSearchService | None:
    """Build a SemanticSearchService or return None if the stack is unavailable."""
    try:
        if fts5_service is None:
            fts5_service = build_search_service(settings)
        if fts5_service is None:
            # FTS5 is the foundation; no point in a vector-only stack.
            return None
        if embedder is None:
            embedder = SentenceTransformerEmbedder(
                settings.semantic_search.embedding_model,
            )
        if vector_repo is None:
            vector_repo = SQLAlchemyVectorIndexRepository(settings)
        return SemanticSearchService(
            fts5_service=fts5_service,
            embedder=embedder,
            vector_repo=vector_repo,
            settings=settings,
        )
    except (VectorIndexUnavailableError, EmbedderUnavailableError,
            SpacyUnavailableError):
        return None
```

`build_tdoc_cr_service` calls `build_semantic_search_service`
alongside the existing `build_search_service` and passes the
result (possibly `None`) as `semantic_service=...` to
`TDocCrService(...)`.

## DTOs

### `SemanticSearchHit`

```python
@dataclass(slots=True, frozen=True)
class SemanticSearchHit:
    tdoc_id: str
    rrf_score: float
    rank_fts5: int | None         # None if not in FTS5 fan-out
    rank_vec: int | None          # None if not in vector fan-out
    min_chunk_distance: float | None
    best_chunk_id: str | None     # for --explain rendering
    hit: SearchHit           # reused from the FTS5 spec
```

`hit` is the existing `SearchHit` dataclass (title, meeting,
tsg, uploaded_date, ftp_url, wis, previews). The CLI renders
`hit` as a sub-section; `rrf_score` and the rank provenance
are the new fields.

### Error hierarchy

```
SearchError                          (existing base)
└── SemanticSearchError              (new base; CLI catches for exit 1)
    ├── SemanticSearchUnavailableError  (no stack at all)
    ├── SemanticSearchQueryError        (empty after stopword strip)
    ├── SpacyUnavailableError           (en_core_web_sm not installed)
    ├── EmbedderUnavailableError        (model load failed)
    └── VectorIndexUnavailableError     (sqlite-vec missing)
```

All new errors extend `SearchError` so the existing
catch-all in the CLI works, but each gets its own `except` branch
in `cli.py` for the friendly one-liner.

## CLI surface

### `doc3gpp search sem QUERY [filters]`

```
doc3gpp search sem "what CRs touch NB-IoT power saving" [flags]
```

Flags:

| Flag | Type | Default | Effect |
|---|---|---|---|
| `QUERY` (positional, required) | string | — | Natural-language input |
| `--tsg` | str | None | `meetings.tsg` filter |
| `--meeting` | str | None | `meetings.name` filter |
| `--meeting-id` | int | None | `meetings.meeting_id` filter |
| `--tdoc-id` | str | None | exact `tdocs.tdoc_id` filter |
| `--release` | str | None | `tdocs.release` filter |
| `--spec` | str | None | spec-number filter (e.g. `38.300`) |
| `--since` | date (YYYY-MM-DD) | None | `tdocs.uploaded_date >= since` |
| `--until` | date (YYYY-MM-DD) | None | `tdocs.uploaded_date <= until` |
| `--limit` | int | 20 | Final result count after RRF |
| `--vector-weight` | float | 0.7 | Blend weight for vector rank in RRF (0.0..1.0) |
| `--format` | choice | `table` | `table` \| `json` \| `markdown` |
| `--compact` | flag | False | Strip markdown/json decorators |
| `--explain` | flag | False | Print stopword-stripped FTS5 query, query embedding dim, RRF constants, best chunk per hit |
| `--quiet` | flag | False | Suppress stale-index hint |

Output formats:

- **`table`** (default): columns `rank`, `tdoc_id`, `rrf`, `fts`,
  `vec`, `dist`, `title`. `fts` / `vec` are the per-side ranks
  (or `–` if not in that side's fan-out); `dist` is the
  `min_chunk_distance` (or `–`).
- **`json`**: full `SemanticSearchHit` records (including
  `hit` sub-record). `--compact` → single-line JSON.
- **`markdown`**: human-friendly list with bolded `tdoc_id`,
  RRF score, and the existing `hit.previews` blockquote.
  `--compact` strips per the existing convention.

### `doc3gpp search index` extensions

`search index` (no flags) gains embedding-index fields in its
existing `SearchIndexStatus` output:

```
Search index:        enabled (sqlite + fts5)
Rows indexed:        4,231
Embedding index:     enabled (sqlite-vec, 384d, all-MiniLM-L6-v2)
Vector rows:         12,847
Last rebuild:        2026-07-28 14:32:11 UTC
Last rebuild (vec):  2026-07-28 14:38:42 UTC
Last indexed:        tdocs.uploaded_date ≤ 2026-07-28 14:18:00 UTC
Latest tdocs:        tdocs.uploaded_date  2026-07-29 09:01:14 UTC
Status:              STALE — newer tdocs exist; run `doc3gpp search index --rebuild`
```

New flag:

| Flag | Effect |
|---|---|
| `--rebuild-embeddings` | Drop and rebuild the `vec_tdoc_embeddings` virtual table. Iterates every `tdocs` row, calls `index_for_tdoc` per id, updates `vec_meta` for resume. |
| `--stale-only` (works on both) | Only re-index rows whose `tdocs.uploaded_date > last_indexed_uploaded_date`. |

The existing `--rebuild` continues to mean "FTS5 only"; the new
`--rebuild-embeddings` is the vector-only sibling. A combined
`--rebuild-all` runs both in sequence.

## Settings additions

```python
class SemanticSearchSettings(BaseModel):
    enabled: bool = True
    auto_embed_on_parse: bool = True
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    chunk_size: int = 800
    chunk_overlap: int = 100
    rrf_k: int = 60
    vector_weight: float = 0.7
    fanout_multiplier: int = 2
    final_limit: int = 20
    # Stopword-set customization (see "Custom stopword set").
    user_defined_stop_words: list[str] = []
    keep_negation_words: list[str] = ["not"]
```

The CLI's `--limit` and `--vector-weight` flags override the
`final_limit` and `vector_weight` settings on a per-call basis;
the rest are config-only (no CLI surface in v1). Pydantic
validates `chunk_size > 0`, `0 <= chunk_overlap < chunk_size`,
`0.0 <= vector_weight <= 1.0`, `fanout_multiplier >= 1`,
`rrf_k > 0`; bad values raise at `Settings` load time with a
clear error pointing at the offending key.

## `pyproject.toml` additions

```toml
[project.optional-dependencies]
# existing: cli, extract, search, mysql, postgres, dev
semantic = [
  "sentence-transformers>=2.7.0",
  "spacy>=3.7.0",
  "sqlite-vec>=0.1.0",
]
# `python -m spacy download en_core_web_sm` is a one-time op
# documented in the [semantic] extra's description below.
```

`[project]` gets no new mandatory deps. `pip install
doc3gpp[search]` (the FTS5 extra) is unchanged; `pip install
doc3gpp[semantic]` is the new install target. Users can install
both: `pip install "doc3gpp[search,semantic]"`.

## Error handling & resilience

### Three-layer defense (mirrors the FTS5 spec)

1. **Build is best-effort.** `build_semantic_search_service` catches
   the three unavailable errors and returns `None`.
   `TDocCrService.__init__` accepts `semantic_service: SemanticSearchService | None = None`;
   the hook short-circuits on `None`. Net effect: doc3gpp without
   `[semantic]` behaves exactly like today.
2. **The hook is best-effort.** `_embed_after_parse` catches every
   exception and logs a warning. A failing embed never aborts a
   successful parse.
3. **The repo / embedder / stopword filter raise typed errors.**
   The CLI catches each subclass with its own message:

   | Error | Message | Exit |
   |---|---|---|
   | `SemanticSearchUnavailableError` | `search sem unavailable: <reason>` + `pip install doc3gpp[semantic]` | 1 |
   | `SemanticSearchQueryError` | `bad query: <reason>` | 2 |
   | `SpacyUnavailableError` | `spaCy model not installed; run \`python -m spacy download en_core_web_sm\`` | 1 |
   | `EmbedderUnavailableError` | `embedding model load failed: <reason>` | 1 |
   | `VectorIndexUnavailableError` | `vector index unavailable: <reason>` + `pip install doc3gpp[semantic]` | 1 |
   | `Settings.semantic_search.enabled = false` | `search sem disabled in settings` | 0 |

### Index-text rebuild resilience

`SemanticSearchService.rebuild_embeddings` is a generator that
mirrors `SearchService.rebuild`:

- Resumable via `vec_meta.last_rebuild_last_tdoc_id`.
- Per-row fault tolerance: one failing TDoc (corrupt gzip blob,
  OOM in the embedder) logs a warning and continues.
- CLI's `--quiet` controls per-batch progress output.

### Stale-index hint

After every CLI command that touches tdocs, the existing stale
check is extended to check both indexes. The hint mentions
`--rebuild` and `--rebuild-embeddings` independently.

## Test plan

### Unit tests (no model, no network)

| File | Covers |
|---|---|
| `tests/unit/test_chunker.py` | Chunk size + overlap corpus; boundary cases (empty, single chunk, text shorter than size, long text); `overlap >= size` raises `ValueError`. |
| `tests/unit/test_stopwords.py` | spaCy strip corpus with a small fixture text; lemma output; empty / punctuation-only inputs. `user_defined_stop_words` corpus (default-empty → `"tdoc"` token kept; with `["tdoc"]` → `"tdoc"` token dropped). `keep_negation_words` corpus (`["not"]` default → `"not"` retained in `"do not relate"`; `[]` → `"not"` stripped). Mock the spaCy pipeline via `spacy.util.minibatch` patching to keep tests deterministic and fast. |
| `tests/unit/test_embedder.py` | Mock `SentenceTransformer.encode`; dim + dtype checks; lazy model load. |
| `tests/unit/test_rrf.py` | RRF merge corpus: known ranked inputs → known merged output; min(distance) per tdoc_id; `vector_weight=0.0` / `1.0` degenerate cases. |
| `tests/unit/test_semantic_search_service.py` | Mock FTS5 service + mock vector repo + mock embedder; full `search(...)` flow; empty-after-strip error; both-side-empty result. |
| `tests/unit/test_cli_search_sem.py` | Typer `CliRunner` flag parsing; `--limit <0` rejected; `--vector-weight` out of `[0,1]` rejected; error-to-message mapping. |

### Integration tests (sqlite + sqlite-vec, on-disk temp DB)

| File | Covers |
|---|---|
| `tests/integration/test_vector_index_lifecycle.py` | sqlite-vec probe; insert chunks; KNN returns expected order; remove chunks; rebuild; status. |
| `tests/integration/test_search_sem_filters.py` | Every filter flag end-to-end on `search sem`. |
| `tests/integration/test_search_sem_end_to_end.py` | Insert fixture TDocs with cover / change / ttcn rows; `search sem "what CRs touch NB-IoT power saving"` returns expected TDoc + RRF ordering. |
| `tests/integration/test_embed_after_parse.py` | `TDocCrService.extract` triggers both FTS5 and embedding hooks; both indexes reflect the new TDoc; re-parse of a shorter TDoc drops the surplus chunks. |
| `tests/integration/test_semantic_extras_disabled.py` | Three scenarios: extra not installed, sqlite-vec missing, `Settings.semantic_search.enabled = false`. |

### Fixture corpus

`tests/fixtures/semantic_search_corpus.py` exports a small but
diverse set:

- 8 TDocs spanning RAN/SA plenary and regular meetings
- 3 with TTCN sidecars
- 2 with change-details rows
- 1 with no cover/ttcn/change (metadata only — must still be
  indexed with one or two chunks from the title + meeting)
- 1 with multi-chunk text (long cover + change body)
- 1 with single-chunk text (short title-only)
- 1 with a `38.300` spec reference
- 1 with NB-IoT jargon

Rows use small gzip-compressed JSON blobs so the `_build_embed_text`
JOIN runs end-to-end. Embeddings in the fixture corpus are
pre-computed via the same `SentenceTransformerEmbedder` and
serialized as float32 ndarrays, so the integration tests don't
pay the model-load cost on every run (the model load is covered
by a separate, opt-in integration test).

### Edge cases pinned in tests

1. `search sem` with an empty query → `SemanticSearchQueryError`.
2. `search sem` with a stopword-only query → same error.
3. FTS5 hit set empty + vector hit set non-empty → vector side
   dominates, RRF still produces a valid ordering.
4. Vector hit set empty + FTS5 hit set non-empty → FTS5 side
   dominates, vector side contributes 0, RRF still produces a
   valid ordering. (`vector_weight=1.0` would zero this out; test
   pins both `0.5` and `1.0` behaviors.)
5. Same tdoc_id in both fan-outs with multiple vector chunks →
   `min(distance)` rule applied; `best_chunk_id` is the chunk
   that produced the min.
6. Re-parse of a TDoc that drops from 8 chunks to 4 chunks →
   the surplus 4 chunks are deleted; vec_meta reflects the new
   `last_indexed_uploaded_date`.
7. Embedder model swap to a 768-dim model →
   `--rebuild-embeddings` drops + recreates the table; the next
   `search sem` works against the new dim.
8. `search sem` on a TDoc with `auto_embed_on_parse=False` and no
   manual embed → that TDoc is absent from the vector fan-out
   but still surfaces via FTS5 if it matches.
9. `--vector-weight=0.5` + 50/50 FTS5/vector fan-out → both
   ranks contribute equally.
10. `--limit=0` → returns nothing (pinned at impl).
11. `--limit=-1` → `typer.BadParameter`.
12. `--vector-weight=1.5` → `typer.BadParameter`.
13. Stale-index hint fires once per CLI invocation, not per
    parsed TDoc (already pinned by the FTS5 spec; this spec
    reuses the same latch).
14. `search index --rebuild-embeddings --resume` after a
    simulated crash → picks up at the cursor.
15. Auto-embed during a parse that fails halfway → no vector row
    created (the embed runs **after** all four FTS5 upserts
    return successfully; the same DB transaction boundary
    protects both indexes).
16. `search sem "which CRs do not relate to NB-IoT"` with
    default `keep_negation_words=["not"]` → stripped query
    keeps `"not"` (and `"relate"`, `"NB-IoT"`, `"CRs"`); the
    FTS5 match then honors the negation. With
    `keep_negation_words=[]` → `"not"` is dropped, the query
    becomes `"relate NB-IoT"`, and the FTS5 match inverts the
    user intent. Test pins both.
17. `user_defined_stop_words=["tdoc"]` → `"tdoc"` (and `"tdocs"`,
    case-insensitive) is dropped from the strip; with the
    default empty list, the same token survives. Test pins
    both.

### Coverage targets

Per the existing suite convention, ≥ 90% line coverage for new
modules. The chunker + RRF + stopword-strip corpora lock down
the three highest-risk pure functions.

## File / symbol summary

| File | Symbols |
|---|---|
| `models/semantic_search.py` (new) | `SemanticSearchHit`, `SemanticSearchError`, `SemanticSearchUnavailableError`, `SemanticSearchQueryError`, `SpacyUnavailableError`, `EmbedderUnavailableError`, `VectorIndexUnavailableError` |
| `repository/protocols.py` (extend) | `Embedder`, `VectorIndexRepository` |
| `services/embedding/chunker.py` (new) | `_chunks`, `CHUNK_SIZE_DEFAULT`, `CHUNK_OVERLAP_DEFAULT` |
| `services/embedding/stopwords.py` (new) | `strip_stopwords`, `_get_spacy_pipeline` (cached loader), `_effective_stopwords` (cached composed set) |
| `services/embedding/embedder.py` (new) | `Embedder` (re-exported from `protocols` for typing), `SentenceTransformerEmbedder` |
| `services/semantic_search_service.py` (new) | `SemanticSearchService`, `rrf_merge` |
| `storage/repositories/vector_sql.py` (new) | `SQLAlchemyVectorIndexRepository`, `_check_sqlite_vec`, `_build_embed_text` |
| `services/factory.py` (extend) | `build_semantic_search_service`; wire into `build_tdoc_cr_service` |
| `services/tdoc_cr_service.py` (modify) | `TDocCrService.__init__` gains `semantic_service`; `_embed_after_parse` private helper; two new call sites (sibling of `_index_after_parse`) |
| `cli.py` (extend) | `search sem` command; `search index --rebuild-embeddings` |
| `settings/schema.py` (extend) | `SemanticSearchSettings` |
| `storage/db/migrate.py` (extend) | `vec_tdoc_embeddings` + `vec_meta` DDL gated on sqlite + sqlite-vec |
| `pyproject.toml` (modify) | New `[semantic]` extra |

## Cross-cutting concerns

| Concern | Strategy |
|---|---|
| `tdocs` row deletion (meeting resync) | Add `semantic_service.remove_for_tdoc(tdoc_id)` next to the existing `search_service.remove_for_tdoc(tdoc_id)` call |
| `tdoc_cr_cover_page` row updated (re-parse same tdoc_id) | FTS5 + vector rows auto-re-upserted by the hooks |
| `tdoc_extracts` row deleted (rare) | Remove FTS5 + vector rows alongside |
| MySQL/Postgres deployment | `VectorIndexUnavailableError`; virtual table never created in `create_schema`; `search sem` reports unavailable |
| `[semantic]` extra toggling mid-session | Auto-embed hook checks `_semantic_service is None` at call time; toggling requires restart, which is fine |
| Concurrent rebuilds | `Lock` on the same FTS5 rebuild lock (or a new sibling lock) prevents two vector rebuilds running at once |
| Model swap | `vec_meta.embedding_dim` mismatch on next upsert → CLI prompts `--rebuild-embeddings` |
| Empty query / stopwords-only | `SemanticSearchQueryError` with a clear message |
| Settings precedence | CLI flag > `[semantic_search]` TOML > default (matches AGENTS.md convention) |
| `search query` (FTS5-only) | Unchanged; unaffected by the new extra |
| `tdoc parse` happy path | Both `_index_after_parse` and `_embed_after_parse` fire after all four FTS5 upserts return successfully |
| Test model load | Opt-in via `-m semantic` marker; default tests use pre-computed embeddings in fixtures |

## Open implementation notes

1. **`sqlite_vec.load()` import path.** The PyPI package is
   `sqlite-vec` (with a hyphen); the import is `import sqlite_vec`
   (with an underscore). Implementation phase verifies.
2. **`en_core_web_sm` download UX.** Originally `build_semantic_search_service`
   did **not** auto-download the spaCy model and the CLI told the
   user to run `python -m spacy download en_core_web_sm`. In
   practice this caused two-step installs (`pip install ...`
   + `python -m spacy download ...`) that confused users, since
   `[semantic]` *seemed* complete after pip but the search path
   still errored at runtime. Updated packaging bundles
   `en_core_web_sm-3.8.0` via a direct wheel URL in
   `pyproject.toml`'s `[semantic]` extras (with
   `[tool.hatch.metadata] allow-direct-references = true`), so a
   single `pip install doc3gpp[semantic]` installs both the
   spaCy library and the model. The CLI still surfaces the
   `python -m spacy download en_core_web_sm` hint as a fallback
   for users on conda or non-pip installs.
3. **`vec_meta.embedding_dim` upgrade story.** The migration
   writes the dim on first `CREATE VIRTUAL TABLE`; on a model
   swap to a different dim, the next embed upsert detects the
   mismatch and the CLI exits 1 with the rebuild hint
   (non-interactive; no `typer.confirm`). `--rebuild-embeddings`
   drops and recreates the table.
4. **Cosine vs L2 distance.** `sqlite-vec` supports both; cosine
   is the right choice for sentence-transformers. The vector
   column is stored as `float32` (matching the model's dtype);
   the KNN query uses `vec_distance_cosine`.
5. **Chunking with very long cover bodies.** A CR with a 50KB
   cover produces ~80 chunks; embedding 80 chunks × 200ms is
   16s of parse latency. `Settings.semantic_search.max_chunks_per_tdoc`
   (default 32) caps the chunk count and records the truncation
   in a `tdoc_embed_progress` sidecar for future resumption.
   (Implementation phase tests with a synthetic 50KB fixture.)

## TL;DR

- New CLI subcommand `doc3gpp search sem QUERY [filters]` for
  natural-language queries. `doc3gpp search query` is unchanged.
- Local `sentence-transformers` embeddings (default
  `all-MiniLM-L6-v2`, 384d, pluggable model name). No hosted
  APIs in v1.
- Vector store: `sqlite-vec` extension on the existing sqlite
  DB. `vec_tdoc_embeddings` is a separate virtual table from
  `tdoc_search`; same `tdoc_id` identity, chunked rows
  (`chunk_id = "{tdoc_id}#{i}"`).
- Per-TDoc chunking: `_chunks(text, size=800, overlap=100)`,
  both configurable. Rerank reduces multiple chunks per TDoc
  to a single distance via `min(distance)`.
- FTS5 path in `search sem` runs the query through spaCy
  `en_core_web_sm` stopword + lemmatization before
  `SearchQueryBuilder` builds the FTS5 expression. The
  embedding path uses the **original** query, not the
  stripped one.
- Reciprocal-rank fusion (RRF) merges the FTS5 and vector
  rankings: `rrf = 1/(k + rank_fts5) * (1 - W) + 1/(k + rank_vec) * W`
  with `k=60`, `W=0.7` (default), `W` exposed as the
  `--vector-weight` CLI flag and `Settings.semantic_search.vector_weight`.
- Internal fan-out: `2N` per side, then truncated to the
  user-supplied `--limit` (default 20). No new top-N flags.
- Auto-embed on every successful parse (sibling hook to
  `_index_after_parse`). Manual rebuild via
  `search index --rebuild-embeddings`.
- Three-layer graceful degradation: build is best-effort, hook
  is best-effort, repo raises typed errors. doc3gpp without
  `[semantic]` behaves exactly like today; `search query`
  is unaffected.
- The FTS5 spec's `PassthroughReranker` stays as the v1 default
  for `search query`; this spec does not consume the reranker
  Protocol (the reranker model is too narrow for chunked
  vectors + RRF). The Protocol is preserved as the v1 hook
  for a future single-vector rerank if one is wanted.
