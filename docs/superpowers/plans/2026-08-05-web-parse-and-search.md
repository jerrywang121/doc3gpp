# Web Parse Trigger + Search Page Updates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a parse-trigger button to the web TDoc detail page (enqueues the existing PARSE_TDOCS job, auto-indexing FTS5 + embeddings), share one embedding model across the web app's services, and upgrade the `/search` + `/search/sem` pages with a 5-column grid, a semantic-rerank input on `/search`, full filter parity on `/search/sem`, and cross links.

**Architecture:** The parse button reuses the existing `POST /jobs/parse/tdocs` endpoint and `partials/job_status.html` polling — no new job machinery. The shared embedder is a single `SentenceTransformerEmbedder` built once in `build_state` and injected into `build_tdoc_cr_service` / `build_search_service` / `build_semantic_search_service` (all three gain an optional `embedder` kwarg). `SearchService.search` gains an optional `sem_query` kwarg implementing the CLI's `--sem-query` path (fanout + `SemanticReranker.rerank`); `None` bypasses the reranker entirely (CLI parity). The search routes/templates/MCP tool pass the new params through.

**Tech Stack:** Python 3.10+, FastAPI, Jinja2, HTMX, SQLAlchemy 2.0, sentence-transformers (optional extra), pytest.

## Global Constraints

- Follow the layered architecture: `models/` never leak ORM attrs; `scraping/` has no parsing; `parsers/` has no network; services reach storage only via `repository/` Protocols.
- `SearchService.search(query, filters, sem_query=None)` — `None` means pure FTS5 with NO reranker invocation (CLI parity with `search query` without `--sem-query`).
- The hybrid path in `SemanticSearchService.search` calls `self._fts5.search(fts5_expr, fts5_filters)` — with the new default `sem_query=None` this stays pure FTS5; hybrid behavior must NOT change.
- The web parse button must NOT create new job kinds or endpoints — reuse `POST /jobs/parse/tdocs` + `partials/job_status.html`.
- The MCP `parse_tdocs` tool is unchanged; `search_tdocs` gains an optional `sem_query` param.
- JSON output parity: `?format=json` payloads must stay byte-identical to the CLI's `--format json` (no field changes).
- Lint: `ruff check .` must pass. Tests: `./scripts/test_sqlite.sh` (unit + integration, sqlite-only).
- Docs must be updated in the same change set: `docs/web-server.md`, `AGENTS.md` (web row + workflows), `docs/cli.md` only if CLI surface changes (it does not).
- Commit per task with a message matching repo style (`feat:`, `test:`, `docs:` prefixes).

---

### Task 1: `SearchService.search` gains `sem_query` (CLI `--sem-query` parity)

**Files:**
- Modify: `src/doc3gpp/services/search_service.py:105-127` (`search` method)
- Test: `tests/unit/test_search_service.py:114-121` (update `test_search_runs_reranker`), add new tests after line 121

**Interfaces:**
- Consumes: `SearchFilters` (frozen dataclass, `limit: int = 20`), `SearchIndexRepository.search(query, filters)`, `EmbeddingReranker.rerank(semantic_query, hits, final_limit=None, quiet=False)`, `Settings.search.search_fanout_factor` (default 4).
- Produces: `SearchService.search(self, query: str, filters: SearchFilters, sem_query: str | None = None) -> list[SearchHit]`. When `sem_query is None` → return `repo.search(...)` hits directly (no rerank). When provided → fanout `filters.limit * settings.search.search_fanout_factor`, re-run `repo.search` with a `SearchFilters` copy whose `limit` is the fanout, then `self._reranker.rerank(semantic_query=sem_query, hits=raw_hits, final_limit=filters.limit, quiet=self._quiet)`.

- [ ] **Step 1: Update the existing reranker test to pin the new default**

Replace `test_search_runs_reranker` (lines 114-121) in `tests/unit/test_search_service.py`:

```python
def test_search_without_sem_query_skips_reranker() -> None:
    repo = MockRepo()
    reranker = StubReranker()
    svc = SearchService(repo=repo, reranker=reranker)
    hits = svc.search("anything", SearchFilters(limit=5))
    assert len(hits) == 1
    assert reranker.invocations == 0
    assert reranker.queries == []
    assert repo.search_query is not None
    assert repo.search_query[1].limit == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_search_service.py::test_search_without_sem_query_skips_reranker -v`
Expected: FAIL — `reranker.invocations == 1` (current behavior reranks unconditionally).

- [ ] **Step 3: Add the sem_query test**

Append after the test from Step 1:

```python
def test_search_with_sem_query_reranks_with_fanout() -> None:
    repo = MockRepo()
    reranker = StubReranker()
    svc = SearchService(repo=repo, reranker=reranker)
    hits = svc.search("anything", SearchFilters(limit=5), sem_query="semantic text")
    assert len(hits) == 1
    assert reranker.invocations == 1
    assert reranker.queries == ["semantic text"]
    assert repo.search_query is not None
    assert repo.search_query[1].limit == 5 * 4  # search_fanout_factor default 4
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest tests/unit/test_search_service.py::test_search_with_sem_query_reranks_with_fanout -v`
Expected: FAIL — `TypeError: search() got an unexpected keyword argument 'sem_query'`.

- [ ] **Step 5: Implement `sem_query` in `SearchService.search`**

In `src/doc3gpp/services/search_service.py`, replace the `search` method body (lines 105-127) with:

```python
    def search(
        self,
        query: str,
        filters: SearchFilters,
        sem_query: str | None = None,
    ) -> list[SearchHit]:
        """Run FTS5 ``MATCH`` + optional semantic rerank + return hits.

        The raw query is normalised into a valid FTS5 ``MATCH``
        expression via :class:`SearchQueryBuilder` (the same path the
        CLI uses) so jargon like ``nb-iot`` is quoted rather than
        parsed as a column-minus-token; a stopwords-only or empty
        query raises :class:`SearchQueryError`.

        ``sem_query`` mirrors the CLI's ``search query --sem-query``:
        when ``None`` (default) the hits are returned verbatim and the
        reranker is NOT invoked — pure FTS5, matching the CLI without
        ``--sem-query``. When provided, the FTS5 query is re-run with
        a fanout limit (``filters.limit * search_fanout_factor``) and
        the raw hits are reordered by cosine similarity to
        ``sem_query`` via :meth:`EmbeddingReranker.rerank`, truncated
        back to ``filters.limit``. The *raw* query is forwarded to the
        reranker only when ``sem_query`` is provided; the reranker
        embeds that text verbatim.
        """
        from doc3gpp.cli_filters import SearchQueryBuilder
        from doc3gpp.settings.loader import get_settings

        match_expr = SearchQueryBuilder(query).build()
        if sem_query is None:
            return self._repo.search(match_expr, filters)
        settings = get_settings()
        fanout = filters.limit * settings.search.search_fanout_factor
        fanout_filters = SearchFilters(
            tsg=filters.tsg,
            meeting=filters.meeting,
            meeting_id=filters.meeting_id,
            tdoc_id=filters.tdoc_id,
            release=filters.release,
            spec=filters.spec,
            since=filters.since,
            until=filters.until,
            limit=fanout,
        )
        raw_hits = self._repo.search(match_expr, fanout_filters)
        return self._reranker.rerank(
            semantic_query=sem_query,
            hits=raw_hits,
            final_limit=filters.limit,
            quiet=self._quiet,
        )
```

- [ ] **Step 6: Run both tests to verify they pass**

Run: `pytest tests/unit/test_search_service.py -v`
Expected: PASS — both new tests green; all other tests in the file still pass (the `test_search_runs_reranker` name is gone, replaced by the two new tests).

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/services/search_service.py tests/unit/test_search_service.py
git commit -m "feat: add sem_query rerank path to SearchService.search"
```

---

### Task 2: Thread-safe lazy model load in `SentenceTransformerEmbedder`

**Files:**
- Modify: `src/doc3gpp/services/embedding/embedder.py:39-75`
- Test: `tests/unit/test_embedder.py` (append)

**Interfaces:**
- Consumes: `SentenceTransformerEmbedder(model_name)` with `_model: None | object`, `_load_model()`, `encode(texts)`, `dim`.
- Produces: `SentenceTransformerEmbedder.__init__` additionally sets `self._lock = threading.Lock()`; `encode` and `dim` load the model under the lock with a double-checked pattern so concurrent first-encodes load exactly once.

- [ ] **Step 1: Write the failing concurrent-load test**

Append to `tests/unit/test_embedder.py`:

```python
def test_concurrent_first_encode_loads_model_once() -> None:
    import threading

    emb = SentenceTransformerEmbedder("fake-model")
    fake_model = MagicMock()
    fake_model.get_sentence_embedding_dimension.return_value = 384
    fake_model.encode.return_value = np.zeros((1, 384), dtype=np.float32)

    barrier = threading.Barrier(4)

    def _slow_load():
        barrier.wait()
        return fake_model

    with patch.object(
        SentenceTransformerEmbedder, "_load_model", side_effect=_slow_load
    ) as loader:
        results: list[np.ndarray] = []
        errors: list[Exception] = []

        def _worker():
            try:
                results.append(emb.encode(["x"]))
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert not errors
    assert len(results) == 4
    assert loader.call_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_embedder.py::test_concurrent_first_encode_loads_model_once -v`
Expected: FAIL — `loader.call_count == 4` (no lock today; each thread loads).

- [ ] **Step 3: Implement the lock**

In `src/doc3gpp/services/embedding/embedder.py`:

Add `import threading` to the imports (after `import logging`).

In `__init__` (line 40-42), add the lock:

```python
    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model = None
        self._lock = threading.Lock()
```

Add a private helper and use it in both `encode` and `dim`:

```python
    def _ensure_model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    try:
                        self._model = self._load_model()
                    except (OSError, Exception) as exc:
                        raise EmbedderUnavailableError(
                            f"failed to load embedding model {self._model_name!r}: {exc}"
                        ) from exc
        return self._model
```

Replace the load block in `encode` (lines 52-58) with:

```python
        model = self._ensure_model()
        vec = model.encode(
            texts,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return vec.astype(np.float32, copy=False)
```

Replace the load block in `dim` (lines 68-74) with:

```python
        return int(self._ensure_model().get_sentence_embedding_dimension())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_embedder.py -v`
Expected: PASS — all 6 tests green (existing 5 + new concurrent test).

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/embedding/embedder.py tests/unit/test_embedder.py
git commit -m "feat: make embedding model load thread-safe"
```

---

### Task 3: Factory — `build_embedder` + `embedder` kwarg on the three builders

**Files:**
- Modify: `src/doc3gpp/services/factory.py` (`build_tdoc_cr_service` line 143, `build_semantic_search_service` line 220, `build_search_service` line 270)
- Test: `tests/unit/test_search_service.py` (append factory tests after line 372)

**Interfaces:**
- Consumes: `SentenceTransformerEmbedder(model_name)`, `SemanticReranker(embedder, vector_repo, settings)`, `SemanticSearchService(fts5_service, embedder, vector_repo, settings)`, `TDocCrService(..., search_service=None, semantic_service=None)`.
- Produces:
  - `factory.build_embedder(settings: Settings | None = None) -> SentenceTransformerEmbedder` — returns `SentenceTransformerEmbedder(settings.semantic_search.embedding_model)`; does NOT load the model.
  - `build_tdoc_cr_service(..., *, max_tdoc_size_bytes=None, embedder: Embedder | None = None)` — when `embedder` is provided, pass it through to `build_search_service(embedder=embedder)` and `build_semantic_search_service(embedder=embedder)`; otherwise current behavior.
  - `build_search_service(settings=None, repo=None, reranker=None, *, quiet=False, embedder: Embedder | None = None)` — when `embedder` is provided, use it directly in `SemanticReranker` instead of constructing a new `SentenceTransformerEmbedder`.
  - `build_semantic_search_service(settings=None, fts5_service=None, embedder=None, vector_repo=None)` — already accepts `embedder`; no signature change.

- [ ] **Step 1: Write the failing factory tests**

Append to `tests/unit/test_search_service.py`:

```python
def test_factory_build_embedder_returns_lazy_embedder(monkeypatch) -> None:
    from doc3gpp.services import factory as f

    class FakeSettings:
        class semantic_search:
            embedding_model = "fake-model"

    fake_embedder = MagicMock()
    monkeypatch.setattr(
        f, "SentenceTransformerEmbedder", lambda model: fake_embedder,
    )
    assert f.build_embedder(FakeSettings()) is fake_embedder


def test_factory_search_service_uses_injected_embedder(monkeypatch) -> None:
    from unittest.mock import MagicMock

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
        f, "SQLAlchemyVectorIndexRepository", lambda: fake_vector_repo,
    )
    monkeypatch.setattr(
        f, "SQLAlchemySearchIndexRepository", lambda: MagicMock(),
    )

    svc = f.build_search_service(FakeSettings(), embedder=fake_embedder)
    assert isinstance(svc, SearchService)
    assert isinstance(svc._reranker, SemanticReranker)
    assert svc._reranker._embedder is fake_embedder


def test_factory_tdoc_cr_service_forwards_embedder(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from doc3gpp.services import factory as f

    class FakeSettings:
        class tdoc_parse:
            max_tdoc_size_kb = 1000

        class cache:
            dir = "/tmp/cache"
            size_limit_mb = 100

        class search:
            enabled = True

        class semantic_search:
            enabled = True
            embedding_model = "fake-model"

    monkeypatch.setattr(f, "get_settings", lambda: FakeSettings())
    fake_embedder = MagicMock()
    fake_cr_service = MagicMock()
    monkeypatch.setattr(f, "TDocCrService", lambda **kw: fake_cr_service)
    monkeypatch.setattr(f, "TDocCache", lambda **kw: MagicMock())
    monkeypatch.setattr(f, "ScraperClient", lambda: MagicMock())
    monkeypatch.setattr(f, "SQLAlchemyTDocCrRepository", lambda: MagicMock())
    monkeypatch.setattr(f, "SQLAlchemyTDocRepository", lambda: MagicMock())
    monkeypatch.setattr(f, "build_tdoc_cr_ttcn_repository", lambda: MagicMock())
    monkeypatch.setattr(
        f, "build_tdoc_cr_change_details_repository", lambda: MagicMock(),
    )

    captured: dict = {}

    def _fake_search_service(**kw):
        captured["search"] = kw
        return MagicMock()

    def _fake_semantic_service(**kw):
        captured["semantic"] = kw
        return MagicMock()

    monkeypatch.setattr(f, "build_search_service", _fake_search_service)
    monkeypatch.setattr(f, "build_semantic_search_service", _fake_semantic_service)

    f.build_tdoc_cr_service(embedder=fake_embedder)
    assert captured["search"]["embedder"] is fake_embedder
    assert captured["semantic"]["embedder"] is fake_embedder
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_search_service.py -k "factory" -v`
Expected: FAIL — `AttributeError: module 'doc3gpp.services.factory' has no attribute 'build_embedder'` and `TypeError: build_search_service() got an unexpected keyword argument 'embedder'`.

- [ ] **Step 3: Implement `build_embedder` + kwarg plumbing**

In `src/doc3gpp/services/factory.py`:

Add a new function before `build_tdoc_cr_service` (after line 141):

```python
def build_embedder(settings: Settings | None = None) -> SentenceTransformerEmbedder:
    """Construct the shared :class:`SentenceTransformerEmbedder`.

    Lazy: the model is only loaded on the first ``encode()`` call.
    The web app builds ONE instance and injects it into every
    service that embeds (search reranker, semantic search, parse
    auto-embed) so a single server process loads the model at most
    once.
    """
    if settings is None:
        settings = get_settings()
    return SentenceTransformerEmbedder(settings.semantic_search.embedding_model)
```

Change `build_tdoc_cr_service` signature (line 143-148) to:

```python
def build_tdoc_cr_service(
    cr_ttcn_repository: TDocCrTTCNDetailRepository | None = None,
    cr_change_details_repository: TDocCrChangeDetailsRepository | None = None,
    *,
    max_tdoc_size_bytes: int | None = None,
    embedder: Embedder | None = None,  # noqa: F821
) -> TDocCrService:
```

and update the two wiring lines (215-216) to:

```python
        search_service=build_search_service(embedder=embedder),
        semantic_service=build_semantic_search_service(embedder=embedder),
```

Change `build_search_service` signature (line 270-276) to:

```python
def build_search_service(
    settings: Settings | None = None,
    repo: SearchIndexRepository | None = None,
    reranker: EmbeddingReranker | None = None,
    *,
    quiet: bool = False,
    embedder: Embedder | None = None,  # noqa: F821
) -> SearchService | None:
```

and update the reranker construction block (lines 318-326) to:

```python
                try:
                    if embedder is None:
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
```

Update the `build_search_service` docstring Args section to add:

```
        embedder: Optional shared embedder. When ``None`` (default),
            the factory constructs a fresh
            :class:`SentenceTransformerEmbedder`. The web app passes
            the single shared instance so the model loads once per
            process.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_search_service.py -v`
Expected: PASS — all tests green including the three new factory tests and the existing factory tests (lines 289-372).

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/factory.py tests/unit/test_search_service.py
git commit -m "feat: share one embedder across web services via factory"
```

---

### Task 4: `build_state` wires the shared embedder

**Files:**
- Modify: `src/doc3gpp/web/app.py:46-71` (`build_state`)
- Test: `tests/unit/test_web_app.py` (append)

**Interfaces:**
- Consumes: `factory.build_embedder(settings)`, `factory.build_tdoc_cr_service(embedder=...)`, `factory.build_search_service(embedder=...)`, `factory.build_semantic_search_service(embedder=...)`, `ServiceContainer(...)`.
- Produces: `build_state(settings)` builds ONE embedder and passes it to the three builders; `ServiceContainer` shape unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_web_app.py`:

```python
def test_build_state_shares_one_embedder(sqlite_env) -> None:
    from unittest.mock import MagicMock

    from doc3gpp.services import factory
    from doc3gpp.web.app import build_state
    from doc3gpp.settings.schema import Settings

    settings = Settings()
    fake_embedder = MagicMock()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(factory, "build_embedder", lambda s: fake_embedder)
    try:
        state = build_state(settings)
    finally:
        monkeypatch.undo()
    assert state.services.search._reranker._embedder is fake_embedder
    assert state.services.semantic_search._embedder is fake_embedder
    assert state.services.tdoc_cr._semantic_service._embedder is fake_embedder
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_web_app.py::test_build_state_shares_one_embedder -v`
Expected: FAIL — `AttributeError: module 'doc3gpp.services.factory' has no attribute 'build_embedder'` (or the identity asserts fail because each service built its own embedder).

- [ ] **Step 3: Implement the shared embedder in `build_state`**

In `src/doc3gpp/web/app.py`, replace the `build_state` body (lines 52-65) with:

```python
    engine = get_engine()
    embedder = factory.build_embedder(settings)
    services = ServiceContainer(
        meeting=factory.build_meeting_service(),
        tdoc=factory.build_tdoc_service(),
        tdoc_cr=factory.build_tdoc_cr_service(embedder=embedder),
        tdoc_sync=factory.build_tdoc_sync_coordinator(),
        tdoc_repo=factory.build_tdoc_repository(),
        tsg=factory.build_tsg_service(),
        wi=factory.build_wi_service(),
        search=factory.build_search_service(embedder=embedder),
        semantic_search=factory.build_semantic_search_service(embedder=embedder),
        tdoc_file_repo=factory.build_tdoc_file_repository(),
        job_repo=SQLAlchemyJobRepository(),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_web_app.py -v`
Expected: PASS — new test green; existing `test_build_state_wires_service_container` still passes.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/web/app.py tests/unit/test_web_app.py
git commit -m "feat: wire one shared embedder through web build_state"
```

---

### Task 5: `/search` route — `sem` param + `sem_query` forwarding

**Files:**
- Modify: `src/doc3gpp/web/routes/search.py:115-180` (`search_query`)
- Test: `tests/unit/test_web_routes.py` (append near line 1560)

**Interfaces:**
- Consumes: `SearchService.search(query, filters, sem_query=None)` (Task 1), `FakeSearchService` in tests.
- Produces: `GET /search` accepts `sem: str | None = Query(default=None)`; passes `sem_query=sem` to `service.search`; adds `"sem": sem or ""` to the template `filters` context.

- [ ] **Step 1: Update `FakeSearchService` to accept `sem_query`**

In `tests/unit/test_web_routes.py`, replace the `FakeSearchService.search` method (lines 209-211) with:

```python
    def search(
        self, _query: str, _filters: Any, sem_query: str | None = None,
    ) -> list[SearchHit]:
        self.last_filters = _filters
        self.last_sem_query = sem_query
        return list(self._hits)
```

- [ ] **Step 2: Write the failing route test**

Append after `test_search_query_empty_tdoc_id_is_no_filter` (line 1574) in `tests/unit/test_web_routes.py`:

```python
def test_search_query_sem_param_forwarded(client: TestClient) -> None:
    """``GET /search?sem=<text>`` forwards sem_query into the service."""
    from doc3gpp.web.deps import get_search_service

    service = FakeSearchService()
    client.app.dependency_overrides[get_search_service] = lambda: service
    try:
        response = client.get("/search?q=foo&sem=hybrid+rerank")
    finally:
        client.app.dependency_overrides.pop(get_search_service, None)
    assert response.status_code == 200
    assert service.last_sem_query == "hybrid rerank"


def test_search_query_sem_empty_is_none(client: TestClient) -> None:
    """``GET /search?sem=`` leaves sem_query None (no rerank)."""
    from doc3gpp.web.deps import get_search_service

    service = FakeSearchService()
    client.app.dependency_overrides[get_search_service] = lambda: service
    try:
        response = client.get("/search?q=foo&sem=")
    finally:
        client.app.dependency_overrides.pop(get_search_service, None)
    assert response.status_code == 200
    assert service.last_sem_query is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/test_web_routes.py -k "sem_param or sem_empty" -v`
Expected: FAIL — `AttributeError: 'FakeSearchService' object has no attribute 'last_sem_query'`.

- [ ] **Step 4: Implement the route change**

In `src/doc3gpp/web/routes/search.py`, in `search_query`:

Add the param after `until` (line 125):

```python
    sem: str | None = Query(default=None),
```

Change the service call (line 148) to:

```python
        hits = service.search(q, filters, sem_query=sem)
```

Add `"sem": sem or ""` to the `filters` context dict (after line 177):

```python
                "sem": sem or "",
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_web_routes.py -v`
Expected: PASS — new tests green; all existing search tests still pass.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/routes/search.py tests/unit/test_web_routes.py
git commit -m "feat: forward sem rerank query through /search route"
```

---

### Task 6: `/search/sem` route — full filter parity

**Files:**
- Modify: `src/doc3gpp/web/routes/search.py:183-247` (`search_semantic`)
- Test: `tests/unit/test_web_routes.py` (append near line 1590)

**Interfaces:**
- Consumes: `_build_filters(tsg, meeting, release, spec, since, until, tdoc_id, limit)` (already defined at line 43), `SemanticSearchService.search(query, fts5_query, filters, limit, fts5_weight)`.
- Produces: `GET /search/sem` accepts `tsg, meeting, release, spec, since, until` params; builds filters via `_build_filters`; adds all filter values to the template `filters` context.

- [ ] **Step 1: Write the failing route test**

Append after `test_search_sem_tdoc_id_filter_forwarded` (line 1590) in `tests/unit/test_web_routes.py`:

```python
def test_search_sem_full_filters_forwarded(client: TestClient) -> None:
    """``GET /search/sem`` forwards tsg/meeting/release/spec/since/until."""
    from doc3gpp.web.deps import get_semantic_search_service

    service = FakeSemanticSearchService()
    client.app.dependency_overrides[get_semantic_search_service] = lambda: service
    try:
        response = client.get(
            "/search/sem?q=foo&tsg=R5&meeting=RAN5%2399-e"
            "&release=18&spec=38.300&since=2026-01-01&until=2026-06-01"
        )
    finally:
        client.app.dependency_overrides.pop(get_semantic_search_service, None)
    assert response.status_code == 200
    filters = service.last_kwargs.get("filters")
    assert filters is not None
    assert filters.tsg == "R5"
    assert filters.meeting == "RAN5#99-e"
    assert filters.release == "18"
    assert filters.spec == "38.300"
    assert filters.since == "2026-01-01"
    assert filters.until == "2026-06-01"


def test_search_sem_bad_date_filter_400(client: TestClient) -> None:
    """``GET /search/sem?since=<bad>`` returns 400 invalid_filter."""
    response = client.get("/search/sem?q=foo&since=not-a-date")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_filter"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_web_routes.py -k "full_filters or bad_date_filter_400" -v`
Expected: FAIL — `filters.tsg is None` (route ignores the new params today).

- [ ] **Step 3: Implement the route change**

In `src/doc3gpp/web/routes/search.py`, in `search_semantic`:

Add params after `q` (line 186):

```python
    tsg: str | None = Query(default=None),
    meeting: str | None = Query(default=None),
    release: str | None = Query(default=None),
    spec: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
```

Replace the inline `SearchFilters(...)` construction (lines 214-217) with:

```python
            filters=_build_filters(
                tsg=tsg, meeting=meeting, release=release,
                spec=spec, since=since, until=until, tdoc_id=tdoc_id,
                limit=parsed_limit,
            ),
```

Replace the `filters` context dict (lines 243-245) with:

```python
            "filters": {
                "tsg": tsg or "",
                "meeting": meeting or "",
                "release": release or "",
                "spec": spec or "",
                "since": since or "",
                "until": until or "",
                "tdoc_id": tdoc_id or "",
            },
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_web_routes.py -v`
Expected: PASS — new tests green; existing `test_search_sem_tdoc_id_filter_forwarded` still passes.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/web/routes/search.py tests/unit/test_web_routes.py
git commit -m "feat: full filter parity on /search/sem route"
```

---

### Task 7: Search form template — 5-column grid + semantic input + full filters

**Files:**
- Modify: `src/doc3gpp/web/templates/partials/search_form.html`
- Modify: `src/doc3gpp/web/static/style.css` (add grid + page-actions classes)
- Test: `tests/unit/test_web_routes.py` (append)

**Interfaces:**
- Consumes: template context `mode` (`'fts5'` | `'sem'`), `query`, `fts5_query`, `fts5_weight`, `limit`, `filters` dict (now with `tsg/meeting/release/spec/since/until/tdoc_id/sem` keys).
- Produces: `/search` form — first row is a 5-column grid: Query (2 cols) + Semantic (3 cols, `name="sem"`); filter row below (TSG, Meeting, TDoc, Release, Spec, Since, Until, Limit). `/search/sem` form — first row: Query (3 cols) + FTS5 query (2 cols); filter row below (TSG, Meeting, TDoc, Release, Spec, Since, Until, FTS5 weight, Limit).

- [ ] **Step 1: Write the failing template tests**

Append to `tests/unit/test_web_routes.py`:

```python
def test_search_form_fts5_has_semantic_input(client: TestClient) -> None:
    """The FTS5 form carries a Semantic input with the round-tripped value."""
    html = client.get("/search?q=foo&sem=rerank+me").text
    assert 'name="sem"' in html
    assert 'value="rerank me"' in html


def test_search_form_sem_has_full_filters(client: TestClient) -> None:
    """The semantic form carries TSG/Meeting/Release/Spec/Since/Until inputs."""
    html = client.get(
        "/search/sem?q=foo&tsg=R5&meeting=RAN5%2399-e&release=18"
        "&spec=38.300&since=2026-01-01&until=2026-06-01"
    ).text
    for name in ("tsg", "meeting", "release", "spec", "since", "until"):
        assert f'name="{name}"' in html
    assert 'value="R5"' in html
    assert 'value="RAN5#99-e"' in html
    assert 'value="2026-01-01"' in html


def test_search_form_sem_keeps_fts5_weight_and_limit(client: TestClient) -> None:
    """The semantic form keeps the FTS5 weight + Limit controls."""
    html = client.get("/search/sem?q=foo").text
    assert 'name="fts5_weight"' in html
    assert 'name="limit"' in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_web_routes.py -k "semantic_input or full_filters or keeps_fts5_weight" -v`
Expected: FAIL — `'name="sem"' not in html` (fts5 branch has no sem input) and `'name="tsg"' not in html` (sem branch lacks the filters).

- [ ] **Step 3: Rewrite the search form partial**

Replace the entire content of `src/doc3gpp/web/templates/partials/search_form.html` with:

```html
<form
  hx-get="{{ '/search/sem' if mode == 'sem' else '/search' }}"
  hx-target="#results"
  hx-trigger="submit"
  hx-swap="outerHTML"
  class="filters"
>
  <div class="search-grid-5">
    {% if mode == 'sem' %}
      <label class="span-3">Query
        <input type="text" name="q" value="{{ query or '' }}" autofocus>
      </label>
      <label class="span-2">FTS5 query
        <input type="text" name="fts5_query" value="{{ fts5_query or '' }}">
      </label>
    {% else %}
      <label class="span-2">Query
        <input type="text" name="q" value="{{ query or '' }}" autofocus>
      </label>
      <label class="span-3">Semantic
        <input type="text" name="sem" value="{{ filters.sem or '' }}" placeholder="optional semantic rerank query">
      </label>
    {% endif %}
  </div>
  <label>TSG
    <input type="text" name="tsg" value="{{ filters.tsg or '' }}">
  </label>
  <label>Meeting
    <input type="text" name="meeting" value="{{ filters.meeting or '' }}">
  </label>
  <label>TDoc
    <input type="text" name="tdoc-id" value="{{ filters.tdoc_id or '' }}">
  </label>
  <label>Release
    <input type="text" name="release" value="{{ filters.release or '' }}">
  </label>
  <label>Spec
    <input type="text" name="spec" value="{{ filters.spec or '' }}">
  </label>
  <label>Since
    <input type="text" name="since" placeholder="YYYY-MM-DD" value="{{ filters.since or '' }}">
  </label>
  <label>Until
    <input type="text" name="until" placeholder="YYYY-MM-DD" value="{{ filters.until or '' }}">
  </label>
  {% if mode == 'sem' %}
    <label>FTS5 weight
      <input type="number" step="0.1" min="0" max="1" name="fts5_weight" value="{{ fts5_weight }}">
    </label>
  {% endif %}
  <label>Limit
    <input type="number" name="limit" value="{{ limit }}" min="1" max="200">
  </label>
  <button type="submit" class="btn">Search</button>
</form>
```

- [ ] **Step 4: Add the CSS classes**

Append to `src/doc3gpp/web/static/style.css`:

```css
/* Search form: 5-column grid for the query row (Query + Semantic/FTS5). */
.search-grid-5 {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: .75rem;
  align-items: end;
  width: 100%;
}

.search-grid-5 .span-2 { grid-column: span 2; }
.search-grid-5 .span-3 { grid-column: span 3; }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_web_routes.py -v`
Expected: PASS — new template tests green; existing form tests (`test_search_form_renders_tdoc_input_fts5`, `test_search_form_renders_tdoc_input_sem`) still pass.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/templates/partials/search_form.html src/doc3gpp/web/static/style.css tests/unit/test_web_routes.py
git commit -m "feat: 5-column search form grid with semantic input and full filters"
```

---

### Task 8: Cross links between `/search` and `/search/sem`

**Files:**
- Modify: `src/doc3gpp/web/templates/search_results.html`
- Modify: `src/doc3gpp/web/static/style.css` (add `.page-actions`)
- Test: `tests/unit/test_web_routes.py` (append)

**Interfaces:**
- Consumes: template context `mode`.
- Produces: a header row above the search form with a top-right link — `mode == 'sem'` → `/search` ("FTS5 search"); else → `/search/sem` ("Hybrid search").

- [ ] **Step 1: Write the failing template tests**

Append to `tests/unit/test_web_routes.py`:

```python
def test_search_page_links_to_hybrid(client: TestClient) -> None:
    """The FTS5 search page links to /search/sem at top right."""
    html = client.get("/search?q=foo").text
    assert 'href="/search/sem"' in html
    assert "Hybrid search" in html


def test_search_sem_page_links_to_fts5(client: TestClient) -> None:
    """The semantic search page links to /search at top right."""
    html = client.get("/search/sem?q=foo").text
    assert 'href="/search"' in html
    assert "FTS5 search" in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_web_routes.py -k "links_to_hybrid or links_to_fts5" -v`
Expected: FAIL — no cross links in the current template.

- [ ] **Step 3: Add the header row to the template**

In `src/doc3gpp/web/templates/search_results.html`, replace lines 4-6:

```html
  <h1>Search</h1>

  {% include "partials/search_form.html" %}
```

with:

```html
  <div class="page-header">
    <h1>Search</h1>
    <div class="page-actions">
      {% if mode == 'sem' %}
        <a class="btn" href="/search">FTS5 search</a>
      {% else %}
        <a class="btn" href="/search/sem">Hybrid search</a>
      {% endif %}
    </div>
  </div>

  {% include "partials/search_form.html" %}
```

- [ ] **Step 4: Add the CSS**

Append to `src/doc3gpp/web/static/style.css`:

```css
/* Page header row: title left, action links right. */
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 1rem;
}

.page-header h1 {
  margin: 0;
}

.page-actions {
  display: flex;
  gap: .5rem;
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_web_routes.py -v`
Expected: PASS — new tests green; `test_search_full_page_loads_search_js` and all other search tests still pass.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/templates/search_results.html src/doc3gpp/web/static/style.css tests/unit/test_web_routes.py
git commit -m "feat: cross links between FTS5 and hybrid search pages"
```

---

### Task 9: MCP `search_tdocs` gains `sem_query`

**Files:**
- Modify: `src/doc3gpp/web/mcp_server.py:318-336` (`search_tdocs`)
- Test: `tests/integration/test_mcp_end_to_end.py` (append)

**Interfaces:**
- Consumes: `services.search.search(query, filters, sem_query=None)` (Task 1).
- Produces: MCP tool `search_tdocs(query, tsg=None, meeting=None, release=None, spec=None, since=None, until=None, limit=20, sem_query=None)` — passes `sem_query` through to the service.

- [ ] **Step 1: Write the failing MCP test**

Append to `tests/integration/test_mcp_end_to_end.py`:

```python
def test_search_tdocs_accepts_sem_query(sqlite_env, search_corpus) -> None:
    """search_tdocs forwards sem_query to the search service."""
    from doc3gpp.web.mcp_server import build_mcp_server
    from doc3gpp.web.state import ServiceContainer, WebState
    from doc3gpp.web.workers.job_worker import JobWorkerHandle
    from doc3gpp.settings.schema import Settings
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository
    from doc3gpp.services import factory

    settings = Settings()
    embedder = factory.build_embedder(settings)
    services = ServiceContainer(
        meeting=factory.build_meeting_service(),
        tdoc=factory.build_tdoc_service(),
        tdoc_cr=factory.build_tdoc_cr_service(embedder=embedder),
        tdoc_sync=factory.build_tdoc_sync_coordinator(),
        tdoc_repo=factory.build_tdoc_repository(),
        tsg=factory.build_tsg_service(),
        wi=factory.build_wi_service(),
        search=factory.build_search_service(embedder=embedder),
        semantic_search=factory.build_semantic_search_service(embedder=embedder),
        tdoc_file_repo=factory.build_tdoc_file_repository(),
        job_repo=SQLAlchemyJobRepository(),
    )
    state = WebState(
        settings=settings,
        engine=get_engine(),
        services=services,
        jobs=JobWorkerHandle(),
    )
    server = build_mcp_server(state)
    result = server.call_tool(
        "search_tdocs",
        {"query": "scheduling", "limit": 5, "sem_query": "scheduling"},
    )
    assert result is not None
    text = result[0].text
    assert text.startswith("[")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_mcp_end_to_end.py::test_search_tdocs_accepts_sem_query -v`
Expected: FAIL — `TypeError: search_tdocs() got an unexpected keyword argument 'sem_query'` (MCP tool rejects the unknown arg).

- [ ] **Step 3: Implement the MCP change**

In `src/doc3gpp/web/mcp_server.py`, change the `search_tdocs` signature (lines 320-329) to:

```python
    def search_tdocs(
        query: str,
        tsg: str | None = None,
        meeting: str | None = None,
        release: str | None = None,
        spec: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 20,
        sem_query: str | None = None,
    ) -> str:
```

and the service call (line 335) to:

```python
        hits = services.search.search(query, filters, sem_query=sem_query)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_mcp_end_to_end.py -v`
Expected: PASS — new test green; existing MCP tests (tool list, jargon normalisation, stopwords) still pass.

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/web/mcp_server.py tests/integration/test_mcp_end_to_end.py
git commit -m "feat: sem_query param on MCP search_tdocs tool"
```

---

### Task 10: Parse card on the TDoc detail page

**Files:**
- Modify: `src/doc3gpp/web/templates/tdoc_show.html` (insert between line 39 and line 41)
- Create: `src/doc3gpp/web/static/js/tdoc_parse.js`
- Test: `tests/unit/test_web_routes.py` (append)

**Interfaces:**
- Consumes: `record.tdoc.tdoc_id`, `record.tdoc.ftp_url` (template context), `POST /jobs/parse/tdocs` (JSON body `{"filter": {"tdoc_id": ...}, "force": bool, "full": bool}` → 202 `{job_id, status, links}`), `GET /jobs/{job_id}?format=html` → `partials/job_status.html`.
- Produces: a "Parse" card between the TDoc card and the Cover page section, shown only when `record.tdoc.ftp_url` is set; a form with hidden `tdoc_id`, checkboxes `force` and `full`, and a "Parse this TDoc" button; `tdoc_parse.js` POSTs the form via fetch, then injects `<div hx-get="/jobs/{job_id}?format=html" hx-trigger="load" hx-swap="outerHTML">` into `#parse-job-target` so the existing job_status partial polls every 2s.

- [ ] **Step 1: Write the failing template test**

Append to `tests/unit/test_web_routes.py`:

```python
def test_tdoc_show_parse_card_rendered_when_ftp_url(
    client: TestClient, sqlite_env: Any,
) -> None:
    """A TDoc with an ftp_url shows the Parse card with force/full checkboxes."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260001", ftp_url="R5/26.001/R5-260001.zip"),
    )
    html = client.get("/tdocs/R5-260001").text
    assert "Parse this TDoc" in html
    assert 'name="force"' in html
    assert 'name="full"' in html
    assert 'src="/static/js/tdoc_parse.js"' in html


def test_tdoc_show_parse_card_hidden_without_ftp_url(
    client: TestClient, sqlite_env: Any,
) -> None:
    """A TDoc without an ftp_url shows no Parse card."""
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5-260001"))
    html = client.get("/tdocs/R5-260001").text
    assert "Parse this TDoc" not in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_web_routes.py -k "parse_card" -v`
Expected: FAIL — `"Parse this TDoc" not in html`.

- [ ] **Step 3: Add the Parse card to the template**

In `src/doc3gpp/web/templates/tdoc_show.html`, insert between the TDoc card's closing `</section>` (line 39) and the `{% if record.cover %}` line (line 41):

```html
  {% if record.tdoc.ftp_url %}
    <section class="card">
      <h2>Parse</h2>
      <form
        id="parse-form"
        class="parse-form"
        data-tdoc-id="{{ record.tdoc.tdoc_id }}"
      >
        <label class="inline-check">
          <input type="checkbox" name="force"> Force re-parse
        </label>
        <label class="inline-check">
          <input type="checkbox" name="full"> Full extraction
        </label>
        <button type="submit" class="btn primary">Parse this TDoc</button>
        <span class="parse-queued" style="display:none">Parse job queued</span>
      </form>
      <div id="parse-job-target"></div>
    </section>
  {% endif %}
```

At the end of the file (after the final `{% endblock %}`), add the script tag:

```html
  <script src="/static/js/tdoc_parse.js" defer></script>
{% endblock %}
```

(Replace the existing final `{% endblock %}` with the two lines above.)

- [ ] **Step 4: Create `tdoc_parse.js`**

Create `src/doc3gpp/web/static/js/tdoc_parse.js`:

```javascript
// Parse-trigger for the TDoc detail page.
// POSTs the parse job to /jobs/parse/tdocs, then injects a div that
// hx-get's the job status partial (which polls every 2s until terminal).
(function () {
  "use strict";

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || form.id !== "parse-form") {
      return;
    }
    event.preventDefault();

    var tdocId = form.getAttribute("data-tdoc-id");
    var force = form.querySelector('input[name="force"]').checked;
    var full = form.querySelector('input[name="full"]').checked;
    var queued = form.querySelector(".parse-queued");
    var target = document.getElementById("parse-job-target");

    fetch("/jobs/parse/tdocs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filter: { tdoc_id: tdocId },
        force: force,
        full: full,
      }),
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("parse enqueue failed: HTTP " + response.status);
        }
        return response.json();
      })
      .then(function (body) {
        if (queued) {
          queued.style.display = "inline";
        }
        var div = document.createElement("div");
        div.setAttribute("hx-get", "/jobs/" + body.job_id + "?format=html");
        div.setAttribute("hx-trigger", "load");
        div.setAttribute("hx-swap", "outerHTML");
        target.appendChild(div);
        if (window.htmx) {
          window.htmx.process(div);
        }
      })
      .catch(function (err) {
        if (queued) {
          queued.textContent = "Failed to enqueue parse job";
          queued.style.display = "inline";
        }
        console.error(err);
      });
  });
})();
```

- [ ] **Step 5: Add the CSS for the parse form**

Append to `src/doc3gpp/web/static/style.css`:

```css
/* Parse card on the TDoc detail page. */
.parse-form {
  display: flex;
  flex-wrap: wrap;
  gap: .75rem;
  align-items: center;
}

.inline-check {
  display: flex;
  align-items: center;
  gap: .35rem;
  font-size: .875rem;
  color: var(--muted, #666);
}

.parse-queued {
  margin-left: .4rem;
  color: #1a7f37;
  font-size: .85rem;
}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/unit/test_web_routes.py -k "parse_card" -v`
Expected: PASS — both new tests green.

- [ ] **Step 7: Verify the full web route suite**

Run: `pytest tests/unit/test_web_routes.py -v`
Expected: PASS — all existing tdoc_show tests (FTP URL links, TTCN, auxiliary files) still pass.

- [ ] **Step 8: Commit**

```bash
git add src/doc3gpp/web/templates/tdoc_show.html src/doc3gpp/web/static/js/tdoc_parse.js src/doc3gpp/web/static/style.css tests/unit/test_web_routes.py
git commit -m "feat: parse trigger card on tdoc detail page"
```

---

### Task 11: Web-triggered parse job enqueues single-tdoc params

**Files:**
- Test: `tests/unit/test_web_jobs_routes.py` (append after `test_post_parse_tdocs` line 151)

**Interfaces:**
- Consumes: `POST /jobs/parse/tdocs` with `{"filter": {"tdoc_id": ...}, "force": bool, "full": bool}` (the exact body `tdoc_parse.js` sends).
- Produces: a test pinning that the single-tdoc payload creates a `PARSE_TDOCS` job with the exact params.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_web_jobs_routes.py` after `test_post_parse_tdocs` (line 151):

```python
def test_post_parse_tdocs_single_tdoc_payload(client: Any) -> None:
    """The tdoc detail page's payload enqueues a single-tdoc parse job."""
    c, repo, _ = client
    r = c.post(
        "/jobs/parse/tdocs",
        json={
            "filter": {"tdoc_id": "R5-260001"},
            "force": True,
            "full": True,
        },
    )
    assert r.status_code == 202
    job = repo.get(r.json()["job_id"])
    assert job is not None
    assert job.kind is JobKind.PARSE_TDOCS
    assert job.params == {
        "filter": {"tdoc_id": "R5-260001"},
        "force": True,
        "full": True,
    }
```

- [ ] **Step 2: Run test to verify it passes (endpoint already supports it)**

Run: `pytest tests/unit/test_web_jobs_routes.py::test_post_parse_tdocs_single_tdoc_payload -v`
Expected: PASS — the endpoint already accepts this payload (this test pins the contract the new button relies on).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_web_jobs_routes.py
git commit -m "test: pin single-tdoc parse job payload"
```

---

### Task 12: Integration — auto FTS5 + embed after web-triggered parse

**Files:**
- Test: `tests/integration/test_embed_after_parse.py` (append)

**Interfaces:**
- Consumes: `TDocCrService.extract_many` → `_index_after_parse` / `_embed_after_parse` (existing behavior), `SearchService.upsert_for_tdoc`, `SemanticSearchService.index_for_tdoc`.
- Produces: an integration test proving a parse triggered through the same job path the web button uses still auto-indexes FTS5 and embeddings.

- [ ] **Step 1: Read the existing test file to match its fixtures**

Run: `rtk read tests/integration/test_embed_after_parse.py`
Expected: shows the existing fixture pattern (sqlite_env, seeded tdoc, stubbed scraper/parser, `TDocCrService` with fake search/semantic services recording calls).

- [ ] **Step 2: Write the failing test**

Append to `tests/integration/test_embed_after_parse.py` (adapting the existing fixture pattern from Step 1):

```python
def test_web_job_path_parse_auto_indexes_fts5_and_embeddings(
    sqlite_env, monkeypatch,
) -> None:
    """A parse enqueued via the web job path (same params the tdoc
    detail page sends) auto-indexes FTS5 + embeddings on success."""
    from unittest.mock import MagicMock

    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260001", ftp_url="R5/26.001/R5-260001.zip"),
    )

    search_service = MagicMock()
    semantic_service = MagicMock()
    # ... construct TDocCrService with the same stubbed scraper/parser
    # the existing tests in this file use, plus search_service and
    # semantic_service; run extract_many(["R5-260001"], force=True,
    # full=True) with the parser returning a successful result.

    search_service.upsert_for_tdoc.assert_called_once_with("R5-260001")
    semantic_service.index_for_tdoc.assert_called_once_with("R5-260001")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/integration/test_embed_after_parse.py::test_web_job_path_parse_auto_indexes_fts5_and_embeddings -v`
Expected: FAIL — the test body is incomplete (see Step 4: the existing file's fixture pattern must be copied verbatim; the assertion names are the contract).

- [ ] **Step 4: Complete the test by copying the existing fixture pattern**

Read the existing tests in `tests/integration/test_embed_after_parse.py` and fill in the `TDocCrService` construction exactly as they do (stubbed `scraper_client`, `parser` returning a `TDocCRParseResult`, `cr_repository`/`cr_ttcn_repository`/`cr_change_details_repository` stubs, `tdoc_repository`), passing `search_service=search_service, semantic_service=semantic_service`. Keep the two `assert_called_once_with("R5-260001")` assertions.

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/integration/test_embed_after_parse.py -v`
Expected: PASS — the new test green; existing tests in the file still pass.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/test_embed_after_parse.py
git commit -m "test: web job path parse auto-indexes fts5 and embeddings"
```

---

### Task 13: Docs — web-server.md, AGENTS.md

**Files:**
- Modify: `docs/web-server.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: the final behavior of Tasks 1-12.
- Produces: updated docs describing the parse button, the shared embedder, the search page grid + sem input + cross links, and the MCP `sem_query` param.

- [ ] **Step 1: Update `docs/web-server.md`**

In the HTTP routes table (line 168), update the `/search` row to mention the `sem` param:

```
| GET | `/search` | FTS5 search (`?format=json`). Accepts an optional `sem` query param — when present, the FTS5 hits are reordered by cosine similarity to that text (CLI `--sem-query` parity; empty/absent = pure FTS5). |
```

After the paragraph at lines 213-217 ("Both search modes ..."), add:

```
The search form uses a 5-column grid: on `/search` the Query box spans
2 columns and an optional Semantic box (the `sem` param) spans the
remaining 3; on `/search/sem` the Query box spans 3 columns and the
FTS5 query box spans 2. Both forms share the full filter set (TSG,
Meeting, TDoc, Release, Spec, Since, Until, Limit; `/search/sem` also
keeps FTS5 weight). Each page links to the other at the top right
(`/search` → "Hybrid search", `/search/sem` → "FTS5 search").
```

After the TDoc detail page paragraph (lines 200-204), add:

```
The TDoc detail page shows a Parse card (only when the TDoc has an FTP
URL) with "Force re-parse" and "Full extraction" checkboxes. Submitting
enqueues a `parse_tdocs` job filtered to that single TDoc id; the job
status partial polls inline until the job finishes. A successful parse
auto-indexes the FTS5 row and the embedding chunks when
`[search].auto_index_on_parse` / `[semantic_search].auto_embed_on_parse`
are enabled — the same hooks the CLI parse path uses.
```

In the MCP reference section (after line 287, the `search_tdocs` sentence), add:

```
`search_tdocs` also accepts an optional `sem_query` argument — when
provided, the FTS5 hits are reordered by cosine similarity to that
text, mirroring the `/search?sem=` route and the CLI's
`search query --sem-query`.
```

- [ ] **Step 2: Update `AGENTS.md`**

In the "Where to look" table, update the "Add a web route / HTML page" row's notes to mention the parse card + shared embedder:

```
| Add a web route / HTML page | `src/doc3gpp/web/routes/` + `src/doc3gpp/web/render.py` + templates in `src/doc3gpp/web/templates/` + `src/doc3gpp/web/filters.py` (`is_htmx_request`) | Routes are thin adapters over services via `web/deps.py` `Depends` helpers; keep HTML/JSON/CLI output byte-consistent. The tdoc detail page's Parse card enqueues `POST /jobs/parse/tdocs` (single-tdoc filter) and polls `partials/job_status.html` via `static/js/tdoc_parse.js`; the search pages share a 5-column grid form with a `sem` rerank input on `/search` and full filter parity on `/search/sem`. The web app builds ONE shared `SentenceTransformerEmbedder` in `build_state` and injects it into `build_tdoc_cr_service` / `build_search_service` / `build_semantic_search_service` so the model loads once per process. |
```

- [ ] **Step 3: Verify docs render (no test run needed)**

Run: `rtk grep -n "sem_query\|Parse card\|5-column" docs/web-server.md AGENTS.md`
Expected: the new lines are present.

- [ ] **Step 4: Commit**

```bash
git add docs/web-server.md AGENTS.md
git commit -m "docs: web parse button, shared embedder, search page updates"
```

---

### Task 14: Full verification pass

**Files:**
- None (verification only)

**Interfaces:**
- Consumes: all of Tasks 1-13.

- [ ] **Step 1: Run the full sqlite test suite**

Run: `./scripts/test_sqlite.sh`
Expected: PASS — every unit + integration test green.

- [ ] **Step 2: Run the linter**

Run: `ruff check .`
Expected: PASS — no lint errors.

- [ ] **Step 3: Manual smoke check of the search pages (optional, server enabled)**

Run: `doc3gpp server start` then:
- `curl -s "http://127.0.0.1:8765/search?q=foo"` → contains `name="sem"` and `href="/search/sem"`.
- `curl -s "http://127.0.0.1:8765/search/sem?q=foo"` → contains `name="tsg"` and `href="/search"`.
- `curl -s "http://127.0.0.1:8765/tdocs/R5-260001"` (with a seeded tdoc) → contains `Parse this TDoc`.

- [ ] **Step 4: Commit any stragglers**

Run: `rtk git status`
Expected: clean working tree (all changes committed per task).

---

## Self-Review

**1. Spec coverage:**
- Parse button on tdoc detail page → Task 10 (card + JS) + Task 11 (job payload pin) + Task 12 (auto-index integration).
- Shared embedder → Task 2 (lock), Task 3 (factory), Task 4 (build_state).
- `/search` 5-col grid, FTS5 2 cols + semantic 3 cols, `--sem-query` parity → Task 1 (service), Task 5 (route), Task 7 (form).
- `/search/sem` semantic 3 cols + FTS5 2 cols + full filters → Task 6 (route), Task 7 (form).
- Cross links → Task 8.
- MCP `search_tdocs` sem_query → Task 9; `parse_tdocs` unchanged (spec out of scope).
- Docs → Task 13.

**2. Placeholder scan:** No TBD/TODO; every step has concrete code. Task 12 Step 4 intentionally references copying the existing fixture pattern from the same file (the file's exact stub construction is read in Step 1 before writing) — the assertion contract is fully specified.

**3. Type consistency:**
- `SearchService.search(query, filters, sem_query=None)` — used identically in Task 1 (impl), Task 5 (route), Task 9 (MCP).
- `factory.build_embedder(settings)` — defined Task 3, consumed Task 4.
- `build_search_service(..., embedder=...)` / `build_tdoc_cr_service(..., embedder=...)` — defined Task 3, consumed Task 4.
- `filters` context dict keys (`tsg/meeting/release/spec/since/until/tdoc_id/sem`) — produced by Tasks 5-6, consumed by Task 7 template.
- `FakeSearchService.search(..., sem_query=None)` — updated in Task 5 Step 1 before the route change in Step 4 (same task).
- `POST /jobs/parse/tdocs` body `{"filter": {"tdoc_id"}, "force", "full"}` — produced by Task 10 JS, pinned by Task 11.
