# MCP: `parse_tdoc_url` — parse a TDoc from a 3GPP FTP URL

**Date:** 2026-08-13
**Status:** Approved design
**Branch:** new branch off `main` (e.g. `mcp-parse-tdoc-url`)

## Goal

Surface `doc3gpp tdoc parse --from-url <url>` over MCP as a new tool,
`parse_tdoc_url`, so an MCP client can hand the server a 3GPP FTP URL
(file or folder) and get the same per-file parse, cache write, and DB
upsert behaviour the CLI gets. The tool only accepts URLs that pass
`is_3gpp_ftp_url` — non-3GPP URLs are rejected at the tool boundary
before any job is enqueued. The HTTP web surface is **not** touched in
this change; it lands in a follow-up.

## Section 1 — Architecture

```
MCP client
   │
   ▼
build_mcp_server(state)
   │  registers: parse_tdocs, sync_*, ...,
   ▼  ──►  parse_tdoc_url  (NEW)
   │            │ url (str, required, must pass is_3gpp_ftp_url)
   │            │ recursive (bool, default False)
   │            │ max_depth (int, default 2)
   │            │ force (bool, default False)
   │            │ full (bool, default False)
   │            ▼
   │       _enqueue(state, JobKind.PARSE_TDOC_URL, params, msg)
   │            │
   │            ▼
   │       job_repo.create(PARSE_TDOC_URL, params)
   │            ▼
   │       state.jobs.enqueue(job_id) ──► JobWorkerHandle
   │
   ▼
web/workers/handlers.py
   │  KIND_TO_HANDLER[JobKind.PARSE_TDOC_URL] = _parse_tdoc_url  (NEW)
   ▼
_parse_tdoc_url(job, services, settings, *, progress, cancel_event)
   1. url = job.params["url"]; defence-in-depth re-check is_3gpp_ftp_url.
   2. resolve depth: recursive=True → max_depth=-1 (BFS until listing
      exhausted); else honour params["max_depth"] (default 2).
   3. if settings.sync.auto_sync:
        candidates = collect_tdoc_candidates_for_url(url, tdoc_service=...)
        trigger_auto_sync(auto_sync_enabled=True, meeting_service=...,
                          tdoc_sync_coordinator=..., tdoc_ids=candidates)
        (auto-sync failures → log warning, never abort the parse)
   4. max_tdoc_size_bytes = settings.tdoc_parse.max_tdoc_size_kb * 1024
      (0 = unlimited, same convention as the CLI).
   5. await asyncio.to_thread(
          services.tdoc_cr.extract_from_url_batch,
          url, max_depth=..., force=force, full=full,
          max_tdoc_size_bytes=max_tdoc_size_bytes or None,
      )
   6. progress("batch: N requested, M ok, F failed, S skipped")  per batch
   7. return {"requested", "successes", "failures", "skipped", "files": [...]}
```

No HTTP route is added in this change. The existing `mcp_end_to_end`
parity test only guards `?format=json` byte equality for surfaces that
have a matching HTTP route; we'll add the new tool to the registered-
tool-names assertion only, and defer byte-parity to when the HTTP
route lands.

### Touched files

- `src/doc3gpp/models/jobs.py` — add `JobKind.PARSE_TDOC_URL`.
- `src/doc3gpp/web/workers/handlers.py` — add `_parse_tdoc_url` and
  register in `KIND_TO_HANDLER`. Pulls in `collect_tdoc_candidates_for_url`
  and `trigger_auto_sync` from `doc3gpp.cli_auto_sync` (same imports
  the CLI uses; verified during implementation — fall back to a shared
  `services/` shim only if a cycle surfaces).
- `src/doc3gpp/web/mcp_server.py` — add `parse_tdoc_url` tool. Uses the
  existing `_enqueue` helper and `_mcp_error_guard` decorator; no new
  response envelope shape.
- `tests/unit/web/workers/test_handlers.py` — new unit tests for
  `_parse_tdoc_url` (URL rejection, depth resolution, auto-sync on/off,
  cancellation, per-file result mapping).
- `tests/unit/web/test_mcp_server.py` — new unit tests for the tool
  (URL rejection, mutex validation, `_enqueue` params).
- `tests/integration/test_mcp_end_to_end.py` — add `"parse_tdoc_url"`
  to the registered-tool-names assertion.
- `tests/integration/test_jobs.py` — new integration test that runs a
  `PARSE_TDOC_URL` job through the real `JobWorkerHandle` (mocked
  service) and asserts final state + progress lines.
- `docs/cli.md` — register the new tool in the MCP tools table.
- `README.md` — add to the MCP tools list.
- `AGENTS.md` — add a one-line note in the "Add a background job kind"
  row, plus a bullet under the "Add a web route" row pointing at the
  follow-up HTTP surface.

## Section 2 — Contracts

### MCP tool signature

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
    return _enqueue(state, JobKind.PARSE_TDOC_URL, params, f"queued parse_tdoc_url for {url}")
```

Notes:
- `is_3gpp_ftp_url` is imported from `doc3gpp.parsers.direct_extractor` —
  one source of truth for the URL allowlist, used by the CLI today.
- `recursive=True` → `max_depth` key is omitted from the params (worker
  defaults to BFS-until-exhausted via `max_depth=-1`).
- Mutex rule: `recursive` and `max_depth` are mutually exclusive. The
  tool uses `max_depth != 2` as the "user really set max_depth" signal
  (since `2` is the tool default); `recursive=True, max_depth=2` is
  therefore accepted as "user only set recursive" and the `max_depth`
  param is dropped from the enqueued `Job`. Any other combination of
  `recursive=True` with a non-default `max_depth` is rejected with
  `InvalidFilterError` (clean MCP error, no job enqueued).

### `JobKind.PARSE_TDOC_URL`

```python
class JobKind(str, Enum):
    ...
    PARSE_TDOCS = "parse_tdocs"
    PARSE_TDOC_URL = "parse_tdoc_url"  # NEW
    REBUILD_SEARCH = "rebuild_search"
    ...
```

No new fields on the `Job` model. The new kind inherits the same
progress / cancellation / failure semantics as `PARSE_TDOCS`.

### Worker handler

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
                    tdoc_sync_coordinator=services.tdoc_sync_coordinator,
                    tdoc_ids=candidates,
                )
            except Exception as exc:  # noqa: BLE001
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

`KIND_TO_HANDLER[JobKind.PARSE_TDOC_URL] = _parse_tdoc_url` is the only
registration change.

### Service-layer contract (unchanged)

`TDocCrService.extract_from_url_batch` already:
- detects file vs folder URLs (raises `NotAFolderError` for files;
  the worker does **not** need to special-case single-file URLs — the
  service's batch path treats them as a one-element list);
- writes `zips/<cache_file>` and `markdown/<cache_file>` for FK hits;
- upserts `tdoc_cr_cover_page` / `tdoc_extracts` rows;
- emits warnings (not failures) for FK misses;
- routes `TDocTooLargeError` to the `skipped` map.

The worker reuses it as-is — no service-layer changes.

### Error / status mapping

| Situation | Tool response | Job final state |
|---|---|---|
| URL fails `is_3gpp_ftp_url` (caught at the MCP boundary) | `InvalidFilterError` → `-32602` MCP error | n/a (no job created) |
| Job enqueued with non-3GPP URL (defence-in-depth, e.g. tampered DB) | n/a — the handler raises `ValueError`, the job worker marks the job FAILED | FAILED |
| Folder listing empty, no files matched | `parse_tdoc_url` returns job_id; handler progress: "no matching files found at the root level" (emitted by the service path) | SUCCEEDED with `requested=0` |
| Auto-sync enabled but TSG/tdoc-list sync fails | Warning logged, parse proceeds | SUCCEEDED (CLI parity) |
| One or more files fail to parse | `failures` map populated per file URL; job still SUCCEEDED | SUCCEEDED with non-zero `failures` count |
| `TDocTooLargeError` on any file | File goes to `skipped` map (service-layer behaviour) | SUCCEEDED with non-zero `skipped` count |
| `cancel_job` called mid-run | `asyncio.CancelledError` raised in worker → `JobStatus.CANCELLED` | CANCELLED |

Per-file failures and oversized files are **not** job failures; this
matches `extract_from_url_batch`'s contract and the CLI's
`_emit_url_batch_results` behaviour.

### Cache + DB writes (unchanged from CLI)

The service layer is the single source of truth. The worker calls
`extract_from_url_batch`, which calls `extract_from_url` per file,
which writes `zips/<cache_file>` and `markdown/<cache_file>` for FK
hits and upserts `tdoc_cr_cover_page` / `tdoc_extracts`. For FK
misses (no row in `tdocs` for the extracted id) the service emits a
warning and writes nothing — same as the CLI.

## Section 3 — Testing

### Unit: `tests/unit/web/workers/test_handlers.py::test_parse_tdoc_url`

- Rejects `Job` whose `params["url"]` fails `is_3gpp_ftp_url`
  (defence-in-depth, raises `ValueError`).
- `recursive=True` → `max_depth=-1` passed to service.
- `max_depth=None` in params (tool default) → `max_depth=2` passed.
- `max_depth=5` in params → `max_depth=5` passed.
- `settings.sync.auto_sync=True` + a non-empty candidate list →
  `trigger_auto_sync` invoked once with the candidate list; auto-sync
  raising does **not** abort the parse.
- `settings.sync.auto_sync=True` + empty candidate list → no
  `trigger_auto_sync` call.
- `settings.sync.auto_sync=False` → `trigger_auto_sync` is NOT invoked
  (the `collect_tdoc_candidates_for_url` helper is also not called).
- `cancel_event` set before service call → `asyncio.CancelledError`
  raised, no service call.
- `max_tdoc_size_bytes=0` → no cap forwarded to service; cap > 0 →
  cap forwarded as `kb * 1024`.
- Per-file results mapped to `files[]` with `{tdoc_id, ftp_url, status}`;
  `failures` count surfaced; `skipped` count surfaced; `status` is
  `"ok"` when `result.persisted` else `"parsed-no-fk"`.
- Result `tdoc_id is None` → skipped from `files[]` (synthetic local
  ids never reach the DB write path).

### Unit: `tests/unit/web/test_mcp_server.py::test_parse_tdoc_url_tool`

- Non-3GPP URL (`https://example.com/...`) → `InvalidFilterError`.
- Empty `url` → `InvalidFilterError`.
- `recursive=True` + `max_depth=5` → `InvalidFilterError` (mutex).
- `recursive=True` + `max_depth=2` (default) → accepted; `max_depth`
  key **omitted** from `params`.
- 3GPP URL with `force=True, full=True, recursive=True` →
  `_enqueue` called once with
  `JobKind.PARSE_TDOC_URL` and
  `params == {"url": ..., "force": True, "full": True, "recursive": True}`.
- 3GPP URL with `max_depth=3` only → `params["max_depth"] == 3`.
- 3GPP URL with all defaults → `params == {"url", "force": False,
  "full": False, "recursive": False, "max_depth": 2}`.

### Integration: `tests/integration/test_mcp_end_to_end.py`

- Add `"parse_tdoc_url"` to the registered-tool-names assertion so
  every registered tool is enumerated. The current parity test only
  checks the JSON-output tools; `parse_tdoc_url` is async-only and
  joins the same "registered tool names" set as `parse_tdocs`,
  `sync_meetings`, etc. No `?format=json` byte check is added (HTTP
  route is deferred).

### Integration: `tests/integration/test_jobs.py`

- New test that enqueues a `PARSE_TDOC_URL` job via `JobWorkerHandle`,
  runs the worker against the in-process SQLite test DB, and asserts:
  - Final status is SUCCEEDED for a mocked happy-path service call.
  - `progress` callback receives the "auto-sync" line when
    `auto_sync` is on.
  - `progress` callback receives the "done" line with correct counts.
- Service layer is patched so no FTP request goes out.

### Docs sync (per `docs/conventions.md` §"Documentation sync")

- `docs/cli.md` — append `parse_tdoc_url` MCP tool entry to the MCP
  tools table.
- `README.md` — add to the MCP tools list.
- `AGENTS.md` — add a one-line note under the "Add a background job
  kind" / "Add an MCP tool" conventions, plus a bullet in the "Add a
  web route" row pointing at the follow-up HTTP surface.
- `docs/web-server.md` — mention the new tool in the MCP tools
  section if one exists.

### Test data / mocking strategy

- Handler unit tests mock `services.tdoc_cr.extract_from_url_batch` to
  return a `DirectParseBatchResult` with two results (one persisted,
  one FK-miss), one failure, one skipped — no real network, no real
  DB.
- MCP tool unit tests use the existing fake `WebState` fixture and
  patch `state.services.job_repo.create` to capture the
  `JobKind` / `params` pair.
- Integration test for the job worker uses the in-process SQLite test
  DB and a real `JobWorkerHandle`; the service layer is patched so no
  FTP request goes out.
- The existing `mcp_end_to_end` parity test stays online-only because
  it exercises the live `MCPServer` mount; we don't add a 3GPP network
  call there.

### Anti-patterns to avoid (per `docs/conventions.md`)

- Don't branch the existing `_parse_tdocs` handler on "URL vs filter"
  — keep the two handlers independent.
- Don't add an HTTP route in this change (per the user's "later" —
  keeps the diff focused).
- Don't re-implement the URL classifier (`is_3gpp_ftp_url`) — import
  from `parsers.direct_extractor`.
- Don't bypass `_mcp_error_guard` — every new tool must use it so
  domain errors get the right `-32xxx` codes.
- Don't change the response shape of `_enqueue` — keep the same
  `{job_id, status, message, links}` envelope.

### Scope check

Single implementation plan, single PR. Two edited files in `src/`
plus one new enum member; two new test cases in existing test files
plus a small assertion addition to the parity test; four doc
touch-ups. The CLI's `--from-url` path is untouched — we re-use
`TDocCrService.extract_from_url_batch` as the single source of truth.
