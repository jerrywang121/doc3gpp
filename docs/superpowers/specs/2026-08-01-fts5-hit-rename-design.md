# `fts5_hit` → `hit` field rename — Design Spec

**Status:** Draft (pending user review)
**Date:** 2026-08-01
**Branch:** `feat/embedding-search`
**Author:** brainstorming session
**Builds on:** [`2026-08-01-semantic-search-revision-design.md`](2026-08-01-semantic-search-revision-design.md)

## Goal

Rename the `fts5_hit: SearchHit` sub-record field on `SemanticSearchHit`
to `hit: SearchHit`. The new name is short, source-agnostic, and matches
the user's mental model: the sub-record carries a TDoc's metadata bag
(title, ftp_url, wis, meeting, tsg, uploaded_date, previews), not
something specific to the FTS5 path.

The name `fts5_hit` was inherited from the prior design where the
natural-language query flowed into FTS5 and a missing `fts5_hit` on a
`SemanticSearchHit` meant "the FTS5 path didn't surface this TDoc, so
synthesize a stub from `tdocs` / `meetings`." Under the 2026-08-01
revision the natural-language query flows only into the vector path;
`fts5_query` is opt-in via `--fts5-query`. The "vector-only TDoc has
no FTS5 hit" reasoning behind the original name no longer holds, and
the name actively misleads users reading the JSON / markdown / table
output of `search sem` (most hits are vector-only — no FTS5 path
involved).

## Non-goals

- **Rename `fts5_hits` (plural)** — the input list parameter to
  `rrf_merge()`. That parameter IS an FTS5 result list (data sourced
  from the FTS5 search service); the name is technically correct.
  Renaming it would touch the `rrf_merge()` signature, every internal
  caller, and every test, with no user-visible benefit. Scope-limited
  to the singular field only.
- **Rename `SearchHit` itself** — the TDoc metadata dataclass. Its
  name is correct: each `SearchHit` is the result of a search over
  TDoc metadata, not a TDoc.
- **Change the synthesized-stub behavior** — `_populate_metadata_stubs`
  still runs in both the vector-only and hybrid paths; the rename is
  orthogonal.
- **Public-API breakage beyond the field name** — same dataclass, same
  kwarg semantics, same JSON / markdown / table shape except for the
  one renamed key.

## Architecture

No architectural change. Single dataclass field rename plus the
cascading accessor / kwarg / JSON-key updates. Renderer contract for
`_render_semantic_hits` is unchanged except for the one renamed key
inside the JSON dict comprehension.

### Data flow — `search sem ... --format json`

```
"search sem ..."
       │
       ▼
SemanticSearchService.read(...) → list[SemanticSearchHit]
       │
       ▼
_render_semantic_hits(hits, format="json")
       │
       ▼
  for h in hits:
      {"tdoc_id": h.tdoc_id,
       "rrf_score": h.rrf_score,
       "rank_fts5": h.rank_fts5,
       "rank_vec": h.rank_vec,
       "min_chunk_distance": h.min_chunk_distance,
       "best_chunk_id": h.best_chunk_id,
       "hit": {                          # was: "fts5_hit": {...}
           "tdoc_id": h.hit.tdoc_id,
           "title":   h.hit.title,
           "ftp_url": h.hit.ftp_url,
           "wis":     h.hit.wis,
       }}
```

### Data flow — `search sem ... --format markdown`

The markdown renderer reads `h.hit.title` (was `h.fts5_hit.title`) to
emit the indented `title:` line under each numbered hit.

### Data flow — `search sem ... --format table`

The table renderer reads `h.hit.title` (was `h.fts5_hit.title`) for
the truncated `title` column.

## Modules affected

| File | Action |
|---|---|
| `models/semantic_search.py` (modify) | Rename `fts5_hit` field on `SemanticSearchHit` → `hit`; update the docstring (3 references) and the `rank_fts5`/`rank_vec` reference to the renamed field. |
| `services/semantic_search_service.py` (modify) | Two `SemanticSearchHit(fts5_hit=...)` keyword-arg constructor calls; the `h.fts5_hit` reads inside `_populate_metadata_stubs`; one guard `if h.fts5_hit is None:`; four docstring references; one inline comment `fts5_hit=h.fts5_hit or _build_fts5_stub(...)`. |
| `cli.py` (modify) | One JSON dict key in `_render_semantic_hits`; four `h.fts5_hit.*` reads; one docstring. |
| `tests/unit/test_rrf.py` (modify) | Test function name: `test_rrf_synthesizes_fts5_hit_for_vector_only_tdoc` → `test_rrf_synthesizes_hit_for_vector_only_tdoc`; one assertion; one comment. |
| `tests/unit/test_semantic_models.py` (modify) | Two `SemanticSearchHit(fts5_hit=...)` constructor calls; two `h.fts5_hit.tdoc_id` assertions. |
| `tests/unit/test_semantic_search_service.py` (modify) | ~10 assertions on `hit.fts5_hit.*` (`.is not None`, `.title`, `.ftp_url`, `.wis`, `.meeting`, `.tsg`, `.uploaded_date`); one inline comment. |
| `tests/integration/test_search_sem_end_to_end.py` (modify) | One assertion `h.fts5_hit is not None`. |
| `docs/cli.md` (modify) | The `search sem` JSON output example. |
| `README.md` (modify) | The `search sem` JSON output example. |
| `docs/superpowers/specs/2026-07-31-embedding-search-design.md` (modify) | Historical spec amendment — `fts5_hit: SearchHit` → `hit: SearchHit` in the `SemanticSearchHit` dataclass definition + the docstring references. |
| `docs/superpowers/specs/2026-08-01-semantic-search-revision-design.md` (modify) | Same historical-spec amendment. |

### Out of scope

- `fts5_hits` (plural): the input list parameter to `rrf_merge()`.
- `SearchHit` dataclass.
- `_build_fts5_stub` helper name (the `_fts5_stub` suffix describes
  what it builds — a stub FTS5-shaped `SearchHit` — and stays).
- The `fts5_query` Typer flag, the `fts5_weight` setting, the
  `fts5_service` / `fts5_filters` / `fts5_expr` / `fts5_rank` /
  `fts5_by_id` internal locals — all of these refer to FTS5 data and
  keep their names.

## DTO implications

`SemanticSearchHit` (`models/semantic_search.py`) field rename only:

```diff
 @dataclass(slots=True, frozen=True)
 class SemanticSearchHit:
     """A single merged hit from the RRF fusion of FTS5 + vector rankings.

     ``rank_fts5`` / ``rank_vec`` are the 0-based positions in the
     respective fan-out lists, or ``None`` when the ``tdoc_id`` was not
     present in that side's fan-out. ``min_chunk_distance`` is the
-    lowest cosine distance across all chunks for this ``tdoc_id``
-    (``None`` when the tdoc had no vector rows). ``best_chunk_id`` is
-    the chunk that produced the min distance (for ``--explain``
-    rendering). ``fts5_hit`` is the existing :class:`SearchHit`
-    sub-record; when the tdoc was vector-only, the service synthesizes
-    a minimal :class:`SearchHit` from the ``tdocs`` JOIN so the
-    renderer can reuse the existing shape.
+    lowest cosine distance across all chunks for this ``tdoc_id``
+    (``None`` when the tdoc had no vector rows). ``best_chunk_id`` is
+    the chunk that produced the min distance (for ``--explain``
+    rendering). ``hit`` is the existing :class:`SearchHit` sub-record
+    carrying the TDoc's metadata bag (title, ftp_url, meeting, tsg,
+    uploaded_date, wis, previews); when the tdoc was vector-only, the
+    service synthesizes a minimal :class:`SearchHit` from the
+    ``tdocs`` JOIN so the renderer can reuse the existing shape.
     """

     tdoc_id: str
     rrf_score: float
-    fts5_hit: SearchHit
+    hit: SearchHit
     rank_fts5: int | None = None
     rank_vec: int | None = None
     min_chunk_distance: float | None = None
     best_chunk_id: str | None = None
```

`SemanticSearchHit.hit` is required (no default) — same as the prior
`fts5_hit`. The dataclass stays `frozen=True`.

## CLI surface

No flag change. No help-text change. The rendered output changes in
exactly one place per format:

| Format | Before | After |
|---|---|---|
| `json` | `"fts5_hit": {…}` | `"hit": {…}` |
| `markdown` | (no key prefix; the renderer prints `h.fts5_hit.title` as the `title:` line) | identical line, sourced from `h.hit.title` |
| `table` | (the table prints `h.fts5_hit.title` in the title column) | identical column, sourced from `h.hit.title` |

## Settings additions

None. The rename is purely a dataclass field + accessor / kwarg /
JSON-key sweep.

## `pyproject.toml` changes

None.

## Test plan

### Tests renamed

- `tests/unit/test_rrf.py::test_rrf_synthesizes_fts5_hit_for_vector_only_tdoc` → `test_rrf_synthesizes_hit_for_vector_only_tdoc`. The test body's single assertion `assert out[0].fts5_hit is None` becomes `assert out[0].hit is None`. The comment "# fts5_hit is None for vector-only; service fills it later" becomes "# hit is None for vector-only; service fills it later".

### Tests updated (no rename)

| File | Change |
|---|---|
| `tests/unit/test_semantic_models.py` | Two `SemanticSearchHit(fts5_hit=_hit())` constructor calls become `SemanticSearchHit(hit=_hit())`. Two `h.fts5_hit.tdoc_id == "R5-1"` assertions become `h.hit.tdoc_id == "R5-1"`. |
| `tests/unit/test_semantic_search_service.py` | All `hit.fts5_hit.*` reads become `hit.hit.*`. ~10 sites. |
| `tests/integration/test_search_sem_end_to_end.py` | One `h.fts5_hit is not None` becomes `h.hit is not None`. |

### Tests added

None. The rename is purely structural; existing tests pin the same
behavior under the new name.

### Edge cases pinned (already covered; verify after rename)

1. Vector-only hit (no `--fts5-query`) has `rank_fts5 is None` and a
   synthesized `hit` (non-`None`) with title / ftp_url / wis / meeting
   / tsg / uploaded_date populated from `tdocs` / `meetings`.
2. Hybrid-path hit (both `--fts5-query` and `--fts5-query` supplied)
   with the FTS5 path covering a hit has `rank_fts5` set and `hit`
   populated from the FTS5 `SearchHit` (not the synthesized stub).
3. Deleted-tdoc vector-only hit: `hit` synthesized but empty (no
   metadata available).

## Cross-cutting concerns

| Concern | Strategy |
|---|---|
| Backwards compat for users scripting against the prior JSON key | Out of scope — this is a pre-1.0 CLI; no compatibility promise. Document the change in the release notes / CHANGELOG. |
| Historical specs (`2026-07-31-…`, `2026-08-01-…`) | Amend in the same change set per the AGENTS.md doc-sync convention. The semantic content of those specs is unchanged; only the field name reference is corrected. |
| `fts5_hit` string in user-reported logs / exception messages | None — the codebase does not embed `fts5_hit` in any user-visible message; it's only a Python attribute name. |
| Tests that use `fts5_hit` in a docstring / comment | Three sites; updated to `hit` for consistency. |
| Pre-existing ruff F401 in `tests/unit/test_protocols_semantic.py:7` | Unrelated to this rename; pre-existed before Task 1 of the prior revision. Out of scope. |

## Open implementation notes

1. **`SemanticSearchHit` is `frozen=True`.** Rename the field in the
   `@dataclass(...)` declaration line; pydantic-equivalent immutability
   means no `.replace()` call sites to update. (No `.replace()` is
   used on `fts5_hit` in the codebase.)
2. **`_render_semantic_hits` JSON dict comprehension.** The
   `"fts5_hit": {…}` literal key changes to `"hit": {…}`. The four
   `h.fts5_hit.*` reads inside the dict comprehension change to
   `h.hit.*`. The renderer does not need to know about the rename in
   its docstring — that's a renderer-internal concern.
3. **Markdown / table renderers.** Neither prints a `fts5_hit`
   label; both read `h.fts5_hit.title` for the title line. Update
   the reads; the rendered text is unchanged.
4. **Doc-sync.** `docs/cli.md` and `README.md` show JSON output
   examples with `"fts5_hit": {…}`; the rename changes both. Per the
   AGENTS.md doc-sync convention this lands in the same commit.

## TL;DR

- One dataclass field rename: `SemanticSearchHit.fts5_hit` → `SemanticSearchHit.hit`.
- All accessor / kwarg / JSON-key sites in `src/` and `tests/` follow.
- Two user-facing docs (`README.md`, `docs/cli.md`) and two historical
  spec docs update in the same change set.
- `fts5_hits` (plural) stays — it's an internal `rrf_merge()` parameter
  that genuinely IS FTS5 data.
- `SearchHit` stays — it's the TDoc-metadata dataclass.
- Output JSON: `"fts5_hit": {…}` → `"hit": {…}`.
- No behavior change.
