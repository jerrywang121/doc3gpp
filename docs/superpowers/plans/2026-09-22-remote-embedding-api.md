# Remote-only embedding backend (OpenAI-compatible API) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the local sentence-transformers embedder with a remote-only OpenAI-compatible HTTP embedder, gated on `embedding_base_url`, with dim+model tracking in `vec_meta`.

**Architecture:** New `OpenAICompatibleEmbedder` in `services/embedding/remote_embedder.py` implements the existing `Embedder` Protocol over `httpx`; factories return `None` when no URL is configured; `migrate._create_vector_schema` and the CLI rebuild path stamp/check both dim and model in `vec_meta`; `config show` redacts the API key.

**Tech Stack:** Python 3.10+, httpx (already core dep), pydantic v2, SQLAlchemy 2.0 + sqlite-vec, Typer, pytest, ruff.

## Global Constraints

- No new runtime dependencies (httpx already required).
- No Ollama-native `/api/embed` client; single `POST {base_url}/embeddings` shape.
- `SentenceTransformerEmbedder` deleted; `[semantic]` extra keeps `sqlite-vec`, drops `sentence-transformers`.
- URL unset/empty means the semantic stack is disabled via existing `None` paths, never an error.
- Same-model-same-dim is the only happy path; model or dim change forces `--rebuild-embeddings`.
- API key never logged, echoed, or included in error messages.
- Layering: `scraping/` transport style stays out; the embedder owns its httpx client. `parsers/` untouched. CLI stays thin via factories.
- Commit policy: one task = one commit, `ruff check .` clean, offline suite green (`./scripts/test_sqlite.sh`).

---

### Task 1: Settings — new remote knobs + key env allowlist + redaction

**Files:**
- Modify: `src/doc3gpp/settings/schema.py:58-68` (ALLOWED_ENV_VARS), `src/doc3gpp/settings/schema.py:500-560` (SemanticSearchSettings)
- Modify: `src/doc3gpp/cli.py:5023-5037` (config_show redaction)
- Test: `tests/unit/test_semantic_settings.py`
- Test: `tests/unit/test_remote_embedding_settings.py` (new)

**Interfaces:**
- Consumes: `FilteredEnvSettingsSource`, `ALLOWED_ENV_VARS`, `env_var_for_dotted_key`.
- Produces: `SemanticSearchSettings(base_url, model, api_key, timeout_s, batch_size)` fields consumed by Task 2 (exact names: `embedding_base_url: str | None = None`, `embedding_api_key: str | None = None`, `embedding_model: str = "nomic-embed-text"`, `embedding_timeout_s: float = 30.0`, `embedding_batch_size: int = 32`); `ALLOWED_ENV_VARS` gains `DOC3GPP_SEMANTIC_SEARCH__EMBEDDING_API_KEY` for Task 6 tests.

- [ ] **Step 1: Write the failing settings test**

```python
def test_remote_embedding_settings_defaults():
    from doc3gpp.settings.schema import SemanticSearchSettings
    s = SemanticSearchSettings()
    assert s.embedding_model == "nomic-embed-text"
    assert s.embedding_base_url is None
    assert s.embedding_api_key is None
    assert s.embedding_timeout_s == 30.0
    assert s.embedding_batch_size == 32


def test_remote_embedding_settings_validation():
    import pytest
    from doc3gpp.settings.schema import SemanticSearchSettings
    SemanticSearchSettings(embedding_timeout_s=1.0, embedding_batch_size=1)
    with pytest.raises(Exception):
        SemanticSearchSettings(embedding_timeout_s=0.0)
    with pytest.raises(Exception):
        SemanticSearchSettings(embedding_timeout_s=301.0)
    with pytest.raises(Exception):
        SemanticSearchSettings(embedding_batch_size=0)
    with pytest.raises(Exception):
        SemanticSearchSettings(embedding_batch_size=513)


def test_embedding_api_key_env_is_allowlisted():
    from doc3gpp.settings.schema import ALLOWED_ENV_VARS, env_var_for_dotted_key
    assert "DOC3GPP_SEMANTIC_SEARCH__EMBEDDING_API_KEY" in ALLOWED_ENV_VARS
    assert env_var_for_dotted_key("semantic_search.embedding_api_key") == (
        "DOC3GPP_SEMANTIC_SEARCH__EMBEDDING_API_KEY"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_remote_embedding_settings.py -v`
Expected: FAIL with "SemanticSearchSettings ... unexpected keyword" or ModuleNotFound.

- [ ] **Step 3: Implement settings change**

In `src/doc3gpp/settings/schema.py`, replace the `embedding_model` field block:

```python
    embedding_model: str = Field(
        default="nomic-embed-text",
        description="Remote embedding model name sent in the OpenAI-compatible /embeddings payload.",
    )
    embedding_base_url: str | None = Field(
        default=None,
        description="Base URL of the OpenAI-compatible embeddings API (e.g. http://localhost:11434/v1). Unset disables the semantic stack.",
    )
    embedding_api_key: str | None = Field(
        default=None,
        validation_alias="DOC3GPP_SEMANTIC_SEARCH__EMBEDDING_API_KEY",
        description="Bearer token for the embeddings API. Optional; local servers need none. Never logged.",
    )
    embedding_timeout_s: float = Field(default=30.0, ge=1.0, le=300.0, description="Per-request HTTP timeout in seconds.")
    embedding_batch_size: int = Field(default=32, ge=1, le=512, description="Max input texts per /embeddings request.")
```

Add `"DOC3GPP_SEMANTIC_SEARCH__EMBEDDING_API_KEY"` to `ALLOWED_ENV_VARS`. Update the class docstring to drop the sentence-transformers/HuggingFace wording.

In `src/doc3gpp/cli.py` `config_show`, redact before dumping:

```python
    dumped = settings.model_dump(mode="json")
    sem = dumped.get("semantic_search")
    if isinstance(sem, dict) and sem.get("embedding_api_key"):
        sem["embedding_api_key"] = "***"
    typer.echo(json.dumps(dumped, indent=2, sort_keys=True))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_remote_embedding_settings.py tests/unit/test_semantic_settings.py -v`
Expected: new tests PASS; `test_semantic_settings.py::test_defaults` FAILS on the old HF default — update its `embedding_model` assertion to `"nomic-embed-text"` in the same commit (mechanical follow, not a behavior change).

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/settings/schema.py src/doc3gpp/cli.py tests/unit/test_remote_embedding_settings.py tests/unit/test_semantic_settings.py
git commit -m "feat(embed): remote embedding settings + api key env + redaction"
```

---

### Task 2: `OpenAICompatibleEmbedder` HTTP client

**Files:**
- Create: `src/doc3gpp/services/embedding/remote_embedder.py`
- Modify: `src/doc3gpp/services/embedding/__init__.py`
- Test: `tests/unit/test_remote_embedder.py` (new)

**Interfaces:**
- Consumes: `Embedder` Protocol (`encode(texts) -> np.ndarray`, `dim -> int`), `EmbedderUnavailableError`.
- Produces: `OpenAICompatibleEmbedder(base_url, model, api_key=None, timeout_s=30.0, batch_size=32)` with `.encode()`, `.dim`, `.model_name`, `.close()` consumed by Task 3.

- [ ] **Step 1: Write the failing client test**

```python
import numpy as np


def _fake_response(payload):
    import httpx
    return httpx.Response(200, json=payload)


def test_encode_posts_openai_shape_and_returns_float32(monkeypatch):
    import httpx
    from doc3gpp.services.embedding.remote_embedder import OpenAICompatibleEmbedder
    seen = {}

    def fake_post(self, url, json=None, headers=None):
        seen["url"] = url
        seen["json"] = json
        seen["headers"] = headers
        return _fake_response({"data": [
            {"embedding": [0.1, 0.2, 0.3, 0.4]},
            {"embedding": [0.5, 0.6, 0.7, 0.8]},
        ]})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    emb = OpenAICompatibleEmbedder(base_url="http://localhost:11434/v1/", model="nomic-embed-text")
    out = emb.encode(["a", "b"])
    assert seen["url"] == "http://localhost:11434/v1/embeddings"
    assert seen["json"] == {"model": "nomic-embed-text", "input": ["a", "b"]}
    assert "Authorization" not in seen["headers"]
    assert out.shape == (2, 4) and out.dtype == np.float32


def test_encode_empty_returns_empty_without_http(monkeypatch):
    import httpx
    from doc3gpp.services.embedding.remote_embedder import OpenAICompatibleEmbedder

    def boom(*a, **kw):
        raise AssertionError("no HTTP for empty input")

    monkeypatch.setattr(httpx.Client, "post", boom)
    emb = OpenAICompatibleEmbedder(base_url="http://x/v1", model="m")
    out = emb.encode([])
    assert out.shape == (0, 0)


def test_bearer_header_and_batching(monkeypatch):
    import httpx
    from doc3gpp.services.embedding.remote_embedder import OpenAICompatibleEmbedder
    calls = []

    def fake_post(self, url, json=None, headers=None):
        calls.append((json, headers))
        n = len(json["input"])
        return _fake_response({"data": [{"embedding": [float(i)] * 2} for i in range(n)]})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    emb = OpenAICompatibleEmbedder(base_url="http://x/v1", model="m", api_key="k", batch_size=2)
    out = emb.encode(["a", "b", "c"])
    assert len(calls) == 2
    assert all(h["Authorization"] == "Bearer k" for _, h in calls)
    assert out.shape == (3, 2)


def test_dim_probes_once_and_caches(monkeypatch):
    import httpx
    from doc3gpp.services.embedding.remote_embedder import OpenAICompatibleEmbedder
    calls = []

    def fake_post(self, url, json=None, headers=None):
        calls.append(json)
        return _fake_response({"data": [{"embedding": [0.0] * 8}]})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    emb = OpenAICompatibleEmbedder(base_url="http://x/v1", model="m")
    assert emb.dim == 8
    assert emb.dim == 8
    assert len(calls) == 1


def test_http_error_maps_to_embedder_unavailable(monkeypatch):
    import httpx
    from doc3gpp.models.semantic_search import EmbedderUnavailableError
    from doc3gpp.services.embedding.remote_embedder import OpenAICompatibleEmbedder
    import pytest

    def fake_post(self, url, json=None, headers=None):
        return httpx.Response(401, json={"error": "bad key"})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    emb = OpenAICompatibleEmbedder(base_url="http://x/v1", model="m", api_key="wrong")
    with pytest.raises(EmbedderUnavailableError, match="401"):
        emb.encode(["a"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_remote_embedder.py -v`
Expected: FAIL with "No module named ... remote_embedder".

- [ ] **Step 3: Write minimal implementation**

```python
"""OpenAI-compatible remote embedder for the semantic search subsystem.

Single client for OpenAI, Ollama's ``/v1`` endpoint, vLLM, TEI, and any
other OpenAI-compatible server: ``POST {base_url}/embeddings`` with
``{"model", "input"}``. No new dependencies (httpx is already core).
"""

from __future__ import annotations

import logging
import threading

import httpx
import numpy as np

from doc3gpp.models.semantic_search import EmbedderUnavailableError

logger = logging.getLogger(__name__)


class OpenAICompatibleEmbedder:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_s: float = 30.0,
        batch_size: int = 32,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_name = model
        self._api_key = api_key
        self._batch_size = max(1, batch_size)
        self._client = httpx.Client(timeout=timeout_s)
        self._lock = threading.Lock()
        self._dim: int | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def _headers(self) -> dict[str, str]:
        if self._api_key:
            return {"Authorization": f"Bearer {self._api_key}"}
        return {}

    def _post_batch(self, batch: list[str]) -> list[list[float]]:
        try:
            resp = self._client.post(
                f"{self._base_url}/embeddings",
                json={"model": self._model_name, "input": batch},
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise EmbedderUnavailableError(
                f"embedding API request failed for model {self._model_name!r}: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise EmbedderUnavailableError(
                f"embedding API error HTTP {resp.status_code} for model {self._model_name!r}"
            )
        try:
            data = resp.json()["data"]
            return [row["embedding"] for row in data]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbedderUnavailableError(
                f"malformed embedding API response for model {self._model_name!r}: {exc}"
            ) from exc

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        rows: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            rows.extend(self._post_batch(texts[i:i + self._batch_size]))
        if len(rows) != len(texts):
            raise EmbedderUnavailableError(
                f"embedding API returned {len(rows)} vectors for {len(texts)} inputs "
                f"(model {self._model_name!r})"
            )
        return np.asarray(rows, dtype=np.float32)

    @property
    def dim(self) -> int:
        if self._dim is None:
            with self._lock:
                if self._dim is None:
                    self._dim = int(self.encode(["probe"])[0].shape[-1])
        return self._dim

    def close(self) -> None:
        self._client.close()
```

Also update `src/doc3gpp/services/embedding/__init__.py` to export `OpenAICompatibleEmbedder` instead of `SentenceTransformerEmbedder`. Update the `Embedder` Protocol docstring in `src/doc3gpp/repository/protocols.py:770-786` to describe the remote impl (same commit, doc-only).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_remote_embedder.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/embedding/remote_embedder.py src/doc3gpp/services/embedding/__init__.py src/doc3gpp/repository/protocols.py tests/unit/test_remote_embedder.py
git commit -m "feat(embed): OpenAI-compatible remote embedder over httpx"
```

---

### Task 3: Factories — remote-only wiring, None when unconfigured

**Files:**
- Modify: `src/doc3gpp/services/factory.py:18,161-172,285-288,332-336,357-361`
- Modify: `src/doc3gpp/services/embedding/embedder.py` (delete file)
- Modify: `src/doc3gpp/cli.py:5363-5367,5479-5485` (unavailable hints)
- Test: `tests/unit/test_factory_semantic.py`
- Test: `tests/unit/test_embedder.py` (delete), `tests/unit/test_remote_embedder.py` (keep)

**Interfaces:**
- Consumes: `OpenAICompatibleEmbedder` from Task 2; `SemanticSearchSettings` fields from Task 1.
- Produces: `build_embedder(settings) -> OpenAICompatibleEmbedder | None` (None = URL unset) consumed by `build_state`, `build_tdoc_cr_service`, `build_search_service`, `build_semantic_search_service`; `build_*` returning `None` on `EmbedderUnavailableError` unchanged.

- [ ] **Step 1: Write the failing factory test**

```python
def test_build_embedder_returns_none_when_url_unset():
    from doc3gpp.services import factory
    from doc3gpp.settings.schema import SemanticSearchSettings, Settings
    settings = Settings(semantic_search=SemanticSearchSettings(embedding_base_url=None))
    assert factory.build_embedder(settings) is None


def test_build_embedder_returns_remote_when_url_set():
    from doc3gpp.services import factory
    from doc3gpp.services.embedding.remote_embedder import OpenAICompatibleEmbedder
    from doc3gpp.settings.schema import SemanticSearchSettings, Settings
    settings = Settings(semantic_search=SemanticSearchSettings(
        embedding_base_url="http://localhost:11434/v1",
        embedding_model="nomic-embed-text",
    ))
    emb = factory.build_embedder(settings)
    assert isinstance(emb, OpenAICompatibleEmbedder)
    assert emb.model_name == "nomic-embed-text"
    emb.close()


def test_semantic_service_none_when_url_unset(monkeypatch, sqlite_env=None):
    from doc3gpp.services import factory
    from doc3gpp.settings.schema import SemanticSearchSettings, Settings
    settings = Settings(semantic_search=SemanticSearchSettings(embedding_base_url=None))
    assert factory.build_semantic_search_service(settings) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_factory_semantic.py -v`
Expected: FAIL (build_embedder still returns SentenceTransformerEmbedder unconditionally).

- [ ] **Step 3: Write minimal implementation**

In `src/doc3gpp/services/factory.py`:

```python
from doc3gpp.services.embedding.remote_embedder import OpenAICompatibleEmbedder


def build_embedder(settings: Settings | None = None) -> OpenAICompatibleEmbedder | None:
    """Construct the shared remote embedder, or ``None`` when unconfigured.

    ``None`` (``embedding_base_url`` unset/empty) disables the semantic
    stack via the existing ``None``-service paths. The web app builds ONE
    instance and injects it into every service that embeds so a single
    server process shares one httpx client.
    """
    if settings is None:
        settings = get_settings()
    sem = settings.semantic_search
    base_url = (sem.embedding_base_url or "").strip()
    if not base_url:
        return None
    return OpenAICompatibleEmbedder(
        base_url=base_url,
        model=sem.embedding_model,
        api_key=sem.embedding_api_key,
        timeout_s=sem.embedding_timeout_s,
        batch_size=sem.embedding_batch_size,
    )
```

In `build_semantic_search_service`, replace the embedder construction with:

```python
        if embedder is None:
            embedder = build_embedder(settings)
            if embedder is None:
                return None
```

In `build_search_service`, replace the reranker-branch embedder construction with:

```python
                try:
                    if embedder is None:
                        embedder = build_embedder(settings)
                    if embedder is None:
                        raise _NoEmbedder  # falls through to PassthroughReranker below
                    vector_repo = SQLAlchemyVectorIndexRepository()
                    reranker = SemanticReranker(...)
                except (VectorIndexUnavailableError, EmbedderUnavailableError):
                    reranker = PassthroughReranker()
```

Concretely: guard `if embedder is None: reranker = PassthroughReranker()` without raising — write it as:

```python
                try:
                    if embedder is None:
                        embedder = build_embedder(settings)
                    if embedder is None:
                        reranker = PassthroughReranker()
                    else:
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
```

Also handle explicitly-passed `embedder=None` in `build_tdoc_cr_service` (already `None`-tolerant downstream — no change needed beyond `build_*` returning `None` correctly).

Delete `src/doc3gpp/services/embedding/embedder.py` and `tests/unit/test_embedder.py`. Fix the module-level import in `factory.py:18`. Update CLI unavailable hints in `cli.py` (`sem_command` + `index_command` vec branch): replace ``run `pip install doc3gpp[semantic]` `` with ``set [semantic_search].embedding_base_url (e.g. http://localhost:11434/v1); run `pip install doc3gpp[semantic]` for sqlite-vec``.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_factory_semantic.py tests/unit/test_remote_embedder.py tests/unit/test_semantic_settings.py -v`
Expected: PASS. Then: `ruff check .` clean; `rg -n "SentenceTransformerEmbedder|sentence-transformers" src tests --glob '!docs/**'` returns only intended leftovers (pyproject change lands in Task 5; update callers `tests/unit/test_web_app.py`, `tests/integration/test_mcp_end_to_end.py`, `tests/integration/test_search_query_sem_rerank.py` to use the remote embedder or a mock — mechanical, same commit).

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/factory.py src/doc3gpp/services/embedding/ src/doc3gpp/cli.py tests/
git commit -m "feat(embed): remote-only factory wiring, None when URL unset"
```

---

### Task 4: Vector store — dim + model tracking, drop/recreate rebuild

**Files:**
- Modify: `src/doc3gpp/storage/db/migrate.py:290-339` (`_create_vector_schema`)
- Modify: `src/doc3gpp/storage/repositories/vector_sql.py:43,69-106` (dim+model read/init/check)
- Modify: `src/doc3gpp/services/semantic_search_service.py:244-298` (`rebuild_embeddings` drop/recreate + stamp)
- Test: `tests/integration/test_vector_index_lifecycle.py` (extend)

**Interfaces:**
- Consumes: `OpenAICompatibleEmbedder.dim` / `.model_name` from Task 2; `vec_meta` keys `embedding_dim` (existing) + `embedding_model` (new).
- Produces: `SQLAlchemyVectorIndexRepository(expected_model: str | None = None, expected_dim: int | None = None)` mismatch rule consumed by Task 5 status/CLI text. `rebuild_embeddings` stamps both keys.

- [ ] **Step 1: Write the failing model-tracking tests**

```python
def test_model_mismatch_raises_even_when_dim_matches(sqlite_env):
    import numpy as np
    from doc3gpp.models.semantic_search import VectorIndexUnavailableError
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.vector_sql import SQLAlchemyVectorIndexRepository
    from sqlalchemy import text
    from doc3gpp.storage.db.session import get_engine

    create_schema()
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text("UPDATE vec_meta SET value='old-model' WHERE key='embedding_model'"))
    repo = SQLAlchemyVectorIndexRepository(expected_model="new-model")
    with pytest.raises(VectorIndexUnavailableError, match="rebuild-embeddings"):
        repo.upsert_chunks("R5-1", [np.zeros(384, dtype=np.float32)])


def test_legacy_db_missing_model_treated_as_mismatch(sqlite_env):
    import numpy as np
    from doc3gpp.models.semantic_search import VectorIndexUnavailableError
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.vector_sql import SQLAlchemyVectorIndexRepository
    from sqlalchemy import text
    from doc3gpp.storage.db.session import get_engine

    create_schema()
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM vec_meta WHERE key='embedding_model'"))
    repo = SQLAlchemyVectorIndexRepository(expected_model="nomic-embed-text")
    with pytest.raises(VectorIndexUnavailableError, match="rebuild-embeddings"):
        repo.upsert_chunks("R5-1", [np.zeros(384, dtype=np.float32)])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_vector_index_lifecycle.py -v -m semantic`
Expected: FAIL (no `expected_model` kwarg, no `embedding_model` key).

- [ ] **Step 3: Write minimal implementation**

`vector_sql.py`:

```python
class SQLAlchemyVectorIndexRepository(VectorIndexRepository):
    def __init__(self, expected_model: str | None = None, expected_dim: int | None = None) -> None:
        self._engine = get_engine()
        _check_sqlite_vec(self._engine)
        self._dim, self._stored_model = self._read_or_init_dim_and_model()
        self._expected_model = expected_model
        self._expected_dim = expected_dim

    def _read_or_init_dim_and_model(self) -> tuple[int, str | None]:
        with self._engine.begin() as conn:
            try:
                dim_row = conn.execute(
                    text("SELECT value FROM vec_meta WHERE key = 'embedding_dim'")
                ).scalar()
                model_row = conn.execute(
                    text("SELECT value FROM vec_meta WHERE key = 'embedding_model'")
                ).scalar()
                if dim_row is None:
                    conn.execute(
                        text("INSERT INTO vec_meta (key, value) VALUES ('embedding_dim', :d)"),
                        {"d": str(DEFAULT_DIM)},
                    )
                    return DEFAULT_DIM, model_row
                return int(dim_row), model_row
            except OperationalError as exc:
                raise VectorIndexUnavailableError(
                    "vector schema is not initialized; run `doc3gpp db init` "
                    f"or `doc3gpp db reset` first: {exc}"
                ) from exc

    def _check_compatible(self, dim: int, *, what: str) -> None:
        if dim != self._dim:
            raise VectorIndexUnavailableError(
                f"vector dim mismatch: stored={self._dim} requested={dim}; run "
                f"`doc3gpp search index --rebuild-embeddings`"
            )
        if self._expected_model is not None and self._stored_model != self._expected_model:
            raise VectorIndexUnavailableError(
                f"vector model mismatch: stored={self._stored_model!r} "
                f"expected={self._expected_model!r}; run "
                f"`doc3gpp search index --rebuild-embeddings`"
            )
```

`_check_dim` keeps its name but delegates to `_check_compatible(v.shape[-1], what="upsert")`; `knn` and `get_min_distance_for_tdocs` call `self._check_compatible(q.shape[-1], what="query")` / same for the Sequence path. Factories pass `expected_model=settings.semantic_search.embedding_model, expected_dim=embedder.dim` — note `embedder.dim` probes the API once; wrap in try/except `EmbedderUnavailableError` → return `None` (already the factory pattern).

`migrate._create_vector_schema(dim: int | None = None)`: parameterize the `FLOAT[...]` width (`dim or 384`), keep `IF NOT EXISTS` idempotency, and after creating `vec_meta` also `INSERT OR IGNORE` the `embedding_dim` row when an explicit dim is passed.

`SemanticSearchService.rebuild_embeddings`: at start (non-resume path), drop + recreate `vec0` at `self._embedder.dim` via the same DDL shape, stamp `embedding_dim` + `embedding_model` (+ existing `last_rebuild_at` handling where it lives), then proceed with the existing batch loop unchanged. On the resume path, verify stored model/dim match first and raise the mismatch error otherwise (do not silently mix).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_vector_index_lifecycle.py -v -m semantic`
Expected: PASS. Then full offline suite: `./scripts/test_sqlite.sh`.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/storage/db/migrate.py src/doc3gpp/storage/repositories/vector_sql.py src/doc3gpp/services/semantic_search_service.py tests/integration/test_vector_index_lifecycle.py
git commit -m "feat(embed): track embedding model+dim, drop-recreate rebuild"
```

---

### Task 5: Packaging, CLI text, status diagnostics, docs

**Files:**
- Modify: `pyproject.toml:64-96` (extras + markers text)
- Modify: `src/doc3gpp/cli.py` (sem explain + index status vector block: configured model/base_url host + stored model/dim)
- Modify: `src/doc3gpp/data/doc3gpp.toml.example` + `doc3gpp.toml.example` (new knobs + Ollama/OpenAI examples)
- Modify: `docs/cli.md`, `docs/architecture.md`, `AGENTS.md`, `README.md`, `docs/code-map.md`
- Test: existing CLI/status tests updated

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces: user-visible surface; no new interfaces.

- [ ] **Step 1: Write the failing packaging/docs test**

```python
def test_semantic_extra_drops_sentence_transformers():
    import tomllib
    with open("pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)
    semantic = data["project"]["optional-dependencies"]["semantic"]
    assert not any("sentence-transformers" in dep for dep in semantic)
    assert any("sqlite-vec" in dep for dep in semantic)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_packaging_semantic.py -v`
Expected: FAIL (sentence-transformers still listed).

- [ ] **Step 3: Implement**

  - `pyproject.toml`: remove `"sentence-transformers>=2.7.0"` from `[semantic]`; update the `semantic:` marker comment and `dev`-adjacent docs that name sentence-transformers.
  - `cli.py` `sem_command --explain`: add `embedding_model`, `embedding_base_url` (host only — strip any embedded credentials; key never printed), stored `embedding_dim`/`embedding_model` from `vec_meta`.
  - `cli.py` `index_command` status: extend the vector block with `Vector model: <stored> (configured: <configured>)` and `Vector dim: <stored> (configured: <live or unknown>)`.
  - `doc3gpp.toml.example` (+ packaged copy under `src/doc3gpp/data/` — verify which is canonical with `diff`): uncomment/refresh the `[semantic_search]` block with `embedding_base_url`, `embedding_api_key`, `embedding_model = "nomic-embed-text"`, timeout/batch knobs, plus commented Ollama (`http://localhost:11434/v1`) and OpenAI (`https://api.openai.com/v1`) examples.
  - `docs/cli.md`: `search sem` prerequisites (URL required), new TOML fields, mismatch → rebuild story.
  - `docs/architecture.md` + `AGENTS.md`: replace `SentenceTransformerEmbedder` workflow lines with the remote embedder + disabled-when-unset rule.
  - `README.md`: semantic-setup section (install `[semantic]` for sqlite-vec, set `embedding_base_url`, rebuild).
  - `docs/code-map.md`: `services/embedding/remote_embedder.py` row.

- [ ] **Step 4: Run verification**

Run: `ruff check .` then `./scripts/test_sqlite.sh`
Expected: clean lint; offline suite green.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/doc3gpp/cli.py doc3gpp.toml.example src/doc3gpp/data/doc3gpp.toml.example docs/ README.md AGENTS.md tests/
git commit -m "feat(embed): packaging, diagnostics, and docs for remote backend"
```

---

## Self-Review

**Spec coverage:** §1 settings/env/redaction → Task 1. §2 enablement (None when unset, reuse disabled paths) → Task 3. §3 client shape/batching/auth/dim-cache/error mapping → Task 2. §4 dim+model tracking, legacy-DB mismatch, base_url untracked, status visibility → Task 4 (+ status text in Task 5). §5 errors/diagnostics redaction → Tasks 3+5. §6 tests/docs → spread across tasks + Task 5. No gaps.

**Placeholder scan:** every step has concrete code, exact file:line anchors, and runnable commands. No TBD/TODO/"appropriate handling" language; error paths assert exact messages.

**Type consistency:** `embedding_base_url/api_key/timeout_s/batch_size/model` names identical across Tasks 1-3; `OpenAICompatibleEmbedder(base_url, model, api_key, timeout_s, batch_size)` + `.dim/.model_name/.close()` identical in Tasks 2-4; `expected_model/expected_dim` kwargs defined once in Task 4; `build_embedder -> OpenAICompatibleEmbedder | None` used consistently in Task 3.
