# Semantic Search — Design Revision

**Status:** Revision of [`2026-07-31-embedding-search-design.md`](2026-07-31-embedding-search-design.md)
**Date:** 2026-08-01
**Branch:** `feat/embedding-search`
**Author:** brainstorming session
**Builds on:** [`2026-07-31-embedding-search-design.md`](2026-07-31-embedding-search-design.md)

## Goal

Redesign `doc3gpp search sem QUERY` so the natural-language query
flows **only** to the vector path by default. The FTS5 keyword
match becomes opt-in via a new `--fts5-query` flag and is
processed exactly like `doc3gpp search query` (no spaCy stopword
strip, no lemmatization — the `SearchQueryBuilder` does its usual
work). Rename the blend flag from `--vector-weight` to
`--fts5-weight` and change its default from `0.7` to `0.5`. When
`--fts5-query` is omitted, the FTS5 path is skipped entirely
(no RRF, no FTS5 fan-out); only the embedding KNN results are
returned, truncated to `--limit`.

With FTS5 no longer receiving the natural-language input, the
spaCy preprocessor is unused. spaCy is dropped from the
dependency surface entirely: the `[semantic]` pyproject extra
loses `spacy` and the model-download hint; the
`services/embedding/stopwords.py` module and the
`SpacyUnavailableError` class are removed; their tests are
deleted.

This revision is the **default-now semantic-only, opt-in FTS5**
shape. The previous design was the **default-both-fused,
opt-out nothing** shape; the rationale for changing is at the
top of "Design" below.

### Why this revision

The previous spec's "natural-language query goes to both paths"
design assumed the spaCy preprocessor was a good FTS5 query
producer. In practice the stopword-strip + lemmatization
pipeline loses negation, scrubs terminology (`"NB-IoT"`,
`"38.300"` survive only via the post-strip builder fixups), and
hides from the user what FTS5 is actually being asked to match.
The two paths are conceptually independent: the vector path
wants prose; the FTS5 path wants verbatim terms. Forcing one
string through both was a leaky abstraction. The new shape
honors that — users who want FTS5 supply an FTS5-shaped string;
users who want vectors supply a prose-shaped string; users who
want both supply both.

The blend-flag rename (`vector_weight` → `fts5_weight`) tracks
the same conceptual shift: with both paths opt-in, "how much do
I weight the FTS5 hits?" is the cleaner user mental model than
"how much do I weight the vector hits?", especially now that
1.0 means "FTS5 only" and 0.0 means "vector only" instead of the
old 1.0 = "vector only". The default flips to `0.5` because the
prior `0.7` (vector-dominant) only made sense when vectors were
the only usable path for natural language; with FTS5 off by
default the previous bias is no longer warranted, and a balanced
default is the safe choice for the common case where both paths
are enabled.

The spaCy dependency drop is a direct consequence: with the
FTS5 path now bypassing the stripper pipeline, nothing in the
running code calls `strip_stopwords`. Keeping the dependency
and the module as dead code is dead weight.

## Non-goals

Unchanged from the prior spec:

- Hosted embedding APIs (OpenAI, Voyage, Cohere, Ollama).
  Local `sentence-transformers` only.
- MCP server transport.
- Web UI / search box.
- 3GPP-domain keyword extraction / NER.
- MySQL / Postgres backend.
- GPU acceleration.
- Background embedding worker.
- Auto-embedding on parse.
- Auto-embedding rebuild after re-parse.
- Replacing the FTS5 path. `search query` stays as-is; this
  spec only narrows what `search sem` does.

Newly dropped from scope:

- **spaCy stopword strip + lemmatization for `search sem`.**
  The stripper pipeline is no longer called from the search
  service; the module is removed. spaCy-stripped queries have
  no other caller.
- **`SpacyUnavailableError`** — its only site was the `search
  sem` stripper call.
- **`user_defined_stop_words` / `keep_negation_words` settings.**
  They were inputs to the stripper; with the stripper gone they
  have no semantics. The TOML keys are dropped, not aliased.

## Architecture

The sibling-subsystem shape is preserved; only the read path
is restructured.

```
CLI subcommand
  │
  ├── search query QUERY [filters]    ──► SearchService ──► SearchIndexRepository
  │                                      (unchanged)            │
  │                                                           └─► FTS5 virtual table
  │
  └── search sem QUERY [--fts5-query F] [filters]
        │
        ▼
    SemanticSearchService.read(query, fts5_query, filters, limit, fts5_weight)
        │
        ├─ Always:  embedder.encode([query]) → vec KNN        (SemanticSearchService → VectorIndexRepository)
        │
        └─ If fts5_query is not None:
              SearchQueryBuilder(fts5_query).build()
              SearchService.search(match_expr, filters)         (FTS5 fan-out)
                       │
                       ▼
              rrf_merge(fts5_hits, vec_hits,
                        vector_weight = 1 - fts5_weight,
                        limit)
              truncate to --limit
        │
        └─ If fts5_query is None:
              skip FTS5 + skip RRF
              return top --limit vec hits dressed as SemanticSearchHit
                          (fts5_hit synthesized, rank_fts5=None)
```

The auto-embed-on-parse wiring (`TDocCrService._embed_after_parse`),
the `rebuild_embeddings` path, the `[search]` and
`[semantic_search]` TOML blocks, and the `vec_tdoc_embeddings`
DDL are unchanged.

### Modules affected

| File | Action |
|---|---|
| `models/semantic_search.py` | Remove `SpacyUnavailableError`; the rest (`SemanticSearchHit`, the other errors) is unchanged. |
| `services/embedding/stopwords.py` | Delete. |
| `services/semantic_search_service.py` | `search()` signature change; `strip_stopwords` call removed; vector-only branch added. |
| `cli.py` (extend) | `sem_command` — add `--fts5-query`, rename `--vector-weight` to `--fts5-weight`, drop `SpacyUnavailableError` branch, update `--explain` block. |
| `settings/schema.py` (extend) | `SemanticSearchSettings.vector_weight` → `fts5_weight` (default `0.5`); drop `user_defined_stop_words` and `keep_negation_words`. |
| `data/doc3gpp.toml.example` | Re-key `vector_weight` → `fts5_weight`; drop `user_defined_stop_words` and `keep_negation_words` keys. |
| `pyproject.toml` | Drop `spacy>=3.7.0` from the `[semantic]` extra; drop the model-download hint in the extra description. |
| `tests/unit/test_stopwords.py` | Delete. |
| `tests/unit/test_cli_search_sem.py` | Rename flag references; delete spaCy-related cases. |
| `tests/unit/test_semantic_search_service.py` | Update `search(...)` calls; add vector-only-path cases. |
| `tests/unit/test_rrf.py` | No change (RRF math is unchanged; `vector_weight` parameter stays). |
| `tests/unit/test_semantic_settings.py` | Rename `vector_weight` references; drop stopword-list cases. |
| `tests/integration/test_search_sem_end_to_end.py` | Update `vector_weight=0.7` references; add "FTS5 omitted → pure vector" integration test. |

### Reused, unchanged

- `SearchService.search(match_expr, filters)` — its `match_expr`
  parameter was always a built FTS5 expression. We now build it
  from the **explicit `--fts5-query` string** using
  `SearchQueryBuilder`, exactly as `search query` does.
- `SearchQueryBuilder` — the FTS5 expression builder from the
  FTS5 spec. Its `SearchQueryError` still surfaces bad FTS5
  syntax; the CLI catches it and exits 2.
- `rrf_merge(...)` — the math and the `vector_weight`
  parameter name stay as-is; we just compute `vector_weight =
  1 - fts5_weight` at the call site so the inverse-flip is
  local to the service.
- `TDocCrService._embed_after_parse` — unchanged. Keeps firing
  on every successful parse regardless of whether FTS5 ran at
  query time. Embedding is independent of FTS5 fan-out.
- `VectorIndexRepository` protocol + the sqlite-vec impl —
  unchanged.
- `SQLAlchemySearchIndexRepository`, the FTS5 bm25 weights,
  the snippet machinery — all unchanged.
- Filter grammar (`cli_filters.py`) — unchanged.

### Data flow — `search sem QUERY --fts5-query "..." [filters]`

```
"what CRs touch NB-IoT power saving"  (raw natural-language)
        │
        ├──► embedder.encode([query])               (semantic path)
        │            │
        │            ▼
        │      vector KNN, top (limit * fanout_multiplier)
        │            │
        │            ▼
        │      list[(tdoc_id, chunk_id, chunk_index, distance)]
        │
        └──► if fts5_query is not None:
                "tsg:RP spec:38.300"  (raw --fts5-query, untokenized-as-prose)
                       │
                       ▼
                SearchQueryBuilder(fts5_query).build()
                       │
                       ▼
                SearchService.search(match_expr, filters)[:internal_limit]
                       │
                       ▼
                list[SearchHit]
                       │
                       ▼
                rrf_merge(fts5_hits, vec_hits,
                          vector_weight = 1 - fts5_weight,
                          limit = limit)
                       │
                       ▼
                list[SemanticSearchHit]   (top --limit)
```

### Data flow — `search sem QUERY [filters]`  (no `--fts5-query`)

```
"what CRs touch NB-IoT power saving"  (raw natural-language)
        │
        ├──► embedder.encode([query])
        │            │
        │            ▼
        │      vector KNN, top limit (no internal fan-out)
        │            │
        │            ▼
        │      for each (tdoc_id, chunk_id, chunk_index, distance):
        │          synthesize SemanticSearchHit(
        │              tdoc_id, rrf_score = -distance,    (rank-by-distance)
        │              fts5_hit = _build_fts5_stub(...),  (metadata JOIN)
        │              rank_fts5 = None,
        │              rank_vec = i,
        │              min_chunk_distance = distance,
        │              best_chunk_id = chunk_id,
        │          )
        │      sort ascending by distance
        │      truncate to limit
        │
        └──► list[SemanticSearchHit]   (pure vector, no RRF)
```

### Why no RRF when FTS5 is skipped

RRF is fundamentally a blend across **two ranked lists**. With
only one list there is nothing to blend. The implementation
choice is one of three:

1. **Use RRF with a zero-shaped empty FTS5 list** → identical
   math (every TDoc's FTS5 rank is `None`, so the FTS5 term
   contributes `0`); the RRF collapses to a pure inverse-rank
   of the vector list. Functionally correct, structurally
   dishonest — the DTO still carries `rank_fts5=None` for every
   hit, which the renderer can't distinguish from "FTS5 ran but
   didn't find this hit".
2. **Add a `source: "fts5" | "vector" | "hybrid"` field to
   `SemanticSearchHit`** to make the path explicit. Better
   honesty but requires renderer branching and a new round-trip
   through the CLI's table / json / markdown output paths.
3. **Skip RRF entirely; rank by raw distance and synthesize
   `SemanticSearchHit` directly**. The DTO shape stays
   uniform; only `rank_fts5` and the `fts5_hit` sub-record tell
   the story.

This spec picks option **3**. The renderer doesn't need a new
field; the user already opted out of FTS5 by omitting
`--fts5-query`. The `fts5_hit` sub-record is still synthesized
from the `tdocs` / `meetings` JOIN so the table output stays
the same shape.

The `rrf_score` value in this branch is `-distance`. That lets
the existing renderer's `f"{h.rrf_score:>8.4f}"` formatting work
without a special-case branch — negative cosine distance
(in `[−1, 0]` for normalized vectors, where 0 = identical) is
monotonically related to ranking. The header `rrf` is now
slightly misleading in the pure-vector path; the spec accepts
this in exchange for renderer uniformity. The `--explain`
block makes the path explicit so operators don't have to
guess.

### DTO implications

`SemanticSearchHit` (`models/semantic_search.py`) gains nothing
new. `rrf_score` is repurposed in the vector-only path to
`−min_chunk_distance` (see above); the field name is preserved
for renderer uniformity. `rank_fts5=None` continues to signal
"this hit was not in the FTS5 fan-out", which is true under
both "FTS5 ran and missed" and "FTS5 was skipped entirely"; we
rely on `--explain` + the absence of the `--fts5-query` flag
on the user's command line to disambiguate.

### Error handling

`SpacyUnavailableError` is removed. Its only site was the
stripper call inside `SemanticSearchService.search()`, which is
itself removed.

The remaining error surface for `search sem`:

| Error | Trigger | CLI message | Exit |
|---|---|---|---|
| `SearchQueryError` | `--fts5-query` failed `SearchQueryBuilder.build()` (bad FTS5 syntax) | `bad query: <reason>` | 2 |
| `SemanticSearchQueryError` | Empty after `SearchQueryBuilder.build()` (e.g. `--fts5-query "the and of"`) | `bad query: <reason>` | 2 |
| `EmbedderUnavailableError` | sentence-transformers model load failed | `embedding model load failed: <reason>` | 1 |
| `VectorIndexUnavailableError` | sqlite-vec missing or vector index unwritable | `vector index unavailable: <reason>` + `pip install doc3gpp[semantic]` | 1 |
| `SemanticSearchUnavailableError` | Stack unavailable for any other reason | `search sem unavailable: <reason>` | 1 |

## CLI surface

### `doc3gpp search sem QUERY [--fts5-query FTS5_QUERY] [filters]`

```
doc3gpp search sem "what CRs touch NB-IoT power saving"
                   [--fts5-query "tsg:RP spec:38.300"]
                   [--fts5-weight 0.5]
                   [--limit 20]
                   [--format table|json|markdown]
                   [--compact]
                   [--explain]
                   [--quiet]
                   [--tsg ...] [--meeting ...] [--meeting-id ...]
                   [--tdoc-id ...] [--release ...] [--spec ...]
                   [--since ...] [--until ...]
```

Flags:

| Flag | Type | Default | Effect |
|---|---|---|---|
| `QUERY` (positional, required) | string | — | Natural-language input. Always embedded; **not** preprocessed for FTS5. |
| `--fts5-query` | string | None | Optional FTS5 expression. When omitted, the FTS5 path is skipped entirely; only embedding-KNN results are returned. When supplied, passed verbatim to `SearchQueryBuilder` (same semantics as `doc3gpp search query QUERY`); bad syntax or empty-after-build raises `SearchQueryError` / `SemanticSearchQueryError`. |
| `--fts5-weight` | float | 0.5 | Blend weight for the FTS5 rank in RRF (0.0..1.0). `1 - fts5_weight` is the vector weight. `fts5_weight=0.0` → pure vector; `fts5_weight=1.0` → pure FTS5 (vector candidates without an FTS5 hit are dropped). Ignored when `--fts5-query` is omitted. |
| (filter flags) | — | — | Identical to the prior spec (--tsg, --meeting, --meeting-id, --tdoc-id, --release, --spec, --since, --until). |
| `--limit` | int | 20 | Final result count. |
| `--format` | choice | table | table \| json \| markdown |
| `--compact` | flag | False | Strip markdown / json decorators. |
| `--explain` | flag | False | Print the resolved FTS5 MATCH (when applicable), `fts5_weight` / `vector_weight`, `rrf_k`, `fanout_multiplier`, and the best chunk per hit. |
| `--quiet` | flag | False | Suppress stale-index hint. |

Removed flag:

| Flag | Reason |
|---|---|
| `--vector-weight` | Renamed to `--fts5-weight`. Default flipped from 0.7 to 0.5 (see "Why this revision"). |

Removed CLI behavior:

- The "spaCy-stopword-stripped query → FTS5" pipeline no
  longer runs. The user's `QUERY` does not touch the FTS5
  path. There is no `--keep-negation-words` /
  `--user-stop-words` flag (those settings are dropped).

### `doc3gpp search index`

Unchanged from the prior spec. `search index --rebuild` rebuilds
FTS5; `--rebuild-embeddings` rebuilds the vector table;
`--rebuild-all` does both.

## Settings additions

```python
class SemanticSearchSettings(BaseModel):
    enabled: bool = True
    auto_embed_on_parse: bool = True
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    chunk_size: int = 800
    chunk_overlap: int = 100
    rrf_k: int = 60
    fts5_weight: float = 0.5            # was vector_weight: 0.7
    fanout_multiplier: int = 2
    final_limit: int = 20
    # DROPPED: user_defined_stop_words (no stripper)
    # DROPPED: keep_negation_words  (no stripper)
    max_chunks_per_tdoc: int = 32
```

CLI overrides per call:

| Setting | CLI flag |
|---|---|
| `fts5_weight` | `--fts5-weight` |
| `final_limit` (per-path base) | `--limit` (terminal truncation) |

The `fts5_weight` validator is unchanged: `0.0 <= fts5_weight <= 1.0`.
Pydantic surface key is `fts5_weight`; the TOML key is also
`fts5_weight`. There is **no** alias from `vector_weight` —
manual TOML migration is required. The CLI error for an
unknown TOML key points the user at the rename.

## `pyproject.toml` changes

```diff
 [project.optional-dependencies]
 semantic = [
   "sentence-transformers>=2.7.0",
-  "spacy>=3.7.0",
   "sqlite-vec>=0.1.0",
 ]
-# `python -m spacy download en_core_web_sm` is a one-time op
-# documented in the [semantic] extra's description below.
+ # The [semantic] extra now bundles only the sentence-transformers
+ # model and the sqlite-vec extension. There is no spaCy model
+ # download — the FTS5 path runs the explicit --fts5-query string
+ # through SearchQueryBuilder without stopword stripping.
```

The bundled spaCy wheel URL the previous design introduced
becomes dead — removed.

## Test plan changes

### Tests added

| File | New case |
|---|---|
| `tests/unit/test_cli_search_sem.py` | `--fts5-query` parsed; `--fts5-weight` parsed; absent `--fts5-query` does not surface `SearchQueryError`. |
| `tests/unit/test_semantic_search_service.py` | "FTS5 skipped" branch: `search(query, fts5_query=None, ...)` calls the vector repo with `limit = --limit` (no internal fan-out), synthesizes `SemanticSearchHit` with `rank_fts5=None`, and does not call `fts5_service.search`. |
| `tests/unit/test_semantic_search_service.py` | "FTS5 skipped + vector-only KNN" branch: vector KNN returns `[]` → service returns `[]` (no RRF, no exception). |
| `tests/unit/test_semantic_search_service.py` | `--fts5-query` runs through `SearchQueryBuilder` (mock builder called once with the raw FTS5 string, output passed verbatim to `fts5_service.search`). |
| `tests/unit/test_semantic_search_service.py` | `fts5_weight=1.0` → `vector_weight=0.0` at the `rrf_merge` call site (pin the arithmetic flip). |
| `tests/integration/test_search_sem_end_to_end.py` | `search sem "natural question"` with no `--fts5-query` returns pure-vector results ranked by cosine distance. |
| `tests/integration/test_search_sem_end_to_end.py` | `search sem "natural question" --fts5-query "verbatim fts5 terms"` returns RRF-merged results; fts5_hits and vec_hits are visible in the per-hit `rank_*` fields. |

### Tests deleted

| File | Reason |
|---|---|
| `tests/unit/test_stopwords.py` | spaCy stripper is removed. |
| `tests/unit/test_cli_search_sem.py::test_*spacy_*` | spaCy error-mapping case no longer exists. |
| `tests/unit/test_semantic_settings.py::test_user_defined_stop_words*` | Setting is gone. |
| `tests/unit/test_semantic_settings.py::test_keep_negation_words*` | Setting is gone. |

### Tests updated

| File | Edit |
|---|---|
| `tests/unit/test_cli_search_sem.py::test_search_sem_rejects_vector_weight_out_of_range` | Renamed to `test_search_sem_rejects_fts5_weight_out_of_range`; flag renamed to `--fts5-weight`; default changed to 0.5. |
| `tests/unit/test_rrf.py` | No change. `rrf_merge` keeps `vector_weight` as the RRF-side parameter; the CLI/service layer converts from `fts5_weight`. |
| `tests/unit/test_semantic_settings.py` | `vector_weight` references renamed to `fts5_weight`; default-changed to 0.5. |
| `tests/unit/test_semantic_search_service.py` | All `vector_weight=` arguments renamed to `fts5_weight=`; `search(...)` calls updated to match the new `(query, fts5_query, filters, limit, fts5_weight)` signature. |
| `tests/integration/test_search_sem_end_to_end.py` | Rename `vector_weight` references; the pure-FTS5 (`vector_weight=0.0`) case becomes `fts5_weight=1.0` (this is now literally a vector-weight-of-zero RRF). |

### Edge cases pinned in tests

Carried forward from the prior spec, with renames applied:

1. `search sem` with empty QUERY → `EmbedderUnavailableError` or
   no-result depending on the model; the empty-query guard now
   lives at the embedder level, not in a `strip_stopwords` step.
   **New**: `--fts5-query ""` → `SemanticSearchQueryError` from
   `SearchQueryBuilder`.
2. `search sem` with stopword-only `--fts5-query "the and of"`
   (if provided explicitly) → `SemanticSearchQueryError`. The
   stripper is no longer in the loop, so "the / and / of" are
   not pre-removed before the builder sees them — but
   `SearchQueryBuilder` still rejects queries that have no
   indexable tokens.
3. FTS5 hit set empty + vector hit set non-empty (with
   `--fts5-query` provided) → vector side dominates; RRF still
   produces a valid ordering.
4. FTS5 hit set empty + vector hit set non-empty (no
   `--fts5-query`) → vector-only path; no RRF.
5. Vector hit set empty + FTS5 hit set non-empty → FTS5 side
   dominates; vector side contributes 0; RRF still produces a
   valid ordering.
6. **New**: Vector hit set empty + FTS5 hit set non-empty (no
   `--fts5-query`) → returns `[]` (pure vector; nothing to
   rank).
7. **New**: `--fts5-query "verbatim fts5 terms"` + vector
   `fts5_weight=1.0` → `vector_weight=0.0` is passed to
   `rrf_merge`; pinned at impl.
8. `--fts5-weight=0.5` + 50/50 FTS5/vector fan-out → both
   ranks contribute equally.
9. `--limit=0` → returns nothing. (Both branches.)
10. `--limit=-1` → `typer.BadParameter`.
11. `--fts5-weight=1.5` → `typer.BadParameter`.
12. **New**: `--fts5-weight=0.5` + no `--fts5-query` → flag
    value is silently ignored (RRF doesn't run).
13. **New**: `search sem` with spaCy NOT installed (the extra
    was added before this spec landed) → still works. No
    `SpacyUnavailableError` is raised. `pip uninstall spacy` is
    safe.
14. **New**: `search sem "what CRs touch NB-IoT power saving"`
    followed by `search sem "what CRs touch NB-IoT power saving"
    --fts5-query "spec:38.300"` → same set of candidates
    surfaces (the embedding is deterministic), but the second
    call's rank table contains FTS5 rows that the first call
    doesn't.
15. Stale-index hint fires once per CLI invocation, unchanged.
16. `search index --rebuild-embeddings --resume` after a
    simulated crash → unchanged.

### Coverage targets

Per the existing suite convention, ≥ 90% line coverage for new
modules and any touched line in existing modules. The vector-only
branch, the new `--fts5-query` parameter wiring, and the renamed
flag references are the three highest-risk touched areas and are
covered explicitly in the table above.

## Cross-cutting concerns

| Concern | Strategy |
|---|---|
| `tdocs` row deletion (meeting resync) | `semantic_service.remove_for_tdoc(tdoc_id)` — unchanged |
| `tdoc_cr_cover_page` row updated (re-parse) | `_index_after_parse` (FTS5) and `_embed_after_parse` (vector) both fire — unchanged |
| `tdoc_extracts` row deleted (rare) | Remove FTS5 + vector rows — unchanged |
| MySQL / Postgres | `VectorIndexUnavailableError`; same as before |
| `[semantic]` extra toggling mid-session | Auto-embed checks `_semantic_service is None` at call time — unchanged |
| Concurrent rebuilds | Same lock as the FTS5 rebuild — unchanged |
| Model swap | Same `vec_meta.embedding_dim` mismatch path — unchanged |
| Empty `--fts5-query` | `SemanticSearchQueryError` from `SearchQueryBuilder` |
| Bad FTS5 syntax | `SearchQueryError` from `SearchQueryBuilder` (caught explicitly in the CLI's `sem_command` happy path; not currently in the exception chain — **implementation note** flag at the top of the file) |
| Settings precedence | CLI flag > `[semantic_search]` TOML > default |
| `search query` (FTS5-only) | Unchanged; unaffected |
| `tdoc parse` happy path | FTS5 + embedding hooks fire — unchanged |
| Test model load | Opt-in via `-m semantic`; default tests use pre-computed embeddings in fixtures — unchanged |
| **Renamed TOML key `vector_weight` → `fts5_weight`** | No automatic alias. Existing users see a pydantic-settings "extra fields ignored" warning. Implementation-phase `doc3gpp config init --force` refreshes the file; instructions in CHANGELOG / migration notes. |
| **Dropped TOML keys `user_defined_stop_words` / `keep_negation_words`** | Same strategy — silently ignored at load time. No error. |
| **Dropped `[semantic]` dep `spacy`** | Users with the extra installed will keep a working install (spaCy is harmless when unused). Users running `pip install --force-reinstall doc3gpp[semantic]` no longer pull `spacy`. |

## Open implementation notes

Carried forward from the prior spec with renames:

1. **`sqlite_vec.load()` import path.** Same as before
   (`sqlite-vec` PyPI; `import sqlite_vec`).
2. **`vec_meta.embedding_dim` upgrade story.** Same as before.
3. **Cosine vs L2 distance.** Same — cosine.
4. **Chunking with very long cover bodies.** Same — capped at
   `max_chunks_per_tdoc`.

New:

5. **`SearchQueryError` handling in `sem_command`.** The FTS5
   spec's `search query` command catches `SearchError` at the
   CLI layer for the corrupt-index case (exit 3). The `sem`
   command's current error handler catches
   `SemanticSearchQueryError` but not bare `SearchQueryError`.
   The renamed / new flag wiring must add an explicit
   `except SearchQueryError as exc: typer.echo("bad fts5
   query: ...", err=True); raise typer.Exit(code=2)` branch
   to `sem_command` BEFORE the FTS5 call returns. Implementation
   test pins this.
6. **`pip` extras marker for cleanup.** After
   `pip uninstall doc3gpp[semantic]`, a stale `spacy` install
   remains on the system. Not the project's problem to clean
   up — note in the release notes.
7. **TOML sample refresh.** `data/doc3gpp.toml.example`
   re-keys `vector_weight` → `fts5_weight` (default `0.5`) and
   drops the two stopword keys. Users who ran `doc3gpp config
   init` previously will get the new keys the next time they
   init or run `config init --force`.

## TL;DR

- `doc3gpp search sem QUERY` now embeds `QUERY` (vector path)
  only; **no more spaCy preprocessing**, no more "natural
  language flows into FTS5".
- A new `--fts5-query` flag opts FTS5 in. When provided, the
  FTS5 string is processed by `SearchQueryBuilder` exactly as
  `search query` processes it. When omitted, no FTS5 fan-out
  and no RRF.
- `--vector-weight` is renamed to `--fts5-weight`; default
  `0.7` → `0.5`. The CLI computes `vector_weight = 1 -
  fts5_weight` at the call to `rrf_merge`; RRF math is
  identical.
- When `--fts5-query` is omitted, the FTS5 fan-out is skipped,
  RRF is skipped, and the top `--limit` vector-KNN hits are
  returned (ranked by raw cosine distance, dressed as
  `SemanticSearchHit`).
- spaCy is gone: the stripper module, the
  `SpacyUnavailableError` class, the `[semantic]` extra entry,
  and the bundled model wheel are all removed. Two settings
  (`user_defined_stop_words`, `keep_negation_words`) go with
  them.
- The `search query` subcommand, the FTS5 index, the vector
  index, auto-embed on parse, and the rebuild subcommands are
  all unchanged.
