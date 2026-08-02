# FTS5 Semantic Rerank Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `--sem-query` flag to `doc3gpp search query` that reorders the FTS5 hit list by cosine similarity to a user-supplied semantic string. FTS5 fetches `limit * search_fanout_factor` candidates (default `4 * limit`); the new `SemanticReranker` re-orders them and truncates back to `limit`.

**Architecture:** New `SemanticReranker(EmbeddingReranker)` impl in `src/doc3gpp/services/semantic_reranker.py`. The `EmbeddingReranker` Protocol gains a `final_limit: int | None = None` kwarg. The CLI drops `--rerank` and adds `--sem-query`. `build_search_service` chooses `SemanticReranker` when both `[search].enabled` and `[semantic_search].enabled`; otherwise `PassthroughReranker`. A new `VectorIndexRepository.get_min_distance_for_tdocs(tdoc_ids, query_vec)` method does the batched KNN lookup against `vec_tdoc_embeddings`.

**Tech Stack:** Python 3.10+, pydantic-settings, Typer, SQLAlchemy 2.0, sqlite-vec, sentence-transformers, pytest, ruff.

**Spec:** [`docs/superpowers/specs/2026-08-01-fts5-semantic-rerank-design.md`](../specs/2026-08-01-fts5-semantic-rerank-design.md)

## Global Constraints

- Python ≥ 3.10. Pydantic v2 + pydantic-settings.
- The new `search_fanout_factor` is a TOML-only knob in `[search]`, no env override. Range `1..64`, default `4`. Pydantic range validator enforces the bounds.
- `Settings.search.search_fanout_factor` is **only** consulted when `--sem-query` is set on `search query`. With no `--sem-query`, the FTS5 path runs with the user's `--limit` directly.
- The `--rerank` flag is **removed** from `search query`. The CLI raises `typer.BadParameter` if a caller supplies it. The error message points at `--sem-query`.
- `EmbeddingReranker.rerank` signature becomes `rerank(self, semantic_query: str, hits: list[SearchHit], final_limit: int | None = None) -> list[SearchHit]`. `PassthroughReranker` is updated to accept the new kwargs and honor `final_limit` (slice `hits[:final_limit]`).
- `SearchService.search`'s public signature is unchanged. The fanout cap is the CLI's responsibility: it builds `SearchFilters(limit=fanout)` for the repo and passes `final_limit=user_limit` to the reranker.
- `VectorIndexRepository` gains one new method: `get_min_distance_for_tdocs(tdoc_ids, query_vec) -> dict[str, tuple[float, str] | None]`. The implementation in `SQLAlchemyVectorIndexRepository` issues a single batched SQL trip using sqlite-vec's `WHERE embedding MATCH :q AND k = :k`.
- `SemanticReranker` uses `MISSING_FLOOR = float("-inf")` for candidates with no `vec_tdoc_embeddings` rows. The floor sorts strictly below every real score by construction.
- The empty-string guard: `--sem-query ""` is treated as `None` (no rerank), no embedder call.
- The vector-empty guard: when every candidate scores `MISSING_FLOOR`, output is the FTS5 order truncated to `limit`, and `logger.warning("semantic rerank: no rows in vec_tdoc_embeddings; falling back to FTS5 order")` fires once per invocation (gated on `--quiet`).
- All new code in this plan goes under existing test markers and conventions: unit tests are pure; integration tests use sqlite + sqlite-vec. Embedder is mocked.
- All commits on the branch `fts5-semantic-rerank` (already created at `/home/jerry/personal/doc3gpp/.worktrees/fts5-semantic-rerank`).
- Run `ruff check .` at the end of every task touching `src/` or `tests/`.

## File / Symbol Map

| File | Action |
|---|---|
| `src/doc3gpp/settings/schema.py` | Add `search_fanout_factor` field to `SearchSettings` (default 4, range 1..64). |
| `src/doc3gpp/repository/protocols.py` | `EmbeddingReranker.rerank` gains `final_limit: int \| None = None`; rename `query` → `semantic_query`. `VectorIndexRepository` gains `get_min_distance_for_tdocs`. |
| `src/doc3gpp/services/search_service.py` | `PassthroughReranker.rerank` accepts the new signature, honors `final_limit`. `SearchService.search` docstring notes the fanout-cap contract. |
| `src/doc3gpp/services/semantic_reranker.py` | **New file.** `SemanticReranker(EmbeddingReranker)` impl. |
| `src/doc3gpp/storage/repositories/vector_sql.py` | Implement `get_min_distance_for_tdocs`. |
| `src/doc3gpp/services/factory.py` | `build_search_service` picks `SemanticReranker` when both `enabled` flags are True and the embedder + vector_repo construct cleanly; else `PassthroughReranker`. |
| `src/doc3gpp/cli.py` | `search_command`: drop `--rerank`, add `--sem-query`. New wiring: when `sem_query` is set, build fanout filters, call `svc._repo.search(...)` directly, then `svc._reranker.rerank(semantic_query, raw_hits, final_limit=limit)`. Empty string → no-op. `search sem` is unchanged. |
| `src/doc3gpp/data/doc3gpp.toml.example` | Add `search_fanout_factor = 4` under `[search]`. |
| `docs/cli.md` | Document `--sem-query`, the `search_fanout_factor` knob, the removal of `--rerank`, the empty-vector fallback warning. |
| `AGENTS.md` | Add a "Where to look" row for the rerank. |
| `tests/unit/test_search_settings.py` | New field has default 4; range validator rejects 0/65; accepts 1/64. |
| `tests/unit/test_search_service.py` | `PassthroughReranker` accepts the new kwargs; `final_limit=5` truncates to 5. |
| `tests/unit/test_semantic_reranker.py` | New file. Empty input → no embedder call. One encode call per query regardless of hit-list length. Stable sort. `final_limit` truncation. Missing vector row → candidate sorts below every real score. All missing → FTS5 order preserved. |
| `tests/unit/test_cli_search_query.py` | `--sem-query` triggers `svc._reranker.rerank(semantic_query=..., hits=..., final_limit=limit)`. `--rerank` raises `BadParameter`. `--sem-query=""` skips rerank. |
| `tests/integration/test_search_query_sem_rerank.py` | New file. Seeded DB; embedder mocked; verify output order, output count, embedder call count. Variants: vector empty, fanout=1, FTS5 empty, empty `--sem-query`. |
| `tests/unit/test_vector_repo.py` (or wherever the existing `vector_sql` integration tests live) | Add a `get_min_distance_for_tdocs` integration case. |

---

## Task 1: Add `search_fanout_factor` to `SearchSettings` (failing tests first)

**Files:**
- Modify: `src/doc3gpp/settings/schema.py:380-458` (add field)
- Modify: `tests/unit/test_search_settings.py` (add tests)
- Test: `tests/unit/test_search_settings.py`

**Interfaces:**
- Consumes: existing `SearchSettings` model — no constructor change
- Produces: new attribute `Settings.search.search_fanout_factor: int = 4` with `ge=1, le=64`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_search_settings.py`:

```python
def test_search_fanout_factor_default_is_4():
    from doc3gpp.settings.schema import SearchSettings
    s = SearchSettings()
    assert s.search_fanout_factor == 4


def test_search_fanout_factor_accepts_bounds():
    from doc3gpp.settings.schema import SearchSettings
    assert SearchSettings(search_fanout_factor=1).search_fanout_factor == 1
    assert SearchSettings(search_fanout_factor=64).search_fanout_factor == 64


def test_search_fanout_factor_rejects_below_one():
    import pytest
    from doc3gpp.settings.schema import SearchSettings
    with pytest.raises(ValueError):
        SearchSettings(search_fanout_factor=0)


def test_search_fanout_factor_rejects_above_64():
    import pytest
    from doc3gpp.settings.schema import SearchSettings
    with pytest.raises(ValueError):
        SearchSettings(search_fanout_factor=65)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_search_settings.py -k search_fanout_factor -v`
Expected: 4 failures (`AttributeError: SearchSettings has no attribute search_fanout_factor`, then `ValueError` for the range tests once the field exists without a validator).

- [ ] **Step 3: Add the field**

In `src/doc3gpp/settings/schema.py`, append after the `bm25_weights` field block (after line 458), inside `class SearchSettings`:

```python
    search_fanout_factor: int = Field(
        default=4, ge=1, le=64,
        description=(
            "When `search query --sem-query` is used, the FTS5 path "
            "fetches limit * search_fanout_factor candidates before "
            "the semantic reranker truncates back to limit. Higher "
            "values give the reranker more to work with at the cost "
            "of more vector lookups per query. Only honored when "
            "--sem-query is supplied. Default 4. Range 1..64."
        ),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_search_settings.py -k search_fanout_factor -v`
Expected: 4 passes.

- [ ] **Step 5: Lint and commit**

```bash
ruff check src/doc3gpp/settings/schema.py tests/unit/test_search_settings.py
git add src/doc3gpp/settings/schema.py tests/unit/test_search_settings.py
git commit -m "feat(search): add search_fanout_factor knob to SearchSettings"
```

---

## Task 2: Update `EmbeddingReranker` Protocol + `PassthroughReranker`

**Files:**
- Modify: `src/doc3gpp/repository/protocols.py:610-632` (Protocol)
- Modify: `src/doc3gpp/services/search_service.py:43-55` (PassthroughReranker)
- Modify: `tests/unit/test_search_service.py` (existing test for `PassthroughReranker`)

**Interfaces:**
- Consumes: existing `EmbeddingReranker.rerank(query, hits) -> list[SearchHit]`
- Produces: new `EmbeddingReranker.rerank(semantic_query, hits, final_limit=None) -> list[SearchHit]`. `PassthroughReranker.rerank` honors `final_limit` by slicing.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_search_service.py`:

```python
def test_passthrough_reranker_returns_list_copy():
    from doc3gpp.models.search import SearchHit
    from doc3gpp.services.search_service import PassthroughReranker
    h = SearchHit(
        tdoc_id="R5-1", score=0.0, previews={}, title="t", meeting="m",
        tsg="S1", uploaded_date="2026-01-01", ftp_url="https://x", wis=(),
    )
    r = PassthroughReranker()
    out = r.rerank("anything", [h])
    assert out == [h]
    assert out is not [h]  # copy, not the same list


def test_passthrough_reranker_honors_final_limit():
    from doc3gpp.models.search import SearchHit
    from doc3gpp.services.search_service import PassthroughReranker

    def _hit(t):
        return SearchHit(
            tdoc_id=t, score=0.0, previews={}, title="t", meeting="m",
            tsg="S1", uploaded_date="2026-01-01", ftp_url="https://x", wis=(),
        )
    hits = [_hit("R5-1"), _hit("R5-2"), _hit("R5-3")]
    r = PassthroughReranker()
    out = r.rerank("anything", hits, final_limit=2)
    assert [h.tdoc_id for h in out] == ["R5-1", "R5-2"]


def test_passthrough_reranker_empty_input():
    from doc3gpp.services.search_service import PassthroughReranker
    r = PassthroughReranker()
    assert r.rerank("anything", []) == []
    assert r.rerank("anything", [], final_limit=5) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_search_service.py::test_passthrough_reranker_returns_list_copy tests/unit/test_search_service.py::test_passthrough_reranker_honors_final_limit tests/unit/test_search_service.py::test_passthrough_reranker_empty_input -v`
Expected: 3 failures (`TypeError: rerank() got an unexpected keyword argument 'final_limit'`).

- [ ] **Step 3: Update `PassthroughReranker`**

In `src/doc3gpp/services/search_service.py`, replace the `PassthroughReranker` class body (lines 43-55):

```python
class PassthroughReranker(EmbeddingReranker):
    """Default reranker that returns hits unchanged.

    Used when the semantic search stack is not available
    (no ``[semantic]`` extra, no sqlite-vec, ``enabled=False``).
    Returns a *copy* of the input so callers may mutate the
    reranker's output without disturbing the upstream list.
    Honors ``final_limit`` by slicing.
    """

    def rerank(
        self,
        semantic_query: str,
        hits: list[SearchHit],
        final_limit: int | None = None,
    ) -> list[SearchHit]:
        _ = semantic_query
        copied = list(hits)
        if final_limit is not None:
            return copied[:final_limit]
        return copied
```

- [ ] **Step 4: Update the `EmbeddingReranker` Protocol**

In `src/doc3gpp/repository/protocols.py`, replace the `EmbeddingReranker.rerank` method (lines 620-632):

```python
    def rerank(
        self,
        semantic_query: str,
        hits: list[SearchHit],
        final_limit: int | None = None,
    ) -> list[SearchHit]:
        """Return ``hits`` re-ordered (and possibly truncated) by relevance.

        ``semantic_query`` is the *embedding* input — distinct from any
        FTS5 expression. The default ``PassthroughReranker`` returns
        ``hits`` verbatim (a copy, sliced to ``final_limit`` if given).
        ``SemanticReranker`` encodes ``semantic_query`` once, looks up
        each candidate's closest chunk in ``vec_tdoc_embeddings`` via
        :meth:`VectorIndexRepository.get_min_distance_for_tdocs`,
        sorts by ``-min_distance`` desc, and truncates to
        ``final_limit``.

        ``final_limit`` is the user-visible output count (e.g. the
        ``--limit`` value from ``search query --sem-query``).
        The caller (CLI) is responsible for asking the upstream
        FTS5 repo for a *wider* candidate bag, then letting the
        reranker trim back to ``final_limit``.
        """
        ...
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_search_service.py -v`
Expected: all `PassthroughReranker` tests pass; existing `SearchService` tests still pass (signature unchanged).

- [ ] **Step 6: Lint and commit**

```bash
ruff check src/doc3gpp/repository/protocols.py src/doc3gpp/services/search_service.py tests/unit/test_search_service.py
git add src/doc3gpp/repository/protocols.py src/doc3gpp/services/search_service.py tests/unit/test_search_service.py
git commit -m "refactor(search): extend EmbeddingReranker Protocol with final_limit"
```

---

## Task 3: Implement `VectorIndexRepository.get_min_distance_for_tdocs` Protocol + SQL

**Files:**
- Modify: `src/doc3gpp/repository/protocols.py:654-723` (add method to Protocol)
- Modify: `src/doc3gpp/storage/repositories/vector_sql.py:63-` (add method to impl)
- Modify: the existing `tests/integration/test_embed_after_parse.py` or `tests/integration/test_search_sem_end_to_end.py` (whichever exercises `SQLAlchemyVectorIndexRepository` end-to-end) — add a new test for `get_min_distance_for_tdocs`. If neither test file is appropriate, create `tests/integration/test_vector_repo_get_min_distance.py`.

**Interfaces:**
- Consumes: `VectorIndexRepository` Protocol
- Produces: new method `get_min_distance_for_tdocs(tdoc_ids: Sequence[str], query_vec: Sequence[float]) -> dict[str, tuple[float, str] | None]`. A tdoc_id with no vector rows maps to `None`; a tdoc_id with rows maps to `(min_distance, best_chunk_id)`.

- [ ] **Step 1: Write the failing integration test**

Create `tests/integration/test_vector_repo_get_min_distance.py`:

```python
"""Tests for SQLAlchemyVectorIndexRepository.get_min_distance_for_tdocs."""
from __future__ import annotations

import numpy as np
import pytest


@pytest.fixture
def repo_and_seeded_db(tmp_path, monkeypatch):
    """Build a sqlite repo with a tiny vec_tdoc_embeddings table."""
    from sqlalchemy import create_engine, text
    from doc3gpp.storage.db import session as session_module
    from doc3gpp.storage.repositories.vector_sql import (
        SQLAlchemyVectorIndexRepository,
    )
    from doc3gpp.storage.db.migrate import _create_vector_schema  # noqa: F401

    db = tmp_path / "test.db"
    engine = create_engine(f"sqlite+pysqlite:///{db}")
    monkeypatch.setattr(session_module, "get_engine", lambda: engine)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE vec_tdoc_embeddings ("
            " chunk_id TEXT PRIMARY KEY,"
            " tdoc_id TEXT NOT NULL,"
            " chunk_index INTEGER NOT NULL,"
            " embedding BLOB NOT NULL)"
        ))
    return SQLAlchemyVectorIndexRepository(dim=4)


def _insert(repo, tdoc_id, vec):
    repo.upsert_chunks(tdoc_id, [np.asarray(vec, dtype=np.float32)])


def test_empty_input_returns_empty_dict(repo_and_seeded_db):
    assert repo_and_seeded_db.get_min_distance_for_tdocs([], np.zeros(4)) == {}


def test_missing_tdocs_map_to_none(repo_and_seeded_db):
    out = repo_and_seeded_db.get_min_distance_for_tdocs(["R5-1"], np.zeros(4))
    assert out == {"R5-1": None}


def test_returns_min_distance_and_chunk_id(repo_and_seeded_db):
    repo = repo_and_seeded_db
    # 4-D vectors; store 2 chunks for R5-1, 1 for R5-2.
    _insert(repo, "R5-1", [1.0, 0.0, 0.0, 0.0])
    _insert(repo, "R5-1", [0.0, 1.0, 0.0, 0.0])
    _insert(repo, "R5-2", [0.0, 0.0, 1.0, 0.0])
    out = repo.get_min_distance_for_tdocs(
        ["R5-1", "R5-2", "R5-3"],
        np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
    )
    assert out["R5-1"][0] == pytest.approx(0.0, abs=1e-3)
    assert out["R5-1"][1] == "R5-1#0"
    assert out["R5-2"][0] > 0.0
    assert out["R5-2"][1] == "R5-2#0"
    assert out["R5-3"] is None
```

- [ ] **Step 2: Add the Protocol method**

In `src/doc3gpp/repository/protocols.py`, add to `VectorIndexRepository` (after `get_tdocs_metadata`, line ~722):

```python
    def get_min_distance_for_tdocs(
        self,
        tdoc_ids: Sequence[str],
        query_vec: Sequence[float],
    ) -> dict[str, tuple[float, str] | None]:
        """For each tdoc_id, return the closest-chunk distance to ``query_vec``.

        Returns a dict keyed by tdoc_id; each value is either
        ``(min_distance, best_chunk_id)`` for the row with the
        smallest cosine distance to ``query_vec``, or ``None`` if the
        tdoc has no rows in ``vec_tdoc_embeddings``.

        The implementation must issue a single batched SQL trip.
        TDoc ids with no rows are not an error — they map to ``None``
        so the caller can apply its missing-candidate policy
        (e.g. ``SemanticReranker`` uses ``MISSING_FLOOR``).

        Empty ``tdoc_ids`` returns an empty dict without touching
        the database.
        """
        ...
```

Add `from collections.abc import Sequence` to the existing imports in `protocols.py` (it may already be there; if so skip).

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/integration/test_vector_repo_get_min_distance.py -v`
Expected: 3 failures (`AttributeError: SQLAlchemyVectorIndexRepository has no attribute get_min_distance_for_tdocs`).

- [ ] **Step 4: Implement the SQL method**

First, add a module-level constant near the top of `src/doc3gpp/storage/repositories/vector_sql.py` (after the imports, before the class definition):

```python
MAX_CHUNKS_PER_TDOC = 32
```

This mirrors `Settings.semantic_search.max_chunks_per_tdoc` (default 32) and is used as the per-tdoc chunk cap for the batched KNN. If the setting ever changes, both should move together.

Then, add the new method after `get_tdocs_metadata` (after line 278's body, find the right insertion point). The implementation:

```python
    def get_min_distance_for_tdocs(
        self,
        tdoc_ids: Sequence[str],
        query_vec: Sequence[float],
    ) -> dict[str, tuple[float, str] | None]:
        if not tdoc_ids:
            return {}
        q = np.asarray(query_vec, dtype=np.float32)
        if q.shape[-1] != self._dim:
            raise VectorIndexUnavailableError(
                f"query dim mismatch: stored={self._dim} "
                f"requested={q.shape[-1]}"
            )
        # sqlite-vec KNN: per-row ``distance`` column, K=1 per tdoc_id.
        # We request K = number of distinct chunks for the asked tdoc_ids,
        # then group-by in Python (cheaper than a CTE, fewer sqlite-vec
        # version dependencies). Bounded to len(tdoc_ids) * 32 chunks
        # worst case; the common case is << that.
        sql = [
            "SELECT tdoc_id, chunk_id, distance",
            "  FROM vec_tdoc_embeddings",
            " WHERE tdoc_id IN :tdoc_ids",
            "   AND embedding MATCH :q",
            "   AND k = :k",
            " ORDER BY distance IS NULL, distance ASC, chunk_id ASC",
        ]
        params: dict = {
            "tdoc_ids": tuple(tdoc_ids),
            "q": q.tobytes(),
            "k": len(tdoc_ids) * 32,
        }
        from sqlalchemy import bindparam, text
        stmt = text("\n".join(sql)).bindparams(
            bindparam("tdoc_ids", expanding=True),
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt, params).all()
        # Pick the closest chunk per tdoc_id. Initialize all asked
        # ids to None so the caller can distinguish "no rows" from
        # "rows with inf distance" (sqlite-vec returns NULL when
        # the row is far enough to be effectively irrelevant).
        out: dict[str, tuple[float, str] | None] = {tid: None for tid in tdoc_ids}
        for tdoc_id, chunk_id, distance in rows:
            if out[tdoc_id] is not None:
                continue
            if distance is None:
                continue
            out[tdoc_id] = (float(distance), chunk_id)
        return out
```

Notes:
- The 32-chunk-per-tdoc cap is the `MAX_CHUNKS_PER_TDOC` constant declared above; use it in place of the literal `32` in the implementation.
- The `expanding=True` bindparam lets the `IN :tdoc_ids` clause accept a tuple.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/integration/test_vector_repo_get_min_distance.py -v`
Expected: 3 passes.

- [ ] **Step 6: Lint and commit**

```bash
ruff check src/doc3gpp/repository/protocols.py src/doc3gpp/storage/repositories/vector_sql.py tests/integration/test_vector_repo_get_min_distance.py
git add src/doc3gpp/repository/protocols.py src/doc3gpp/storage/repositories/vector_sql.py tests/integration/test_vector_repo_get_min_distance.py
git commit -m "feat(vector): add get_min_distance_for_tdocs batched KNN lookup"
```

---

## Task 4: Implement `SemanticReranker`

**Files:**
- Create: `src/doc3gpp/services/semantic_reranker.py`
- Create: `tests/unit/test_semantic_reranker.py`

**Interfaces:**
- Consumes: `Embedder` (Protocol — `encode(texts: list[str]) -> np.ndarray`, `dim: int`), `VectorIndexRepository` (Protocol — `get_min_distance_for_tdocs(tdoc_ids, query_vec) -> dict[str, tuple[float, str] | None]`), `Settings`
- Produces: `class SemanticReranker(EmbeddingReranker)` with `__init__(embedder, vector_repo, settings) -> None` and `rerank(semantic_query, hits, final_limit=None) -> list[SearchHit]`. The class constant `SemanticReranker.MISSING_FLOOR: float = float("-inf")`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_semantic_reranker.py`:

```python
"""Unit tests for SemanticReranker (no live model, no DB)."""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from doc3gpp.models.search import SearchHit
from doc3gpp.services.semantic_reranker import SemanticReranker


def _hit(t: str) -> SearchHit:
    return SearchHit(
        tdoc_id=t, score=0.0, previews={}, title="t", meeting="m",
        tsg="S1", uploaded_date="2026-01-01", ftp_url="https://x", wis=(),
    )


def _settings() -> MagicMock:
    s = MagicMock()
    s.search_fanout_factor = 4  # not read by reranker, but present
    return s


def _mock_embedder() -> MagicMock:
    e = MagicMock()
    e.encode.return_value = np.asarray([[0.0, 0.0, 0.0, 0.0]], dtype=np.float32)
    e.dim = 4
    return e


def test_empty_input_no_embedder_call():
    emb = _mock_embedder()
    vec = MagicMock()
    svc = SemanticReranker(emb, vec, _settings())
    out = svc.rerank("query", [])
    assert out == []
    emb.encode.assert_not_called()


def test_one_embedder_call_regardless_of_hit_count():
    emb = _mock_embedder()
    vec = MagicMock()
    vec.get_min_distance_for_tdocs.return_value = {
        f"R5-{i}": (0.1 * i, f"R5-{i}#0") for i in range(1, 6)
    }
    svc = SemanticReranker(emb, vec, _settings())
    hits = [_hit(f"R5-{i}") for i in range(1, 6)]
    svc.rerank("query", hits)
    emb.encode.assert_called_once_with(["query"])


def test_orders_by_negated_distance_desc():
    emb = _mock_embedder()
    vec = MagicMock()
    # Lower distance = better; -distance is the score we sort by.
    vec.get_min_distance_for_tdocs.return_value = {
        "R5-1": (0.5, "R5-1#0"),
        "R5-2": (0.1, "R5-2#0"),
        "R5-3": (0.9, "R5-3#0"),
    }
    svc = SemanticReranker(emb, vec, _settings())
    hits = [_hit("R5-1"), _hit("R5-2"), _hit("R5-3")]
    out = svc.rerank("query", hits)
    assert [h.tdoc_id for h in out] == ["R5-2", "R5-1", "R5-3"]


def test_stable_sort_preserves_input_order_on_ties():
    emb = _mock_embedder()
    vec = MagicMock()
    vec.get_min_distance_for_tdocs.return_value = {
        "R5-1": (0.1, "R5-1#0"),
        "R5-2": (0.1, "R5-2#0"),
        "R5-3": (0.1, "R5-3#0"),
    }
    svc = SemanticReranker(emb, vec, _settings())
    hits = [_hit("R5-1"), _hit("R5-2"), _hit("R5-3")]
    out = svc.rerank("query", hits)
    assert [h.tdoc_id for h in out] == ["R5-1", "R5-2", "R5-3"]


def test_final_limit_truncates():
    emb = _mock_embedder()
    vec = MagicMock()
    vec.get_min_distance_for_tdocs.return_value = {
        f"R5-{i}": (0.1 * i, f"R5-{i}#0") for i in range(1, 6)
    }
    svc = SemanticReranker(emb, vec, _settings())
    hits = [_hit(f"R5-{i}") for i in range(1, 6)]
    out = svc.rerank("query", hits, final_limit=2)
    assert [h.tdoc_id for h in out] == ["R5-1", "R5-2"]


def test_final_limit_none_returns_full_list():
    emb = _mock_embedder()
    vec = MagicMock()
    vec.get_min_distance_for_tdocs.return_value = {
        f"R5-{i}": (0.1 * i, f"R5-{i}#0") for i in range(1, 4)
    }
    svc = SemanticReranker(emb, vec, _settings())
    hits = [_hit(f"R5-{i}") for i in range(1, 4)]
    out = svc.rerank("query", hits)
    assert len(out) == 3


def test_missing_vector_row_sorts_below_real_scores():
    emb = _mock_embedder()
    vec = MagicMock()
    # R5-2 has no vector row.
    vec.get_min_distance_for_tdocs.return_value = {
        "R5-1": (0.1, "R5-1#0"),
        "R5-2": None,
    }
    svc = SemanticReranker(emb, vec, _settings())
    hits = [_hit("R5-1"), _hit("R5-2")]
    out = svc.rerank("query", hits)
    assert [h.tdoc_id for h in out] == ["R5-1", "R5-2"]


def test_all_missing_preserves_input_order():
    emb = _mock_embedder()
    vec = MagicMock()
    vec.get_min_distance_for_tdocs.return_value = {
        "R5-1": None,
        "R5-2": None,
    }
    svc = SemanticReranker(emb, vec, _settings())
    hits = [_hit("R5-1"), _hit("R5-2")]
    out = svc.rerank("query", hits)
    assert [h.tdoc_id for h in out] == ["R5-1", "R5-2"]


def test_missing_floor_constant_is_negative_infinity():
    assert SemanticReranker.MISSING_FLOOR == float("-inf")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_semantic_reranker.py -v`
Expected: 9 failures (`ModuleNotFoundError: No module named doc3gpp.services.semantic_reranker`).

- [ ] **Step 3: Implement `SemanticReranker`**

Create `src/doc3gpp/services/semantic_reranker.py`:

```python
"""Semantic rerank for the FTS5 hit list.

The :class:`SemanticReranker` is the embedding-backed impl of the
:class:`doc3gpp.repository.protocols.EmbeddingReranker` Protocol. The
FTS5 path in ``search query --sem-query`` fetches a wider candidate
bag; this class re-orders it by cosine similarity to a user-supplied
string.

Scoring source: :class:`doc3gpp.repository.protocols.VectorIndexRepository`
— specifically, :meth:`get_min_distance_for_tdocs`. Candidates with
no row in ``vec_tdoc_embeddings`` get
:attr:`MISSING_FLOOR <float("-inf")>` so they sort strictly below
every real score. When every candidate is missing the output is the
FTS5 input order (the caller logs the vector-empty warning).
"""
from __future__ import annotations

import logging
from collections.abc import Sequence

import numpy as np

from doc3gpp.models.search import SearchHit
from doc3gpp.repository.protocols import Embedder, VectorIndexRepository

logger = logging.getLogger(__name__)


class SemanticReranker:
    """Rerank FTS5 hits by cosine distance to a user-supplied query.

    The class is duck-typed against the
    :class:`~doc3gpp.repository.protocols.EmbeddingReranker` Protocol
    (it implements the same ``rerank(semantic_query, hits, final_limit)``
    signature). It is NOT declared as ``EmbeddingReranker`` in the
    type annotation because ``build_search_service`` constructs the
    instance lazily and tests inject mock embedders / vector repos
    via constructor injection.
    """

    MISSING_FLOOR: float = float("-inf")

    def __init__(
        self,
        embedder: Embedder,
        vector_repo: VectorIndexRepository,
        settings: object,
    ) -> None:
        self._embedder = embedder
        self._vector_repo = vector_repo
        self._settings = settings

    def rerank(
        self,
        semantic_query: str,
        hits: list[SearchHit],
        final_limit: int | None = None,
    ) -> list[SearchHit]:
        if not hits:
            return []
        query_vec = self._embedder.encode([semantic_query])[0]
        scores = self._vector_repo.get_min_distance_for_tdocs(
            [h.tdoc_id for h in hits], query_vec,
        )
        decorated: list[tuple[float, int, SearchHit]] = []
        any_real = False
        for idx, hit in enumerate(hits):
            entry = scores.get(hit.tdoc_id)
            if entry is None:
                score = self.MISSING_FLOOR
            else:
                score = -entry[0]  # higher = better
                any_real = True
            decorated.append((score, idx, hit))
        if not any_real:
            logger.warning(
                "semantic rerank: no rows in vec_tdoc_embeddings; "
                "falling back to FTS5 order"
            )
        # Sort by score desc; on ties preserve input order via ``idx``.
        decorated.sort(key=lambda t: (t[0], -t[1]), reverse=True)
        ordered = [h for _, _, h in decorated]
        if final_limit is not None:
            return ordered[:final_limit]
        return ordered
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_semantic_reranker.py -v`
Expected: 9 passes.

- [ ] **Step 5: Lint and commit**

```bash
ruff check src/doc3gpp/services/semantic_reranker.py tests/unit/test_semantic_reranker.py
git add src/doc3gpp/services/semantic_reranker.py tests/unit/test_semantic_reranker.py
git commit -m "feat(search): add SemanticReranker (cosine distance, MISSING_FLOOR)"
```

---

## Task 5: Wire `SemanticReranker` into `build_search_service`

**Files:**
- Modify: `src/doc3gpp/services/factory.py:269-317` (`build_search_service`)
- Modify: `tests/unit/test_factory.py` (if it exists) or `tests/unit/test_search_service.py` (add a factory test)

**Interfaces:**
- Consumes: `Settings`, `SearchIndexRepository` (Protocol), `EmbeddingReranker` (Protocol)
- Produces: `build_search_service` chooses `SemanticReranker(embedder, vector_repo, settings)` when `settings.search.enabled and settings.semantic_search.enabled` and the embedder + vector_repo construct cleanly; otherwise `PassthroughReranker()`. Existing `reranker=` injection argument still wins.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_search_service.py`:

```python
def test_factory_chooses_semantic_reranker_when_both_enabled(monkeypatch):
    from doc3gpp.services import factory as f
    from doc3gpp.services.search_service import SearchService
    from doc3gpp.services.semantic_reranker import SemanticReranker

    class FakeSettings:
        class search:
            enabled = True
        class semantic_search:
            enabled = True
            embedding_model = "fake-model"

    monkeypatch.setattr(f, "get_settings", lambda: FakeSettings())
    fake_embedder = MagicMock()
    fake_vector_repo = MagicMock()
    monkeypatch.setattr(
        f, "SentenceTransformerEmbedder", lambda _model: fake_embedder,
    )
    monkeypatch.setattr(
        f, "SQLAlchemyVectorIndexRepository", lambda: fake_vector_repo,
    )
    fake_engine = MagicMock()
    fake_engine.dialect.name = "sqlite"
    monkeypatch.setattr(f, "get_engine", lambda: fake_engine)
    # Stub the FTS5 repo construction.
    monkeypatch.setattr(
        f, "SQLAlchemySearchIndexRepository", lambda: MagicMock(),
    )

    svc = f.build_search_service(FakeSettings())
    assert isinstance(svc, SearchService)
    # Reach the private attr to assert which reranker was wired in.
    assert isinstance(svc._reranker, SemanticReranker)


def test_factory_falls_back_to_passthrough_when_semantic_disabled(monkeypatch):
    from doc3gpp.services import factory as f
    from doc3gpp.services.search_service import PassthroughReranker, SearchService

    class FakeSettings:
        class search:
            enabled = True
        class semantic_search:
            enabled = False

    monkeypatch.setattr(f, "get_settings", lambda: FakeSettings())
    fake_engine = MagicMock()
    fake_engine.dialect.name = "sqlite"
    monkeypatch.setattr(f, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(
        f, "SQLAlchemySearchIndexRepository", lambda: MagicMock(),
    )

    svc = f.build_search_service(FakeSettings())
    assert isinstance(svc, SearchService)
    assert isinstance(svc._reranker, PassthroughReranker)


def test_factory_falls_back_to_passthrough_when_embedder_unavailable(monkeypatch):
    from doc3gpp.services import factory as f
    from doc3gpp.services.search_service import PassthroughReranker, SearchService
    from doc3gpp.models.semantic_search import EmbedderUnavailableError

    class FakeSettings:
        class search:
            enabled = True
        class semantic_search:
            enabled = True
            embedding_model = "fake-model"

    monkeypatch.setattr(f, "get_settings", lambda: FakeSettings())
    fake_engine = MagicMock()
    fake_engine.dialect.name = "sqlite"
    monkeypatch.setattr(f, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(
        f, "SQLAlchemySearchIndexRepository", lambda: MagicMock(),
    )

    def _raise(_model):
        raise EmbedderUnavailableError("nope")

    monkeypatch.setattr(
        f, "SentenceTransformerEmbedder", _raise,
    )

    svc = f.build_search_service(FakeSettings())
    assert isinstance(svc, SearchService)
    assert isinstance(svc._reranker, PassthroughReranker)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_search_service.py -k factory_ -v`
Expected: 3 failures (`AssertionError` on the reranker isinstance check; today the factory unconditionally returns `PassthroughReranker`).

- [ ] **Step 3: Update `build_search_service`**

In `src/doc3gpp/services/factory.py`, replace the `if reranker is None: ...` block (lines 309-314):

```python
    if reranker is None:
        from doc3gpp.models.semantic_search import (
            EmbedderUnavailableError,
            VectorIndexUnavailableError,
        )
        from doc3gpp.services.search_service import PassthroughReranker
        from doc3gpp.services.semantic_reranker import SemanticReranker

        # Use SemanticReranker when both the FTS5 and semantic
        # stacks are enabled and the embedder + vector repo can be
        # constructed. Any failure (missing extra, model load
        # error, sqlite-vec missing) falls back to PassthroughReranker
        # so the FTS5 read path still works.
        if (
            settings.search.enabled
            and settings.semantic_search.enabled
        ):
            try:
                from doc3gpp.services.embedding.embedder import (
                    SentenceTransformerEmbedder,
                )
                from doc3gpp.storage.repositories.vector_sql import (
                    SQLAlchemyVectorIndexRepository,
                )
                embedder = SentenceTransformerEmbedder(
                    settings.semantic_search.embedding_model,
                )
                vector_repo = SQLAlchemyVectorIndexRepository()
                reranker = SemanticReranker(
                    embedder=embedder, vector_repo=vector_repo,
                    settings=settings,
                )
            except (
                VectorIndexUnavailableError,
                EmbedderUnavailableError,
            ):
                reranker = PassthroughReranker()
        else:
            reranker = PassthroughReranker()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_search_service.py -k factory_ -v`
Expected: 3 passes.

- [ ] **Step 5: Lint and commit**

```bash
ruff check src/doc3gpp/services/factory.py tests/unit/test_search_service.py
git add src/doc3gpp/services/factory.py tests/unit/test_search_service.py
git commit -m "feat(factory): wire SemanticReranker into build_search_service"
```

---

## Task 6: Update `search_command` CLI: drop `--rerank`, add `--sem-query`

**Files:**
- Modify: `src/doc3gpp/cli.py:4144-4223` (`search_command` body)
- Modify: `tests/unit/test_cli_search_query.py` (existing or new test file for `search query`)

**Interfaces:**
- Consumes: existing `search_command(query, --tsg, --meeting, --meeting-id, --tdoc-id, --release, --spec, --since, --until, --limit, --format, --compact, --snippet-tokens, --explain, --quiet)`
- Produces: new `search_command(..., --sem-query: str | None = None)`, `--rerank` removed. When `--sem-query` is set and non-empty, the FTS5 repo is asked for `limit * search.search_fanout_factor` candidates; the reranker is then called with `final_limit=limit`. When `--sem-query` is absent or empty, the FTS5 repo is asked for `limit` candidates and the reranker is not called (the existing `PassthroughReranker` no-op preserves today's behavior for builds without the semantic stack).

- [ ] **Step 1: Write the failing tests**

Inspect `tests/unit/test_cli_search_query.py`. If the file does not exist, create it. The new tests assert:

```python
def test_search_query_no_sem_query_does_not_invoke_reranker(monkeypatch):
    """Without --sem-query the CLI bypasses the reranker (today's behavior)."""
    from typer.testing import CliRunner
    from doc3gpp import cli
    from doc3gpp.services.search_service import SearchService

    runner = CliRunner()
    fake_svc = MagicMock(spec=SearchService)
    fake_svc._repo.search.return_value = []  # type: ignore[attr-defined]
    fake_svc.status.return_value = MagicMock(is_stale=False)
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_search_service",
        lambda: fake_svc,
    )
    from doc3gpp.cli import search_app
    result = runner.invoke(
        search_app, ["query", "anything"],
    )
    assert result.exit_code == 0
    fake_svc._reranker.rerank.assert_not_called()  # type: ignore[attr-defined]


def test_search_query_sem_query_invokes_reranker_with_fanout(monkeypatch):
    """--sem-query triggers fanout filters and a rerank call."""
    from typer.testing import CliRunner
    from doc3gpp.services.search_service import SearchService
    from doc3gpp.models.search import SearchFilters

    fake_svc = MagicMock(spec=SearchService)
    fake_svc._repo.search.return_value = []  # type: ignore[attr-defined]
    fake_svc.status.return_value = MagicMock(is_stale=False)

    captured: dict = {}
    def _capture_rerank(semantic_query, hits, final_limit=None):
        captured["semantic_query"] = semantic_query
        captured["hits"] = hits
        captured["final_limit"] = final_limit
        return []
    fake_svc._reranker.rerank.side_effect = _capture_rerank  # type: ignore[attr-defined]

    fake_settings = MagicMock()
    fake_settings.search.search_fanout_factor = 4
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_search_service", lambda: fake_svc,
    )
    monkeypatch.setattr(
        "doc3gpp.cli.get_settings", lambda: fake_settings,
    )

    from doc3gpp.cli import search_app
    runner = CliRunner()
    result = runner.invoke(
        search_app, ["query", "R5-1", "--sem-query", "TTCN handover"],
    )
    assert result.exit_code == 0
    assert captured["semantic_query"] == "TTCN handover"
    assert captured["final_limit"] == 20  # default
    # The repo was called with filters whose limit == 20 * 4 == 80.
    call_args = fake_svc._repo.search.call_args  # type: ignore[attr-defined]
    filters = call_args[0][1]
    assert isinstance(filters, SearchFilters)
    assert filters.limit == 80


def test_search_query_sem_query_empty_string_treated_as_none(monkeypatch):
    """--sem-query '' is a no-op (no rerank, no embedder call)."""
    from typer.testing import CliRunner
    from doc3gpp.services.search_service import SearchService

    fake_svc = MagicMock(spec=SearchService)
    fake_svc._repo.search.return_value = []  # type: ignore[attr-defined]
    fake_svc.status.return_value = MagicMock(is_stale=False)
    monkeypatch.setattr(
        "doc3gpp.services.factory.build_search_service", lambda: fake_svc,
    )
    from doc3gpp.cli import search_app
    runner = CliRunner()
    result = runner.invoke(search_app, ["query", "R5-1", "--sem-query", ""])
    assert result.exit_code == 0
    fake_svc._reranker.rerank.assert_not_called()  # type: ignore[attr-defined]


def test_search_query_rerank_flag_raises_bad_parameter():
    from typer.testing import CliRunner
    from doc3gpp.cli import search_app
    runner = CliRunner()
    result = runner.invoke(search_app, ["query", "R5-1", "--rerank"])
    assert result.exit_code != 0
    assert "--rerank" in result.output or "--rerank" in (result.stderr or "")
```

If a `tests/unit/test_cli_search_query.py` already exists, **append** the new tests; do not duplicate the file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_cli_search_query.py -v` (or the equivalent test path).
Expected: 4 failures. Today the CLI doesn't accept `--sem-query` (Typer exits 2 with "No such option"), and the `--rerank` flag still exists (so its `BadParameter` test fails on the error message).

- [ ] **Step 3: Update `search_command`**

In `src/doc3gpp/cli.py`, modify `search_command` (lines 4144-4223). The change has two parts: signature and body.

Signature (replace the option block, keeping `query` and other options intact):

```python
    sem_query: str | None = typer.Option(
        None, "--sem-query",
        help=(
            "Reorder the FTS5 hit list by cosine similarity to this "
            "natural-language string. The FTS5 path fetches "
            "`limit * search.search_fanout_factor` candidates "
            "(default 4x) before the reranker truncates back to "
            "`limit`. Requires the [semantic] extra + vector "
            "index; otherwise the command exits with code 1."
        ),
    ),
    # NOTE: the old --rerank flag was removed. It is rejected at parse
    # time by the explicit @typer.Option below so callers get a clear
    # migration message.
```

Add an explicit-rejection option for the old `--rerank` flag. The simplest way is to insert an unrelated `typer.Option` that the parser still sees, and then raise `BadParameter` inside the body. Use the `ctx` argument that's already in the signature:

```python
    ctx: typer.Context,
```

Inside the body, before the `typer.echo("search disabled in settings", ...)` check, add:

```python
    # Reject the removed --rerank flag.
    if ctx.get_parameter_source("--rerank") is not None:
        raise typer.BadParameter(
            "--rerank was removed; use --sem-query to enable semantic rerank"
        )
```

Body (replace lines 4211-4222 — the search call block):

```python
    from doc3gpp.settings import get_settings
    settings = get_settings()
    fanout = limit * settings.search.search_fanout_factor
    filters = SearchFilters(
        tsg=tsg, meeting=meeting, meeting_id=meeting_id, tdoc_id=tdoc_id,
        release=release, spec=spec, since=since, until=until,
        limit=fanout if sem_query else limit,
    )
    if explain:
        _emit_explain(
            match_expr=match_expr,
            snippet_tokens=snippet_tokens,
            repo=svc._repo,  # noqa: SLF001
        )
    try:
        raw_hits = svc._repo.search(  # noqa: SLF001
            match_expr, filters, snippet_tokens=snippet_tokens,
        )
        if sem_query:
            hits = svc._reranker.rerank(  # noqa: SLF001
                semantic_query=sem_query, hits=raw_hits,
                final_limit=limit,
            )
        else:
            hits = raw_hits
    except SearchError:
        typer.echo("search index corrupt; run `doc3gpp search index --rebuild`", err=True)
        raise typer.Exit(code=3)
    _render_search_hits(hits, format=format, compact=compact)
    _emit_search_status(svc, quiet=quiet)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_cli_search_query.py -v`
Expected: 4 passes. The pre-existing `search_command` tests (if any) should also pass — confirm the existing test runner doesn't import `--rerank` anywhere else.

- [ ] **Step 5: Lint and commit**

```bash
ruff check src/doc3gpp/cli.py tests/unit/test_cli_search_query.py
git add src/doc3gpp/cli.py tests/unit/test_cli_search_query.py
git commit -m "feat(cli): add --sem-query to search query, remove --rerank"
```

---

## Task 7: Update the TOML example + AGENTS.md + docs/cli.md

**Files:**
- Modify: `src/doc3gpp/data/doc3gpp.toml.example` (add the new field under `[search]`)
- Modify: `AGENTS.md` (add a "Where to look" row)
- Modify: `docs/cli.md` (document the new flag, the new knob, the removal of `--rerank`, the empty-vector fallback warning)

- [ ] **Step 1: Update the TOML example**

In `src/doc3gpp/data/doc3gpp.toml.example`, find the `[search]` block. Append (preserving the existing fields):

```toml
# When `search query --sem-query` is used, the FTS5 path fetches
# limit * search_fanout_factor candidates before the semantic
# reranker truncates back to limit. Higher values give the
# reranker more to work with at the cost of more vector lookups
# per query. Only honored when --sem-query is supplied.
search_fanout_factor = 4
```

- [ ] **Step 2: Update AGENTS.md**

In `AGENTS.md`, in the "Where to look" table, modify the existing "Add a domain keyword / NER" row (which currently mentions `EmbeddingReranker`) so the body reads:

```
| Add a search rerank flag / knob | `src/doc3gpp/services/semantic_reranker.py` + `src/doc3gpp/services/search_service.py` (`PassthroughReranker`) + `src/doc3gpp/settings/schema.py` (`SearchSettings.search_fanout_factor`) + `src/doc3gpp/cli.py` (`search_command`) | The `EmbeddingReranker` Protocol lives in `src/doc3gpp/repository/protocols.py`. Vector lookup helper: `VectorIndexRepository.get_min_distance_for_tdocs`. |
```

Also amend the existing "Tune the FTS5 search subsystem" row to mention `search_fanout_factor`.

- [ ] **Step 3: Update docs/cli.md**

In `docs/cli.md`, in the `search query` section:
- Add a row to the flag table: `--sem-query STR` — "Reorder hits by cosine similarity to STR. Requires the [semantic] extra + vec_tdoc_embeddings. See `search.search_fanout_factor`."
- Remove the `--rerank` row.
- Add a subsection "## Semantic rerank" that explains the fanout knob, the empty-vector fallback, the migration from `--rerank`.
- Add the new `[search].search_fanout_factor` knob to the config reference section.

- [ ] **Step 4: Lint and commit**

```bash
ruff check .  # ensure docs didn't accidentally touch any .py
git add src/doc3gpp/data/doc3gpp.toml.example AGENTS.md docs/cli.md
git commit -m "docs: document --sem-query, search_fanout_factor, --rerank removal"
```

---

## Task 8: End-to-end integration test for `search query --sem-query`

**Files:**
- Create: `tests/integration/test_search_query_sem_rerank.py`

**Interfaces:**
- Consumes: a sqlite + sqlite-vec DB seeded with `tdocs` + `tdoc_search` + `vec_tdoc_embeddings`; a mocked `SentenceTransformerEmbedder`; the real `build_search_service` factory
- Produces: assertions on the CLI exit code, the rendered output order, the embedder call count, the warn-on-empty-vector behavior.

- [ ] **Step 1: Write the integration test**

Create `tests/integration/test_search_query_sem_rerank.py`:

```python
"""End-to-end test for `search query --sem-query` over sqlite + sqlite-vec."""
from __future__ import annotations

import logging
from unittest.mock import patch

import numpy as np
import pytest
from typer.testing import CliRunner

from doc3gpp import cli
from doc3gpp.cli import search_app
from doc3gpp.services.embedding.embedder import SentenceTransformerEmbedder
from doc3gpp.storage.repositories.vector_sql import (
    MAX_CHUNKS_PER_TDOC,
    SQLAlchemyVectorIndexRepository,
)


@pytest.fixture
def seeded_engine(tmp_path, monkeypatch):
    """Build a sqlite engine with tdocs + tdoc_search + vec_tdoc_embeddings."""
    from sqlalchemy import create_engine, text
    from doc3gpp.storage.db import session as session_module

    db = tmp_path / "fts5_sem_rerank.db"
    engine = create_engine(f"sqlite+pysqlite:///{db}")
    monkeypatch.setattr(session_module, "get_engine", lambda: engine)
    with engine.begin() as conn:
        # tdocs (minimal schema)
        conn.execute(text(
            "CREATE TABLE tdocs ("
            " tdoc_id TEXT PRIMARY KEY,"
            " title TEXT,"
            " meeting_id INTEGER,"
            " release TEXT,"
            " spec TEXT,"
            " uploaded_date TEXT,"
            " ftp_url TEXT)"
        ))
        conn.execute(text(
            "CREATE TABLE meetings ("
            " meeting_id INTEGER PRIMARY KEY,"
            " name TEXT,"
            " tsg TEXT)"
        ))
        # tdoc_search (FTS5 virtual table)
        conn.execute(text(
            "CREATE VIRTUAL TABLE tdoc_search USING fts5("
            " tdoc_id UNINDEXED,"
            " title, content, change_text, ttcn_text, wis_text, meeting_text, tsg_text,"
            " tokenize='unicode61')"
        ))
        # vec_tdoc_embeddings (sqlite-vec)
        conn.execute(text(
            "CREATE VIRTUAL TABLE vec_tdoc_embeddings USING vec0("
            " chunk_id TEXT PRIMARY KEY,"
            " tdoc_id TEXT,"
            " chunk_index INTEGER,"
            " embedding float[4])"
        ))
    return engine


def _seed_tdocs(engine):
    with engine.begin() as conn:
        for tid, title in [
            ("R5-1", "alpha"),
            ("R5-2", "beta"),
            ("R5-3", "gamma"),
        ]:
            conn.execute(
                text(
                    "INSERT INTO tdocs (tdoc_id, title, meeting_id, release, "
                    "spec, uploaded_date, ftp_url) VALUES "
                    "(:t, :ti, 1, 'Rel-18', '38.300', '2026-01-01', :u)"
                ),
                {"t": tid, "ti": title, "u": f"https://x/{tid}.doc"},
            )
            conn.execute(
                text(
                    "INSERT INTO tdoc_search (tdoc_id, title, content, "
                    "change_text, ttcn_text, wis_text, meeting_text, tsg_text) "
                    "VALUES (:t, :ti, :c, '', '', '', 'm1', 'S1')"
                ),
                {"t": tid, "ti": title, "c": title},
            )


def _seed_vectors(engine, mappings):
    """``mappings``: dict[tdoc_id, list[vec]] (4-D)."""
    with engine.begin() as conn:
        for tid, vecs in mappings.items():
            for i, v in enumerate(vecs):
                conn.execute(
                    text(
                        "INSERT INTO vec_tdoc_embeddings "
                        "(chunk_id, tdoc_id, chunk_index, embedding) "
                        "VALUES (:c, :t, :i, :e)"
                    ),
                    {
                        "c": f"{tid}#{i}", "t": tid, "i": i,
                        "e": np.asarray(v, dtype=np.float32).tobytes(),
                    },
                )


class _FakeEmbedder:
    """Encodes every input to a fixed 4-D vector; tracks call count."""
    dim = 4
    def __init__(self, fixed: np.ndarray):
        self._fixed = np.asarray(fixed, dtype=np.float32)
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str]) -> np.ndarray:
        self.calls.append(list(texts))
        return np.tile(self._fixed, (len(texts), 1))


def test_sem_query_uses_4x_fanout_then_truncates_to_limit(seeded_engine):
    engine = seeded_engine
    _seed_tdocs(engine)
    _seed_vectors(engine, {
        "R5-1": [[1.0, 0.0, 0.0, 0.0]],
        "R5-2": [[0.0, 1.0, 0.0, 0.0]],
        "R5-3": [[0.0, 0.0, 1.0, 0.0]],
    })
    runner = CliRunner()
    embedder = _FakeEmbedder(np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    with patch.object(
        SentenceTransformerEmbedder, "__init__", lambda self, model: None,
    ), patch.object(
        SentenceTransformerEmbedder, "encode", embedder.encode,
    ):
        result = runner.invoke(
            search_app,
            ["query", "R5-*", "--sem-query", "anything", "--limit", "2"],
        )
    assert result.exit_code == 0
    # Embedder was called exactly once (for the semantic query).
    assert len(embedder.calls) == 1
    assert embedder.calls[0] == ["anything"]
    # Top 2 by cosine distance to [1,0,0,0] are R5-1, then R5-2.
    out_lines = [
        line for line in result.output.splitlines() if line.startswith("R5-")
    ]
    assert out_lines[0].startswith("R5-1")
    assert out_lines[1].startswith("R5-2")


def test_sem_query_empty_vector_index_falls_back_to_fts5_order(
    seeded_engine, caplog
):
    engine = seeded_engine
    _seed_tdocs(engine)
    # No vectors seeded.
    runner = CliRunner()
    embedder = _FakeEmbedder(np.asarray([0.0, 0.0, 0.0, 0.0], dtype=np.float32))
    with patch.object(
        SentenceTransformerEmbedder, "__init__", lambda self, model: None,
    ), patch.object(
        SentenceTransformerEmbedder, "encode", embedder.encode,
    ):
        with caplog.at_level(logging.WARNING):
            result = runner.invoke(
                search_app,
                ["query", "R5-*", "--sem-query", "anything"],
            )
    assert result.exit_code == 0
    assert any(
        "no rows in vec_tdoc_embeddings" in rec.message
        for rec in caplog.records
    )


def test_sem_query_empty_string_is_no_op(seeded_engine):
    engine = seeded_engine
    _seed_tdocs(engine)
    runner = CliRunner()
    embedder = _FakeEmbedder(np.asarray([0.0, 0.0, 0.0, 0.0], dtype=np.float32))
    with patch.object(
        SentenceTransformerEmbedder, "__init__", lambda self, model: None,
    ), patch.object(
        SentenceTransformerEmbedder, "encode", embedder.encode,
    ):
        result = runner.invoke(
            search_app, ["query", "R5-*", "--sem-query", ""],
        )
    assert result.exit_code == 0
    assert embedder.calls == []


def test_sem_query_fts5_zero_results_does_not_encode(seeded_engine):
    engine = seeded_engine
    _seed_tdocs(engine)  # only R5-*, so "nothing" returns 0 hits
    runner = CliRunner()
    embedder = _FakeEmbedder(np.asarray([0.0, 0.0, 0.0, 0.0], dtype=np.float32))
    with patch.object(
        SentenceTransformerEmbedder, "__init__", lambda self, model: None,
    ), patch.object(
        SentenceTransformerEmbedder, "encode", embedder.encode,
    ):
        result = runner.invoke(
            search_app, ["query", "nothing", "--sem-query", "anything"],
        )
    assert result.exit_code == 0
    assert embedder.calls == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/integration/test_search_query_sem_rerank.py -v`
Expected: 4 failures — the CLI doesn't accept `--sem-query` yet, OR the factory hasn't been updated, OR the SQLAlchemy index isn't recognized. Whichever surfaces first.

- [ ] **Step 3: Confirm the prior tasks are landed**

The integration test is the end-to-end safety net for tasks 1-6. By the time you reach this step, all earlier tasks must have been committed and the test_sqlite.sh script must pass (run `./scripts/test_sqlite.sh` to confirm). The integration test should pass with no further code changes.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/integration/test_search_query_sem_rerank.py -v`
Expected: 4 passes.

- [ ] **Step 5: Lint and commit**

```bash
ruff check tests/integration/test_search_query_sem_rerank.py
git add tests/integration/test_search_query_sem_rerank.py
git commit -m "test(search): end-to-end coverage for search query --sem-query"
```

---

## Task 9: Final pass — full sqlite test suite + ruff

- [ ] **Step 1: Run the full sqlite suite**

Run: `./scripts/test_sqlite.sh`
Expected: all tests pass. If the existing `test_protocols_semantic.py:7:35 F401` lint violation (already on `main`) surfaces, ignore it; it is not in this plan's scope.

- [ ] **Step 2: Run ruff over the whole tree**

Run: `ruff check .`
Expected: 0 new violations. The pre-existing `F401` in `tests/unit/test_protocols_semantic.py` is not introduced by this plan.

- [ ] **Step 3: Commit any final touch-ups (if needed)**

If the previous steps surfaced a real issue this plan didn't cover, fix it and commit. Otherwise this task is a no-op commit-wise; close it out without a commit.

---

## Self-Review Checklist (run before declaring the plan complete)

1. **Spec coverage:**
   - `[search].search_fanout_factor` knob — covered by Task 1
   - `EmbeddingReranker` Protocol change — Task 2
   - `PassthroughReranker` honors new signature — Task 2
   - `VectorIndexRepository.get_min_distance_for_tdocs` — Task 3
   - `SemanticReranker` impl — Task 4
   - Factory chooses SemanticReranker — Task 5
   - CLI drops `--rerank`, adds `--sem-query` — Task 6
   - Empty FTS5 result, empty `--sem-query`, empty vector index — Tasks 4, 6, 8
   - TOML example + AGENTS.md + docs/cli.md — Task 7
   - Tests: unit settings, unit service, unit reranker, unit CLI, integration rerank — Tasks 1, 2, 4, 5, 6, 8
2. **Placeholder scan:** no `TBD` / `TODO` / "implement later" in any step.
3. **Type consistency:**
   - `EmbeddingReranker.rerank(semantic_query, hits, final_limit=None) -> list[SearchHit]` introduced in Task 2, consumed by Task 4 (SemanticReranker) and Task 6 (CLI).
   - `VectorIndexRepository.get_min_distance_for_tdocs(tdoc_ids, query_vec) -> dict[str, tuple[float, str] | None]` introduced in Task 3, consumed by Task 4.
   - `Settings.search.search_fanout_factor: int = 4, ge=1, le=64` introduced in Task 1, consumed by Task 6.
   - `SemanticReranker.MISSING_FLOOR: float = float("-inf")` defined in Task 4, used internally only.
4. **Edge cases:**
   - Empty `tdoc_ids` to `get_min_distance_for_tdocs` → empty dict (Task 3 test 1).
   - Missing `vec_tdoc_embeddings` row → `None` value, candidate sorts last (Task 4 test 6).
   - All candidates missing → FTS5 order preserved + one-shot warning (Task 4 test 7 + Task 8 test 2).
   - Empty `--sem-query` → no rerank (Task 6 test 3 + Task 8 test 3).
   - FTS5 returns 0 hits → no embedder call (Task 8 test 4).
   - `--rerank` → `BadParameter` (Task 6 test 4).
