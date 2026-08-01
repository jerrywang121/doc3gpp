# FTS5 Semantic Rerank Design

**Status:** Draft for review
**Date:** 2026-08-01
**Branch:** main
**Author:** brainstorming session

## Problem

`doc3gpp search query` runs an FTS5 `MATCH` against `tdoc_search`, applies
filter flags, scores with `bm25()`, and renders the top `N` hits. There is
no way to reorder those hits by *semantic* relevance to a user query that
is distinct from the FTS5 expression — users who want "find me TDocs about
TTCN test cases for 5G NR handover" are stuck with whatever FTS5 `MATCH`
order produced. The existing `--rerank` flag is a documented future hook
that today is a no-op (`PassthroughReranker` returns hits unchanged) and
proposes to reuse the FTS5 query string as the rerank signal, which is
the wrong shape of input for an embedding model.

`doc3gpp search sem` exists for vector-first hybrid search, but it
expects the user to want vector or fused RRF results, not to refine an
already-narrow FTS5 hit list. The two commands solve different problems.

This spec adds a small, opt-in semantic rerank path on `search query`:
the FTS5 path fetches a wider candidate set, an embedding-based reranker
reorders it, and the top `N` survives. The rerank signal is a *separate*
string the user supplies — distinct from the FTS5 expression — so the
operator can write a precise FTS5 query (`tdoc_id:R5-23* AND tsg:SA2`)
and a natural-language intent (`discussions of network slicing
architecture`).

## Goals

1. Let `search query` reorder its FTS5 hits by cosine similarity to a
   user-supplied semantic string.
2. Keep FTS5 as the candidate selector (precision over recall, plus all
   the existing filter flags keep working unchanged).
3. Bound the rerank budget via a config knob; never encode per-candidate
   content at query time.
4. Make the embedding model the only signal that matters when
   `--sem-query` is set — drop the existing `--rerank` flag.

## Non-Goals

- Replacing `search sem`. The vector-first hybrid path stays as-is.
- Re-encoding per-candidate content. Scoring is always against
  `vec_tdoc_embeddings` rows that already exist.
- Per-candidate tuning of the floor score, the fanout factor, or the
  reranker's own model. All knobs live in `doc3gpp.toml`.
- New CLI command. The change is flag-only on the existing
  `search query` command.

## Design

### CLI surface

`doc3gpp search query "FTS5_EXPR" --sem-query "SEM_STR" [--limit N]
[--tsg ...] [--meeting ...] [--release ...] [--spec ...]
[--since ...] [--until ...] [--format ...] [--compact ...]
[--snippet-tokens ...] [--explain] [--quiet]`

- `--sem-query` (`str | None`, default `None`): a natural-language
  string embedded once and used to score every FTS5 candidate by
  cosine distance. When `None`, the command runs as today (FTS5
  only, no rerank).
- `--rerank`: **removed.** The flag is a no-op today and would
  conflict with the new design (it would reuse the FTS5 expression as
  the rerank input). The CLI raises `BadParameter` with the message
  `"--rerank was removed; use --sem-query to enable semantic rerank"`.

The two flags are mutually exclusive by construction — only one exists.

**Terminology:** throughout this spec, *user limit* means the value of
`--limit` (default 20) — the number of hits the CLI renders. *Fanout*
means the internal cap the FTS5 repo sees, equal to
`user_limit * search_fanout_factor` (default `4 * user_limit`). The
FTS5 repo is asked for `fanout` hits; the reranker is then responsible
for truncating to `user_limit`.

### Config

`[search]` in `doc3gpp.toml` gains one new field:

```toml
[search]
# When `search query --sem-query` is used, the FTS5 path fetches
# limit * search_fanout_factor candidates before the semantic
# reranker truncates back to limit. Higher values give the reranker
# more to work with at the cost of more vector lookups per query.
# Only honored when --sem-query is supplied. Default 4. Range 1..64.
search_fanout_factor = 4
```

Implementation: `Settings.search.search_fanout_factor: int = Field(default=4, ge=1, le=64)`
in `src/doc3gpp/settings/schema.py:SearchSettings`. The field is TOML-only
(no env override), matching every other nested `[search]` knob.

### Data flow

```
search query "FTS5_EXPR" --sem-query "SEM_STR" --limit N
        |
        v
  build_search_service()                            [factory.py]
        |  -- chooses reranker:
        |     [search].enabled && [semantic_search].enabled
        |       -> SemanticReranker(embedder, vector_repo, settings)
        |     else
        |       -> PassthroughReranker()
        v
  search_command()                                   [cli.py]
        |  1) fanout = limit * settings.search.search_fanout_factor
        |  2) filters = SearchFilters(..., limit=fanout)
        |  3) raw_hits = svc._repo.search(FTS5_EXPR, filters, snippet_tokens)
        |  4) reranked = svc._reranker.rerank(
        |                  semantic_query=SEM_STR,
        |                  hits=raw_hits,
        |                  final_limit=limit,
        |              )
        |  5) _render_search_hits(reranked, format, compact)
        v
  list[SearchHit]   (semantic order, length = min(N, len(raw_hits)))
```

When `--sem-query` is `None`, the CLI drops step 4: `raw_hits` is the
final result, the reranker is never invoked.

### `SemanticReranker`

New file `src/doc3gpp/services/semantic_reranker.py`:

```python
class SemanticReranker(EmbeddingReranker):
    """Rerank FTS5 hits by cosine similarity to a user-supplied query.

    Implements the EmbeddingReranker Protocol. The FTS5 path is
    responsible for producing a candidate bag; this class only
    re-orders and truncates that bag.

    Scoring source: vec_tdoc_embeddings. For each candidate tdoc_id
    we look up the chunk with the minimum cosine distance to the
    query vector. A tdoc with no vector rows gets MISSING_FLOOR
    (sorts strictly below every real score).
    """

    MISSING_FLOOR: float = float("-inf")

    def __init__(
        self,
        embedder: Embedder,
        vector_repo: VectorIndexRepository,
        settings: Settings,
    ) -> None: ...

    def rerank(
        self,
        semantic_query: str,
        hits: list[SearchHit],
        final_limit: int | None = None,
    ) -> list[SearchHit]:
        # 1. Empty input -> empty output, no embedder call.
        if not hits:
            return []
        # 2. One embedder call for the whole batch.
        query_vec = self._embedder.encode([semantic_query])[0]
        # 3. Batched lookup of closest chunk per tdoc_id.
        scores = self._vector_repo.get_min_distance_for_tdocs(
            [h.tdoc_id for h in hits], query_vec,
        )
        # 4. Stable sort by score desc; missing -> MISSING_FLOOR.
        decorated = [
            (scores.get(h.tdoc_id, (self.MISSING_FLOOR, None))[0], i, h)
            for i, h in enumerate(hits)
        ]
        decorated.sort(key=lambda t: (t[0], -t[1]), reverse=True)
        # 5. Truncate.
        ordered = [h for _, _, h in decorated]
        if final_limit is not None:
            return ordered[:final_limit]
        return ordered
```

The `MISSING_FLOOR` is `float("-inf")` and lives as a class constant so
it can be referenced in tests. Any real `min_chunk_distance` is `>= 0`
in the cosine-distance model, so `MISSING_FLOOR` is strictly below every
real score by construction.

### `EmbeddingReranker` Protocol change

`src/doc3gpp/repository/protocols.py:EmbeddingReranker.rerank` gains a
new kwarg:

```python
def rerank(
    self,
    semantic_query: str,
    hits: list[SearchHit],
    final_limit: int | None = None,
) -> list[SearchHit]: ...
```

Three changes from the v1 signature:

1. `query: str` is renamed to `semantic_query: str` to clarify that this
   is the *embedding* input, not an FTS5 expression. The old name
   invited the same confusion `--rerank` already suffered.
2. `final_limit: int | None = None` lets the same impl truncate when
   the caller asked for fewer than the candidate bag. `PassthroughReranker`
   honors it (slice `hits[:final_limit]`) so the no-semantic-search
   path still works.
3. The docstring updates the example to "SemanticReranker encodes
   `semantic_query`, looks up each candidate's closest chunk in
   `vec_tdoc_embeddings`, sorts by `-min_distance` desc, truncates to
   `final_limit`."

`PassthroughReranker` in `src/doc3gpp/services/search_service.py` is
updated to accept the new signature; its body becomes
`return list(hits) if final_limit is None else list(hits)[:final_limit]`.

### `VectorIndexRepository` Protocol change

`src/doc3gpp/repository/protocols.py:VectorIndexRepository` gains:

```python
def get_min_distance_for_tdocs(
    self,
    tdoc_ids: Sequence[str],
    query_vec: Sequence[float],
) -> dict[str, tuple[float, str] | None]:
    """For each tdoc_id, return (min_distance, best_chunk_id) or None.

    A single batched SQL trip. The implementation uses sqlite-vec's
    KNN function (or its nearest equivalent) to find the row in
    vec_tdoc_embeddings with the lowest distance to query_vec for
    each tdoc_id. Returns a dict keyed by tdoc_id; missing tdoc_ids
    (no vector row) map to None.
    """
    ...
```

The implementation in
`src/doc3gpp/storage/repositories/vector_sql.py:SQLAlchemyVectorIndexRepository`
performs one batched query that joins `vec_tdoc_embeddings` against an
inline KNN per tdoc, or — depending on the sqlite-vec API — one
`SELECT tdoc_id, chunk_id, MIN(...) ... WHERE tdoc_id IN (...) ...`
group-by query. The exact SQL is captured in the implementation plan;
the contract here is just "one round trip, missing = None".

### Factory wiring

`src/doc3gpp/services/factory.py:build_search_service` chooses the
reranker:

```python
if reranker is None:
    if (
        settings.search.enabled
        and settings.semantic_search.enabled
    ):
        try:
            embedder = SentenceTransformerEmbedder(
                settings.semantic_search.embedding_model,
            )
            vector_repo = SQLAlchemyVectorIndexRepository()
            reranker = SemanticReranker(
                embedder=embedder, vector_repo=vector_repo,
                settings=settings,
            )
        except (VectorIndexUnavailableError, EmbedderUnavailableError):
            reranker = PassthroughReranker()
    else:
        reranker = PassthroughReranker()
```

The two existing `try/except` shapes from
`build_semantic_search_service` are reused so the same extra-missing
failure modes are swallowed consistently. The embedder is constructed
once at factory time (lazy model load — the first real `encode()` call
inside `rerank` is what triggers the heavy load); the embedder object
is shared between `SemanticReranker` and any other consumer that
`build_semantic_search_service` instantiates, to avoid loading the
model twice per process. Both factory functions reuse the same
`SentenceTransformerEmbedder` instance when both are built in the
same process.

### Error & edge handling

| Condition | Behavior |
|---|---|
| FTS5 returns 0 hits with `--sem-query` | Return `[]`, no embedding, no warning. Same as plain FTS5 today. |
| FTS5 returns < `4N` hits (small corpus) | Rerank over what came back; truncate to `min(N, len(hits))`. |
| `--sem-query` on a build without `[semantic]` extra | `build_search_service` returns a `SearchService` with `PassthroughReranker`. CLI prints `search sem rerank unavailable; run \`pip install doc3gpp[semantic]\`` to stderr and exits 1. Same UX as today's `search sem` without the extra. |
| Vector index empty (no embeddings ever built) | All candidates score `MISSING_FLOOR`. Output is the FTS5 order truncated to `N`. One-shot `logger.warning` in non-`--quiet` mode: `"semantic rerank: no rows in vec_tdoc_embeddings; falling back to FTS5 order"`. |
| `search_fanout_factor=1` | Fanout == user limit; rerank can only resolve ties. Documented, not prevented. |
| A rerank candidate has no `tdoc_id` | Impossible by construction — `SearchHit.tdoc_id` is required and set by the FTS5 repo. Defensive `KeyError` if it ever happens. |
| `--limit=0` | Fanout is `0 * 4 = 0`; repo returns 0 hits; rerank is a no-op; output is empty. Same as today's `--limit=0` behavior. |
| `--rerank` passed by an old caller | `typer.BadParameter` with the migration message. |
| `--sem-query=""` (empty string) | Treated as `None` — the FTS5 path runs without rerank. The empty string is a programming mistake; a silent fallback is friendlier than an embedder call on a zero-length input. |
| `--sem-query` + `--explain` | `--explain` continues to print the resolved FTS5 `MATCH` and the SQL plan. The semantic rerank runs after `--explain`'s preflight dump, so the explain output reflects the FTS5 side only. A future enhancement could append a rerank summary; out of scope for v1. |

### Data model impact

No schema changes. `vec_tdoc_embeddings` is read-only here. `tdoc_search`
is read-only. The new field on `SearchSettings` is a config field only.

### Performance

Per `--sem-query` query, on a 20-row limit:

- One `SentenceTransformerEmbedder.encode([query])` call. With a warm
  MiniLM model this is ~10ms; cold load is ~1s and shared with the
  rest of the process.
- One batched SQL trip in
  `vector_repo.get_min_distance_for_tdocs([80 tdoc_ids], query_vec)`.
  Expected < 50ms on the existing `vec_tdoc_embeddings` index.
- 80 small Python comparisons in the rerank layer. Negligible.

Total wall-time impact on a warm model: ~50-100ms per reranked query.
On a cold model: ~1s for the first query, ~50-100ms thereafter.

### Migration & compatibility

- `--rerank` is removed. Today it is a no-op (`PassthroughReranker`),
  so no real user has working code on top of it. The CLI prints a
  clear `BadParameter` pointing at `--sem-query` for any caller that
  tries.
- The `EmbeddingReranker` Protocol signature change (rename `query` →
  `semantic_query`, add `final_limit`) is internal. The only direct
  caller of `svc._reranker.rerank` in the codebase is `search_command`,
  and `PassthroughReranker.rerank` is the only other implementation.
  Both are updated in the same change set.
- `SearchService.search`'s public signature is unchanged. The fanout
  is resolved by the CLI when it builds the `SearchFilters.limit`
  it hands to the repo; the service remains a thin orchestrator.

## Testing

### Unit (no network, no live model)

`tests/unit/test_semantic_reranker.py`:

- Empty input → empty output, zero embedder calls.
- Single embedder call regardless of hit-list length.
- Stable sort: identical scores preserve input order.
- `final_limit=None` → return full list.
- `final_limit=N` → truncate to first `N`.
- Missing vector row → candidate sorts below every real score.
- All candidates missing → output is the input order (FTS5 order
  preserved), still truncated by `final_limit`.

`tests/unit/test_search_settings.py`:

- `search_fanout_factor` default is 4.
- Range validator: 0 and 65 raise `ValueError`; 1 and 64 pass.

`tests/unit/test_cli_search_query.py`:

- `--sem-query` absent → no rerank call, `svc._reranker.rerank` not
  invoked.
- `--sem-query` set → `svc._reranker.rerank(semantic_query=..., hits=raw_hits, final_limit=limit)` invoked; `raw_hits` has `limit * search_fanout_factor` length cap on the `SearchFilters` it was built from.
- `--rerank` → `typer.BadParameter`.

`tests/unit/test_search_service.py`:

- `PassthroughReranker.rerank(semantic_query, hits)` returns `list(hits)`.
- `PassthroughReranker.rerank(semantic_query, hits, final_limit=5)` returns `list(hits)[:5]`.

### Integration (sqlite + mocked embedder, no live network)

`tests/integration/test_search_query_sem_rerank.py`:

- Seed `tdocs`, `tdoc_search`, `vec_tdoc_embeddings`. Run
  `search query --sem-query "..."`. Assert: output order matches the
  expected cosine ranking, output length equals `--limit`, embedder
  called exactly once for the semantic query.
- Variant: `vec_tdoc_embeddings` empty. Output is the FTS5 order,
  one-shot `logger.warning` fires, exit 0.
- Variant: `search_fanout_factor=1`. Output is
  `min(N, fts5_hits_count)`, embedder still called once.
- Variant: FTS5 returns 0 hits. Output is empty, embedder *not*
  called.
- Variant: `--sem-query=""`. Output is the FTS5 order, embedder not
  called.

### Manual

- `doc3gpp config init --force` writes the new `[search]` block with
  the new field.
- `doc3gpp config set search.search_fanout_factor 8` updates and
  persists the value.
- `doc3gpp tdoc parse` (the auto-index path) is unaffected.
- `doc3gpp search sem` is unaffected.

## Documentation

- `docs/cli.md` — `search query` section: new `--sem-query` flag,
  removal of `--rerank`, the new `[search].search_fanout_factor`
  knob, the empty-vector fallback warning.
- `doc3gpp.toml.example` — `[search]` block gains the new field with
  its default and a comment.
- `AGENTS.md` — "Where to look" table: add a row for
  `Add a search rerank flag / knob` pointing at
  `src/doc3gpp/services/semantic_reranker.py` +
  `src/doc3gpp/settings/schema.py` (`SearchSettings`) +
  `src/doc3gpp/cli.py` (`search_command`). The existing `Tune the FTS5
  search subsystem` row absorbs the new knob.
- `docs/conventions.md` — add a note that `--sem-query` is a
  string-only flag (not a flag-with-value) and that
  `search_fanout_factor` is the user-facing name for the
  `Settings.search.search_fanout_factor` field.

## Out of scope

- Changing `search sem` in any way.
- Re-encoding per-candidate content.
- A new `RerankService` Protocol or class.
- Per-TDoc or per-call floor-score overrides.
- Per-call rerank model overrides.
- Streaming / progress reporting for the rerank step.
- A new `--rerank-mode` (cross-encoder, BM25, etc.) flag.

## Open questions

None at design time. Resolved during brainstorming:

1. `--sem-query` vs `--rerank` interaction: `--sem-query` is the only
   trigger; `--rerank` is removed.
2. Where `search_fanout_factor` lives: `[search]` block, default 4.
3. Empty FTS5 result with `--sem-query`: return FTS5 empty result
   unchanged.
4. Rerank location: new `SemanticReranker` implementing the existing
   `EmbeddingReranker` Protocol.
5. Candidate scoring source: vector-only, missing candidates get
   `MISSING_FLOOR`.
