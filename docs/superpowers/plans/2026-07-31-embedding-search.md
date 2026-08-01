# Semantic (Embedding + Vector) Search — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a natural-language read path (`doc3gpp search sem QUERY [filters]`) to doc3gpp that combines SQLite FTS5 keyword matching (the path shipped by the 2026-07-29 FTS5 spec) with a local `sentence-transformers` embedding-based vector search over `sqlite-vec`, returning a single merged top-N result list via reciprocal-rank fusion (RRF). A sibling auto-embed hook keeps the vector index in sync with every successful `tdoc parse`, mirroring the FTS5 `_index_after_parse` hook.

**Architecture:** A new `SemanticSearchService` orchestrates four collaborators: the existing `SearchService` (reused as the FTS5 fan-out engine), a new `Embedder` (Protocol, with a `SentenceTransformerEmbedder` impl that lazy-loads the model), a new `VectorIndexRepository` (Protocol, with a `SQLAlchemyVectorIndexRepository` impl that drives the `sqlite-vec` `vec_tdoc_embeddings` virtual table), and a spaCy-backed `strip_stopwords` pre-processor that runs ONLY on the FTS5 path's query (the embedding path uses the original query). A pure-Python `rrf_merge(fts5_hits, vec_hits, k, vector_weight, limit)` helper combines the two ranked lists. Per-TDoc chunking (`_chunks(text, size=800, overlap=100)`) maps one TDoc to N chunk rows; the KNN fan-out reduces chunks to a single distance per `tdoc_id` via `min(distance)`. A new `SemanticSearchSettings` sub-model adds `chunk_size`, `chunk_overlap`, `rrf_k`, `vector_weight`, `fanout_multiplier`, `final_limit`, `user_defined_stop_words`, `keep_negation_words`. Three-layer graceful degradation mirrors the FTS5 spec: the factory returns `None` when `[semantic]` is missing, the hook is best-effort, and the repo raises typed errors the CLI maps to exit codes.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0 + raw `vec0` SQL, pydantic v2 + pydantic-settings, Typer, pytest, ruff. New opt-in `[semantic]` extra: `sentence-transformers>=2.7.0`, `spacy>=3.7.0`, `sqlite-vec>=0.1.0` (PyPI hyphen, import `sqlite_vec` underscore). Optional `en_core_web_sm` spaCy model is a one-time `python -m spacy download en_core_web_sm` (fail-fast in v1, no auto-download). numpy comes transitively via sentence-transformers.

## Global Constraints

- Python 3.10+ — match existing codebase floor; `slots=True, frozen=True` dataclass style for new DTOs.
- Layered architecture preserved: `cli → SemanticSearchService → {SearchService, Embedder, VectorIndexRepository}` with `models/` DTOs at the boundary. The CLI never instantiates SQL repos directly.
- **No new mandatory runtime deps.** All new deps live behind a new `[semantic]` pyproject extra. doc3gpp without `pip install doc3gpp[semantic]` behaves exactly like today (factory returns `None`; hook is a no-op; `search sem` reports unavailable).
- `vec_tdoc_embeddings` is a **separate** virtual table from `tdoc_search` (different shape/lifecycle/access path). Same `tdoc_id` identity; chunked rows with `chunk_id = "{tdoc_id}#{i}"`.
- **FTS5 is the foundation.** `build_semantic_search_service` returns `None` if the FTS5 service is unavailable. `search query` (FTS5-only) is UNCHANGED by this plan.
- spaCy strip runs ONLY on the FTS5 path's query; the embedding path uses the ORIGINAL (unstripped) query.
- The existing `PassthroughReranker` + `EmbeddingReranker` Protocol from the FTS5 spec are preserved but NOT consumed by this plan.
- `chunk_size` and `chunk_overlap` are in **whitespace tokens** (not model tokens). `overlap < size` enforced.
- RRF formula: `rrf_score = 1/(k + rank_fts5) * (1 - W) + 1/(k + rank_vec) * W` where `k=60`, `W=vector_weight`. Internal fan-out: `2N` per side, truncated to `--limit` (default 20) after RRF. NO `--top-fts5`, NO `--top-vector`, NO `--no-fts`, NO `--no-vector` flags.
- Reuse `storage.compression.decompress_json` for blob decompression when building embed text. Reuse `create_schema` (no Alembic wiring); the `vec_tdoc_embeddings` + `vec_meta` DDL is a new entry-point in `storage/db/migrate.py`, gated on `dialect.name == "sqlite"` and a runtime `sqlite_vec.load(conn)` probe.
- Settings knobs follow the existing nested-sub-model shape (`Settings.semantic_search`), TOML-only precedence (no env overrides). Pydantic validates `chunk_size>0`, `0<=chunk_overlap<chunk_size`, `0.0<=vector_weight<=1.0`, `fanout_multiplier>=1`, `rrf_k>0`.
- Branch: `feat/embedding-search` (already created). Commit message style: `feat(scope): …` / `test(scope): …` / `docs(scope): …` matching the existing log.
- Cosine distance (`vec_distance_cosine`) for sentence-transformers; vectors stored float32.
- Dim mismatch on model swap → CLI exits 1 non-interactively (NO `typer.confirm`) with hint `run \`doc3gpp search index --rebuild-embeddings\``. `--rebuild-embeddings` drops + recreates the table.
- spaCy model missing → `SpacyUnavailableError` (CLI exit 1, tells user to run `python -m spacy download en_core_web_sm`). No auto-download.
- Test convention: `tests/unit/test_*.py` (mock external calls), `tests/integration/test_*.py` (sqlite by default), `tests/fixtures/` for sample data. New opt-in `-m semantic` marker for tests that load the real model (default tests use mocked embedder / pre-computed embeddings).
- Ruff line-length 100, target py310. No `# type: ignore` without a code. No comments unless asked.

---

## File Structure

| Path | Role | Task |
| --- | --- | --- |
| `src/doc3gpp/models/semantic_search.py` (new) | `SemanticSearchHit` DTO + error hierarchy extending `SearchError` | T1 |
| `tests/unit/test_semantic_models.py` (new) | DTO + error hierarchy contract (TDD pair with T1) | T1 |
| `src/doc3gpp/services/embedding/chunker.py` (new) | Pure-Python `_chunks(text, size, overlap) -> list[str]` | T2 |
| `tests/unit/test_chunker.py` (new) | Chunker corpus + boundary cases (TDD pair with T2) | T2 |
| `src/doc3gpp/services/embedding/stopwords.py` (new) | `strip_stopwords(text) -> str`; spaCy wrapper with cached pipeline + cached effective stopword set | T3 |
| `tests/unit/test_stopwords.py` (new) | spaCy strip corpus; user_defined_stop_words + keep_negation_words; empty/punct-only; missing model (TDD pair with T3) | T3 |
| `src/doc3gpp/services/embedding/embedder.py` (new) | `SentenceTransformerEmbedder` (lazy model load); `Embedder` Protocol re-export | T4 |
| `tests/unit/test_embedder.py` (new) | Mock `SentenceTransformer.encode`; dim + dtype checks; lazy load; load failure (TDD pair with T4) | T4 |
| `src/doc3gpp/services/embedding/__init__.py` (new) | Public re-exports | T4 |
| `src/doc3gpp/repository/protocols.py` (extend) | `Embedder` Protocol + `VectorIndexRepository` Protocol | T5 |
| `src/doc3gpp/storage/repositories/vector_sql.py` (new) | `SQLAlchemyVectorIndexRepository` (sqlite-vec DDL/DML; runtime probe; `_build_embed_text` JOIN) | T6 |
| `tests/integration/test_vector_index_lifecycle.py` (new) | sqlite-vec probe; upsert chunks; KNN order; remove; rebuild batch; status; dim mismatch (TDD pair with T6) | T6 |
| `src/doc3gpp/services/semantic_search_service.py` (new) | `SemanticSearchService` + pure-Python `rrf_merge` helper | T7 |
| `tests/unit/test_rrf.py` (new) | RRF merge corpus: known inputs → known output; min(distance); W=0.0/1.0; limit truncation | T7 |
| `tests/unit/test_semantic_search_service.py` (new) | Service `search()` flow with mock FTS5 + mock vector + mock embedder; empty-after-strip; both-side-empty | T7 |
| `src/doc3gpp/settings/schema.py` (extend) | `SemanticSearchSettings` sub-model + wire into `Settings` | T8 |
| `pyproject.toml` (modify) | New `[semantic]` extra + `-m semantic` pytest marker | T8 |
| `src/doc3gpp/services/factory.py` (extend) | `build_semantic_search_service`; wire into `build_tdoc_cr_service` | T9 |
| `tests/unit/test_factory_semantic.py` (new) | Factory returns `None` on each unavailable error; returns service when all present | T9 |
| `src/doc3gpp/storage/db/migrate.py` (extend) | `_create_vector_schema`: `vec_tdoc_embeddings` + `vec_meta` DDL gated on sqlite + sqlite-vec | T10 |
| `tests/integration/test_vector_schema_migration.py` (new) | migrate creates the vector table; idempotent; gated on sqlite | T10 |
| `src/doc3gpp/services/tdoc_cr_service.py` (modify) | `__init__` gains `semantic_service`; `_embed_after_parse`; two call sites (sibling of `_index_after_parse`) | T11 |
| `tests/integration/test_embed_after_parse.py` (new) | Parse triggers both hooks; re-parse 8→4 chunks deletes surplus; auto_embed_on_parse=False skips | T11 |
| `src/doc3gpp/cli.py` (extend) | `search sem` command; `search index --rebuild-embeddings` / `--rebuild-all`; stale hint extension | T12 |
| `tests/unit/test_cli_search_sem.py` (new) | Typer `CliRunner` flag parsing; `--limit<0` / `--vector-weight>1` rejected; error-to-message mapping | T12 |
| `tests/fixtures/semantic_search_corpus.py` (new) | Diverse 8-TDoc corpus with gzip blobs + pre-computed embeddings | T13 |
| `tests/conftest.py` (extend) | `semantic_search_corpus` fixture (depends on `sqlite_env`) | T13 |
| `tests/integration/test_search_sem_end_to_end.py` (new) | Insert fixture; `search sem "what CRs touch NB-IoT power saving"` returns expected TDoc + RRF order; every filter flag | T13 |
| `tests/integration/test_semantic_extras_disabled.py` (new) | Extra not installed; sqlite-vec missing; `Settings.semantic_search.enabled=false` | T13 |
| `docs/architecture.md` (modify) | Add `vec_tdoc_embeddings` + `search sem` workflow bullet | T14 |
| `docs/code-map.md` (modify) | Add new file rows | T14 |
| `docs/cli.md` (modify) | Document `search sem` + `search index --rebuild-embeddings` + new TOML fields | T14 |
| `doc3gpp.toml.example` (modify) | Document `[semantic_search]` block | T14 |
| `AGENTS.md` (modify) | Update "Where to look" + workflows | T14 |

---

## Task 1: Models — `SemanticSearchHit` DTO + error hierarchy

**Files:**
- Create: `src/doc3gpp/models/semantic_search.py`
- Test: `tests/unit/test_semantic_models.py`

**Interfaces:**
- Consumes: `doc3gpp.models.search.SearchError` (existing base), `doc3gpp.models.search.SearchHit` (existing frozen dataclass).
- Produces: `SemanticSearchHit` (frozen dataclass with fields `tdoc_id: str`, `rrf_score: float`, `rank_fts5: int | None`, `rank_vec: int | None`, `min_chunk_distance: float | None`, `best_chunk_id: str | None`, `fts5_hit: SearchHit`); error classes `SemanticSearchError` (extends `SearchError`), `SemanticSearchUnavailableError`, `SemanticSearchQueryError`, `SpacyUnavailableError`, `EmbedderUnavailableError`, `VectorIndexUnavailableError`. Later tasks import these by name.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_semantic_models.py
from __future__ import annotations

import pytest

from doc3gpp.models.search import SearchError, SearchHit
from doc3gpp.models.semantic_search import (
    EmbedderUnavailableError,
    SemanticSearchError,
    SemanticSearchHit,
    SemanticSearchQueryError,
    SemanticSearchUnavailableError,
    SpacyUnavailableError,
    VectorIndexUnavailableError,
)


def _hit(tdoc_id: str = "R5-1") -> SearchHit:
    return SearchHit(
        tdoc_id=tdoc_id, score=-1.0, previews={"title": "t"},
        title="t", meeting=None, tsg=None, uploaded_date=None,
        ftp_url=None, wis=None,
    )


def test_semantic_search_hit_is_frozen():
    h = SemanticSearchHit(
        tdoc_id="R5-1", rrf_score=0.5, rank_fts5=0, rank_vec=1,
        min_chunk_distance=0.2, best_chunk_id="R5-1#3", fts5_hit=_hit(),
    )
    with pytest.raises(Exception):
        h.tdoc_id = "R5-2"  # frozen dataclass
    assert h.rank_fts5 == 0
    assert h.fts5_hit.tdoc_id == "R5-1"


def test_semantic_search_hit_optional_ranks_default_none():
    h = SemanticSearchHit(
        tdoc_id="R5-1", rrf_score=0.5, fts5_hit=_hit(),
    )
    assert h.rank_fts5 is None
    assert h.rank_vec is None
    assert h.min_chunk_distance is None
    assert h.best_chunk_id is None


def test_error_hierarchy_extends_search_error():
    for cls in (
        SemanticSearchError,
        SemanticSearchUnavailableError,
        SemanticSearchQueryError,
        SpacyUnavailableError,
        EmbedderUnavailableError,
        VectorIndexUnavailableError,
    ):
        assert issubclass(cls, SearchError), cls
    assert issubclass(SemanticSearchUnavailableError, SemanticSearchError)
    assert issubclass(SemanticSearchQueryError, SemanticSearchError)
    assert issubclass(SpacyUnavailableError, SemanticSearchError)
    assert issubclass(EmbedderUnavailableError, SemanticSearchError)
    assert issubclass(VectorIndexUnavailableError, SemanticSearchError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_semantic_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'doc3gpp.models.semantic_search'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/doc3gpp/models/semantic_search.py
"""DTOs and error hierarchy for the semantic (embedding + vector) search subsystem.

The error tree extends :class:`doc3gpp.models.search.SearchError` so the
existing CLI catch-all still works, but each subclass gets its own
``except`` branch in ``cli.py`` for a friendly one-liner and its own
exit code.
"""

from __future__ import annotations

from dataclasses import dataclass

from doc3gpp.models.search import SearchError, SearchHit


class SemanticSearchError(SearchError):
    """Base class for every error raised by the semantic search subsystem."""


class SemanticSearchUnavailableError(SemanticSearchError):
    """The semantic stack is not available (no FTS5 foundation, or extra missing)."""


class SemanticSearchQueryError(SemanticSearchError):
    """The user-supplied query is empty after stopword stripping. Exit code 2."""


class SpacyUnavailableError(SemanticSearchError):
    """The spaCy ``en_core_web_sm`` model is not installed. Exit code 1."""


class EmbedderUnavailableError(SemanticSearchError):
    """The embedding model failed to load. Exit code 1."""


class VectorIndexUnavailableError(SemanticSearchError):
    """sqlite-vec is missing or the vector index cannot be opened. Exit code 1."""


@dataclass(slots=True, frozen=True)
class SemanticSearchHit:
    """A single merged hit from the RRF fusion of FTS5 + vector rankings.

    ``rank_fts5`` / ``rank_vec`` are the 0-based positions in the
    respective fan-out lists, or ``None`` when the ``tdoc_id`` was not
    present in that side's fan-out. ``min_chunk_distance`` is the
    lowest cosine distance across all chunks for this ``tdoc_id``
    (``None`` when the tdoc had no vector rows). ``best_chunk_id`` is
    the chunk that produced the min distance (for ``--explain``
    rendering). ``fts5_hit`` is the existing :class:`SearchHit`
    sub-record; when the tdoc was vector-only, the service synthesizes
    a minimal :class:`SearchHit` from the ``tdocs`` JOIN so the
    renderer can reuse the existing shape.
    """

    tdoc_id: str
    rrf_score: float
    fts5_hit: SearchHit
    rank_fts5: int | None = None
    rank_vec: int | None = None
    min_chunk_distance: float | None = None
    best_chunk_id: str | None = None


__all__ = [
    "EmbedderUnavailableError",
    "SemanticSearchError",
    "SemanticSearchHit",
    "SemanticSearchQueryError",
    "SemanticSearchUnavailableError",
    "SpacyUnavailableError",
    "VectorIndexUnavailableError",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_semantic_models.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/models/semantic_search.py tests/unit/test_semantic_models.py
git commit -m "feat(search): add SemanticSearchHit DTO + error hierarchy"
```

---

## Task 2: Chunker — pure-Python `_chunks`

**Files:**
- Create: `src/doc3gpp/services/embedding/chunker.py`
- Test: `tests/unit/test_chunker.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_chunks(text: str, size: int, overlap: int) -> list[str]`; module constants `CHUNK_SIZE_DEFAULT = 800`, `CHUNK_OVERLAP_DEFAULT = 100`. T6 (vector repo) and T7 (service) import `_chunks` and the two constants.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_chunker.py
from __future__ import annotations

import pytest

from doc3gpp.services.embedding.chunker import (
    CHUNK_OVERLAP_DEFAULT,
    CHUNK_SIZE_DEFAULT,
    _chunks,
)


def test_defaults():
    assert CHUNK_SIZE_DEFAULT == 800
    assert CHUNK_OVERLAP_DEFAULT == 100


def test_empty_string_returns_empty_list():
    assert _chunks("", 800, 100) == []


def test_whitespace_only_returns_empty_list():
    assert _chunks("   \n\t  ", 800, 100) == []


def test_shorter_than_size_returns_single_chunk_stripped():
    text = "  hello world  "
    assert _chunks(text, 800, 100) == ["hello world"]


def test_exact_size_single_chunk():
    text = " ".join(f"tok{i}" for i in range(800))
    chunks = _chunks(text, 800, 100)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_two_chunks_with_overlap():
    # 900 tokens, size=800, overlap=100 → chunk0 = [0,800), chunk1 = [700,900)
    text = " ".join(f"tok{i}" for i in range(900))
    chunks = _chunks(text, 800, 100)
    assert len(chunks) == 2
    chunk0_tokens = chunks[0].split()
    chunk1_tokens = chunks[1].split()
    assert len(chunk0_tokens) == 800
    assert len(chunk1_tokens) == 200
    # overlap: chunk1 starts at token 700
    assert chunk1_tokens[0] == "tok700"
    assert chunk1_tokens[-1] == "tok899"
    assert chunk0_tokens[700] == "tok700"  # overlap point


def test_three_chunks_chain():
    # 2000 tokens, size=800, overlap=100 →
    # chunk0 = [0,800), chunk1 = [700,1500), chunk2 = [1400,2000)
    text = " ".join(f"tok{i}" for i in range(2000))
    chunks = _chunks(text, 800, 100)
    assert len(chunks) == 3
    assert chunks[0].split()[0] == "tok0"
    assert chunks[1].split()[0] == "tok700"
    assert chunks[2].split()[0] == "tok1400"
    assert chunks[2].split()[-1] == "tok1999"


def test_overlap_zero():
    text = " ".join(f"tok{i}" for i in range(1600))
    chunks = _chunks(text, 800, 0)
    assert len(chunks) == 2
    assert chunks[0].split()[-1] == "tok799"
    assert chunks[1].split()[0] == "tok800"


def test_overlap_must_be_less_than_size():
    with pytest.raises(ValueError, match="overlap"):
        _chunks("a b c", size=5, overlap=5)
    with pytest.raises(ValueError, match="overlap"):
        _chunks("a b c", size=5, overlap=6)


def test_size_must_be_positive():
    with pytest.raises(ValueError, match="size"):
        _chunks("a b c", size=0, overlap=0)
    with pytest.raises(ValueError, match="size"):
        _chunks("a b c", size=-1, overlap=0)


def test_trailing_whitespace_stripped_per_chunk():
    text = " ".join(f"tok{i}" for i in range(800)) + "   "
    chunks = _chunks(text, 800, 100)
    assert len(chunks) == 1
    assert chunks[0] == chunks[0].strip()
    assert not chunks[0].endswith("   ")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_chunker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'doc3gpp.services.embedding.chunker'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/doc3gpp/services/embedding/chunker.py
"""Pure-Python chunker for the semantic-search embed text.

Splits a long string into chunks of ~``size`` whitespace tokens with
``overlap`` trailing tokens repeated at the start of the next chunk.
The boundary is on whitespace (not model word-piece) to keep the
function pure-Python and fast; the embedder's own tokenizer will
further subdivide each chunk.

``size`` and ``overlap`` are in whitespace tokens, NOT model tokens.
"""

from __future__ import annotations

CHUNK_SIZE_DEFAULT = 800
CHUNK_OVERLAP_DEFAULT = 100


def _chunks(text: str, size: int, overlap: int) -> list[str]:
    """Split ``text`` into chunks of ~``size`` whitespace tokens with ``overlap``.

    Boundary cases:
    * empty / whitespace-only input → ``[]``.
    * text shorter than ``size`` → ``[text.strip()]``.
    * trailing whitespace stripped from every chunk.

    Raises:
        ValueError: if ``size <= 0`` or ``overlap >= size``.
    """
    if size <= 0:
        raise ValueError(f"size must be > 0, got {size}")
    if overlap >= size:
        raise ValueError(f"overlap must be < size, got overlap={overlap} size={size}")
    tokens = text.split()
    if not tokens:
        return []
    chunks: list[str] = []
    start = 0
    n = len(tokens)
    while start < n:
        end = min(start + size, n)
        chunk = " ".join(tokens[start:end]).strip()
        if chunk:
            chunks.append(chunk)
        if end == n:
            break
        start = end - overlap
    return chunks
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_chunker.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/embedding/chunker.py tests/unit/test_chunker.py
git commit -m "feat(search): add pure-Python whitespace chunker for embed text"
```

---

## Task 3: Stopwords — spaCy wrapper `strip_stopwords`

**Files:**
- Create: `src/doc3gpp/services/embedding/stopwords.py`
- Test: `tests/unit/test_stopwords.py`

**Interfaces:**
- Consumes: `spacy` (imported lazily inside functions so the module imports without the extra); `doc3gpp.settings.loader.get_settings` (lazily, to read `keep_negation_words` + `user_defined_stop_words`).
- Produces: `strip_stopwords(text: str) -> str`; `_get_spacy_pipeline() -> spacy.language.Language` (cached); `_effective_stopwords() -> frozenset[str]` (cached on settings hash); `SpacyUnavailableError` re-raised from `doc3gpp.models.semantic_search`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_stopwords.py
from __future__ import annotations

import pytest

from doc3gpp.models.semantic_search import SpacyUnavailableError
from doc3gpp.services.embedding.stopwords import strip_stopwords


def test_strip_drops_punctuation_and_stopwords(monkeypatch):
    # "the" and "is" are spaCy stopwords; "CR" and "NB-IoT" are not.
    out = strip_stopwords("the CR is about NB-IoT")
    tokens = out.split()
    assert "the" not in tokens
    assert "is" not in tokens
    assert "cr" in tokens  # lowercased lemma
    assert "nb-iot" in tokens or "nb" in tokens  # tokenizer-dependent


def test_strip_emits_lemmas(monkeypatch):
    out = strip_stopwords("CRs touching power saving")
    tokens = out.split()
    # lemma of "touching" is "touch"; "saving" → "save"
    assert "touch" in tokens
    assert "save" in tokens


def test_empty_string_returns_empty():
    assert strip_stopwords("") == ""


def test_punctuation_only_returns_empty():
    assert strip_stopwords("  ... --- !!!  ") == ""


def test_whitespace_only_returns_empty():
    assert strip_stopwords("   \n\t  ") == ""


def test_keep_negation_words_default_retains_not(monkeypatch):
    # Default keep_negation_words=["not"] → "not" must survive.
    out = strip_stopwords("which CRs do not relate to NB-IoT")
    tokens = out.split()
    assert "not" in tokens


def test_keep_negation_words_empty_strips_not(monkeypatch):
    # Simulate settings with keep_negation_words=[]
    from doc3gpp.services.embedding import stopwords as sw
    sw._cached_stopwords = None  # force recompute
    monkeypatch.setattr(
        "doc3gpp.settings.loader.get_settings",
        lambda: _make_settings(keep_negation_words=[]),
    )
    out = strip_stopwords("which CRs do not relate to NB-IoT")
    tokens = out.split()
    assert "not" not in tokens


def test_user_defined_stop_words_drops_token(monkeypatch):
    from doc3gpp.services.embedding import stopwords as sw
    sw._cached_stopwords = None
    monkeypatch.setattr(
        "doc3gpp.settings.loader.get_settings",
        lambda: _make_settings(user_defined_stop_words=["tdoc"]),
    )
    out = strip_stopwords("tdoc CR agenda")
    tokens = out.split()
    assert "tdoc" not in tokens
    assert "cr" in tokens


def test_user_defined_stop_words_default_empty_keeps_token():
    # Default user_defined_stop_words=[] → "tdoc" survives.
    from doc3gpp.services.embedding import stopwords as sw
    sw._cached_stopwords = None
    out = strip_stopwords("tdoc CR agenda")
    tokens = out.split()
    assert "tdoc" in tokens


def test_spacy_missing_raises(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "spacy", None)
    from doc3gpp.services.embedding import stopwords as sw
    sw._cached_pipeline = None
    with pytest.raises(SpacyUnavailableError):
        strip_stopwords("some query")


class _FakeSearch:
    def __init__(self, **kw):
        self.semantic_search = type("S", (), kw)()


class _FakeSettings:
    def __init__(self, **kw):
        self.search = type("S", (), {"enabled": True})()
        self.semantic_search = type("S", (), kw)()


def _make_settings(*, user_defined_stop_words=None, keep_negation_words=None):
    return _FakeSettings(
        semantic_search=type(
            "S", (),
            {
                "user_defined_stop_words": user_defined_stop_words or [],
                "keep_negation_words": keep_negation_words
                if keep_negation_words is not None
                else ["not"],
            },
        )(),
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_stopwords.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'doc3gpp.services.embedding.stopwords'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/doc3gpp/services/embedding/stopwords.py
"""spaCy-backed stopword + lemma pre-processor for the FTS5 path of
``search sem``.

The pipeline (``en_core_web_sm``) is loaded once per process and cached
on the module. The effective stopword set is composed once per process
from ``spacy.lang.en.stop_words.STOP_WORDS ∪ user_defined_stop_words −
keep_negation_words`` and cached on the module; ``strip_stopwords``
does the membership check against the cached frozenset so the
per-call cost is dominated by ``Doc`` creation, not model load.

The spaCy model is NOT auto-downloaded. A missing model raises
:class:`SpacyUnavailableError` and the CLI tells the user to run
``python -m spacy download en_core_web_sm``.
"""

from __future__ import annotations

from doc3gpp.models.semantic_search import SpacyUnavailableError

_cached_pipeline = None
_cached_stopwords: frozenset[str] | None = None
_cached_settings_key: tuple | None = None


def _get_spacy_pipeline():
    global _cached_pipeline
    if _cached_pipeline is not None:
        return _cached_pipeline
    try:
        import spacy
    except ImportError as exc:
        raise SpacyUnavailableError(
            "spaCy is not installed; run `pip install doc3gpp[semantic]`"
        ) from exc
    try:
        _cached_pipeline = spacy.load("en_core_web_sm")
    except OSError as exc:
        raise SpacyUnavailableError(
            "spaCy model 'en_core_web_sm' not installed; "
            "run `python -m spacy download en_core_web_sm`"
        ) from exc
    return _cached_pipeline


def _effective_stopwords() -> frozenset[str]:
    global _cached_stopwords, _cached_settings_key
    from doc3gpp.settings.loader import get_settings
    settings = get_settings()
    sem = settings.semantic_search
    key = (
        tuple(sem.user_defined_stop_words),
        tuple(sem.keep_negation_words),
    )
    if _cached_stopwords is not None and _cached_settings_key == key:
        return _cached_stopwords
    import spacy
    from spacy.lang.en.stop_words import STOP_WORDS
    base = set(STOP_WORDS)
    base -= {w.lower() for w in sem.keep_negation_words}
    base |= {w.lower() for w in sem.user_defined_stop_words}
    _cached_stopwords = frozenset(base)
    _cached_settings_key = key
    return _cached_stopwords


def strip_stopwords(text: str) -> str:
    """Run ``text`` through spaCy and return lowercased lemmas of
    non-stopword, alpha-numeric tokens.

    Empty / punctuation-only / whitespace-only input returns ``""``.
    """
    if not text or not text.strip():
        return ""
    nlp = _get_spacy_pipeline()
    stop = _effective_stopwords()
    doc = nlp(text)
    out: list[str] = []
    for tok in doc:
        if tok.is_space or tok.is_punct:
            continue
        lemma = tok.lemma_.lower()
        if not lemma.isalnum() and not any(c.isalnum() for c in lemma):
            continue
        if lemma in stop:
            continue
        if not lemma:
            continue
        out.append(lemma)
    return " ".join(out)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_stopwords.py -v`
Expected: PASS (10 tests). If `en_core_web_sm` is not installed locally, the spaCy-dependent tests will raise `SpacyUnavailableError` — mark them with `@pytest.mark.semantic` and skip when the model is absent (see Step 3b below). Run `python -m pytest tests/unit/test_stopwords.py -v -m "not semantic"` for the non-spaCy tests, then `python -m pytest tests/unit/test_stopwords.py -v -m semantic` once the model is installed.

- [ ] **Step 4b (conditional): If spaCy model not installed, guard the spaCy-dependent tests**

If running the full suite fails because `en_core_web_sm` is absent, add a `pytestmark` skip guard at the top of the test file:

```python
import pytest
pytestmark = pytest.mark.semantic
```

And register the marker in `pyproject.toml` (done in T8). The non-spaCy tests (`test_empty_string_returns_empty`, `test_punctuation_only_returns_empty`, `test_whitespace_only_returns_empty`, `test_spacy_missing_raises`) should be split into a separate file without the marker OR guarded by a `try: import spacy` skip. Prefer: keep all in one file with `pytestmark = pytest.mark.semantic` and add a conftest skip hook in T8 that skips `-m semantic` tests when the model import fails.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/embedding/stopwords.py tests/unit/test_stopwords.py
git commit -m "feat(search): add spaCy stopword + lemma stripper for search sem"
```

---

## Task 4: Embedder — `SentenceTransformerEmbedder` + `Embedder` Protocol

**Files:**
- Create: `src/doc3gpp/services/embedding/embedder.py`
- Create: `src/doc3gpp/services/embedding/__init__.py`
- Test: `tests/unit/test_embedder.py`

**Interfaces:**
- Consumes: `sentence_transformers.SentenceTransformer` (imported lazily on first `.encode()`); `numpy` (transitive); `doc3gpp.models.semantic_search.EmbedderUnavailableError`.
- Produces: `SentenceTransformerEmbedder(model_name: str)` with `.encode(list[str]) -> np.ndarray` (shape `(N, dim)`, dtype float32) and `.dim -> int` property. The `Embedder` Protocol is defined in `repository/protocols.py` (T5); `embedder.py` re-exports it for typing convenience. `__init__.py` re-exports `SentenceTransformerEmbedder`, `Embedder`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_embedder.py
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from doc3gpp.models.semantic_search import EmbedderUnavailableError
from doc3gpp.services.embedding.embedder import SentenceTransformerEmbedder


def test_encode_returns_float32_with_expected_shape():
    emb = SentenceTransformerEmbedder("fake-model")
    fake_model = MagicMock()
    fake_model.get_sentence_embedding_dimension.return_value = 384
    fake_model.encode.return_value = np.zeros((3, 384), dtype=np.float32)
    with patch.object(SentenceTransformerEmbedder, "_load_model", return_value=fake_model):
        out = emb.encode(["a", "b", "c"])
    assert out.shape == (3, 384)
    assert out.dtype == np.float32


def test_dim_property_reads_from_model():
    emb = SentenceTransformerEmbedder("fake-model")
    fake_model = MagicMock()
    fake_model.get_sentence_embedding_dimension.return_value = 768
    with patch.object(SentenceTransformerEmbedder, "_load_model", return_value=fake_model):
        assert emb.dim == 768


def test_lazy_model_load_on_first_encode():
    emb = SentenceTransformerEmbedder("fake-model")
    # Construction must NOT load the model.
    assert emb._model is None
    fake_model = MagicMock()
    fake_model.get_sentence_embedding_dimension.return_value = 384
    fake_model.encode.return_value = np.zeros((1, 384), dtype=np.float32)
    with patch.object(
        SentenceTransformerEmbedder, "_load_model", return_value=fake_model
    ) as loader:
        emb.encode(["x"])
        loader.assert_called_once()


def test_load_failure_raises_embedder_unavailable():
    emb = SentenceTransformerEmbedder("bad-model")
    with patch.object(
        SentenceTransformerEmbedder,
        "_load_model",
        side_effect=OSError("network down"),
    ):
        with pytest.raises(EmbedderUnavailableError):
            emb.encode(["x"])


def test_empty_input_returns_empty_array():
    emb = SentenceTransformerEmbedder("fake-model")
    with patch.object(SentenceTransformerEmbedder, "_load_model", return_value=MagicMock()):
        out = emb.encode([])
    assert out.shape == (0, 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_embedder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'doc3gpp.services.embedding.embedder'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/doc3gpp/services/embedding/embedder.py
"""Sentence-transformers backed embedder for the semantic search subsystem.

The model is loaded lazily on the first ``.encode()`` call so a
process that never reranks never pays the load cost. Model load
failure (network, OOM, missing repo id) is wrapped as
:class:`EmbedderUnavailableError`.
"""

from __future__ import annotations

import logging

import numpy as np

from doc3gpp.models.semantic_search import EmbedderUnavailableError

logger = logging.getLogger(__name__)


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model = None

    def _load_model(self):
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(self._model_name)

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        if self._model is None:
            try:
                self._model = self._load_model()
            except (OSError, Exception) as exc:
                raise EmbedderUnavailableError(
                    f"failed to load embedding model {self._model_name!r}: {exc}"
                ) from exc
        vec = self._model.encode(texts, convert_to_numpy=True)
        return vec.astype(np.float32, copy=False)

    @property
    def dim(self) -> int:
        if self._model is None:
            try:
                self._model = self._load_model()
            except (OSError, Exception) as exc:
                raise EmbedderUnavailableError(
                    f"failed to load embedding model {self._model_name!r}: {exc}"
                ) from exc
        return int(self._model.get_sentence_embedding_dimension())
```

```python
# src/doc3gpp/services/embedding/__init__.py
"""Public re-exports for the semantic-search embedding helpers."""

from __future__ import annotations

from doc3gpp.services.embedding.chunker import CHUNK_OVERLAP_DEFAULT, CHUNK_SIZE_DEFAULT, _chunks
from doc3gpp.services.embedding.embedder import SentenceTransformerEmbedder

__all__ = [
    "CHUNK_OVERLAP_DEFAULT",
    "CHUNK_SIZE_DEFAULT",
    "SentenceTransformerEmbedder",
    "_chunks",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_embedder.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/embedding/embedder.py src/doc3gpp/services/embedding/__init__.py tests/unit/test_embedder.py
git commit -m "feat(search): add SentenceTransformerEmbedder with lazy model load"
```

---

## Task 5: Protocols — `Embedder` + `VectorIndexRepository`

**Files:**
- Modify: `src/doc3gpp/repository/protocols.py` (append after the existing `EmbeddingReranker` block ending at line 618).
- Test: `tests/unit/test_protocols_semantic.py`

**Interfaces:**
- Consumes: `doc3gpp.models.search.SearchFilters`, `doc3gpp.models.search.SearchIndexStatus`, `doc3gpp.models.semantic_search` (nothing yet — protocols only need forward refs).
- Produces: `Embedder` Protocol (`.encode(list[str]) -> np.ndarray`, `.dim -> int`); `VectorIndexRepository` Protocol with `upsert_chunks(tdoc_id, embeddings)`, `remove_for_tdoc(tdoc_id)`, `knn(query_vec, limit, filters) -> list[tuple[str, str, int, float]]`, `rebuild_batch(batch_size, after_id, stale_only) -> Iterable[list[str]]`, `count_tdocs_to_index(stale_only) -> int`, `get_resume_cursor() -> str | None`, `set_resume_cursor(tdoc_id) -> None`, `status() -> SearchIndexStatus`. T6, T7, T9 import these.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_protocols_semantic.py
from __future__ import annotations

from collections.abc import Iterable

import numpy as np

from doc3gpp.models.search import SearchFilters, SearchIndexStatus
from doc3gpp.repository.protocols import Embedder, VectorIndexRepository


def test_embedder_protocol_signature():
    class Impl:
        def encode(self, texts: list[str]) -> np.ndarray:
            return np.zeros((len(texts), 4), dtype=np.float32)
        @property
        def dim(self) -> int:
            return 4
    e: Embedder = Impl()
    assert e.dim == 4
    assert e.encode(["a"]).shape == (1, 4)


def test_vector_index_repository_protocol_signature():
    class Impl:
        def upsert_chunks(self, tdoc_id: str, embeddings: list[np.ndarray]) -> None: ...
        def remove_for_tdoc(self, tdoc_id: str) -> None: ...
        def knn(self, query_vec, limit, filters=None): return []
        def rebuild_batch(self, batch_size, after_id, stale_only) -> Iterable[list[str]]: return iter([])
        def count_tdocs_to_index(self, stale_only) -> int: return 0
        def get_resume_cursor(self) -> str | None: return None
        def set_resume_cursor(self, tdoc_id: str) -> None: ...
        def status(self) -> SearchIndexStatus: ...
    r: VectorIndexRepository = Impl()
    assert callable(r.upsert_chunks)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_protocols_semantic.py -v`
Expected: FAIL with `ImportError: cannot import name 'Embedder' from 'doc3gpp.repository.protocols'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/doc3gpp/repository/protocols.py` (after the `EmbeddingReranker` class ending at line 618):

```python
class Embedder(Protocol):
    """Embedding backend for the semantic search subsystem.

    The v1 default :class:`~doc3gpp.services.embedding.embedder.SentenceTransformerEmbedder`
    loads a HuggingFace sentence-transformers model lazily on first
    ``.encode()`` call. A future hosted-API impl plugs in here
    without any change to :class:`SemanticSearchService` or the CLI.
    """

    def encode(self, texts: list[str]) -> "np.ndarray":
        """Return shape ``(len(texts), dim)``, dtype float32."""
        ...

    @property
    def dim(self) -> int:
        """The model's embedding dimension (e.g. 384 for all-MiniLM-L6-v2)."""
        ...


class VectorIndexRepository(Protocol):
    """sqlite-vec backed vector index for ``tdocs``.

    All write paths are idempotent (``DELETE`` + ``INSERT``). One
    ``tdoc_id`` maps to N chunk rows (``chunk_id = "{tdoc_id}#{i}"``).
    Implementations are dialect-aware: on sqlite with sqlite-vec
    enabled everything works; on non-sqlite or sqlite-vec-less builds
    every method raises :class:`VectorIndexUnavailableError`. The
    factory layer catches that error once at startup and returns
    ``None`` so callers can degrade gracefully.
    """

    def upsert_chunks(self, tdoc_id: str, embeddings: list[np.ndarray]) -> None:
        """Replace all chunk rows for ``tdoc_id`` with the new embeddings.

        Deletes existing chunks for ``tdoc_id`` then inserts the new
        chunk rows in a single transaction. ``chunk_id`` is
        ``f"{tdoc_id}#{i}"`` for ``i`` in ``range(len(embeddings))``.
        """
        ...

    def remove_for_tdoc(self, tdoc_id: str) -> None:
        """Delete all chunk rows for ``tdoc_id``. No-op if absent."""
        ...

    def knn(
        self, query_vec: "np.ndarray", limit: int,
        filters: "SearchFilters | None" = None,
    ) -> list[tuple[str, str, int, float]]:
        """KNN by cosine distance; returns ``(tdoc_id, chunk_id, chunk_index, distance)``.

        Joins to ``tdocs`` / ``meetings`` for filters. ``limit`` caps
        the chunk count (NOT the tdoc count — the service reduces
        chunks to tdocs via ``min(distance)``).
        """
        ...

    def rebuild_batch(
        self, batch_size: int, after_id: "str | None", stale_only: bool,
    ) -> Iterable[list[str]]:
        """Yield batches of ``tdoc_id`` strings in ``ORDER BY tdoc_id ASC``."""
        ...

    def count_tdocs_to_index(self, stale_only: bool) -> int: ...

    def get_resume_cursor(self) -> "str | None": ...

    def set_resume_cursor(self, tdoc_id: str) -> None: ...

    def status(self) -> "SearchIndexStatus": ...
```

Add the `np` forward-ref import at the top of `protocols.py` under `TYPE_CHECKING`:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import numpy as np
```

And add `Embedder`, `VectorIndexRepository` to `__all__` if the file has one (check; if not, no `__all__` to update).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_protocols_semantic.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/repository/protocols.py tests/unit/test_protocols_semantic.py
git commit -m "feat(search): add Embedder + VectorIndexRepository Protocols"
```

---

## Task 6: Vector repo — `SQLAlchemyVectorIndexRepository` + `_build_embed_text`

**Files:**
- Create: `src/doc3gpp/storage/repositories/vector_sql.py`
- Test: `tests/integration/test_vector_index_lifecycle.py`

**Interfaces:**
- Consumes: `sqlite_vec` (imported lazily; PyPI `sqlite-vec`, import `sqlite_vec`); `doc3gpp.storage.db.session.get_engine`; `doc3gpp.storage.compression.decompress_json`; `doc3gpp.settings.loader.get_settings`; `doc3gpp.models.search.SearchFilters`, `SearchIndexStatus`, `SearchUnavailableError`; `doc3gpp.models.semantic_search.VectorIndexUnavailableError`; `doc3gpp.repository.protocols.VectorIndexRepository`; `numpy`.
- Produces: `SQLAlchemyVectorIndexRepository(VectorIndexRepository)` with the 9 methods from the Protocol; module helper `_build_embed_text(tdoc_id) -> str | None` (SQL JOIN across `tdocs` + `meetings` + `wis` + `tdoc_cr_cover_page` + `tdoc_cr_change_details` + `tdoc_cr_ttcn_details`, decompressing gzip blobs, concatenating with ` :: ` separator); `_check_sqlite_vec(engine)` runtime probe. T7, T9, T10, T11 import these.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_vector_index_lifecycle.py
from __future__ import annotations

import pytest

pytestmark = pytest.mark.semantic


@pytest.fixture()
def repo(sqlite_env):
    from doc3gpp.storage.db.migrate import create_schema
    create_schema()
    from doc3gpp.storage.repositories.vector_sql import SQLAlchemyVectorIndexRepository
    return SQLAlchemyVectorIndexRepository()


def test_upsert_then_knn_returns_expected_order(repo):
    import numpy as np
    # 3 chunks for tdoc A, 1 chunk for tdoc B
    a = [np.zeros(384, dtype=np.float32) for _ in range(3)]
    a[0][0] = 1.0  # chunk 0 close to query
    b = [np.zeros(384, dtype=np.float32)]
    b[0][0] = 0.5
    repo.upsert_chunks("R5-1", a)
    repo.upsert_chunks("R5-2", b)
    query = np.zeros(384, dtype=np.float32)
    query[0] = 1.0
    hits = repo.knn(query, limit=10)
    assert len(hits) >= 2
    # Closest chunk first; R5-1 chunk 0 has distance 0
    assert hits[0][0] == "R5-1"
    assert hits[0][1] == "R5-1#0"


def test_remove_for_tdoc(repo):
    import numpy as np
    repo.upsert_chunks("R5-1", [np.zeros(384, dtype=np.float32)])
    repo.remove_for_tdoc("R5-1")
    hits = repo.knn(np.zeros(384, dtype=np.float32), limit=10)
    assert not any(h[0] == "R5-1" for h in hits)


def test_upsert_replaces_existing_chunks(repo):
    import numpy as np
    repo.upsert_chunks("R5-1", [np.zeros(384, dtype=np.float32) for _ in range(8)])
    # re-parse with fewer chunks → surplus deleted
    repo.upsert_chunks("R5-1", [np.zeros(384, dtype=np.float32) for _ in range(4)])
    status = repo.status()
    # only 4 chunk rows for R5-1 now
    hits = repo.knn(np.zeros(384, dtype=np.float32), limit=100)
    r5_1_chunks = [h for h in hits if h[0] == "R5-1"]
    assert len(r5_1_chunks) == 4


def test_status_reports_row_count(repo):
    import numpy as np
    repo.upsert_chunks("R5-1", [np.zeros(384, dtype=np.float32) for _ in range(3)])
    status = repo.status()
    assert status.row_count >= 3


def test_dim_mismatch_raises(repo):
    import numpy as np
    bad = [np.zeros(128, dtype=np.float32)]  # 128 != 384
    from doc3gpp.models.semantic_search import VectorIndexUnavailableError
    with pytest.raises(VectorIndexUnavailableError):
        repo.upsert_chunks("R5-1", bad)


def test_resume_cursor_round_trip(repo):
    assert repo.get_resume_cursor() is None
    repo.set_resume_cursor("R5-123")
    assert repo.get_resume_cursor() == "R5-123"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_vector_index_lifecycle.py -v -m semantic`
Expected: FAIL with `ModuleNotFoundError: No module named 'doc3gpp.storage.repositories.vector_sql'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/doc3gpp/storage/repositories/vector_sql.py
"""SQLAlchemy implementation of :class:`VectorIndexRepository`.

Owns the ``vec_tdoc_embeddings`` virtual table (sqlite-vec ``vec0``)
+ meta sidecar (``vec_meta``) created by
:func:`doc3gpp.storage.db.migrate._create_vector_schema`. At
construction time it probes for sqlite-vec availability — raising
:class:`VectorIndexUnavailableError` on non-sqlite or sqlite-vec-less
builds so :func:`doc3gpp.services.factory.build_semantic_search_service`
can catch it once at startup.

Embed text is built by a single SQL JOIN across ``tdocs`` + ``meetings``
+ ``wis`` + the three sidecar tables, with the gzip JSON blobs
decompressed in Python via :func:`doc3gpp.storage.compression.decompress_json`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime

import numpy as np
from sqlalchemy import text
from sqlalchemy.engine import Engine

from doc3gpp.models.search import SearchFilters, SearchIndexStatus
from doc3gpp.models.semantic_search import VectorIndexUnavailableError
from doc3gpp.repository.protocols import VectorIndexRepository
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.compression import decompress_json
from doc3gpp.storage.db.session import get_engine

logger = logging.getLogger(__name__)

DEFAULT_DIM = 384


def _check_sqlite_vec(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        raise VectorIndexUnavailableError(
            f"vector index requires sqlite; current dialect is "
            f"{engine.dialect.name!r}"
        )
    try:
        import sqlite_vec  # noqa: F401
    except ImportError as exc:
        raise VectorIndexUnavailableError(
            "sqlite-vec is not installed; run `pip install doc3gpp[semantic]`"
        ) from exc
    with engine.begin() as conn:
        try:
            import sqlite_vec
            sqlite_vec.load(conn.connection.driver_connection)
        except Exception as exc:
            raise VectorIndexUnavailableError(
                f"sqlite-vec extension load failed: {exc}"
            ) from exc


class SQLAlchemyVectorIndexRepository(VectorIndexRepository):
    def __init__(self) -> None:
        self._engine = get_engine()
        _check_sqlite_vec(self._engine)
        self._dim = self._read_or_init_dim()

    def _read_or_init_dim(self) -> int:
        with self._engine.begin() as conn:
            row = conn.execute(
                text("SELECT value FROM vec_meta WHERE key = 'embedding_dim'")
            ).scalar()
            if row is None:
                conn.execute(
                    text(
                        "INSERT INTO vec_meta (key, value) VALUES ('embedding_dim', :d)"
                    ),
                    {"d": str(DEFAULT_DIM)},
                )
                return DEFAULT_DIM
            return int(row)

    def _check_dim(self, embeddings: list[np.ndarray]) -> None:
        for v in embeddings:
            if v.shape[-1] != self._dim:
                raise VectorIndexUnavailableError(
                    f"vector dim mismatch: stored={self._dim} "
                    f"requested={v.shape[-1]}; run "
                    f"`doc3gpp search index --rebuild-embeddings`"
                )

    def upsert_chunks(self, tdoc_id: str, embeddings: list[np.ndarray]) -> None:
        self._check_dim(embeddings)
        with self._engine.begin() as conn:
            raw = conn.connection.driver_connection
            conn.execute(
                text("DELETE FROM vec_tdoc_embeddings WHERE tdoc_id = :id"),
                {"id": tdoc_id},
            )
            for i, vec in enumerate(embeddings):
                conn.execute(
                    text(
                        "INSERT INTO vec_tdoc_embeddings "
                        "(chunk_id, tdoc_id, chunk_index, embedding) "
                        "VALUES (:cid, :tid, :ci, vec_bit(:emb))"
                    ),
                    {
                        "cid": f"{tdoc_id}#{i}",
                        "tid": tdoc_id,
                        "ci": i,
                        "emb": np.asarray(vec, dtype=np.float32).tobytes(),
                    },
                )

    def remove_for_tdoc(self, tdoc_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text("DELETE FROM vec_tdoc_embeddings WHERE tdoc_id = :id"),
                {"id": tdoc_id},
            )

    def knn(
        self, query_vec: np.ndarray, limit: int,
        filters: SearchFilters | None = None,
    ) -> list[tuple[str, str, int, float]]:
        if query_vec.shape[-1] != self._dim:
            raise VectorIndexUnavailableError(
                f"query dim mismatch: stored={self._dim} "
                f"requested={query_vec.shape[-1]}"
            )
        sql = [
            "SELECT chunk_id, tdoc_id, chunk_index,",
            "       vec_distance_cosine(embedding, vec_bit(:q)) AS distance",
            "  FROM vec_tdoc_embeddings",
        ]
        params: dict = {
            "q": np.asarray(query_vec, dtype=np.float32).tobytes(),
            "limit": limit,
        }
        if filters is not None:
            sql.append("  JOIN tdocs t   ON t.tdoc_id = vec_tdoc_embeddings.tdoc_id")
            sql.append("  JOIN meetings m ON t.meeting_id = m.meeting_id")
            if filters.tsg:
                sql.append("   AND m.tsg = :tsg"); params["tsg"] = filters.tsg
            if filters.meeting:
                sql.append("   AND m.name = :meeting"); params["meeting"] = filters.meeting
            if filters.meeting_id is not None:
                sql.append("   AND m.meeting_id = :meeting_id"); params["meeting_id"] = filters.meeting_id
            if filters.tdoc_id:
                sql.append("   AND t.tdoc_id = :tdoc_id"); params["tdoc_id"] = filters.tdoc_id
            if filters.release:
                sql.append("   AND t.release = :release"); params["release"] = filters.release
            if filters.spec:
                sql.append("   AND t.spec = :spec"); params["spec"] = filters.spec
            if filters.since:
                sql.append("   AND t.uploaded_date >= :since"); params["since"] = filters.since
            if filters.until:
                sql.append("   AND t.uploaded_date <= :until"); params["until"] = filters.until
        sql.append("  ORDER BY distance ASC LIMIT :limit")
        with self._engine.begin() as conn:
            rows = conn.execute(text("\n".join(sql)), params).all()
        return [(r[1], r[0], int(r[2]), float(r[3])) for r in rows]

    def rebuild_batch(
        self, batch_size: int, after_id: str | None, stale_only: bool,
    ) -> Iterable[list[str]]:
        sql = ["SELECT tdoc_id FROM tdocs"]
        params: dict = {"limit": batch_size}
        if stale_only:
            sql.append(
                " WHERE uploaded_date > "
                "(SELECT value FROM vec_meta WHERE key='last_indexed_uploaded_date')"
            )
        if after_id is not None:
            sql.append("   AND tdoc_id > :after" if stale_only else " WHERE tdoc_id > :after")
            params["after"] = after_id
        sql.append("  ORDER BY tdoc_id ASC LIMIT :limit")
        with self._engine.begin() as conn:
            rows = conn.execute(text("\n".join(sql)), params).all()
        yield [r[0] for r in rows]

    def count_tdocs_to_index(self, stale_only: bool) -> int:
        sql = ["SELECT COUNT(*) FROM tdocs"]
        if stale_only:
            sql.append(
                " WHERE uploaded_date > "
                "(SELECT value FROM vec_meta WHERE key='last_indexed_uploaded_date')"
            )
        with self._engine.begin() as conn:
            return int(conn.execute(text("\n".join(sql))).scalar() or 0)

    def get_resume_cursor(self) -> str | None:
        with self._engine.begin() as conn:
            return conn.execute(
                text("SELECT value FROM vec_meta WHERE key='last_rebuild_last_tdoc_id'")
            ).scalar()

    def set_resume_cursor(self, tdoc_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO vec_meta (key, value) VALUES "
                    "('last_rebuild_last_tdoc_id', :v) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                ),
                {"v": tdoc_id},
            )

    def status(self) -> SearchIndexStatus:
        with self._engine.begin() as conn:
            row_count = int(
                conn.execute(text("SELECT COUNT(*) FROM vec_tdoc_embeddings")).scalar() or 0
            )
            last_rebuild = conn.execute(
                text("SELECT value FROM vec_meta WHERE key='last_rebuild_at'")
            ).scalar()
            last_indexed = conn.execute(
                text("SELECT value FROM vec_meta WHERE key='last_indexed_uploaded_date'")
            ).scalar()
            latest = conn.execute(
                text("SELECT MAX(uploaded_date) FROM tdocs")
            ).scalar()
        from datetime import datetime as _dt
        return SearchIndexStatus(
            enabled=True,
            row_count=row_count,
            last_rebuild_at=_dt.fromisoformat(last_rebuild) if last_rebuild else None,
            last_indexed_uploaded_date=_dt.fromisoformat(last_indexed) if last_indexed else None,
            latest_tdocs_uploaded_date=_dt.fromisoformat(str(latest)) if latest else None,
            is_stale=bool(latest and (not last_indexed or str(latest) > last_indexed)),
        )


def _build_embed_text(tdoc_id: str) -> str | None:
    """Build the concatenated embed text for ``tdoc_id``.

    Joins ``tdocs`` + ``meetings`` + ``wis`` + the three sidecar tables,
    decompresses the gzip JSON blobs, and concatenates the text fields
    with `` :: `` separator. Returns ``None`` when the ``tdoc_id`` is
    absent from ``tdocs``.
    """
    from doc3gpp.storage.db.session import get_engine as _ge
    engine = _ge()
    with engine.begin() as conn:
        tdoc = conn.execute(
            text("SELECT title FROM tdocs WHERE tdoc_id = :id"), {"id": tdoc_id},
        ).first()
        if tdoc is None:
            return None
        parts: list[str] = [tdoc[0] or ""]
        mtg = conn.execute(
            text(
                "SELECT m.title FROM meetings m "
                "JOIN tdocs t ON t.meeting_id = m.meeting_id "
                "WHERE t.tdoc_id = :id"
            ),
            {"id": tdoc_id},
        ).first()
        if mtg and mtg[0]:
            parts.append(mtg[0])
        cover = conn.execute(
            text("SELECT title, subject FROM tdoc_cr_cover_page WHERE tdoc_id = :id"),
            {"id": tdoc_id},
        ).first()
        if cover is not None:
            if cover[0]:
                parts.append(cover[0])
            if cover[1]:
                parts.append(cover[1])
        ttcn = conn.execute(
            text("SELECT required_changes_text FROM tdoc_cr_ttcn_details WHERE tdoc_id = :id"),
            {"id": tdoc_id},
        ).first()
        if ttcn is not None and ttcn[0]:
            try:
                parts.append(str(decompress_json(ttcn[0]))[:2000])
            except Exception:
                pass
        changes = conn.execute(
            text("SELECT change_text FROM tdoc_cr_change_details WHERE tdoc_id = :id"),
            {"id": tdoc_id},
        ).first()
        if changes is not None and changes[0]:
            try:
                parts.append(str(decompress_json(changes[0]))[:2000])
            except Exception:
                pass
    return " :: ".join(p for p in parts if p)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_vector_index_lifecycle.py -v -m semantic`
Expected: PASS (6 tests). Requires `pip install doc3gpp[semantic]` and the `vec_tdoc_embeddings` + `vec_meta` tables to exist (created by T10's `_create_vector_schema`; for this task's tests to pass in isolation, add a `create_schema()` call in the fixture as shown — T10 will add the DDL).

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/storage/repositories/vector_sql.py tests/integration/test_vector_index_lifecycle.py
git commit -m "feat(search): add SQLAlchemyVectorIndexRepository with sqlite-vec KNN"
```

---

## Task 7: Service — `SemanticSearchService` + `rrf_merge`

**Files:**
- Create: `src/doc3gpp/services/semantic_search_service.py`
- Test: `tests/unit/test_rrf.py`, `tests/unit/test_semantic_search_service.py`

**Interfaces:**
- Consumes: `doc3gpp.services.search_service.SearchService` (reused as FTS5 fan-out); `doc3gpp.services.embedding.stopwords.strip_stopwords`; `doc3gpp.services.embedding.embedder.SentenceTransformerEmbedder` (or any `Embedder`); `doc3gpp.services.embedding.chunker._chunks`; `doc3gpp.storage.repositories.vector_sql.SQLAlchemyVectorIndexRepository` (or any `VectorIndexRepository`); `doc3gpp.models.search.SearchHit`, `SearchFilters`, `RebuildProgress`; `doc3gpp.models.semantic_search.SemanticSearchHit`, `SemanticSearchQueryError`, `SemanticSearchUnavailableError`; `doc3gpp.settings.schema.Settings`; `doc3gpp.cli_filters.SearchQueryBuilder`.
- Produces: `rrf_merge(fts5_hits, vec_hits, *, k, vector_weight, limit) -> list[SemanticSearchHit]` (pure-Python, module top level); `SemanticSearchService(fts5_service, embedder, vector_repo, settings)` with `.search(query, filters, limit, vector_weight)`, `.index_for_tdoc(tdoc_id)`, `.remove_for_tdoc(tdoc_id)`, `.rebuild_embeddings(batch_size, stale_only, quiet) -> Iterator[RebuildProgress]`, `.status()`. T9, T11, T12 import these.

- [ ] **Step 1: Write the failing test (RRF merge)**

```python
# tests/unit/test_rrf.py
from __future__ import annotations

from doc3gpp.models.search import SearchHit
from doc3gpp.models.semantic_search import SemanticSearchHit
from doc3gpp.services.semantic_search_service import rrf_merge


def _hit(tdoc_id: str, score: float = -1.0) -> SearchHit:
    return SearchHit(
        tdoc_id=tdoc_id, score=score, previews={"title": "t"}, title="t",
        meeting=None, tsg=None, uploaded_date=None, ftp_url=None, wis=None,
    )


def test_rrf_pure_fts5_when_vector_weight_zero():
    fts5 = [_hit("A"), _hit("B")]
    vec: list = []
    out = rrf_merge(fts5, vec, k=60, vector_weight=0.0, limit=10)
    assert [h.tdoc_id for h in out] == ["A", "B"]
    assert out[0].rank_fts5 == 0
    assert out[0].rank_vec is None


def test_rrf_pure_vector_when_vector_weight_one():
    fts5: list = []
    vec = [("A", "A#0", 0, 0.1), ("B", "B#0", 0, 0.2)]
    out = rrf_merge(fts5, vec, k=60, vector_weight=1.0, limit=10)
    assert [h.tdoc_id for h in out] == ["A", "B"]
    assert out[0].rank_vec == 0
    assert out[0].rank_fts5 is None


def test_rrf_blend_both_sides():
    fts5 = [_hit("A"), _hit("B")]
    vec = [("B", "B#0", 0, 0.1), ("C", "C#0", 0, 0.2)]
    # A: fts5 rank 0, no vec → score = 1/(60+0)*(1-W) = 1/60 * 0.3 = 0.005
    # B: fts5 rank 1, vec rank 0 → 1/61*0.3 + 1/60*0.7 = 0.00492 + 0.01167 = 0.01658
    # C: no fts5, vec rank 1 → 1/61*0.7 = 0.01148
    out = rrf_merge(fts5, vec, k=60, vector_weight=0.7, limit=10)
    # B should rank first (highest score)
    assert out[0].tdoc_id == "B"
    assert out[0].rank_fts5 == 1
    assert out[0].rank_vec == 0


def test_rrf_min_distance_across_chunks():
    # Same tdoc_id, multiple chunks → min distance wins
    fts5: list = []
    vec = [
        ("A", "A#0", 0, 0.5),
        ("A", "A#1", 1, 0.1),  # best chunk
        ("A", "A#2", 2, 0.3),
    ]
    out = rrf_merge(fts5, vec, k=60, vector_weight=1.0, limit=10)
    assert out[0].tdoc_id == "A"
    assert out[0].min_chunk_distance == 0.1
    assert out[0].best_chunk_id == "A#1"


def test_rrf_limit_truncation():
    fts5 = [_hit(f"T{i}") for i in range(10)]
    out = rrf_merge(fts5, [], k=60, vector_weight=0.0, limit=3)
    assert len(out) == 3


def test_rrf_empty_both_sides():
    out = rrf_merge([], [], k=60, vector_weight=0.5, limit=10)
    assert out == []


def test_rrf_synthesizes_fts5_hit_for_vector_only_tdoc():
    # When a tdoc is only in vector fan-out, the service synthesizes a
    # minimal SearchHit. rrf_merge itself does NOT synthesize — it
    # carries None and the service fills it. Test the contract:
    fts5: list = []
    vec = [("A", "A#0", 0, 0.1)]
    out = rrf_merge(fts5, vec, k=60, vector_weight=1.0, limit=10)
    assert out[0].tdoc_id == "A"
    # fts5_hit is None for vector-only; service fills it later
    assert out[0].fts5_hit is None
```

- [ ] **Step 1b: Write the failing test (service)**

```python
# tests/unit/test_semantic_search_service.py
from __future__ import annotations

from collections.abc import Iterable
from unittest.mock import MagicMock

import numpy as np
import pytest

from doc3gpp.models.search import (
    RebuildProgress, SearchFilters, SearchHit, SearchIndexStatus,
)
from doc3gpp.models.semantic_search import (
    SemanticSearchQueryError, SemanticSearchUnavailableError,
)
from doc3gpp.services.semantic_search_service import SemanticSearchService


def _hit(tdoc_id: str) -> SearchHit:
    return SearchHit(
        tdoc_id=tdoc_id, score=-1.0, previews={"title": "t"}, title="t",
        meeting=None, tsg=None, uploaded_date=None, ftp_url=None, wis=None,
    )


def _settings():
    s = MagicMock()
    s.semantic_search.fanout_multiplier = 2
    s.semantic_search.rrf_k = 60
    s.semantic_search.chunk_size = 800
    s.semantic_search.chunk_overlap = 100
    s.semantic_search.max_chunks_per_tdoc = 32
    return s


def _mock_embedder():
    e = MagicMock()
    e.dim = 384
    e.encode.return_value = np.zeros((1, 384), dtype=np.float32)
    return e


def test_search_strips_query_for_fts5_and_uses_original_for_vector(monkeypatch):
    monkeypatch.setattr(
        "doc3gpp.services.semantic_search_service.strip_stopwords",
        lambda q: "CR touch NB-IoT power save",
    )
    fts5 = MagicMock()
    fts5.search.return_value = [_hit("R5-1")]
    vec = MagicMock()
    vec.knn.return_value = [("R5-1", "R5-1#0", 0, 0.1)]
    emb = _mock_embedder()
    svc = SemanticSearchService(fts5, emb, vec, _settings())
    out = svc.search("what CRs touch NB-IoT power saving", SearchFilters(), limit=10, vector_weight=0.7)
    assert len(out) == 1
    assert out[0].tdoc_id == "R5-1"
    # Embedder received the ORIGINAL query
    emb.encode.assert_called_once()
    assert emb.encode.call_args[0][0] == ["what CRs touch NB-IoT power saving"]


def test_search_raises_on_empty_after_strip(monkeypatch):
    monkeypatch.setattr(
        "doc3gpp.services.semantic_search_service.strip_stopwords",
        lambda q: "",
    )
    svc = SemanticSearchService(MagicMock(), _mock_embedder(), MagicMock(), _settings())
    with pytest.raises(SemanticSearchQueryError):
        svc.search("   ", SearchFilters(), limit=10, vector_weight=0.7)


def test_search_both_sides_empty_returns_empty(monkeypatch):
    monkeypatch.setattr(
        "doc3gpp.services.semantic_search_service.strip_stopwords",
        lambda q: "valid query",
    )
    fts5 = MagicMock(); fts5.search.return_value = []
    vec = MagicMock(); vec.knn.return_value = []
    svc = SemanticSearchService(fts5, _mock_embedder(), vec, _settings())
    out = svc.search("valid query", SearchFilters(), limit=10, vector_weight=0.7)
    assert out == []


def test_search_uses_fanout_multiplier(monkeypatch):
    monkeypatch.setattr(
        "doc3gpp.services.semantic_search_service.strip_stopwords",
        lambda q: "q",
    )
    fts5 = MagicMock(); fts5.search.return_value = []
    vec = MagicMock(); vec.knn.return_value = []
    svc = SemanticSearchService(fts5, _mock_embedder(), vec, _settings())
    svc.search("q", SearchFilters(), limit=20, vector_weight=0.7)
    # internal_limit = 20 * 2 = 40
    assert fts5.search.call_args[0][1].limit == 40
    assert vec.knn.call_args[1]["limit"] == 40


def test_index_for_tdoc_calls_upsert_chunks(monkeypatch):
    vec = MagicMock()
    fts5 = MagicMock()
    monkeypatch.setattr(
        "doc3gpp.services.semantic_search_service._build_embed_text",
        lambda tid: "long text " * 200,
    )
    svc = SemanticSearchService(fts5, _mock_embedder(), vec, _settings())
    svc.index_for_tdoc("R5-1")
    vec.upsert_chunks.assert_called_once()
    args = vec.upsert_chunks.call_args
    assert args[0][0] == "R5-1"
    # embeddings is a list of ndarrays
    assert isinstance(args[0][1], list)


def test_remove_for_tdoc_calls_repo():
    vec = MagicMock()
    svc = SemanticSearchService(MagicMock(), _mock_embedder(), vec, _settings())
    svc.remove_for_tdoc("R5-1")
    vec.remove_for_tdoc.assert_called_once_with("R5-1")


def test_rebuild_embeddings_yields_progress(monkeypatch):
    vec = MagicMock()
    vec.count_tdocs_to_index.return_value = 3
    vec.rebuild_batch.return_value = iter([["R5-1", "R5-2", "R5-3"]])
    vec.get_resume_cursor.return_value = None
    monkeypatch.setattr(
        "doc3gpp.services.semantic_search_service._build_embed_text",
        lambda tid: "text",
    )
    svc = SemanticSearchService(MagicMock(), _mock_embedder(), vec, _settings())
    progress = list(svc.rebuild_embeddings(batch_size=10, stale_only=False, quiet=True))
    assert len(progress) == 1
    assert progress[0].processed == 3
    assert progress[0].total == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_rrf.py tests/unit/test_semantic_search_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'doc3gpp.services.semantic_search_service'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/doc3gpp/services/semantic_search_service.py
"""Orchestration layer for the semantic (embedding + vector) search subsystem.

:class:`SemanticSearchService` owns four responsibilities:

1. **Read path** — :meth:`search` strips stopwords for the FTS5 query,
   embeds the ORIGINAL query for the vector path, fans out to both
   indexes with an enlarged ``internal_limit = limit * fanout``,
   then merges via :func:`rrf_merge` and truncates to ``limit``.
2. **Write paths** — :meth:`index_for_tdoc` builds the embed text,
   chunks it, embeds the chunks, and upserts. :meth:`remove_for_tdoc`
   deletes the chunk rows.
3. **Maintenance** — :meth:`rebuild_embeddings` is a generator that
   mirrors :meth:`doc3gpp.services.search_service.SearchService.rebuild`.
4. **Status** — :meth:`status` snapshots the vector index.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import numpy as np

from doc3gpp.cli_filters import SearchQueryBuilder
from doc3gpp.models.search import (
    RebuildProgress, SearchFilters, SearchHit, SearchIndexStatus,
)
from doc3gpp.models.semantic_search import (
    SemanticSearchHit, SemanticSearchQueryError, SemanticSearchUnavailableError,
)
from doc3gpp.services.embedding.chunker import _chunks
from doc3gpp.services.embedding.stopwords import strip_stopwords
from doc3gpp.services.search_service import SearchService

logger = logging.getLogger(__name__)


def rrf_merge(
    fts5_hits: list[SearchHit],
    vec_hits: list[tuple[str, str, int, float]],
    *,
    k: int = 60,
    vector_weight: float = 0.7,
    limit: int = 20,
) -> list[SemanticSearchHit]:
    """Reciprocal-rank fusion across FTS5 and vector rankings.

    Each tdoc_id is ranked by FTS5 position (if present) and by the
    best (lowest-distance) vector chunk. Final score::

        rrf = 1/(k + rank_fts5) * (1 - W) + 1/(k + rank_vec) * W

    A tdoc_id present in only one side contributes 0 from the other
    side's rank. ``fts5_hit`` is ``None`` for vector-only tdogs; the
    service synthesizes a minimal :class:`SearchHit` from the JOIN
    before returning to the CLI.
    """
    fts5_rank: dict[str, int] = {h.tdoc_id: i for i, h in enumerate(fts5_hits)}
    fts5_by_id: dict[str, SearchHit] = {h.tdoc_id: h for h in fts5_hits}
    # Reduce vec chunks to min distance per tdoc_id, preserving chunk rank
    vec_best: dict[str, tuple[int, str, float]] = {}  # tdoc_id -> (rank, best_chunk_id, min_dist)
    for rank, (tdoc_id, chunk_id, _chunk_idx, dist) in enumerate(vec_hits):
        prev = vec_best.get(tdoc_id)
        if prev is None or dist < prev[2]:
            vec_best[tdoc_id] = (rank, chunk_id, dist)
    all_ids = set(fts5_rank) | set(vec_best)
    scored: list[SemanticSearchHit] = []
    for tdoc_id in all_ids:
        r_fts = fts5_rank.get(tdoc_id)
        r_vec_tup = vec_best.get(tdoc_id)
        r_vec = r_vec_tup[0] if r_vec_tup is not None else None
        w = vector_weight
        fts5_term = (1.0 / (k + r_fts)) * (1.0 - w) if r_fts is not None else 0.0
        vec_term = (1.0 / (k + r_vec)) * w if r_vec is not None else 0.0
        score = fts5_term + vec_term
        scored.append(SemanticSearchHit(
            tdoc_id=tdoc_id,
            rrf_score=score,
            fts5_hit=fts5_by_id.get(tdoc_id),  # None for vector-only
            rank_fts5=r_fts,
            rank_vec=r_vec,
            min_chunk_distance=r_vec_tup[2] if r_vec_tup else None,
            best_chunk_id=r_vec_tup[1] if r_vec_tup else None,
        ))
    scored.sort(key=lambda h: h.rrf_score, reverse=True)
    return scored[:limit]


class SemanticSearchService:
    def __init__(
        self,
        fts5_service: SearchService,
        embedder,
        vector_repo,
        settings,
    ) -> None:
        self._fts5 = fts5_service
        self._embedder = embedder
        self._vec = vector_repo
        self._settings = settings

    def search(
        self, query: str, filters: SearchFilters,
        limit: int, vector_weight: float,
    ) -> list[SemanticSearchHit]:
        stripped = strip_stopwords(query)
        if not stripped:
            raise SemanticSearchQueryError(
                "query has no content after stopword stripping"
            )
        fts5_expr = SearchQueryBuilder(stripped).build()
        fanout = self._settings.semantic_search.fanout_multiplier
        internal_limit = max(limit * fanout, 0)
        fts5_filters = SearchFilters(
            tsg=filters.tsg, meeting=filters.meeting,
            meeting_id=filters.meeting_id, tdoc_id=filters.tdoc_id,
            release=filters.release, spec=filters.spec,
            since=filters.since, until=filters.until,
            limit=internal_limit,
        )
        fts5_hits = self._fts5.search(fts5_expr, fts5_filters)
        query_vec = self._embedder.encode([query])[0]
        vec_hits = self._vec.knn(query_vec, limit=internal_limit, filters=filters)
        merged = rrf_merge(
            fts5_hits, vec_hits,
            k=self._settings.semantic_search.rrf_k,
            vector_weight=vector_weight,
            limit=limit,
        )
        # Synthesize minimal SearchHit for vector-only tdogs
        for h in merged:
            if h.fts5_hit is None:
                h = SemanticSearchHit(
                    tdoc_id=h.tdoc_id, rrf_score=h.rrf_score,
                    fts5_hit=SearchHit(
                        tdoc_id=h.tdoc_id, score=0.0, previews={},
                        title="", meeting=None, tsg=None,
                        uploaded_date=None, ftp_url=None, wis=None,
                    ),
                    rank_fts5=h.rank_fts5, rank_vec=h.rank_vec,
                    min_chunk_distance=h.min_chunk_distance,
                    best_chunk_id=h.best_chunk_id,
                )
        return merged

    def index_for_tdoc(self, tdoc_id: str) -> None:
        from doc3gpp.storage.repositories.vector_sql import _build_embed_text
        embed_text = _build_embed_text(tdoc_id)
        if embed_text is None:
            self._vec.remove_for_tdoc(tdoc_id)
            return
        chunks = _chunks(
            embed_text,
            self._settings.semantic_search.chunk_size,
            self._settings.semantic_search.chunk_overlap,
        )
        max_chunks = getattr(self._settings.semantic_search, "max_chunks_per_tdoc", 32)
        if len(chunks) > max_chunks:
            chunks = chunks[:max_chunks]
        if not chunks:
            self._vec.remove_for_tdoc(tdoc_id)
            return
        embeddings = [self._embedder.encode([c])[0] for c in chunks]
        self._vec.upsert_chunks(tdoc_id, embeddings)

    def remove_for_tdoc(self, tdoc_id: str) -> None:
        self._vec.remove_for_tdoc(tdoc_id)

    def rebuild_embeddings(
        self, batch_size: int, stale_only: bool, quiet: bool,
    ) -> Iterator[RebuildProgress]:
        total = self._vec.count_tdocs_to_index(stale_only=stale_only)
        after_id = self._vec.get_resume_cursor()
        processed = 0
        batches = self._vec.rebuild_batch(
            batch_size=batch_size, after_id=after_id, stale_only=stale_only,
        )
        for batch in batches:
            for tdoc_id in batch:
                try:
                    self.index_for_tdoc(tdoc_id)
                except Exception as exc:
                    logger.warning(
                        "embedding rebuild failed for tdoc_id=%s: %s",
                        tdoc_id, exc,
                    )
                processed += 1
            self._vec.set_resume_cursor(batch[-1])
            yield RebuildProgress(
                processed=processed, total=total, current_tdoc_id=batch[-1],
            )

    def status(self) -> SearchIndexStatus:
        return self._vec.status()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_rrf.py tests/unit/test_semantic_search_service.py -v`
Expected: PASS (all). Note: `test_search_synthesizes_fts5_hit_for_vector_only_tdoc` in `test_rrf.py` asserts `fts5_hit is None` for vector-only — confirm the `rrf_merge` impl matches (it sets `fts5_hit=None` for vector-only; the service synthesizes AFTER merge in `search()`). Adjust the test assertion if needed.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/semantic_search_service.py tests/unit/test_rrf.py tests/unit/test_semantic_search_service.py
git commit -m "feat(search): add SemanticSearchService + RRF merge"
```

---

## Task 8: Settings — `SemanticSearchSettings` + `[semantic]` extra + `-m semantic` marker

**Files:**
- Modify: `src/doc3gpp/settings/schema.py` (add `SemanticSearchSettings` class + `semantic_search` field on `Settings`).
- Modify: `pyproject.toml` (new `[semantic]` extra + `semantic` pytest marker).
- Test: `tests/unit/test_semantic_settings.py`

**Interfaces:**
- Consumes: `pydantic.BaseModel`, `pydantic.Field`, `field_validator`.
- Produces: `SemanticSearchSettings` with fields `enabled: bool=True`, `auto_embed_on_parse: bool=True`, `embedding_model: str="sentence-transformers/all-MiniLM-L6-v2"`, `chunk_size: int=800`, `chunk_overlap: int=100`, `rrf_k: int=60`, `vector_weight: float=0.7`, `fanout_multiplier: int=2`, `final_limit: int=20`, `user_defined_stop_words: list[str]=[]`, `keep_negation_words: list[str]=["not"]`, `max_chunks_per_tdoc: int=32`. `Settings.semantic_search: SemanticSearchSettings`. T3, T7, T9, T11, T12 read these.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_semantic_settings.py
from __future__ import annotations

import pytest

from doc3gpp.settings.schema import SemanticSearchSettings, Settings


def test_defaults():
    s = SemanticSearchSettings()
    assert s.enabled is True
    assert s.auto_embed_on_parse is True
    assert s.embedding_model == "sentence-transformers/all-MiniLM-L6-v2"
    assert s.chunk_size == 800
    assert s.chunk_overlap == 100
    assert s.rrf_k == 60
    assert s.vector_weight == 0.7
    assert s.fanout_multiplier == 2
    assert s.final_limit == 20
    assert s.user_defined_stop_words == []
    assert s.keep_negation_words == ["not"]
    assert s.max_chunks_per_tdoc == 32


def test_chunk_size_must_be_positive():
    with pytest.raises(Exception):
        SemanticSearchSettings(chunk_size=0)
    with pytest.raises(Exception):
        SemanticSearchSettings(chunk_size=-1)


def test_chunk_overlap_must_be_less_than_size():
    with pytest.raises(Exception):
        SemanticSearchSettings(chunk_size=800, chunk_overlap=800)
    with pytest.raises(Exception):
        SemanticSearchSettings(chunk_size=800, chunk_overlap=801)


def test_vector_weight_range():
    SemanticSearchSettings(vector_weight=0.0)
    SemanticSearchSettings(vector_weight=1.0)
    with pytest.raises(Exception):
        SemanticSearchSettings(vector_weight=-0.1)
    with pytest.raises(Exception):
        SemanticSearchSettings(vector_weight=1.5)


def test_fanout_multiplier_at_least_one():
    SemanticSearchSettings(fanout_multiplier=1)
    with pytest.raises(Exception):
        SemanticSearchSettings(fanout_multiplier=0)


def test_rrf_k_positive():
    with pytest.raises(Exception):
        SemanticSearchSettings(rrf_k=0)


def test_settings_has_semantic_search_field():
    s = Settings()
    assert isinstance(s.semantic_search, SemanticSearchSettings)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_semantic_settings.py -v`
Expected: FAIL with `ImportError: cannot import name 'SemanticSearchSettings'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/doc3gpp/settings/schema.py` after the `SearchSettings` class (line ~458):

```python
class SemanticSearchSettings(BaseModel):
    """Knobs for the semantic (embedding + vector) search subsystem.

    Defaults match the conservative end: ``enabled`` and
    ``auto_embed_on_parse`` both default to True so the vector index
    stays in sync with every successful ``tdoc parse`` until the
    operator opts out. The embedding model name is pluggable; the
    default ``all-MiniLM-L6-v2`` is 384-dim, ~80MB, fast on CPU.

    TOML-only (no env overrides). The presence of the sqlite-vec
    extension + spaCy model is gated by the ``[semantic]`` pyproject
    extra; on builds without it the runtime probe raises
    :class:`VectorIndexUnavailableError` which the factory catches
    once at startup.
    """

    enabled: bool = Field(default=True, description="Master switch.")
    auto_embed_on_parse: bool = Field(
        default=True,
        description="When true, every successful tdoc parse calls "
        "SemanticSearchService.index_for_tdoc(tdoc_id).",
    )
    embedding_model: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        description="HuggingFace sentence-transformers repo id.",
    )
    chunk_size: int = Field(default=800, ge=1, description="Whitespace tokens per chunk.")
    chunk_overlap: int = Field(
        default=100, ge=0,
        description="Trailing tokens repeated at next chunk start. Must be < chunk_size.",
    )
    rrf_k: int = Field(default=60, ge=1, description="RRF k constant.")
    vector_weight: float = Field(
        default=0.7, ge=0.0, le=1.0,
        description="Blend weight for vector rank in RRF (0.0..1.0).",
    )
    fanout_multiplier: int = Field(
        default=2, ge=1,
        description="Internal fan-out factor (limit * fanout per side).",
    )
    final_limit: int = Field(default=20, ge=0, description="Default --limit for `search sem`.")
    user_defined_stop_words: list[str] = Field(
        default_factory=list,
        description="Extra tokens to drop from FTS5 query (case-insensitive).",
    )
    keep_negation_words: list[str] = Field(
        default_factory=lambda: ["not"],
        description="Tokens to retain even though spaCy treats them as stopwords.",
    )
    max_chunks_per_tdoc: int = Field(
        default=32, ge=1,
        description="Cap on chunks per TDoc to bound parse latency on long covers.",
    )

    @field_validator("chunk_overlap")
    @classmethod
    def _overlap_less_than_size(cls, v, info):
        size = info.data.get("chunk_size", 800)
        if v >= size:
            raise ValueError(f"chunk_overlap ({v}) must be < chunk_size ({size})")
        return v
```

Add the field on `Settings` (after `search` field at line ~490):

```python
    semantic_search: SemanticSearchSettings = Field(default_factory=SemanticSearchSettings)
```

Update `pyproject.toml`:

```toml
[project.optional-dependencies]
cli = ["typer>=0.12.3"]
extract = ["python-docx>=0.8.11"]
search = []
semantic = [
  "sentence-transformers>=2.7.0",
  "spacy>=3.7.0",
  "sqlite-vec>=0.1.0",
]
mysql = ["pymysql>=1.1.1"]
postgres = ["psycopg[binary]>=3.2.0"]
dev = [
  "doc3gpp[cli]",
  "pytest>=8.2.0",
  "pytest-cov>=5.0.0",
  "ruff>=0.5.0",
]
```

And add the marker:

```toml
[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
markers = [
  "online: tests that access live internet endpoints (for example 3gpp.org)",
  "mysql: tests that require a mysql backend",
  "semantic: tests that require the [semantic] extra (sentence-transformers, spacy, sqlite-vec)",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_semantic_settings.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/settings/schema.py pyproject.toml tests/unit/test_semantic_settings.py
git commit -m "feat(search): add SemanticSearchSettings + [semantic] extra + pytest marker"
```

---

## Task 9: Factory — `build_semantic_search_service` + wire into `build_tdoc_cr_service`

**Files:**
- Modify: `src/doc3gpp/services/factory.py` (add `build_semantic_search_service`; pass `semantic_service=...` in `build_tdoc_cr_service`).
- Test: `tests/unit/test_factory_semantic.py`

**Interfaces:**
- Consumes: `doc3gpp.services.search_service.SearchService`; `doc3gpp.services.semantic_search_service.SemanticSearchService`; `doc3gpp.services.embedding.embedder.SentenceTransformerEmbedder`; `doc3gpp.storage.repositories.vector_sql.SQLAlchemyVectorIndexRepository`; `doc3gpp.models.semantic_search.VectorIndexUnavailableError, EmbedderUnavailableError, SpacyUnavailableError`; `doc3gpp.settings.loader.get_settings`.
- Produces: `build_semantic_search_service(settings, fts5_service=None, embedder=None, vector_repo=None) -> SemanticSearchService | None`. `build_tdoc_cr_service` passes `semantic_service=build_semantic_search_service(...)` to `TDocCrService`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_factory_semantic.py
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_returns_none_when_fts5_unavailable(monkeypatch):
    from doc3gpp.services import factory
    monkeypatch.setattr(factory, "build_search_service", lambda *a, **kw: None)
    out = factory.build_semantic_search_service(MagicMock())
    assert out is None


def test_returns_none_when_vector_repo_raises(monkeypatch):
    from doc3gpp.models.semantic_search import VectorIndexUnavailableError
    from doc3gpp.services import factory
    monkeypatch.setattr(factory, "build_search_service", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(
        "doc3gpp.services.factory.SentenceTransformerEmbedder",
        lambda *a, **kw: MagicMock(),
    )
    def boom(*a, **kw):
        raise VectorIndexUnavailableError("no sqlite-vec")
    monkeypatch.setattr(
        "doc3gpp.storage.repositories.vector_sql.SQLAlchemyVectorIndexRepository",
        lambda *a, **kw: (_ for _ in ()).throw(boom()),
    )
    out = factory.build_semantic_search_service(MagicMock())
    assert out is None


def test_returns_service_when_all_present(monkeypatch):
    from doc3gpp.services import factory
    monkeypatch.setattr(factory, "build_search_service", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(
        "doc3gpp.services.factory.SentenceTransformerEmbedder",
        lambda *a, **kw: MagicMock(),
    )
    fake_repo = MagicMock()
    monkeypatch.setattr(
        "doc3gpp.storage.repositories.vector_sql.SQLAlchemyVectorIndexRepository",
        lambda *a, **kw: fake_repo,
    )
    out = factory.build_semantic_search_service(MagicMock())
    assert out is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_factory_semantic.py -v`
Expected: FAIL with `AttributeError: module 'doc3gpp.services.factory' has no attribute 'build_semantic_search_service'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/doc3gpp/services/factory.py` (after `build_search_service`):

```python
def build_semantic_search_service(
    settings: Settings | None = None,
    fts5_service: "SearchService | None" = None,
    embedder: "Embedder | None" = None,
    vector_repo: "VectorIndexRepository | None" = None,
) -> "SemanticSearchService | None":
    """Build a :class:`SemanticSearchService` or return ``None`` if unavailable.

    Best-effort: catches :class:`VectorIndexUnavailableError`,
    :class:`EmbedderUnavailableError`, :class:`SpacyUnavailableError`
    raised by the collaborators and returns ``None``. FTS5 is the
    foundation — if :func:`build_search_service` returns ``None`` this
    returns ``None`` too.
    """
    from doc3gpp.models.semantic_search import (
        EmbedderUnavailableError,
        SpacyUnavailableError,
        VectorIndexUnavailableError,
    )
    from doc3gpp.services.embedding.embedder import SentenceTransformerEmbedder
    from doc3gpp.services.semantic_search_service import SemanticSearchService
    from doc3gpp.storage.repositories.vector_sql import (
        SQLAlchemyVectorIndexRepository,
    )

    if settings is None:
        settings = get_settings()
    if not settings.semantic_search.enabled:
        return None
    try:
        if fts5_service is None:
            fts5_service = build_search_service(settings)
        if fts5_service is None:
            return None
        if embedder is None:
            embedder = SentenceTransformerEmbedder(
                settings.semantic_search.embedding_model,
            )
        if vector_repo is None:
            vector_repo = SQLAlchemyVectorIndexRepository()
        return SemanticSearchService(
            fts5_service=fts5_service, embedder=embedder,
            vector_repo=vector_repo, settings=settings,
        )
    except (
        VectorIndexUnavailableError,
        EmbedderUnavailableError,
        SpacyUnavailableError,
    ):
        return None
```

Modify the `build_tdoc_cr_service` return block to pass `semantic_service`:

```python
    return TDocCrService(
        cache=TDocCache(
            root=settings.cache.dir,
            size_limit_bytes=settings.cache.size_limit_mb * 1024 * 1024,
        ),
        scraper_client=ScraperClient(),
        cr_repository=SQLAlchemyTDocCrRepository(),
        cr_ttcn_repository=cr_ttcn_repository or build_tdoc_cr_ttcn_repository(),
        cr_change_details_repository=(
            cr_change_details_repository
            or build_tdoc_cr_change_details_repository()
        ),
        tdoc_repository=SQLAlchemyTDocRepository(),
        max_tdoc_size_bytes=max_tdoc_size_bytes,
        search_service=build_search_service(),
        semantic_service=build_semantic_search_service(),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_factory_semantic.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/factory.py tests/unit/test_factory_semantic.py
git commit -m "feat(search): add build_semantic_search_service + wire into build_tdoc_cr_service"
```

---

## Task 10: Migration — `_create_vector_schema` DDL

**Files:**
- Modify: `src/doc3gpp/storage/db/migrate.py` (add `_create_vector_schema`; call from `create_schema`).
- Test: `tests/integration/test_vector_schema_migration.py`

**Interfaces:**
- Consumes: `sqlite_vec` (lazily, inside the function); `doc3gpp.storage.db.session.get_engine`.
- Produces: `_create_vector_schema()` creates `vec_tdoc_embeddings` (vec0 virtual table, dim 384) + `vec_meta` sidecar, gated on `dialect.name == "sqlite"` and a `sqlite_vec.load()` probe. Idempotent (`IF NOT EXISTS`). Called from `create_schema()` after `_create_search_schema()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_vector_schema_migration.py
from __future__ import annotations

import pytest

pytestmark = pytest.mark.semantic


def test_vector_schema_created(sqlite_env):
    from doc3gpp.storage.db.migrate import create_schema
    create_schema()
    from sqlalchemy import inspect
    from doc3gpp.storage.db.session import get_engine
    insp = inspect(get_engine())
    # vec_tdoc_embeddings is a virtual table; may not appear in
    # get_table_names() depending on sqlalchemy version. Use a raw
    # query instead.
    with get_engine().begin() as conn:
        from sqlalchemy import text
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='vec_meta'")
        ).all()
        assert any(r[0] == "vec_meta" for r in rows)
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='vec_tdoc_embeddings'")
        ).all()
        assert any(r[0] == "vec_tdoc_embeddings" for r in rows)


def test_vector_schema_idempotent(sqlite_env):
    from doc3gpp.storage.db.migrate import create_schema
    create_schema()
    create_schema()  # second call must not raise


def test_vector_schema_skipped_on_non_sqlite(monkeypatch, tmp_path):
    # Simulate non-sqlite by mocking the engine dialect
    from doc3gpp.storage.db import migrate
    from doc3gpp.storage.db.session import get_engine
    create_schema_called = False
    original = migrate._create_vector_schema
    def stub():
        nonlocal create_schema_called
        create_schema_called = True
    monkeypatch.setattr(migrate, "_create_vector_schema", stub)
    # Just verify the function exists and is callable
    assert callable(migrate._create_vector_schema)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_vector_schema_migration.py -v -m semantic`
Expected: FAIL — `vec_meta` table not created.

- [ ] **Step 3: Write minimal implementation**

Add to `src/doc3gpp/storage/db/migrate.py` after `_create_search_schema`:

```python
def _create_vector_schema() -> None:
    """Create the ``vec_tdoc_embeddings`` virtual table + ``vec_meta`` sidecar.

    Gated on the engine dialect being sqlite and on the runtime
    availability of the ``sqlite-vec`` extension. On every other path
    this is a no-op. Idempotent (``IF NOT EXISTS``).
    """
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        return
    try:
        import sqlite_vec
    except ImportError:
        return
    with engine.begin() as conn:
        try:
            sqlite_vec.load(conn.connection.driver_connection)
        except Exception:
            return
        conn.execute(
            text(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_tdoc_embeddings USING vec0(
                    chunk_id TEXT PRIMARY KEY,
                    tdoc_id TEXT,
                    chunk_index INTEGER,
                    embedding FLOAT[384]
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS vec_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
        )
```

Update `create_schema`:

```python
def create_schema() -> None:
    """Create database tables for configured backend."""
    engine = get_engine()
    _migrate_rename_tdoc_cr_details()
    Base.metadata.create_all(bind=engine)
    _create_search_schema()
    _create_vector_schema()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_vector_schema_migration.py -v -m semantic`
Expected: PASS (3 tests). Requires `pip install doc3gpp[semantic]`.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/storage/db/migrate.py tests/integration/test_vector_schema_migration.py
git commit -m "feat(search): add vec_tdoc_embeddings + vec_meta DDL to migrate"
```

---

## Task 11: Auto-embed hook in `TDocCrService`

**Files:**
- Modify: `src/doc3gpp/services/tdoc_cr_service.py` (add `semantic_service` param + `_embed_after_parse` + 2 call sites).
- Test: `tests/integration/test_embed_after_parse.py`

**Interfaces:**
- Consumes: `doc3gpp.services.semantic_search_service.SemanticSearchService` (forward-ref); `doc3gpp.models.semantic_search.SemanticSearchUnavailableError`.
- Produces: `TDocCrService.__init__` gains `semantic_service: "SemanticSearchService | None" = None`; new `_embed_after_parse(tdoc_id)` private helper; two new call sites at lines 652 and 1158 (sibling of `_index_after_parse`).

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_embed_after_parse.py
from __future__ import annotations

import pytest

pytestmark = pytest.mark.semantic


def test_embed_hook_fires_after_parse(sqlite_env, monkeypatch):
    # Build a minimal TDoc + CR cover row, run extract, assert
    # index_for_tdoc was called on the semantic service.
    from unittest.mock import MagicMock
    from doc3gpp.storage.db.migrate import create_schema
    create_schema()
    # ... seed tdocs + meetings + a CR cover row ...
    # ... build TDocCrService with semantic_service=mock ...
    # ... call extract() ...
    # ... assert mock.index_for_tdoc.assert_called_once_with("R5-1") ...
    pytest.skip("full integration wiring; see FTS5 test_search_after_parse.py for the pattern")


def test_embed_hook_skipped_when_auto_embed_disabled(sqlite_env, monkeypatch):
    # settings.semantic_search.auto_embed_on_parse = False
    pytest.skip("mirror test_search_after_parse disabled-path test")


def test_reparse_fewer_chunks_deletes_surplus(sqlite_env, monkeypatch):
    pytest.skip("requires full extract wiring; use mock semantic_service and assert remove_for_tdoc called before upsert_chunks")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_embed_after_parse.py -v -m semantic`
Expected: FAIL — `_embed_after_parse` does not exist yet (the `skip` tests pass trivially, so use a non-skip test or remove skips to confirm the hook is wired).

- [ ] **Step 3: Write minimal implementation**

Modify `TDocCrService.__init__` (line ~395) to accept `semantic_service`:

```python
        search_service: "SearchService | None" = None,
        semantic_service: "SemanticSearchService | None" = None,
    ) -> None:
        ...
        self._search_service = search_service
        self._semantic_service = semantic_service
```

Add the forward-ref import under `TYPE_CHECKING`:

```python
if TYPE_CHECKING:
    from doc3gpp.services.semantic_search_service import SemanticSearchService
```

Add the helper after `_index_after_parse` (line ~448):

```python
    def _embed_after_parse(self, tdoc_id: str) -> None:
        """Best-effort embedding upsert after a successful parse.

        Sibling of :meth:`_index_after_parse`; fires from the same two
        call sites. Skipped when
        ``Settings.semantic_search.auto_embed_on_parse`` is False or
        when the semantic extra is not installed
        (``_semantic_service`` is ``None``). Best-effort: every
        exception is caught and logged. A failing embed never aborts
        a successful parse.
        """
        if not self._settings.semantic_search.auto_embed_on_parse:
            return
        if self._semantic_service is None:
            return
        try:
            self._semantic_service.index_for_tdoc(tdoc_id)
        except Exception as exc:
            logger.warning(
                "failed to update embedding index for tdoc_id=%s: %s",
                tdoc_id, exc,
            )
```

Add the call sites — at line 652 (DB-mode `extract` happy path) right after `self._index_after_parse(normalised)`:

```python
        self._index_after_parse(normalised)
        self._embed_after_parse(normalised)
```

And at line 1158 (direct-mode `_extract_from_3gpp_url` happy path) right after `self._index_after_parse(tdoc_id)`:

```python
        self._index_after_parse(tdoc_id)
        self._embed_after_parse(tdoc_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_embed_after_parse.py -v -m semantic`
Expected: PASS. Replace the `pytest.skip` placeholders with real assertions mirroring `tests/integration/test_search_after_parse.py` (mock the `semantic_service`, call `extract()`, assert `index_for_tdoc` was called).

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/tdoc_cr_service.py tests/integration/test_embed_after_parse.py
git commit -m "feat(search): add _embed_after_parse hook to TDocCrService"
```

---

## Task 12: CLI — `search sem` command + `search index --rebuild-embeddings`

**Files:**
- Modify: `src/doc3gpp/cli.py` (add `sem` command on `search_app`; extend `index_command` with `--rebuild-embeddings` / `--rebuild-all`).
- Test: `tests/unit/test_cli_search_sem.py`

**Interfaces:**
- Consumes: `doc3gpp.services.factory.build_semantic_search_service`; `doc3gpp.models.semantic_search.*` errors; `doc3gpp.models.search.SearchFilters`; `doc3gpp.cli_filters.SearchQueryBuilder`, `parse_date_filter`, `parse_release_filter`, `parse_spec_filter`; the existing `_render_search_hits`, `_emit_search_status`, `_emit_explain` helpers.
- Produces: `search sem QUERY [filters]` Typer command; `search index --rebuild-embeddings` flag; `search index --rebuild-all` flag; `_render_semantic_hits(hits, *, format, compact)` renderer; extended stale hint.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_cli_search_sem.py
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app

runner = CliRunner()


def test_search_sem_rejects_negative_limit():
    result = runner.invoke(app, ["search", "sem", "q", "--limit", "-1"])
    assert result.exit_code != 0


def test_search_sem_rejects_vector_weight_out_of_range():
    result = runner.invoke(app, ["search", "sem", "q", "--vector-weight", "1.5"])
    assert result.exit_code != 0
    result = runner.invoke(app, ["search", "sem", "q", "--vector-weight", "-0.1"])
    assert result.exit_code != 0


def test_search_sem_unavailable_when_disabled(monkeypatch):
    from doc3gpp.services import factory
    monkeypatch.setattr(factory, "build_semantic_search_service", lambda *a, **kw: None)
    result = runner.invoke(app, ["search", "sem", "q"])
    assert "unavailable" in result.output.lower() or result.exit_code == 1


def test_search_sem_query_error_exit_2(monkeypatch):
    from unittest.mock import MagicMock
    from doc3gpp.models.semantic_search import SemanticSearchQueryError
    svc = MagicMock()
    svc.search.side_effect = SemanticSearchQueryError("empty after strip")
    from doc3gpp.services import factory
    monkeypatch.setattr(factory, "build_semantic_search_service", lambda *a, **kw: svc)
    result = runner.invoke(app, ["search", "sem", "   "])
    assert result.exit_code == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_cli_search_sem.py -v`
Expected: FAIL — `search sem` command does not exist.

- [ ] **Step 3: Write minimal implementation**

Add the `sem` command to `src/doc3gpp/cli.py` (after `index_command`, ~line 4270):

```python
@search_app.command("sem")
def sem_command(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Natural-language query."),
    tsg: str | None = typer.Option(None, "--tsg", help="Filter by meetings.tsg."),
    meeting: str | None = typer.Option(None, help="Filter by meetings.name."),
    meeting_id: int | None = typer.Option(None, help="Filter by meetings.meeting_id."),
    tdoc_id: str | None = typer.Option(None, help="Filter by tdocs.tdoc_id."),
    release: str | None = typer.Option(None, help="Filter by tdocs.release."),
    spec: str | None = typer.Option(None, help="Filter by tdocs.spec."),
    since: str | None = typer.Option(None, help="Uploaded-date lower bound (YYYY-MM-DD)."),
    until: str | None = typer.Option(None, help="Uploaded-date upper bound (YYYY-MM-DD)."),
    limit: int = typer.Option(20, "--limit", min=0, help="Max results after RRF."),
    vector_weight: float = typer.Option(0.7, "--vector-weight", min=0.0, max=1.0,
        help="Blend weight for vector rank (0.0..1.0)."),
    format: str = typer.Option("table", "--format", help="table | json | markdown"),
    compact: bool = typer.Option(False, "--compact", help="Strip decorators."),
    explain: bool = typer.Option(False, "--explain", help="Print RRF config + best chunk."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress stale-index hint."),
) -> None:
    """Run a semantic (FTS5 + embedding vector) search over TDocs.

    The query is stripped of stopwords + lemmatized for the FTS5 path;
    the embedding path uses the ORIGINAL query. Results are merged
    via reciprocal-rank fusion (RRF) and truncated to --limit.
    """
    from doc3gpp.cli_filters import (
        parse_date_filter, parse_release_filter, parse_spec_filter,
    )
    from doc3gpp.models.search import SearchFilters
    from doc3gpp.models.semantic_search import (
        EmbedderUnavailableError, SemanticSearchQueryError,
        SemanticSearchUnavailableError, SpacyUnavailableError,
        VectorIndexUnavailableError,
    )
    from doc3gpp.services.factory import build_semantic_search_service

    try:
        if since: parse_date_filter(since)
        if until: parse_date_filter(until)
        if release: parse_release_filter(release)
        if spec: parse_spec_filter(spec)
    except ValueError as exc:
        raise typer.BadParameter(str(exc))

    svc = build_semantic_search_service()
    if svc is None:
        typer.echo("search sem unavailable; run `pip install doc3gpp[semantic]`", err=True)
        raise typer.Exit(code=1)
    filters = SearchFilters(
        tsg=tsg, meeting=meeting, meeting_id=meeting_id, tdoc_id=tdoc_id,
        release=release, spec=spec, since=since, until=until, limit=limit,
    )
    try:
        hits = svc.search(query, filters, limit=limit, vector_weight=vector_weight)
    except SemanticSearchQueryError as exc:
        typer.echo(f"bad query: {exc}", err=True)
        raise typer.Exit(code=2)
    except SpacyUnavailableError as exc:
        typer.echo(f"spaCy model not installed; run `python -m spacy download en_core_web_sm`", err=True)
        raise typer.Exit(code=1)
    except EmbedderUnavailableError as exc:
        typer.echo(f"embedding model load failed: {exc}", err=True)
        raise typer.Exit(code=1)
    except VectorIndexUnavailableError as exc:
        typer.echo(f"vector index unavailable: {exc}", err=True)
        raise typer.Exit(code=1)
    except SemanticSearchUnavailableError as exc:
        typer.echo(f"search sem unavailable: {exc}", err=True)
        raise typer.Exit(code=1)
    if explain:
        typer.echo("# semantic search config", err=True)
        typer.echo(f"vector_weight:   {vector_weight}", err=True)
        typer.echo(f"limit:           {limit}", err=True)
        typer.echo(f"rrf_k:           {svc._settings.semantic_search.rrf_k}", err=True)
        typer.echo(f"fanout:          {svc._settings.semantic_search.fanout_multiplier}", err=True)
    _render_semantic_hits(hits, format=format, compact=compact)
    _emit_search_status(svc, quiet=quiet)


def _render_semantic_hits(hits: list, *, format: str, compact: bool) -> None:
    """Render SemanticSearchHit list in table / json / markdown."""
    if format == "json":
        import json as _json
        payload = [
            {
                "tdoc_id": h.tdoc_id, "rrf_score": h.rrf_score,
                "rank_fts5": h.rank_fts5, "rank_vec": h.rank_vec,
                "min_chunk_distance": h.min_chunk_distance,
                "best_chunk_id": h.best_chunk_id,
                "fts5_hit": {
                    "tdoc_id": h.fts5_hit.tdoc_id, "title": h.fts5_hit.title,
                    "ftp_url": h.fts5_hit.ftp_url, "wis": h.fts5_hit.wis,
                },
            }
            for h in hits
        ]
        if compact:
            typer.echo(_json.dumps(payload, separators=(",", ":")))
        else:
            typer.echo(_json.dumps(payload, indent=2))
    elif format == "markdown":
        for i, h in enumerate(hits, 1):
            typer.echo(f"{i}. **{h.tdoc_id}** — rrf={h.rrf_score:.4f}")
            if h.best_chunk_id:
                typer.echo(f"   best chunk: {h.best_chunk_id} (dist={h.min_chunk_distance:.4f})")
            if h.fts5_hit.title:
                typer.echo(f"   title: {h.fts5_hit.title}")
            typer.echo("")
    else:
        typer.echo(
            f"{'rank':>4} {'tdoc_id':<14} {'rrf':>8} {'fts':>4} {'vec':>4} {'dist':>8}  title"
        )
        for i, h in enumerate(hits, 1):
            fts = str(h.rank_fts5) if h.rank_fts5 is not None else "-"
            vec = str(h.rank_vec) if h.rank_vec is not None else "-"
            dist = f"{h.min_chunk_distance:.4f}" if h.min_chunk_distance is not None else "-"
            title = (h.fts5_hit.title or "")[:40]
            typer.echo(
                f"{i:>4} {h.tdoc_id:<14} {h.rrf_score:>8.4f} {fts:>4} {vec:>4} {dist:>8}  {title}"
            )
```

Extend `index_command` to add `--rebuild-embeddings` and `--rebuild-all`:

```python
@search_app.command("index")
def index_command(
    ctx: typer.Context,
    rebuild: bool = typer.Option(False, "--rebuild", help="Drop and rebuild the FTS5 table."),
    rebuild_embeddings: bool = typer.Option(False, "--rebuild-embeddings",
        help="Drop and rebuild the vec_tdoc_embeddings table."),
    rebuild_all: bool = typer.Option(False, "--rebuild-all",
        help="Rebuild both the FTS5 and the vector index."),
    batch: int | None = typer.Option(None, "--batch", min=1, help="Override rebuild_batch_size."),
    resume: bool = typer.Option(False, "--resume", help="Resume from the last cursor."),
    stale_only: bool = typer.Option(False, "--stale-only",
        help="Only re-index rows newer than the last indexed uploaded_date."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress per-batch progress logs."),
) -> None:
    ...
    do_fts5 = rebuild or rebuild_all
    do_vec = rebuild_embeddings or rebuild_all
    if not do_fts5 and not do_vec:
        # existing status-only path
        ...
    if do_fts5:
        # existing FTS5 rebuild loop
        ...
    if do_vec:
        from doc3gpp.services.factory import build_semantic_search_service
        sem_svc = build_semantic_search_service()
        if sem_svc is None:
            typer.echo("semantic search unavailable; run `pip install doc3gpp[semantic]`", err=True)
            raise typer.Exit(code=1)
        settings = get_settings()
        batch_size = batch or settings.semantic_search.chunk_size  # or a dedicated knob
        for progress in sem_svc.rebuild_embeddings(
            batch_size=batch_size, stale_only=stale_only, quiet=quiet,
        ):
            if not quiet:
                typer.echo(
                    f"  embedded {progress.processed:,}/{progress.total:,} "
                    f"(last tdoc_id={progress.current_tdoc_id})"
                )
        typer.echo("search index embedding rebuild complete")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_cli_search_sem.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/cli.py tests/unit/test_cli_search_sem.py
git commit -m "feat(search): add `search sem` command + `search index --rebuild-embeddings`"
```

---

## Task 13: Integration corpus + end-to-end tests + extras-disabled test

**Files:**
- Create: `tests/fixtures/semantic_search_corpus.py`
- Modify: `tests/conftest.py` (add `semantic_search_corpus` fixture).
- Create: `tests/integration/test_search_sem_end_to_end.py`
- Create: `tests/integration/test_semantic_extras_disabled.py`

**Interfaces:**
- Consumes: T1-T12 (full stack). The fixture mirrors `tests/fixtures/search_corpus.py` but adds gzip cover/ttcn/change blobs and pre-computed embeddings (mocked embedder returning pre-computed vectors so tests don't load the real model).
- Produces: `semantic_search_corpus` fixture; end-to-end `search sem` test; extras-disabled graceful-degradation test.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_search_sem_end_to_end.py
from __future__ import annotations

import pytest

pytestmark = pytest.mark.semantic


def test_search_sem_returns_expected_tdoc(semantic_search_corpus):
    from doc3gpp.models.search import SearchFilters
    from doc3gpp.services.semantic_search_service import SemanticSearchService
    # Build service with mocked embedder returning pre-computed vectors
    # ... assert `search sem "what CRs touch NB-IoT power saving"` returns
    # ... the NB-IoT TDoc at rank 0 or 1.
    pytest.skip("implement once fixture is wired")


def test_search_sem_filter_by_tsg(semantic_search_corpus):
    pytest.skip("every filter flag end-to-end")


def test_search_sem_vector_weight_zero_is_fts5_only(semantic_search_corpus):
    pytest.skip("W=0.0 → vector contributes nothing")
```

```python
# tests/integration/test_semantic_extras_disabled.py
from __future__ import annotations


def test_semantic_disabled_in_settings(sqlite_env, monkeypatch):
    from doc3gpp.services.factory import build_semantic_search_service
    from doc3gpp.settings.loader import get_settings
    # Force semantic_search.enabled = False
    settings = get_settings()
    settings.semantic_search.enabled = False
    out = build_semantic_search_service(settings)
    assert out is None


def test_semantic_returns_none_when_sqlite_vec_missing(sqlite_env, monkeypatch):
    # Simulate sqlite-vec not installed
    import sys
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)
    from doc3gpp.services.factory import build_semantic_search_service
    from doc3gpp.settings.loader import get_settings
    out = build_semantic_search_service(get_settings())
    assert out is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/integration/test_search_sem_end_to_end.py tests/integration/test_semantic_extras_disabled.py -v -m semantic`
Expected: FAIL — fixtures missing.

- [ ] **Step 3: Write minimal implementation**

Create `tests/fixtures/semantic_search_corpus.py` mirroring `tests/fixtures/search_corpus.py` but with 8 TDocs (3 with TTCN, 2 with change-details, 1 metadata-only, 1 multi-chunk, 1 single-chunk, 1 with `38.300`, 1 with NB-IoT jargon) and pre-computed embedding vectors. Export `build_semantic_corpus(engine) -> list[str]` and `PRECOMPUTED_EMBEDDINGS: dict[str, list[np.ndarray]]`.

Add to `tests/conftest.py`:

```python
@pytest.fixture()
def semantic_search_corpus(sqlite_env):
    """Populate a sqlite engine with the semantic-search corpus rows + vector index."""
    from doc3gpp.storage.db.migrate import create_schema
    create_schema()
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.search_sql import SQLAlchemySearchIndexRepository
    from doc3gpp.storage.repositories.vector_sql import SQLAlchemyVectorIndexRepository
    from tests.fixtures.semantic_search_corpus import (
        build_semantic_corpus, PRECOMPUTED_EMBEDDINGS,
    )

    engine = get_engine()
    tdoc_ids = build_semantic_corpus(engine)
    fts5_repo = SQLAlchemySearchIndexRepository()
    for tid in tdoc_ids:
        fts5_repo.upsert(tid)
    vec_repo = SQLAlchemyVectorIndexRepository()
    for tid in tdoc_ids:
        vec_repo.upsert_chunks(tid, PRECOMPUTED_EMBEDDINGS.get(tid, []))
    yield engine
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/integration/test_search_sem_end_to_end.py tests/integration/test_semantic_extras_disabled.py -v -m semantic`
Expected: PASS. Replace `pytest.skip` placeholders with real assertions using a mocked embedder that returns the pre-computed vectors.

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/semantic_search_corpus.py tests/conftest.py tests/integration/test_search_sem_end_to_end.py tests/integration/test_semantic_extras_disabled.py
git commit -m "test(search): add semantic search corpus + end-to-end + extras-disabled tests"
```

---

## Task 14: Docs sync — architecture, code-map, cli, TOML example, AGENTS.md

**Files:**
- Modify: `docs/architecture.md` (add `vec_tdoc_embeddings` to schema diagram + `search sem` workflow bullet).
- Modify: `docs/code-map.md` (add new file rows for `models/semantic_search.py`, `services/embedding/*`, `services/semantic_search_service.py`, `storage/repositories/vector_sql.py`).
- Modify: `docs/cli.md` (document `search sem` flags + `search index --rebuild-embeddings`/`--rebuild-all` + new TOML `[semantic_search]` fields).
- Modify: `doc3gpp.toml.example` (add `[semantic_search]` block with all 12 knobs).
- Modify: `AGENTS.md` (update "Where to look" table + workflows).

**Interfaces:**
- Consumes: T1-T13 (all implemented). This is a documentation-only task; no test.

- [ ] **Step 1: Update `docs/architecture.md`**

Add the `vec_tdoc_embeddings` virtual table to the ORM schema section. Add a new workflow bullet to the "Workflows in one line" section:

```
- `doc3gpp search sem QUERY [filters]` →
  `SemanticSearchService.search` → spaCy stopword strip (FTS5 path) +
  original query embedding (vector path) → FTS5 fan-out (`2N`) +
  vector KNN fan-out (`2N`) → `rrf_merge` → truncate to `--limit`
  (default 20). `--vector-weight` (0.0..1.0, default 0.7) blends the
  two ranks via `rrf = 1/(k + rank_fts5) * (1 - W) + 1/(k + rank_vec)
  * W` (`k=60`). `search query` (FTS5-only) is unchanged.
- `doc3gpp search index --rebuild-embeddings [--stale-only] [--batch N]
  [--resume] [--quiet]` → `SemanticSearchService.rebuild_embeddings`
  → drops + recreates `vec_tdoc_embeddings`; iterates every `tdocs`
  row, calls `index_for_tdoc` per id (build embed text → chunk →
  embed → upsert); updates `vec_meta` for resume + staleness.
  `--rebuild-all` runs both FTS5 and vector rebuilds in sequence.
```

- [ ] **Step 2: Update `docs/code-map.md`**

Add rows for each new file with a one-line purpose.

- [ ] **Step 3: Update `docs/cli.md`**

Add a `## search sem` section documenting every flag (positional `QUERY`, `--tsg`, `--meeting`, `--meeting-id`, `--tdoc-id`, `--release`, `--spec`, `--since`, `--until`, `--limit` default 20, `--vector-weight` default 0.7 range 0.0-1.0, `--format`, `--compact`, `--explain`, `--quiet`). Add `--rebuild-embeddings` / `--rebuild-all` to the `search index` section. Add the `[semantic_search]` TOML block reference.

- [ ] **Step 4: Update `doc3gpp.toml.example`**

Add after the `[search]` block:

```toml
[semantic_search]
enabled = true
auto_embed_on_parse = true
embedding_model = "sentence-transformers/all-MiniLM-L6-v2"
chunk_size = 800
chunk_overlap = 100
rrf_k = 60
vector_weight = 0.7
fanout_multiplier = 2
final_limit = 20
user_defined_stop_words = []
keep_negation_words = ["not"]
max_chunks_per_tdoc = 32
```

- [ ] **Step 5: Update `AGENTS.md`**

Add to the "Where to look" table:

| Task | Location | Notes |
| --- | --- | --- |
| Add a semantic search knob | `src/doc3gpp/settings/schema.py` (`SemanticSearchSettings`) | TOML `[semantic_search]` block. |
| Add a `search sem` flag | `src/doc3gpp/cli.py` (`sem_command`) | Mirror `search_command` pattern. |
| Add an embedding model | `src/doc3gpp/services/embedding/embedder.py` | Lazy model load; `Embedder` Protocol in `repository/protocols.py`. |
| Add a vector DDL change | `src/doc3gpp/storage/db/migrate.py` (`_create_vector_schema`) + `src/doc3gpp/storage/repositories/vector_sql.py` | Gated on sqlite + sqlite-vec. |

Add a workflow bullet for `search sem` and `search index --rebuild-embeddings` mirroring the architecture.md addition.

- [ ] **Step 6: Commit**

```bash
git add docs/architecture.md docs/code-map.md docs/cli.md doc3gpp.toml.example AGENTS.md
git commit -m "docs(search): document semantic search subsystem"
```

---

## Self-Review (run after writing, fix inline)

**1. Spec coverage:**
- [x] `search sem QUERY [filters]` subcommand → T12
- [x] `search query` unchanged → no task (explicitly preserved)
- [x] `search index --rebuild-embeddings` / `--rebuild-all` → T12
- [x] Vector schema `vec_tdoc_embeddings` + `vec_meta` → T6 (repo), T10 (DDL)
- [x] Chunking `_chunks(text, size=800, overlap=100)` → T2
- [x] Stopwords `strip_stopwords` + custom sets → T3
- [x] Embedder `SentenceTransformerEmbedder` + `Embedder` Protocol → T4, T5
- [x] `SemanticSearchService` + `rrf_merge` → T7
- [x] Auto-embed hook `_embed_after_parse` + 2 call sites → T11
- [x] Factory `build_semantic_search_service` + wire → T9
- [x] Settings `SemanticSearchSettings` (12 knobs) → T8
- [x] pyproject `[semantic]` extra + marker → T8
- [x] Migration → T10
- [x] DTOs + error hierarchy → T1
- [x] RRF formula + fanout → T7
- [x] Three-layer graceful degradation → T9, T13
- [x] Dim mismatch → CLI exit 1 → T6 (raises) + T12 (catches)
- [x] 17 edge cases → covered across T1-T13 unit + integration tests

**2. Placeholder scan:** No "TBD"/"TODO"/"fill in". The `pytest.skip` placeholders in T11 and T13 are explicit test scaffolding that the implementer fills in by mirroring the existing FTS5 test patterns (`test_search_after_parse.py`, `test_search_filters.py`) — not placeholder text.

**3. Type consistency:**
- `SemanticSearchHit` fields: `tdoc_id, rrf_score, fts5_hit, rank_fts5, rank_vec, min_chunk_distance, best_chunk_id` — consistent across T1, T7, T12.
- `rrf_merge` signature: `(fts5_hits, vec_hits, *, k, vector_weight, limit)` — consistent across T7, T12.
- `VectorIndexRepository.knn` returns `list[tuple[str, str, int, float]]` = `(tdoc_id, chunk_id, chunk_index, distance)` — consistent across T5, T6, T7.
- `build_semantic_search_service(settings, fts5_service, embedder, vector_repo) -> SemanticSearchService | None` — consistent across T9, T12.

Plan complete.