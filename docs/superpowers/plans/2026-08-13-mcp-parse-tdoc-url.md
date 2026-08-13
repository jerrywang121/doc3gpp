# MCP `parse_tdoc_url` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new `parse_tdoc_url` MCP tool that surfaces `doc3gpp tdoc parse --from-url` over MCP. The tool only accepts 3GPP FTP URLs (rejected at the tool boundary with `InvalidFilterError`), enqueues a `JobKind.PARSE_TDOC_URL` job, and the worker re-uses `TDocCrService.extract_from_url_batch` for the parse + cache + DB writes.

**Architecture:** A new `JobKind.PARSE_TDOC_URL` enum value, a new `_parse_tdoc_url` worker handler in `web/workers/handlers.py`, and a new `parse_tdoc_url` MCP tool in `web/mcp_server.py`. No service-layer changes — `TDocCrService.extract_from_url_batch` is the single source of truth. No HTTP route in this change.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0, Pydantic v2, MCP v2 (`mcp.server.mcpserver.MCPServer`), pytest, ruff.

## Global Constraints

- **CLI parity:** the worker must mirror `src/doc3gpp/cli.py:1525-1587` (URL validation → optional auto-sync → service call → per-file write). No new behaviour, no new behaviour switches.
- **URL allowlist:** only URLs that pass `doc3gpp.parsers.direct_extractor.is_3gpp_ftp_url` are accepted. Non-3GPP URLs raise `InvalidFilterError` at the MCP tool boundary before any job is enqueued. The worker also re-checks (defence-in-depth).
- **Auto-sync:** the worker honours `settings.sync.auto_sync` exactly the way the CLI does — when on, call `collect_tdoc_candidates_for_url` then `trigger_auto_sync`; auto-sync failures are warnings, never abort the parse.
- **Per-file byte cap:** the worker forwards `settings.tdoc_parse.max_tdoc_size_kb * 1024` to the service (`0` = unlimited, same convention as the CLI).
- **Per-file failures / oversize skips are not job failures:** the job's final status is `SUCCEEDED` even when individual files fail or are too large; the failures / skipped counts land in the `result_summary`.
- **No new dependencies, no new settings, no new CLI commands, no new HTTP route.**
- **Sync vs async:** the worker calls the sync `extract_from_url_batch` via `asyncio.to_thread(...)`, mirroring the existing pattern in other handlers.
- **Lint:** `ruff check .` must pass before each commit.
- **Tests:** every task with logic adds tests; offline by default (`addopts = ["-m", "not online"]` in `pyproject.toml`); no live 3GPP network calls in this change.

---

## File map

| File | Change |
|---|---|
| `src/doc3gpp/models/jobs.py` | Add `JobKind.PARSE_TDOC_URL = "parse_tdoc_url"` to the `JobKind` enum (alphabetic position between `PARSE_TDOCS` and `REBUILD_SEARCH`). |
| `src/doc3gpp/web/workers/handlers.py` | Add `async def _parse_tdoc_url(...)`. Register in `JobHandlers.KIND_TO_HANDLER`. |
| `src/doc3gpp/web/mcp_server.py` | Add `parse_tdoc_url` tool. Import `is_3gpp_ftp_url` and `InvalidFilterError`. |
| `tests/unit/test_job_worker.py` | Add `test_worker_runs_parse_tdoc_url_job` + handler-level tests (auto-sync on/off, URL rejection, depth resolution, cancellation, per-file mapping). |
| `tests/integration/test_mcp_end_to_end.py` | Add `"parse_tdoc_url"` to the `test_list_tools_exposes_read_and_job_tools` expected set. |
| `tests/integration/test_parse_tdoc_url_job_sqlite.py` (new) | Integration test that enqueues a real `PARSE_TDOC_URL` job, runs the worker, asserts terminal state and progress lines. |
| `docs/cli.md` | Add the new MCP tool row. |
| `README.md` | Add to the MCP tools list. |
| `AGENTS.md` | Update the "Add a background job kind" and "Add an MCP tool" convention rows. |
| `docs/web-server.md` | Mention the new tool in the MCP section. |

No service-layer or storage-layer changes. No HTTP route changes. No `pyproject.toml` changes.

---

## Task 1: Add `JobKind.PARSE_TDOC_URL` enum value

**Files:**
- Modify: `src/doc3gpp/models/jobs.py:43-45` (the existing `PARSE_TDOCS` / `REBUILD_SEARCH` block in the `JobKind` enum)

**Interfaces:**
- Consumes: nothing (enum-only change)
- Produces: `JobKind.PARSE_TDOC_URL` with string value `"parse_tdoc_url"` — used by Task 2 (handler) and Task 3 (MCP tool)

- [ ] **Step 1: Add the enum value**

Edit `src/doc3gpp/models/jobs.py` to add the new kind between `PARSE_TDOCS` and `REBUILD_SEARCH` (keep the alphabetical ordering convention used in the enum):

```python
    PARSE_TDOCS = "parse_tdocs"
    PARSE_TDOC_URL = "parse_tdoc_url"
    REBUILD_SEARCH = "rebuild_search"
```

- [ ] **Step 2: Verify the enum is importable**

Run: `python -c "from doc3gpp.models.jobs import JobKind; print(JobKind.PARSE_TDOC_URL.value)"`
Expected: `parse_tdoc_url`

- [ ] **Step 3: Lint**

Run: `ruff check src/doc3gpp/models/jobs.py`
Expected: no findings.

- [ ] **Step 4: Commit**

```bash
git add src/doc3gpp/models/jobs.py
git commit -m "feat(jobs): add PARSE_TDOC_URL job kind for MCP URL parse"
```

---

## Task 2: Implement the `_parse_tdoc_url` worker handler

**Files:**
- Modify: `src/doc3gpp/web/workers/handlers.py:181-254` (after the existing `_parse_tdocs` handler, before `_rebuild_search`)
- Modify: `src/doc3gpp/web/workers/handlers.py:322-330` (add the new entry to `KIND_TO_HANDLER`)

**Interfaces:**
- Consumes:
  - `Job.params`: `{"url": str, "force"?: bool, "full"?: bool, "recursive"?: bool, "max_depth"?: int}` — `url` is required; other keys have defaults.
  - `services.tdoc_cr` (an instance of `TDocCrService` with the public methods `collect_3gpp_file_urls` and `extract_from_url_batch`).
  - `services.tdoc_sync` (a `TDocSyncCoordinator` instance — the worker's pre-wired instance, not a fresh build).
  - `services.meeting` (a `MeetingService` instance for `trigger_auto_sync`).
  - `settings.sync.auto_sync` (bool)
  - `settings.tdoc_parse.max_tdoc_size_kb` (int)
- Produces:
  - `result_summary` mapping with keys: `requested` (int), `successes` (int), `failures` (int), `skipped` (int), `files` (list of `{tdoc_id, ftp_url, status}` dicts).
  - Progress lines via the `progress` callback (formatted and persisted by the worker).

The handler must call `is_3gpp_ftp_url` itself (defence-in-depth) and raise `ValueError` (which the worker converts to `FAILED`) on a non-3GPP URL.

- [ ] **Step 1: Write the failing test (handler-level)**

Add to `tests/unit/test_job_worker.py` at the end of the file:

```python
from doc3gpp.models.tdoc_cr import DirectParseBatchResult, DirectParseResult


class _FakeTDocCrServiceForUrl:
    """Fake ``TDocCrService`` whose ``extract_from_url_batch`` is stubbed."""

    def __init__(
        self,
        *,
        results: list[DirectParseResult] | None = None,
        failures: dict[str, str] | None = None,
        skipped: dict[str, str] | None = None,
        file_urls: list[str] | None = None,
        raise_extract: Exception | None = None,
    ) -> None:
        from doc3gpp.models.tdoc_cr import DirectParseBatchResult as _BPR
        self.results = results or []
        self.failures = failures or {}
        self.skipped = skipped or {}
        self.file_urls: list[str] = file_urls or []
        self.raise_extract = raise_extract
        self.extract_calls: list[dict] = []
        self.collect_calls: list[dict] = []

    def collect_3gpp_file_urls(self, url: str, *, max_depth: int) -> list[str]:
        self.collect_calls.append({"url": url, "max_depth": max_depth})
        return list(self.file_urls)

    def extract_from_url_batch(
        self,
        url: str,
        *,
        max_depth: int,
        force: bool,
        full: bool,
        max_tdoc_size_bytes: int | None,
    ) -> DirectParseBatchResult:
        self.extract_calls.append({
            "url": url,
            "max_depth": max_depth,
            "force": force,
            "full": full,
            "max_tdoc_size_bytes": max_tdoc_size_bytes,
        })
        if self.raise_extract is not None:
            raise self.raise_extract
        return DirectParseBatchResult(
            results=self.results,
            failures=self.failures,
            skipped=self.skipped,
        )


def test_parse_tdoc_url_handler_rejects_non_3gpp_url() -> None:
    """Defence-in-depth: a tampered Job with a non-3GPP url raises ValueError."""
    from doc3gpp.models.jobs import JobKind

    repo = _make_repo()
    job = repo.create(JobKind.PARSE_TDOC_URL, {"url": "https://example.com/bad.zip"})
    state = _make_state(repo)
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.FAILED
    assert "3GPP FTP" in (done.error or "")


def test_parse_tdoc_url_handler_happy_path() -> None:
    """Happy path: results map to ``files[]`` with the right status labels."""
    from doc3gpp.models.tdoc_cr import DirectParseResult
    from doc3gpp.models.jobs import JobKind

    repo = _make_repo()
    state = _make_state(
        repo,
        url_service=_FakeTDocCrServiceForUrl(
            results=[
                DirectParseResult(
                    source_kind="url-3gpp",
                    markdown="",
                    details=None,
                    extract_meta=None,
                    from_cache=False,
                    persisted=True,
                    tdoc_id="R5-260001",
                    tdoc_id_in_tdocs=True,
                    source_url="https://www.3gpp.org/ftp/R5s260001.zip",
                ),
                DirectParseResult(
                    source_kind="url-3gpp",
                    markdown="",
                    details=None,
                    extract_meta=None,
                    from_cache=False,
                    persisted=False,
                    tdoc_id="R5-260002",
                    tdoc_id_in_tdocs=False,
                    source_url="https://www.3gpp.org/ftp/R5s260002.zip",
                ),
            ],
            failures={"https://www.3gpp.org/ftp/R5s260003.zip": "ZipError: corrupt"},
            skipped={"https://www.3gpp.org/ftp/R5s260004.zip": "TDocTooLargeError: ..."},
        ),
    )
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/", "force": True, "max_depth": 2},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.SUCCEEDED
    assert done.result_summary == {
        "requested": 4,
        "successes": 2,
        "failures": 1,
        "skipped": 1,
        "files": [
            {
                "tdoc_id": "R5-260001",
                "ftp_url": "https://www.3gpp.org/ftp/R5s260001.zip",
                "status": "ok",
            },
            {
                "tdoc_id": "R5-260002",
                "ftp_url": "https://www.3gpp.org/ftp/R5s260002.zip",
                "status": "parsed-no-fk",
            },
        ],
    }
```

- [ ] **Step 2: Extend `_make_state` to take a URL-mode service**

`_make_state` currently wires a `_FakeSpecService` for `spec` and leaves `tdoc_cr=None`. The new tests need a way to inject a `_FakeTDocCrServiceForUrl` (or, for the other tests, keep `tdoc_cr=None` and verify the handler fails). Add an optional `url_service` parameter; when `None`, default to `None` so the existing tests don't break.

Edit `_make_state` in `tests/unit/test_job_worker.py:63-80` to add the `url_service` parameter and pass it into the `ServiceContainer`:

```python
def _make_state(
    repo: JobRepository,
    *,
    fail: bool = False,
    url_service: object | None = None,
) -> WebState:
    services = ServiceContainer(
        meeting=_FakeMeetingService(fail=fail),  # type: ignore[arg-type]
        tdoc=None,  # type: ignore[arg-type]
        tdoc_cr=url_service,  # type: ignore[arg-type]
        tdoc_sync=None,  # type: ignore[arg-type]
        tdoc_repo=None,  # type: ignore[arg-type]
        tsg=None,  # type: ignore[arg-type]
        wi=None,  # type: ignore[arg-type]
        spec=_FakeSpecService(fail=fail),  # type: ignore[arg-type]
        search=None,
        semantic_search=None,
        tdoc_file_repo=None,  # type: ignore[arg-type]
        job_repo=repo,
    )
    settings = Settings()
    return WebState(settings=settings, engine=None, services=services, jobs=_JobWorkerHandleFake())  # type: ignore[arg-type]
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `pytest tests/unit/test_job_worker.py::test_parse_tdoc_url_handler_rejects_non_3gpp_url tests/unit/test_job_worker.py::test_parse_tdoc_url_handler_happy_path -v`
Expected: BOTH FAIL. `test_parse_tdoc_url_handler_rejects_non_3gpp_url` fails because `_make_state` doesn't accept `url_service`; `test_parse_tdoc_url_handler_happy_path` fails for the same reason.

- [ ] **Step 4: Implement the handler**

Add the import and the handler at the end of `src/doc3gpp/web/workers/handlers.py`, just before the `JobHandlers` class (around line 314):

```python
from doc3gpp.cli_auto_sync import (
    collect_tdoc_candidates_for_url,
    trigger_auto_sync,
)
from doc3gpp.parsers.direct_extractor import is_3gpp_ftp_url
```

(Add the import near the top of the file alongside the other imports — there is no circular-import risk because `doc3gpp.cli_auto_sync` already imports from `doc3gpp.services` and the worker module only consumes the helper functions.)

Then add the handler body after `_parse_tdocs` (after line 254) and before `_rebuild_search`:

```python
async def _parse_tdoc_url(
    job: Job,
    services: ServiceContainer,
    settings: Settings,
    *,
    progress: ProgressFn,
    cancel_event: asyncio.Event,
) -> Mapping[str, JSONValue]:
    url = job.params.get("url")
    if not isinstance(url, str) or not is_3gpp_ftp_url(url):
        raise ValueError(
            f"parse_tdoc_url job requires a 3GPP FTP 'url' parameter; got {url!r}"
        )
    force = bool(job.params.get("force", False))
    full = bool(job.params.get("full", False))
    recursive = bool(job.params.get("recursive", False))
    max_depth_param = job.params.get("max_depth")
    if recursive:
        max_depth = -1
    elif max_depth_param is not None:
        max_depth = max(int(max_depth_param), 0)
    else:
        max_depth = 2

    if settings.sync.auto_sync:
        candidates = collect_tdoc_candidates_for_url(
            url,
            tdoc_service=services.tdoc_cr,
            max_depth=max_depth,
        )
        if candidates:
            progress(
                f"auto-sync: {len(candidates)} candidate tdoc_id(s) from URL — "
                f"running trigger_auto_sync"
            )
            try:
                trigger_auto_sync(
                    auto_sync_enabled=True,
                    meeting_service=services.meeting,
                    tdoc_sync_coordinator=services.tdoc_sync,
                    tdoc_ids=candidates,
                )
            except Exception as exc:  # noqa: BLE001 - CLI parity: warn, do not abort
                logger.warning("auto_sync from URL %s failed: %s", url, exc)

    max_tdoc_size_bytes = (
        settings.tdoc_parse.max_tdoc_size_kb * 1024
        if settings.tdoc_parse.max_tdoc_size_kb > 0
        else 0
    )

    if cancel_event.is_set():
        raise asyncio.CancelledError()
    batch = await asyncio.to_thread(
        services.tdoc_cr.extract_from_url_batch,
        url,
        max_depth=max_depth,
        force=force,
        full=full,
        max_tdoc_size_bytes=max_tdoc_size_bytes or None,
    )

    files: list[dict[str, str]] = []
    for r in batch.results:
        if r.tdoc_id is None:
            continue
        files.append({
            "tdoc_id": r.tdoc_id,
            "ftp_url": r.source_url or "",
            "status": "ok" if r.persisted else "parsed-no-fk",
        })

    progress(
        f"done: {len(batch.results)} parsed, {len(batch.failures)} failed, "
        f"{len(batch.skipped)} skipped"
    )

    return {
        "requested": len(batch.results) + len(batch.failures) + len(batch.skipped),
        "successes": len(batch.results),
        "failures": len(batch.failures),
        "skipped": len(batch.skipped),
        "files": files,
    }
```

Register the new entry in `JobHandlers.KIND_TO_HANDLER` (`handlers.py:322-330`):

```python
    KIND_TO_HANDLER: dict[JobKind, Handler] = {
        JobKind.SYNC_MEETINGS: _sync_meetings,
        JobKind.SYNC_TDOCS: _sync_tdocs,
        JobKind.SYNC_TDOCS_ALL: _sync_tdocs_all,
        JobKind.SYNC_SPECS: _sync_specs,
        JobKind.PARSE_TDOCS: _parse_tdocs,
        JobKind.PARSE_TDOC_URL: _parse_tdoc_url,
        JobKind.REBUILD_SEARCH: _rebuild_search,
        JobKind.CACHE_PURGE: _cache_purge,
    }
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `pytest tests/unit/test_job_worker.py::test_parse_tdoc_url_handler_rejects_non_3gpp_url tests/unit/test_job_worker.py::test_parse_tdoc_url_handler_happy_path -v`
Expected: BOTH PASS.

- [ ] **Step 6: Lint**

Run: `ruff check src/doc3gpp/web/workers/handlers.py tests/unit/test_job_worker.py`
Expected: no findings.

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/web/workers/handlers.py tests/unit/test_job_worker.py
git commit -m "feat(jobs): parse_tdoc_url worker handler"
```

---

## Task 3: Add auto-sync / depth / cancellation / size-cap unit tests

**Files:**
- Modify: `tests/unit/test_job_worker.py` (append four new test functions)

**Interfaces:** all four tests use the same `WebState` + `JobWorker` scaffolding established in Task 2. They use a small helper `_make_url_state(repo, fake)` that returns a `WebState` whose `tdoc_cr` is the supplied fake and whose `tdoc_sync` is a `_FakeTDocSyncCoordinator` that records calls. The `MeetingService` is the existing `_FakeMeetingService`.

- [ ] **Step 1: Add the supporting fakes and helper**

Add to `tests/unit/test_job_worker.py`:

```python
class _FakeTDocSyncCoordinator:
    """Fake coordinator that records ``sync_for_meeting_id`` calls."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def sync_for_meeting_id(self, meeting_id: int, *, force: bool = False) -> object:
        from doc3gpp.models.sync import SyncOutcome
        self.calls.append(meeting_id)
        return SyncOutcome(
            status="synced", reason="ok", synced_count=0, file_count=0
        )

    def sync_for_meeting_name(self, *a, **k):  # pragma: no cover - not exercised here
        raise NotImplementedError

    def sync_all_tracked_meetings(self, *, force: bool = False):  # pragma: no cover
        raise NotImplementedError


def _make_url_state(
    repo: JobRepository,
    *,
    url_service: object,
    auto_sync: bool = False,
) -> WebState:
    settings = Settings()
    settings.sync.auto_sync = auto_sync
    services = ServiceContainer(
        meeting=_FakeMeetingService(),  # type: ignore[arg-type]
        tdoc=None,  # type: ignore[arg-type]
        tdoc_cr=url_service,  # type: ignore[arg-type]
        tdoc_sync=_FakeTDocSyncCoordinator(),  # type: ignore[arg-type]
        tdoc_repo=None,  # type: ignore[arg-type]
        tsg=None,  # type: ignore[arg-type]
        wi=None,  # type: ignore[arg-type]
        spec=_FakeSpecService(),  # type: ignore[arg-type]
        search=None,
        semantic_search=None,
        tdoc_file_repo=None,  # type: ignore[arg-type]
        job_repo=repo,
    )
    return WebState(settings=settings, engine=None, services=services, jobs=_JobWorkerHandleFake())  # type: ignore[arg-type]
```

- [ ] **Step 2: Write the four tests**

Add to `tests/unit/test_job_worker.py`:

```python
def test_parse_tdoc_url_handler_recursive_means_bfs_exhausted() -> None:
    """``recursive=True`` is forwarded as ``max_depth=-1`` (BFS-until-exhausted)."""
    from doc3gpp.models.jobs import JobKind

    repo = _make_repo()
    fake = _FakeTDocCrServiceForUrl()
    state = _make_url_state(repo, url_service=fake)
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/", "recursive": True},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done.status is JobStatus.SUCCEEDED
    assert fake.extract_calls[0]["max_depth"] == -1


def test_parse_tdoc_url_handler_explicit_max_depth_forwarded() -> None:
    """``max_depth=5`` is forwarded verbatim; default is 2 when omitted."""
    from doc3gpp.models.jobs import JobKind

    repo = _make_repo()
    fake = _FakeTDocCrServiceForUrl()
    state = _make_url_state(repo, url_service=fake)
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/", "max_depth": 5},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done.status is JobStatus.SUCCEEDED
    assert fake.extract_calls[0]["max_depth"] == 5


def test_parse_tdoc_url_handler_default_max_depth_is_two() -> None:
    """No ``max_depth`` in params → ``max_depth=2`` forwarded to service."""
    from doc3gpp.models.jobs import JobKind

    repo = _make_repo()
    fake = _FakeTDocCrServiceForUrl()
    state = _make_url_state(repo, url_service=fake)
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/"},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done.status is JobStatus.SUCCEEDED
    assert fake.extract_calls[0]["max_depth"] == 2


def test_parse_tdoc_url_handler_auto_sync_runs_when_enabled() -> None:
    """``settings.sync.auto_sync=True`` triggers ``trigger_auto_sync``."""
    from doc3gpp.models.jobs import JobKind

    class _CandidateTDocCrService(_FakeTDocCrServiceForUrl):
    def __init__(self) -> None:
        super().__init__(
            file_urls=["https://www.3gpp.org/ftp/R5s260001.zip"],
        )

    def collect_3gpp_file_urls(self, url: str, *, max_depth: int) -> list[str]:
        return ["https://www.3gpp.org/ftp/R5s260001.zip"]

    repo = _make_repo()
    fake = _CandidateTDocCrService()
    state = _make_url_state(repo, url_service=fake, auto_sync=True)
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/"},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done.status is JobStatus.SUCCEEDED
    # The "auto-sync" progress line is appended; the parse still ran.
    assert any("auto-sync" in line for line in done.log_lines)
    assert any("done:" in line for line in done.log_lines)


def test_parse_tdoc_url_handler_auto_sync_disabled_skips_step() -> None:
    """``settings.sync.auto_sync=False`` → no ``collect_3gpp_file_urls`` call."""
    from doc3gpp.models.jobs import JobKind

    repo = _make_repo()
    fake = _FakeTDocCrServiceForUrl()
    state = _make_url_state(repo, url_service=fake, auto_sync=False)
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/"},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done.status is JobStatus.SUCCEEDED
    assert fake.collect_calls == []
    assert not any("auto-sync" in line for line in done.log_lines)


def test_parse_tdoc_url_handler_auto_sync_failure_does_not_abort() -> None:
    """An exception in ``trigger_auto_sync`` is logged; the parse still runs."""
    from doc3gpp.models.jobs import JobKind

    class _RaisingTDocCrService(_FakeTDocCrServiceForUrl):
        def collect_3gpp_file_urls(self, url: str, *, max_depth: int) -> tuple[str, ...]:
            raise RuntimeError("network down")

    repo = _make_repo()
    fake = _RaisingTDocCrService()
    state = _make_url_state(repo, url_service=fake, auto_sync=True)
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/"},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done.status is JobStatus.SUCCEEDED
    assert any("done:" in line for line in done.log_lines)


def test_parse_tdoc_url_handler_cancellation_raises_cancelled() -> None:
    """``cancel_event`` set before the service call → ``CANCELLED`` job state."""
    import asyncio as _asyncio
    from doc3gpp.models.jobs import JobKind
    from doc3gpp.web.workers.job_worker import JobWorker as _JW

    repo = _make_repo()
    fake = _FakeTDocCrServiceForUrl()
    state = _make_url_state(repo, url_service=fake)
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/"},
    )

    cancel_event = _asyncio.Event()
    cancel_event.set()
    worker = _JW(state, repo=repo)

    async def _claim() -> None:
        sem = _asyncio.Semaphore(1)
        await worker._claim_and_run(repo.get(job.id), sem)  # type: ignore[attr-defined]

    _asyncio.run(_claim())

    done = repo.get(job.id)
    assert done.status is JobStatus.CANCELLED
    assert fake.extract_calls == []  # never reached the service


def test_parse_tdoc_url_handler_size_cap_forwarded() -> None:
    """``settings.tdoc_parse.max_tdoc_size_kb`` is forwarded as ``kb * 1024``."""
    from doc3gpp.models.jobs import JobKind

    repo = _make_repo()
    fake = _FakeTDocCrServiceForUrl()
    state = _make_url_state(repo, url_service=fake)
    state.settings.tdoc_parse.max_tdoc_size_kb = 1000
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/"},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done.status is JobStatus.SUCCEEDED
    assert fake.extract_calls[0]["max_tdoc_size_bytes"] == 1000 * 1024


def test_parse_tdoc_url_handler_size_cap_zero_means_unlimited() -> None:
    """``max_tdoc_size_kb=0`` → ``max_tdoc_size_bytes=None`` forwarded."""
    from doc3gpp.models.jobs import JobKind

    repo = _make_repo()
    fake = _FakeTDocCrServiceForUrl()
    state = _make_url_state(repo, url_service=fake)
    state.settings.tdoc_parse.max_tdoc_size_kb = 0
    job = repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/"},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done.status is JobStatus.SUCCEEDED
    assert fake.extract_calls[0]["max_tdoc_size_bytes"] is None
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `pytest tests/unit/test_job_worker.py -k "parse_tdoc_url_handler" -v`
Expected: most FAIL (the `auto_sync` flag and `tdoc_parse` settings are set on the test's `Settings` instance — verify each test fails because the handler doesn't yet consult those fields, or because the `_FakeTDocCrServiceForUrl.collect_3gpp_file_urls` call hasn't been invoked).

If the tests are passing before Step 4 is run, double-check the test assertions — particularly that the auto-sync tests are actually verifying `collect_calls` (which would be empty) and that the cancellation test is using a separate `JobWorker` instance.

- [ ] **Step 4: Verify the implementation handles the new cases (most should already pass from Task 2)**

Re-run: `pytest tests/unit/test_job_worker.py -k "parse_tdoc_url_handler" -v`
Expected: ALL PASS. (The Task 2 implementation already covers all of these cases — Task 3 is purely about pinning the behaviour with regression tests.)

- [ ] **Step 5: Lint**

Run: `ruff check tests/unit/test_job_worker.py`
Expected: no findings.

- [ ] **Step 6: Commit**

```bash
git add tests/unit/test_job_worker.py
git commit -m "test(jobs): cover parse_tdoc_url handler edge cases"
```

---

## Task 4: Add the `parse_tdoc_url` MCP tool

**Files:**
- Modify: `src/doc3gpp/web/mcp_server.py` (add imports at the top; add the tool after the existing `parse_tdocs` tool, around line 528)

**Interfaces:**
- Consumes:
  - `state` (the `WebState` injected into `build_mcp_server`)
  - `url` (str, required, must pass `is_3gpp_ftp_url`)
  - `recursive` (bool, default `False`)
  - `max_depth` (int, default `2`)
  - `force` (bool, default `False`)
  - `full` (bool, default `False`)
- Produces:
  - The standard `_enqueue` envelope: `{"job_id", "status", "message", "links"}` as a JSON string.
  - Raises `InvalidFilterError` for non-3GPP URLs and for `recursive=True, max_depth!=2` (mutex violation).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/web/test_mcp_server.py` (new file, modelled on the existing `test_mcp_end_to_end.py::_state_and_server` helper but for unit tests):

```python
"""Unit tests for the MCP server tool surface.

These are offline unit tests: no live 3GPP network, no FTS5 setup.
The ``build_mcp_server`` factory is invoked against a freshly built
``WebState`` per test (via ``build_state``), and tool calls are driven
through the in-process ``MCPServer.call_tool`` API.
"""
from __future__ import annotations

import asyncio
import json

import pytest


def _server():
    from doc3gpp.settings.loader import get_settings
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.web.app import build_state
    from doc3gpp.web.mcp_server import build_mcp_server

    create_schema()
    state = build_state(get_settings())
    server = build_mcp_server(state)
    return state, server


async def _call(server, name: str, args: dict):
    return await server.call_tool(name, args)


def test_parse_tdoc_url_rejects_non_3gpp_url(sqlite_env) -> None:
    """Non-3GPP URLs raise ``InvalidFilterError`` (clean MCP error, no job)."""
    _, server = _server()

    async def run():
        return await _call(
            server,
            "parse_tdoc_url",
            {"url": "https://example.com/bad.zip"},
        )

    result = asyncio.run(run())
    assert result.is_error is True
    assert "3GPP FTP" in result.content[0].text


def test_parse_tdoc_url_rejects_empty_url(sqlite_env) -> None:
    """Empty URL is treated as a non-3GPP URL → rejected."""
    _, server = _server()

    async def run():
        return await _call(server, "parse_tdoc_url", {"url": ""})

    result = asyncio.run(run())
    assert result.is_error is True


def test_parse_tdoc_url_rejects_recursive_with_explicit_max_depth(sqlite_env) -> None:
    """``recursive=True, max_depth=5`` → mutex violation, no job enqueued."""
    _, server = _server()

    async def run():
        return await _call(
            server,
            "parse_tdoc_url",
            {
                "url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/",
                "recursive": True,
                "max_depth": 5,
            },
        )

    result = asyncio.run(run())
    assert result.is_error is True
    assert "mutually exclusive" in result.content[0].text


def test_parse_tdoc_url_enqueues_with_all_defaults(sqlite_env) -> None:
    """Default flags → params carry ``max_depth=2``, ``recursive=False``."""
    state, server = _server()
    captured: list[tuple[object, dict]] = []
    real_create = state.services.job_repo.create

    def capturing_create(kind, params):
        job = real_create(kind, params)
        captured.append((kind, dict(params)))
        return job

    state.services.job_repo.create = capturing_create  # type: ignore[assignment]

    async def run():
        return await _call(
            server,
            "parse_tdoc_url",
            {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/"},
        )

    result = asyncio.run(run())
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "queued"
    kind, params = captured[0]
    from doc3gpp.models.jobs import JobKind
    assert kind is JobKind.PARSE_TDOC_URL
    assert params == {
        "url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/",
        "force": False,
        "full": False,
        "recursive": False,
        "max_depth": 2,
    }


def test_parse_tdoc_url_enqueues_recursive_without_max_depth(sqlite_env) -> None:
    """``recursive=True`` (with default ``max_depth=2``) drops ``max_depth`` from params."""
    state, server = _server()
    captured: list[tuple[object, dict]] = []
    real_create = state.services.job_repo.create

    def capturing_create(kind, params):
        job = real_create(kind, params)
        captured.append((kind, dict(params)))
        return job

    state.services.job_repo.create = capturing_create  # type: ignore[assignment]

    async def run():
        return await _call(
            server,
            "parse_tdoc_url",
            {
                "url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/",
                "recursive": True,
                "force": True,
                "full": True,
            },
        )

    result = asyncio.run(run())
    assert result.is_error is False
    kind, params = captured[0]
    from doc3gpp.models.jobs import JobKind
    assert kind is JobKind.PARSE_TDOC_URL
    assert "max_depth" not in params
    assert params == {
        "url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/",
        "force": True,
        "full": True,
        "recursive": True,
    }


def test_parse_tdoc_url_enqueues_explicit_max_depth(sqlite_env) -> None:
    """``max_depth=3`` is forwarded verbatim."""
    state, server = _server()
    captured: list[tuple[object, dict]] = []
    real_create = state.services.job_repo.create

    def capturing_create(kind, params):
        job = real_create(kind, params)
        captured.append((kind, dict(params)))
        return job

    state.services.job_repo.create = capturing_create  # type: ignore[assignment]

    async def run():
        return await _call(
            server,
            "parse_tdoc_url",
            {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/", "max_depth": 3},
        )

    result = asyncio.run(run())
    assert result.is_error is False
    _, params = captured[0]
    assert params["max_depth"] == 3
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/unit/web/test_mcp_server.py -v`
Expected: ALL FAIL with errors like `"parse_tdoc_url" is not a registered tool` (because the tool doesn't exist yet).

- [ ] **Step 3: Add the imports to `mcp_server.py`**

Edit `src/doc3gpp/web/mcp_server.py`:

- Add `from doc3gpp.parsers.direct_extractor import is_3gpp_ftp_url` to the imports (top of the file, after the existing `doc3gpp.web import`s).
- `InvalidFilterError` is already imported from `doc3gpp.web.errors` (line 28). No change needed.

- [ ] **Step 4: Add the tool**

Edit `src/doc3gpp/web/mcp_server.py` to add the new tool immediately after the existing `parse_tdocs` tool (which ends around line 528, right before the `rebuild_search_index` tool):

```python
    @server.tool(
        name="parse_tdoc_url",
        description=(
            "Enqueue a parse of a single TDoc file or a folder of TDoc files "
            "from a 3GPP FTP URL (https://www.3gpp.org/ftp/...). Mirrors "
            "`doc3gpp tdoc parse --from-url`. Use `recursive`/`max_depth` to "
            "scan subfolders; `force` re-parses already-cached files; `full` "
            "parses the TTCN corrections sub-parser. When "
            "Settings.sync.auto_sync is on, the worker runs the same "
            "TSG→meeting→tdoc-list auto-sync the CLI runs before parsing, so "
            "the cover-page FK on tdocs is satisfied. Returns a job_id; poll "
            "`get_job` for progress and `cancel_job` to abort."
        ),
    )
    @_mcp_error_guard
    def parse_tdoc_url(
        url: Annotated[str, Field(description="Absolute 3GPP FTP URL (https://www.3gpp.org/ftp/...). Single .docx/.zip file or a folder URL.")],
        recursive: Annotated[bool, Field(description="Scan subfolders (BFS); equivalent to CLI's --recursive. Mutually exclusive with max_depth.")] = False,
        max_depth: Annotated[int, Field(description="Maximum BFS depth (0 = root folder only). Ignored when recursive=True.")] = 2,
        force: Annotated[bool, Field(description="Re-parse even when a cover-page row already exists (forwarded to the service).")] = False,
        full: Annotated[bool, Field(description="Parse full content for TTCN corrections (forwarded to the service).")] = False,
    ) -> str:
        if not is_3gpp_ftp_url(url):
            raise InvalidFilterError(
                f"url must be a 3GPP FTP URL (https://www.3gpp.org/ftp/...); got {url!r}"
            )
        if recursive and max_depth != 2:
            raise InvalidFilterError(
                "recursive and max_depth are mutually exclusive; set one or the other"
            )
        params: dict[str, Any] = {
            "url": url,
            "force": force,
            "full": full,
            "recursive": recursive,
        }
        if not recursive:
            params["max_depth"] = max_depth
        return _enqueue(
            state,
            JobKind.PARSE_TDOC_URL,
            params,
            f"queued parse_tdoc_url for {url}",
        )
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `pytest tests/unit/web/test_mcp_server.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Lint**

Run: `ruff check src/doc3gpp/web/mcp_server.py tests/unit/web/test_mcp_server.py`
Expected: no findings.

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/web/mcp_server.py tests/unit/web/test_mcp_server.py
git commit -m "feat(mcp): parse_tdoc_url tool"
```

---

## Task 5: Update the MCP end-to-end parity test

**Files:**
- Modify: `tests/integration/test_mcp_end_to_end.py:62-86` (the `expected` set in `test_list_tools_exposes_read_and_job_tools`)

**Interfaces:** the parity test enumerates every tool name returned by `server.list_tools()`; the new tool must appear in the expected set.

- [ ] **Step 1: Add `"parse_tdoc_url"` to the expected set**

Edit `tests/integration/test_mcp_end_to_end.py` to add `"parse_tdoc_url"` between `"parse_tdocs"` and `"rebuild_search_index"` (alphabetical):

```python
        "parse_tdocs",
        "parse_tdoc_url",
        "rebuild_search_index",
```

- [ ] **Step 2: Run the parity test to verify it passes**

Run: `pytest tests/integration/test_mcp_end_to_end.py::test_list_tools_exposes_read_and_job_tools -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_mcp_end_to_end.py
git commit -m "test(mcp): assert parse_tdoc_url in registered tools"
```

---

## Task 6: Add the integration test for the full job pipeline

**Files:**
- Create: `tests/integration/test_parse_tdoc_url_job_sqlite.py`

**Interfaces:** mirrors the offline sqlite pattern used by `tests/integration/test_tdoc_parse_ftp_batch_sqlite.py` and `tests/integration/test_tdoc_parse_direct_sqlite.py`: it uses the `sqlite_env` fixture, builds a `WebState` via `build_state(get_settings())`, enqueues a real `PARSE_TDOC_URL` job via `state.services.job_repo.create`, runs the worker through `JobWorker._claim_and_run`, and asserts the terminal state and the result_summary.

The test must NOT hit the live 3GPP site — the service layer is patched to return a canned `DirectParseBatchResult` so no network call is made.

- [ ] **Step 1: Write the integration test**

Create `tests/integration/test_parse_tdoc_url_job_sqlite.py`:

```python
"""Integration test: a ``PARSE_TDOC_URL`` job is enqueued, the worker runs
the handler, and the result lands in the ``jobs`` row with the right
``result_summary`` + ``log_lines``.

Mirrors the offline-sqlite pattern used by the existing job tests. The
service layer is patched so no 3GPP network call is made.
"""
from __future__ import annotations

import asyncio

import pytest


def _make_state_and_worker():
    from doc3gpp.settings.loader import get_settings
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.web.app import build_state
    from doc3gpp.web.workers.job_worker import JobWorker

    create_schema()
    state = build_state(get_settings())
    worker = JobWorker(state, repo=state.services.job_repo)
    return state, worker


def _run_once(worker, repo, job) -> None:
    async def _claim() -> None:
        sem = asyncio.Semaphore(1)
        await worker._claim_and_run(job, sem)  # type: ignore[attr-defined]

    asyncio.run(_claim())


def test_parse_tdoc_url_job_end_to_end(sqlite_env, monkeypatch) -> None:
    """A 3GPP-URL ``PARSE_TDOC_URL`` job runs, succeeds, summary correct."""
    from doc3gpp.models.jobs import JobKind, JobStatus
    from doc3gpp.models.tdoc_cr import DirectParseBatchResult, DirectParseResult

    state, worker = _make_state_and_worker()

    fake_result = DirectParseBatchResult(
        results=[
            DirectParseResult(
                source_kind="url-3gpp",
                markdown="",
                details=None,
                extract_meta=None,
                from_cache=False,
                persisted=True,
                tdoc_id="R5-260001",
                tdoc_id_in_tdocs=True,
                source_url="https://www.3gpp.org/ftp/R5s260001.zip",
            ),
        ],
        failures={},
        skipped={},
    )

    def fake_extract_from_url_batch(
        url, *, max_depth, force, full, max_tdoc_size_bytes
    ):
        assert url.startswith("https://www.3gpp.org/ftp/")
        assert max_depth == 2  # default
        assert force is False
        assert full is False
        return fake_result

    monkeypatch.setattr(
        state.services.tdoc_cr,
        "extract_from_url_batch",
        fake_extract_from_url_batch,
    )
    monkeypatch.setattr(
        state.services.tdoc_cr,
        "collect_3gpp_file_urls",
        lambda url, *, max_depth: (),
    )
    state.settings.sync.auto_sync = False

    job = state.services.job_repo.create(
        JobKind.PARSE_TDOC_URL,
        {"url": "https://www.3gpp.org/ftp/TSG_RAN/WG5/", "max_depth": 2},
    )

    _run_once(worker, state.services.job_repo, job)

    done = state.services.job_repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.SUCCEEDED
    assert done.result_summary == {
        "requested": 1,
        "successes": 1,
        "failures": 0,
        "skipped": 0,
        "files": [
            {
                "tdoc_id": "R5-260001",
                "ftp_url": "https://www.3gpp.org/ftp/R5s260001.zip",
                "status": "ok",
            },
        ],
    }
    assert any("done:" in line for line in done.log_lines)
```

- [ ] **Step 2: Run the integration test**

Run: `pytest tests/integration/test_parse_tdoc_url_job_sqlite.py -v`
Expected: PASS.

- [ ] **Step 3: Lint**

Run: `ruff check tests/integration/test_parse_tdoc_url_job_sqlite.py`
Expected: no findings.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_parse_tdoc_url_job_sqlite.py
git commit -m "test(jobs): end-to-end integration for parse_tdoc_url"
```

---

## Task 7: Documentation sync

**Files:**
- Modify: `docs/cli.md`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/web-server.md`

**Interfaces:** each doc already enumerates the MCP tools or the job kinds; this task adds the new entry to each.

- [ ] **Step 1: Add the tool to `docs/cli.md`**

Find the MCP tool table in `docs/cli.md` and add a `parse_tdoc_url` row matching the existing `parse_tdocs` row's column structure. If `docs/cli.md` has a per-tool description block, add a new section that mirrors the `parse_tdocs` description block. The exact text should match the tool's `description` arg in `mcp_server.py:parse_tdoc_url`.

- [ ] **Step 2: Add the tool to `README.md`**

Find the MCP tools list and add `parse_tdoc_url` (alphabetic position, next to `parse_tdocs`).

- [ ] **Step 3: Update `AGENTS.md`**

- In the "Where to look" table, find the "Add a background job kind" row and append `parse_tdoc_url` to the example (e.g. "…e.g. `JobKind.PARSE_TDOC_URL`").
- In the "Architecture boundaries" / `web/` row, add a one-line note: "MCP `parse_tdoc_url` tool enqueues a `PARSE_TDOC_URL` job; see `src/doc3gpp/web/workers/handlers.py::_parse_tdoc_url` and `src/doc3gpp/web/mcp_server.py:parse_tdoc_url`."

- [ ] **Step 4: Update `docs/web-server.md`**

In the MCP section (if one exists), add a bullet: "`parse_tdoc_url` — enqueue a parse from a 3GPP FTP URL (file or folder)."

- [ ] **Step 5: Verify the docs render / link**

Run: `grep -n "parse_tdoc_url" docs/cli.md README.md AGENTS.md docs/web-server.md`
Expected: each file has at least one matching line.

- [ ] **Step 6: Commit**

```bash
git add docs/cli.md README.md AGENTS.md docs/web-server.md
git commit -m "docs: document MCP parse_tdoc_url tool"
```

---

## Task 8: Final verification

**Files:** none — verification only.

- [ ] **Step 1: Run the full offline test suite**

Run: `./scripts/test_sqlite.sh`
Expected: all tests pass (the script is offline-by-default and runs unit + integration sqlite tests). The `online` mark is opt-in via `pytest -m online` and is not part of this change.

- [ ] **Step 2: Lint**

Run: `ruff check .`
Expected: no findings.

- [ ] **Step 3: Type-check (if mypy is configured)**

Run: `python -m mypy src/doc3gpp 2>&1 | tail -20` (only if `mypy` is part of the `[dev]` extra; otherwise skip).
Expected: no new errors vs. baseline.

- [ ] **Step 4: Smoke-test the MCP tool registration**

Run:
```bash
python -c "
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.web.app import build_state
from doc3gpp.web.mcp_server import build_mcp_server
create_schema()
state = build_state(get_settings())
server = build_mcp_server(state)
import asyncio
tools = asyncio.run(server.list_tools())
names = sorted(t.name for t in tools)
print('parse_tdoc_url' in names)
"
```
Expected: prints `True`.

- [ ] **Step 5: Commit any uncommitted doc fixes**

If Step 1 / Step 2 surfaced any small fix-ups, commit them on the same branch.

---

## Self-review

1. **Spec coverage:** every spec requirement is mapped to a task:
   - Architecture (Section 1 of spec) → Tasks 2 + 4.
   - MCP tool signature (Section 2) → Task 4.
   - `JobKind` addition → Task 1.
   - Worker handler body (Section 2) → Task 2.
   - Mutex validation (`recursive` + `max_depth`) → Task 4.
   - Auto-sync on / off / failure modes → Task 3.
   - Cancellation → Task 3.
   - Per-file size cap → Task 3.
   - Per-file failure / skipped mapping → Task 2.
   - Defence-in-depth URL check → Task 2.
   - Test plan (Section 3 of spec) → Tasks 2–6.
   - Doc sync → Task 7.
2. **No placeholders:** every step has the actual code or command the engineer needs.
3. **Type / method-name consistency:** `JobKind.PARSE_TDOC_URL` defined Task 1 → consumed by Tasks 2 / 4. `services.tdoc_cr` (not `tdoc_sync_coordinator`) is the field name on `ServiceContainer` (verified at `web/state.py:147`); `trigger_auto_sync` is called with `meeting_service=services.meeting`, `tdoc_sync_coordinator=services.tdoc_sync` (matches the CLI's call shape). `extract_from_url_batch` is the service method, `collect_3gpp_file_urls` is the BFS helper used by `collect_tdoc_candidates_for_url`. `DirectParseResult` field names (`tdoc_id`, `source_url`, `persisted`, `from_cache`, `tdoc_id_in_tdocs`) match the dataclass in `models/tdoc_cr.py:291-348`. `DirectParseBatchResult(results, failures, skipped)` matches `models/tdoc_cr.py:351-365`.
4. **No "fill in later" / "TBD" / vague steps:** searched the plan, none found.
