# Web Server + MCP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single-port HTTP server to `doc3gpp` for browsing
meetings, TDocs, TSGs and WIs from a browser (HTMX + Jinja2) and a
Streamable HTTP MCP sub-app exposing the same capabilities to AI
clients, all without duplicating any business logic — the web layer
is a thin adapter over the existing `services/` factories.

**Architecture:** A new `src/doc3gpp/web/` package owns the
FastAPI app factory (`app.py`), the MCP sub-app mount (`mcp_server.py`),
the background job worker (`workers/job_worker.py` + `workers/handlers.py`),
the read routes (`routes/*.py`), the Jinja2 templates (`templates/`),
and the static HTMX bundle (`static/`). A new `Job` domain dataclass +
ORM model + `JobRepository` Protocol + `SQLAlchemyJobRepository` add
long-running job persistence to the existing storage layer. The new
`server_app` Typer sub-app (`cli.py` + `cli_server.py`) exposes
`doc3gpp server start / stop / status / logs / install / uninstall`,
with a launchd / systemd install helper that bakes the current
`doc3gpp.toml` + env into the generated unit so a service restart
picks up no surprises. The CLI / HTTP / MCP surfaces all reuse the
shared `_apply_text_filter` / `_apply_date_filter` /
`validate_date_filter` / `parse_tdoc_id` helpers from
`cli_filters.py` + `tdoc_sql.py`, so the filter grammar stays
identical across every entry point.

**Tech Stack:** Python 3.10+, FastAPI, uvicorn[standard],
Jinja2, HTMX 2.x, mcp (PyPI package, Streamable HTTP transport,
`stateless_http=True`), markdown-it-py, Pygments, humanfriendly
(duration parsing for retention). All new runtime deps land in a
new `[web]` pyproject extra so existing CLI users stay unaffected.

## Global Constraints

- Match existing `slots=True, frozen=True` dataclass style.
- Strict layered architecture: `web/` → `services/` → `repository/` Protocols
  → SQL repos. **No ORM leaks** past `storage/repositories/`; web routes
  receive domain dataclasses only.
- `[server].enabled=false` and `[mcp].enabled=true` by default. When
  `[server].enabled=false` the `doc3gpp server` commands raise a
  `click.UsageError` with a hint to set `server.enabled = true`.
  `[mcp].enabled` gates only the `/mcp` sub-app mount; HTTP routes
  always load when `[server].enabled=true`.
- HTTP JSON response bytes and MCP tool result bytes MUST be
  identical for the same read — `tests/integration/test_web_end_to_end.py`
  cross-checks every read tool.
- Reuse existing CLI filter grammar; never re-implement the parser.
- Reuse `cache.dir/{zips,markdown}/` for the cache subtree; introduce
  `[server].cache_subdir` (`null` = shared with CLI; `"web"` =
  isolated `cache.dir/web/{zips,markdown}/`). Default `null` so the
  web server reuses already-downloaded zips/markdown.
- Settings knobs follow the existing nested-sub-model shape
  (`Settings.server`, `Settings.mcp`) and TOML-only precedence (no
  env overrides).
- Branch: `web-and-mcp-server`. Commit message style: `feat(scope): …`
  / `test(scope): …` / `docs(scope): …` matching the existing log.
- DB migration: zero-touch — `Base.metadata.create_all` picks up the
  new `jobs` table on next init.
- FastMCP mounted at `/mcp` via `app.mount("/mcp",
  mcp.streamable_http_app())`. Use `stateless_http=True` so each
  request is independent and the worker can run inline without
  per-session state.
- Background job worker runs as an asyncio task inside the FastAPI
  lifespan; `Settings.server.max_concurrent_jobs` defaults to `1`;
  cancellation is cooperative (a `_cancel_event` checked between
  handler iterations).
- SSE channel for job progress: bounded `asyncio.Queue[str]`
  (default size 100, drop oldest) per job to avoid unbounded memory.
- `[server].log_retention` parsed via `humanfriendly.parse_timespan`
  (e.g. `"7d"` → 7 days). Cleanup runs at startup + every
  `Settings.server.cleanup_interval_seconds`.
- Install helper writes systemd user unit by default; `--system`
  uses `/etc/systemd/system/`; macOS users get a launchd plist under
  `~/Library/LaunchAgents/`. Rendered files include
  `X-Doc3gpp-Managed=true` marker so uninstall is fail-safe.
- CLI server commands require `[server].enabled=true`; the install
  helper writes the resolved config snapshot into the unit so the
  service picks up the same TOML path + env vars at boot.

---

## File Structure

| Path | Role | Task |
| --- | --- | --- |
| `src/doc3gpp/settings/schema.py` (extend) | `ServerSettings`, `MCPSettings`, registered in `Settings` | T1 |
| `doc3gpp.toml.example` (extend) | `[server]` and `[mcp]` blocks with defaults + comments | T1 |
| `pyproject.toml` (extend) | New `[web]` extra (fastapi, uvicorn[standard], jinja2, mcp, markdown-it-py, pygments, humanfriendly) | T1 |
| `src/doc3gpp/models/jobs.py` (new) | `Job` dataclass + `JobStatus` + `JobKind` enums | T3 |
| `src/doc3gpp/repository/protocols.py` (extend) | `JobRepository` Protocol | T3 |
| `src/doc3gpp/storage/db/models.py` (extend) | `JobORM` model + `jobs` table | T3 |
| `src/doc3gpp/storage/repositories/jobs_sql.py` (new) | `SQLAlchemyJobRepository` | T3 |
| `src/doc3gpp/web/__init__.py` (new) | Public exports | T4 |
| `src/doc3gpp/web/app.py` (new) | `build_app(settings) -> FastAPI` factory + lifespan + router + MCP mount | T4 |
| `src/doc3gpp/web/errors.py` (new) | `map_domain_error` + `register_error_handlers(app)` | T4 |
| `src/doc3gpp/web/deps.py` (new) | `RequestState` + service factories (`get_meeting_service`, etc.) | T4 |
| `src/doc3gpp/web/filters.py` (new) | HTTP query-param parser reusing `_apply_text_filter` / `_apply_date_filter` / `parse_tdoc_id` / `validate_date_filter` | T5 |
| `src/doc3gpp/models/tdoc_show.py` (new) | `TDocShowRecord.from_tdoc_id` + `TDocShowRecordByUrl.from_url` factory classmethods (moved out of `cli.py`) | T6 |
| `src/doc3gpp/web/render.py` (new) | `to_jsonable(obj)` shared between HTTP + MCP | T9 |
| `src/doc3gpp/web/routes/__init__.py` (new) | Re-export routers | T6 |
| `src/doc3gpp/web/routes/landing.py` (new) | `GET /` | T6 |
| `src/doc3gpp/web/routes/meetings.py` (new) | `GET /meetings`, `GET /meetings/{id}` | T6 |
| `src/doc3gpp/web/routes/tdocs.py` (new) | `GET /tdocs`, `GET /tdocs/{id}`, `GET /tdocs/{id}/content` | T6 |
| `src/doc3gpp/web/routes/tsgs.py` (new) | `GET /tsgs`, `GET /tsgs/{short_name}` | T6 |
| `src/doc3gpp/web/routes/wis.py` (new) | `GET /wis` | T6 |
| `src/doc3gpp/web/routes/search.py` (new) | `GET /search`, `GET /search/sem` | T6 |
| `src/doc3gpp/web/routes/jobs.py` (new) | `POST /jobs/*`, `GET /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/events`, `POST /jobs/{id}/cancel` | T8 |
| `src/doc3gpp/web/mcp_server.py` (new) | `build_mcp_server(state) -> FastMCP` + tool registrations | T9 |
| `src/doc3gpp/web/workers/__init__.py` (new) | Re-exports | T7 |
| `src/doc3gpp/web/workers/job_worker.py` (new) | asyncio worker + cancellation + cleanup | T7 |
| `src/doc3gpp/web/workers/handlers.py` (new) | per-`JobKind` handler dispatch + per-job queues | T7 |
| `src/doc3gpp/web/templates/*.html` (new) | Jinja2 templates + partials | T6 |
| `src/doc3gpp/web/static/htmx.min.js` (new) | vendored HTMX 2.x bundle | T6 |
| `src/doc3gpp/web/static/style.css` (new) | minimal stylesheet | T6 |
| `src/doc3gpp/web/install.py` (new) | `render_systemd_unit`, `render_launchd_plist`, `install_systemd`, `install_launchd`, `uninstall_*` | T10 |
| `src/doc3gpp/cli.py` (extend) | Register `server_app` sub-app + lazy import; switch `_tdoc_show_command` to call `TDocShowRecord.from_tdoc_id` from `models/tdoc_show.py` | T2, T6 |
| `src/doc3gpp/cli_server.py` (new) | `server start / stop / status / logs / install / uninstall` (stub first, real impl in T11) | T2, T11 |
| `tests/unit/test_server_settings.py` (new) | Settings schema defaults + parse | T1 |
| `tests/unit/test_web_filters.py` (new) | HTTP filter parser | T5 |
| `tests/unit/test_web_errors.py` (new) | `map_domain_error` + JSON shape | T4 |
| `tests/unit/test_jobs_sql.py` (new) | `SQLAlchemyJobRepository` against sqlite in-memory | T3 |
| `tests/unit/test_web_routes.py` (new) | Route unit tests with mock services | T6 |
| `tests/unit/test_job_worker.py` (new) | Worker + handlers against in-memory repos | T7 |
| `tests/unit/test_web_install.py` (new) | Render + install/uninstall unit tests with `tmp_path` | T10 |
| `tests/integration/test_web_end_to_end.py` (new) | FastAPI `TestClient` end-to-end + JSON byte parity | T12 |
| `tests/integration/test_mcp_end_to_end.py` (new) | MCP `StreamableHttpTransport` end-to-end + parity | T12 |
| `tests/integration/test_cli_server.py` (new) | `doc3gpp server install/start/stop/status/uninstall` against sqlite | T11 |
| `docs/web-server.md` (new) | End-user guide (install, start, browse, MCP, jobs) | T12 |
| `docs/cli.md` (extend) | `server` sub-app reference | T12 |
| `docs/architecture.md` (extend) | New web layer + job subsystem + MCP + workflow bullets | T12 |
| `docs/code-map.md` (extend) | New file rows | T12 |
| `AGENTS.md` (extend) | New "Where to look" rows | T12 |
| `README.md` (extend) | Web/MCP quick-start section | T12 |
| `doc3gpp.toml.example` (extend) | `[server]` and `[mcp]` blocks | T1 |
| `scripts/dev_run.sh` (extend) | Optional `doc3gpp server start --dev` smoke | T12 |

---

## Task 1: Settings + pyproject extra + TOML example

**Files:**
- Extend: `src/doc3gpp/settings/schema.py`
- Extend: `doc3gpp.toml.example`
- Extend: `pyproject.toml`
- Create: `tests/unit/test_server_settings.py`

**Interfaces:**
- `class ServerSettings(BaseModel)` with fields:
  - `enabled: bool = False`
  - `host: str = "127.0.0.1"`
  - `port: int = 8765`
  - `max_concurrent_jobs: int = Field(default=1, ge=1, le=16)`
  - `cleanup_interval_seconds: int = Field(default=300, ge=10)`
  - `log_retention: str = "7d"` (`@field_validator` accepts `humanfriendly.parse_timespan` format)
  - `cache_subdir: str | None = None`
  - `pid_file: str | None = None` (`None` → `{cache.dir}/server.pid`)
  - `log_file: str | None = None` (`None` → `{cache.dir}/server.log`)
- `class MCPSettings(BaseModel)` with fields:
  - `enabled: bool = True` (effective only when `ServerSettings.enabled=true`)
  - `transport: Literal["streamable_http"] = "streamable_http"`
  - `sse_queue_size: int = Field(default=100, ge=10)`
- Add to `Settings`: `server: ServerSettings = ServerSettings()` + `mcp: MCPSettings = MCPSettings()`.
- TOML `[server]` and `[mcp]` blocks in `doc3gpp.toml.example` with the same defaults + comment headers.

**Steps:**
1. Add `ServerSettings` and `MCPSettings` to `settings/schema.py`. Re-export from the same `__all__`.
2. Add `ServerSettings` and `MCPSettings` to the closed `DOC3GPP_*` env allowlist (next-field error if any other env tries to override; the existing pydantic-settings model-env behaviour matches the rest of the codebase).
3. Append a `[server]` and `[mcp]` block to `doc3gpp.toml.example` (commented-out defaults).
4. Add `[web]` extra to `pyproject.toml`:
   ```toml
   web = [
     "fastapi>=0.115.0",
     "uvicorn[standard]>=0.30.0",
     "jinja2>=3.1.4",
     "mcp>=1.0.0",
     "markdown-it-py>=3.0.0",
     "pygments>=2.18.0",
     "humanfriendly>=10.0",
   ]
   ```
   Add `doc3gpp[web]` to `[all]`.
5. Tests (`tests/unit/test_server_settings.py`):
   - `test_server_defaults`: `Settings().server.host == "127.0.0.1"`, `.port == 8765`, `.enabled is False`.
   - `test_mcp_defaults`: `Settings().mcp.enabled is True`, `.transport == "streamable_http"`.
   - `test_server_overrides`: feed a TOML fixture into `Settings.from_toml(...)`; assert `enabled`, `port`, `cache_subdir`, `log_retention` parsed.
   - `test_log_retention_invalid`: invalid retention raises `ValidationError`.
   - `test_max_concurrent_jobs_bounds`: `0` and `17` raise.

**Verification:**
```bash
ruff check .
./scripts/test_sqlite.sh -k server_settings
```

**Commit:** `feat(settings): add server + mcp nested settings with [web] extra`

---

## Task 2: CLI scaffold — `server_app` Typer sub-app + stubs

**Files:**
- Extend: `src/doc3gpp/cli.py`
- Create: `src/doc3gpp/cli_server.py`
- Create: `tests/unit/test_cli_server_stubs.py`

**Interfaces:**
- `src/doc3gpp/cli_server.py` exports `server_app = typer.Typer(...)` with stubs matching the spec's command surface:
  - `start(host: str = typer.Option(None), port: int = typer.Option(None), open: bool = typer.Option(False, "--open"), reload: bool = typer.Option(False, "--reload"), no_open: bool = typer.Option(False, "--no-open"))`
  - `stop()`
  - `status()`
  - `logs(job: str = typer.Option(None, "--job"), follow: bool = typer.Option(False, "-f"))`
  - `install(target: str = typer.Argument(..., help="systemd | launchd"), scope: str = typer.Option("user", "--user/--system"), no_start: bool = typer.Option(False, "--no-start"), dry_run: bool = typer.Option(False, "--dry-run"))`
  - `uninstall(target: str = typer.Argument(..., help="systemd | launchd"), scope: str = typer.Option("user", "--user/--system"))`
- Each stub raises `NotImplementedError("task N")` until the implementation tasks land.
- In `cli.py` add `from doc3gpp.cli_server import server_app` (lazy) and register it via `app.add_typer(server_app, name="server", ...)`.

**Steps:**
1. Create `src/doc3gpp/cli_server.py` with the stub functions and `server_app = typer.Typer(...)`.
2. Wire into `cli.py` after the existing Typer sub-apps (order: config, meeting, tdoc, wi, search, db, server).
3. Add a guard helper `_require_server_enabled(settings)` that raises `click.UsageError` with the "set `[server] enabled = true`" hint when `Settings.server.enabled is False`; use it as the first line of every server subcommand (T11 keeps this contract).
4. Tests (`tests/unit/test_cli_server_stubs.py`):
   - `test_subcommands_registered`: invoke `doc3gpp server --help`; assert every subcommand name appears.
   - `test_start_stub_raises`: `runner.invoke(server_app, ["start"])` returns a non-zero exit code with "task N" or "NotImplementedError".
   - `test_install_stub_raises`: same for `install`.

**Verification:**
```bash
ruff check .
doc3gpp server --help
doc3gpp server start --help
./scripts/test_sqlite.sh -k cli_server_stubs
```

**Commit:** `feat(cli): scaffold server_app Typer sub-app with start/stop/status/logs/install/uninstall stubs`

---

## Task 3: Job domain + ORM + Protocol + SQL repo

**Files:**
- Create: `src/doc3gpp/models/jobs.py`
- Extend: `src/doc3gpp/repository/protocols.py`
- Extend: `src/doc3gpp/storage/db/models.py`
- Create: `src/doc3gpp/storage/repositories/jobs_sql.py`
- Create: `tests/unit/test_jobs_sql.py`

**Interfaces:**
- `class JobKind(str, Enum)`: `SYNC_MEETINGS`, `SYNC_TDOCS`, `SYNC_TDOCS_ALL`, `PARSE_TDOCS`, `REBUILD_SEARCH`, `CACHE_PURGE`. String values match the spec's URL slugs (e.g. `SYNC_MEETINGS.value == "sync_meetings"`).
- `class JobStatus(str, Enum)`: `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`. String values match the spec contract (`QUEUED.value == "queued"`, etc.).
- `@dataclass(slots=True, frozen=True) class Job` with fields:
  - `id: str` (UUID4 hex), `kind: JobKind`, `status: JobStatus`, `params: Mapping[str, JSONValue]`, `log_lines: tuple[str, ...]` (recent log lines, capped at 50 for the `log_tail` preview), `result_summary: Mapping[str, JSONValue] | None`, `error: str | None`, `created_at: datetime`, `started_at: datetime | None`, `finished_at: datetime | None`.
- `class JobRepository(Protocol)` with:
  - `def create(self, kind: JobKind, params: Mapping[str, JSONValue]) -> Job`
  - `def get(self, job_id: str) -> Job | None`
  - `def list(self, *, limit: int = 50, status: JobStatus | None = None) -> list[Job]`
  - `def mark_running(self, job_id: str, *, message: str = "starting") -> Job`
  - `def append_log(self, job_id: str, *, line: str) -> None`
  - `def mark_succeeded(self, job_id: str, *, summary: Mapping[str, JSONValue]) -> Job`
  - `def mark_failed(self, job_id: str, *, error: str) -> Job`
  - `def mark_cancelled(self, job_id: str) -> Job`
  - `def delete_older_than(self, cutoff: datetime) -> int`
- `class JobORM(Base)` table `jobs`:
  - `job_id TEXT PK` (UUID4), `kind TEXT NOT NULL`, `status TEXT NOT NULL`, `params JSON NOT NULL`, `log_lines JSON NOT NULL DEFAULT '[]'` (newline-delimited string list), `result_summary JSON NULL`, `error TEXT NULL`, `created_at DATETIME NOT NULL`, `started_at DATETIME NULL`, `finished_at DATETIME NULL`.
  - Composite indexes: `(status, created_at DESC)`, `(kind, created_at DESC)`.
- `class SQLAlchemyJobRepository` implementing `JobRepository` over the `JobORM` row → `Job` dataclass mapping. `log_lines` round-trips as `list[str]` (capped at 50 with FIFO eviction). Use the existing `create_schema` flow (no Alembic).

**Steps:**
1. Define `JobKind`, `JobStatus`, `Job` in `models/jobs.py`.
2. Add `JobORM` to `storage/db/models.py`; verify it is picked up by `Base.metadata.create_all`.
3. Add `JobRepository` Protocol to `repository/protocols.py` (last entry, alphabetical order preserved).
4. Implement `SQLAlchemyJobRepository` in `storage/repositories/jobs_sql.py`. `params` and `result_summary` round-trip via `json.dumps` / `json.loads`; `datetime` fields stored as ISO strings.
5. Tests (`tests/unit/test_jobs_sql.py`):
   - Use the existing in-memory sqlite fixture from `tests/conftest.py`.
   - `test_create_returns_id`: `create(...)` returns a `Job` with a UUID4 `id`; `get(id)` round-trip.
   - `test_mark_running_sets_started_at`: `mark_running` populates `started_at`.
   - `test_append_log_caps_at_50`: append 60 lines; `get(...).log_lines` has exactly the last 50.
   - `test_mark_succeeded_sets_finished_at_and_summary`.
   - `test_mark_failed_sets_error`.
   - `test_list_filters_by_status`.
   - `test_delete_older_than` removes old `SUCCEEDED`/`FAILED`/`CANCELLED` rows, leaves newer ones.

**Verification:**
```bash
ruff check .
./scripts/test_sqlite.sh -k jobs_sql
```

**Commit:** `feat(jobs): add Job domain dataclass, ORM model, repository protocol + SQL impl`

---

## Task 4: FastAPI app skeleton + error mapping + dependencies

**Files:**
- Create: `src/doc3gpp/web/__init__.py`
- Create: `src/doc3gpp/web/app.py`
- Create: `src/doc3gpp/web/errors.py`
- Create: `src/doc3gpp/web/deps.py`
- Create: `tests/unit/test_web_app.py`
- Create: `tests/unit/test_web_errors.py`

**Interfaces:**
- `class WebState` (dataclass) holds:
  - `settings: Settings`
  - `engine: Engine`
  - `services: ServiceContainer` (build via `services/factory.build_all`)
  - `jobs: JobWorkerHandle` (placeholder; created by lifespan in T7)
- `def build_app(settings: Settings) -> FastAPI` factory:
  - Lifespan: open engine, build `WebState`, store on `app.state.web`.
  - Mount routers (T6) under their `/...` paths.
  - When `Settings.mcp.enabled and Settings.server.enabled`: mount `mcp.streamable_http_app()` at `/mcp` (T9 supplies the actual `FastMCP`).
  - Register exception handlers from `web/errors.py`.
  - `GET /healthz` route in this same task: returns `{"ok": true}` (used by `server start` in T11 to poll readiness).
- `def map_domain_error(exc: Exception) -> JSONResponse` mapping:
  - `TDocNotFoundError` / `MeetingNotFoundError` / `TSGNotFoundError` / `WINotFoundError` → 404.
  - `InvalidFilterError` → 400.
  - `JobNotFoundError` → 404.
  - `JobAlreadyTerminalError` → 409.
  - `SettingsDisabledError` (a new exception in `web/errors.py`) → 503.
  - `httpx.HTTPError` → 502.
  - Generic `Exception` → 500 with `request_id` correlation id.
- `def register_error_handlers(app: FastAPI) -> None` registers `app.exception_handler(...)` for each.
- `web/deps.py` exposes:
  - `def get_state(request: Request) -> WebState`
  - `def get_settings(request: Request) -> Settings`
  - `def get_engine(request: Request) -> Engine`
  - `def get_meeting_service(request: Request) -> MeetingService` (and equivalents for `tdoc_service`, `tdoc_cr_service`, `wi_service`, `search_service`, `semantic_search_service`, `tdoc_file_repo`, `job_repo`).

**Steps:**
1. Define `WebState` in `web/app.py`.
2. Implement `build_app` with lifespan stub (job worker creation in T7, MCP mount in T9).
3. Implement `map_domain_error` and `register_error_handlers`.
4. Implement `web/deps.py` with the dependency helpers; reuse `services/factory.build_*` so no business logic is duplicated.
5. Tests:
   - `tests/unit/test_web_errors.py`: `map_domain_error(TDocNotFoundError("foo"))` → status 404, body `{"error": "tdoc_not_found", "detail": "foo"}`.
   - `tests/unit/test_web_app.py`: `build_app(Settings())` returns a `FastAPI` instance with `app.state.web.settings.server.enabled is False`; `GET /` returns a 200 with the landing template placeholder (T6 fills the template).

**Verification:**
```bash
ruff check .
./scripts/test_sqlite.sh -k web_app -k web_errors
```

**Commit:** `feat(web): add FastAPI app factory with error handlers and dependency helpers`

---

## Task 5: HTTP filter parser

**Files:**
- Create: `src/doc3gpp/web/filters.py`
- Create: `tests/unit/test_web_filters.py`

**Interfaces:**
- `def parse_text_query(raw: str | None) -> str | None` — passes through to `_apply_text_filter` semantics; `None` → `None`, `"null"` → `"null"`, `"not-null"` → `"not-null"`, `"!foo"` → `"!foo"`, anything else → unchanged.
- `def parse_date_query(raw: str | None) -> str | None` — passes through to `_apply_date_filter` semantics; uses `validate_date_filter` to raise `InvalidFilterError` on bad tokens.
- `def parse_bool_query(raw: str | None) -> bool | None` — `"true"` / `"false"` / `None`; other values raise `InvalidFilterError`.
- `def parse_int_query(raw: str | None, *, min: int | None = None, max: int | None = None) -> int | None` — integer parsing with optional bounds; raises `InvalidFilterError`.
- `def parse_tdoc_id_query(raw: str) -> tuple[str, int]` — wraps the existing `parse_tdoc_id` from `cli_filters.py`; raises `InvalidFilterError` on bad input.
- `class InvalidFilterError(ValueError)` in `web/errors.py`.

**Steps:**
1. Implement the helpers in `web/filters.py`; import `_apply_text_filter` / `_apply_date_filter` from `storage/repositories/tdoc_sql.py` (logic parity), and `parse_tdoc_id` / `validate_date_filter` from `cli_filters.py`.
2. Add `InvalidFilterError` to `web/errors.py`.
3. Tests (`tests/unit/test_web_filters.py`):
   - `parse_text_query(None)` → `None`; `"!foo"` → `"!foo"`; `"null"` → `"null"`.
   - `parse_date_query(">= '2026-01-01'")` round-trip; `parse_date_query("oops")` raises `InvalidFilterError`.
   - `parse_bool_query("true")` → `True`; `"maybe"` raises.
   - `parse_int_query("42")` → `42`; bound `min=10, max=50` rejects `5` and `60`.
   - `parse_tdoc_id_query("R5-123456r2")` → `("R5-123456", 2)`.

**Verification:**
```bash
ruff check .
./scripts/test_sqlite.sh -k web_filters
```

**Commit:** `feat(web): add HTTP query-param filter parser reusing shared CLI helpers`

---

## Task 6: Read routes + Jinja2 templates + static

**Files:**
- Create: `src/doc3gpp/web/routes/__init__.py`, `landing.py`, `meetings.py`, `tdocs.py`, `tsgs.py`, `wis.py`, `search.py`
- Create: `src/doc3gpp/web/templates/base.html`, `landing.html`, `meeting_list.html`, `meeting_show.html`, `tdoc_list.html`, `tdoc_show.html`, `tdoc_content.html`, `tsg_list.html`, `tsg_show.html`, `wi_list.html`, `search_results.html`, `partials/job_status.html`, `partials/search_form.html`, `partials/tdoc_filters.html`
- Create: `src/doc3gpp/web/static/htmx.min.js`, `style.css`
- Extend: `src/doc3gpp/web/app.py` (mount routers)
- Create: `tests/unit/test_web_routes.py`

**Interfaces:**
- Each route module exports an `APIRouter` with the appropriate prefix + tags.
- `landing.py`: `GET /` → `TemplateResponse("landing.html", ...)` with `{"sections": [...]}`. Sections list comes from a constant table: "Meetings", "TDocs", "TSGs", "WIs", "Search", "Jobs". Each is a hyperlink to the list page.
- `meetings.py`:
  - `GET /meetings` → query-params: `tsg`, `year`, `start_after`, `start_before`, `location`, `limit` (default 50). Reuses `MeetingRepository.list(...)`. Renders `meeting_list.html` with rows + filters form.
  - `GET /meetings/{meeting_id}` → resolves the meeting, renders `meeting_show.html` (includes "Sync this meeting" button that POSTs `/jobs/sync_tdocs` with `meeting_id` param).
  - JSON: when `?format=json` is set, returns the underlying list/DTO as JSON; otherwise HTML.
- `tdocs.py`:
  - `GET /tdocs` → `TDocRepository.list(...)` reusing `_apply_text_filter` / `_apply_date_filter` semantics on `meeting_id`, `tdoc_id`, `agenda_item`, `type`, `source`, `for`, `against`, `decision`, `work_item`, `version` and the date filters `start_after` / `start_before`. Renders `tdoc_list.html` with pagination (`limit`, `offset`).
  - `GET /tdocs/{tdoc_id}` → resolves the TDoc; builds a `TDocShowRecord` using the same composition as `cli.py:3707-3734` (resolve cover + ttcn + changes + extracted_at + files via the corresponding repositories). Prefer factoring a `TDocShowRecord.from_tdoc_id(tdoc_id, repos)` classmethod into `models/tdoc_show.py` (move `TDocShowRecord` + `TDocShowRecordByUrl` out of `cli.py`); CLI's `_tdoc_show_command` then calls the same classmethod so HTTP and CLI JSON stay identical. Renders `tdoc_show.html` with sections: `cover`, optional `ttcn`, `extracted_at`, `files` placeholder. Cache miss on cover/TTCN → still renders the metadata with a "Not yet extracted" placeholder.
  - `GET /tdocs/{tdoc_id}/content?format=markdown|html`:
    - `format=markdown`: returns raw bytes from `{cache.dir}/markdown/{derive_cache_file(tdoc.ftp_url)}` if present (404 + hint `doc3gpp tdoc parse --tdoc <id>` otherwise).
    - `format=html`: parses markdown via `markdown_it_py` + `Pygments` (renderer extends `MarkdownIt` with `fence` plugin) and renders `tdoc_content.html`.
  - JSON: `?format=json` returns `TDocShowRecord.to_dict()` via `render.to_jsonable`.
- `tsgs.py`:
  - `GET /tsgs` → all TSGs (single SQL query, small table). `GET /tsgs/{short_name}` → TSG + nested list of meetings.
- `wis.py`:
  - `GET /wis` → `WiRepository.list(...)` with `tsg`, `name`, `id` filters; pagination.
- `search.py`:
  - `GET /search` → `SearchService.search(query, filters)` reusing the same filter grammar; renders `search_results.html` with snippets + scores + filters form.
  - `GET /search/sem` → `SemanticSearchService.search(...)` with `?fts5_query=` opt-in FTS5 path; renders the same template with the rerank score column highlighted.
- Templates:
  - `base.html`: navigation bar (links to each list), main content block, footer; HTMX 2.x script tag pointing to `/static/htmx.min.js`; links to `/static/style.css`.
  - Each list page includes `partials/<resource>_filters.html` (HTMX-powered form that reloads the list on change).
  - `partials/job_status.html`: HTMX partial that swaps in via `hx-trigger="every 2s"` and polls `GET /jobs/{id}/status` until terminal.
- Static:
  - `htmx.min.js`: vendored from the HTMX 2.x official release. Pin the URL (`https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js`) and a sha256 in the plan; the implementer downloads once via `httpx` (already a dep), writes the bytes to `src/doc3gpp/web/static/htmx.min.js`, and commits the file. No runtime download — the bundle ships in the wheel.
  - `style.css`: minimal CSS for the navigation + table + form layout.

**Steps:**
1. Implement each route module.
2. Implement the templates + partials.
3. Vendor HTMX 2.x and the stylesheet into `static/`.
4. Mount the routers in `web/app.py`.
5. Tests (`tests/unit/test_web_routes.py`):
   - Use `fastapi.testclient.TestClient(build_app(settings))` with an in-memory sqlite engine + `services/factory.build_all` overridden via dependency overrides.
   - Cover: `GET /`, `GET /meetings`, `GET /meetings/{id}` 200 + 404, `GET /tdocs`, `GET /tdocs/{id}` 200 + 404, `GET /tdocs/{id}/content?format=markdown` 200 (seed cache) + 404 (cache miss → hint), `GET /tdocs/{id}/content?format=html` 200, `GET /tsgs`, `GET /wis`, `GET /search?q=foo`, `GET /search/sem?q=foo`.
   - JSON parity: for each read, `GET /.../...?format=json` returns the same payload bytes as the corresponding unit-tested service method (`render.to_jsonable` is the single source of truth).

**Verification:**
```bash
ruff check .
./scripts/test_sqlite.sh -k web_routes
```

**Commit:** `feat(web): add read routes (landing/meetings/tdocs/tsgs/wis/search) + Jinja2 templates`

---

## Task 7: Job worker + handlers

**Files:**
- Create: `src/doc3gpp/web/workers/__init__.py`, `job_worker.py`, `handlers.py`
- Create: `tests/unit/test_job_worker.py`

**Interfaces:**
- `class JobWorkerHandle` (dataclass) with `task: asyncio.Task[None]`, `event_queues: dict[str, asyncio.Queue[dict]]`, `cancel_events: dict[str, asyncio.Event]`, `register_queue(job_id)`, `unregister_queue(job_id)`, `cancel(job_id: str) -> bool`, `shutdown()`.
- `class JobWorker` with `__init__(self, state: WebState, *, queue_size: int = 100)`.
- `async def run(self) -> None`:
  - Loop: `repo.list(status=QUEUED, limit=1)`; if present, claim via `mark_running`; resolve handler from `JobHandlers.KIND_TO_HANDLER`; create `cancel_event` + `event_queue`; run `handler(job, progress_callback=..., cancel_event=...)`.
  - `progress_callback(message)` formats `[<ISO timestamp>] <message>` then calls `repo.append_log(job_id, line=...)` + `event_queue.put_nowait({"event": "log", "data": {"line": line}})` (drop oldest when full).
  - On `CancelledError` / `cancel_event.is_set()`: emit `event: status` `data: {"status": "cancelled"}`, then `repo.mark_cancelled(job.id)`.
  - On `Exception`: emit `event: status` `data: {"status": "failed", "error": str(exc)}`, then `repo.mark_failed(job.id, error=str(exc))`.
  - On success: emit `event: status` `data: {"status": "succeeded", "summary": ...}`, then `repo.mark_succeeded(job.id, summary=...)`.
  - Sleep `Settings.server.cleanup_interval_seconds` between ticks; on each tick, run cleanup: `repo.delete_older_than(cutoff)`.
- `class JobHandlers`:
  - `KIND_TO_HANDLER: dict[JobKind, Callable[..., Awaitable[Mapping[str, JSONValue]]]]`
  - Each handler takes `(job: Job, services: ServiceContainer, settings: Settings, *, progress, cancel_event)` and calls into the existing `services/factory.build_*` methods (no business logic duplication).
  - Handlers (initial set):
    - `SYNC_MEETINGS(tsg)` → `meeting_service.sync(...)`.
    - `SYNC_TDOCS(meeting_id | meeting_name)` → `tdoc_sync_coordinator.sync_for_meeting_id(...)` / `.sync_for_meeting_name(...)`.
    - `SYNC_TDOCS_ALL()` → `tdoc_sync_coordinator.sync_all_tracked_meetings(...)`.
    - `PARSE_TDOCS(filter, force, full, max_batch)` → resolve matching TDocs via `tdoc_repository.list(...)` (using the same filter grammar as `TDocCrService.extract_many`), then call `tdoc_cr_service.extract_many(tdocs)` in batches; respect cooperative `cancel_event`. The handler is a thin iterator around the existing service methods — no parse logic is duplicated.
    - `REBUILD_SEARCH(stale_only, resume)` → `search_service.rebuild(...)` iterator.
    - `CACHE_PURGE(scope, yes)` → reuse `cache.purge(...)` from the CLI helper module.
- `workers/handlers.py` is the **only** place that maps `JobKind` → service method; new handlers land here.

**Steps:**
1. Implement `JobWorkerHandle` + `JobWorker` in `workers/job_worker.py`.
2. Implement `JobHandlers` in `workers/handlers.py`.
3. Wire lifespan in `web/app.py` (overwrite the T4 stub):
   - `app.state.web.worker = JobWorkerHandle(...)`; `app.state.web.worker.task = asyncio.create_task(JobWorker(...).run())`.
   - On shutdown: `await worker.shutdown()`.
4. Tests (`tests/unit/test_job_worker.py`):
   - In-memory sqlite; register a fake `JobRepository` + a `ServiceContainer` whose `meeting_service.sync` is a `MagicMock`.
   - `test_worker_runs_queued_job`: enqueue a `SYNC_MEETINGS` job; wait ≤2s; assert `mark_running`, `append_log` ≥1, `mark_succeeded` fired.
   - `test_worker_cancels_on_event`: enqueue a job; `worker.cancel(job_id)`; assert `mark_cancelled` and the handler raises `asyncio.CancelledError`.
   - `test_worker_drops_oldest_events`: fill queue past `queue_size`; oldest is discarded.
   - `test_worker_marks_failed_on_exception`: handler raises; `mark_failed` fired with `str(exc)`.
   - `test_worker_emits_named_sse_events`: assert the SSE queue receives at least one `{"event": "log", ...}` and a terminal `{"event": "status", "data": {"status": "succeeded", ...}}`.

**Verification:**
```bash
ruff check .
./scripts/test_sqlite.sh -k job_worker
```

**Commit:** `feat(web): add background job worker + per-kind handler registry`

---

## Task 8: Job HTTP routes (POST / poll / SSE / cancel)

**Files:**
- Create: `src/doc3gpp/web/routes/jobs.py`
- Extend: `src/doc3gpp/web/app.py` (mount router)
- Create: `tests/unit/test_web_jobs_routes.py`

**Interfaces:**
- Per the spec, jobs use nested URL paths (no `/jobs/{kind}` flat alias):
  - `POST /jobs/sync/meetings` body `{"tsg": "SA2"}`
  - `POST /jobs/sync/tdocs` body `{"meeting_id": "..."}` or `{"meeting": "..."}`
  - `POST /jobs/sync/tdocs/all`
  - `POST /jobs/parse/tdocs` body `{"filter": {...}, "force": bool, "full": bool, "max_batch": int?}`
  - `POST /jobs/search/rebuild` body `{"stale_only": bool, "resume": bool}`
  - `POST /jobs/cache/purge` body `{"scope": "markdown|zips|all", "yes": true}`
  - Each POST returns `202` with the spec's job envelope:
    ```json
    {"job_id": "<uuid>", "status": "queued", "links": {"self": "/jobs/<uuid>", "events": "/jobs/<uuid>/events"}}
    ```
- `GET /jobs?status=&limit=&offset=` → JSON list of jobs (last 50 by default, paginated by `limit`/`offset` per spec).
- `GET /jobs/{job_id}` → JSON for that job in the spec shape:
  ```json
  {"job_id": "...", "kind": "sync_meetings", "status": "running",
   "params": {...}, "result": null, "error": null,
   "summary": {"meetings": 14}, "log_tail": ["..."],
   "created_at": "...", "started_at": "...", "completed_at": null,
   "links": {...}}
  ```
- `GET /jobs/{job_id}/events` → `StreamingResponse` (`text/event-stream`) emitting the spec's SSE format:
  ```
  event: status
  data: {"status": "running"}

  event: log
  data: {"line": "[2026-08-02 12:00:01] fetched meeting SA2#156"}

  event: status
  data: {"status": "succeeded", "summary": {"tdocs": 200, "elapsed_s": 124.3}}
  ```
- `POST /jobs/{job_id}/cancel` → 200 with the updated job envelope (or 409 if already terminal).
- HTML counterparts:
  - `GET /jobs` → `job_status.html` listing running/recent jobs (HTMX polls `/jobs/{job_id}/events` via `partials/job_status.html`).
- Errors:
  - `JobNotFoundError` (new exception in `web/errors.py`) → 404.
  - `JobAlreadyTerminalError` (new exception in `web/errors.py`) → 409.

**Steps:**
1. Define `JobNotFoundError`, `JobAlreadyTerminalError` in `web/errors.py`.
2. Implement the routes in `web/routes/jobs.py` with Pydantic body models per `JobKind`.
3. Mount in `app.py`.
4. Tests (`tests/unit/test_web_jobs_routes.py`):
   - `test_post_creates_job`: `POST /jobs/sync/meetings` → 202 + correct envelope with `job_id`, `status: "queued"`, `links`.
   - `test_post_returns_400_for_unknown_path`: `POST /jobs/nope` → 400.
   - `test_get_jobs_lists_recent`: enqueue two jobs; `GET /jobs` → 200 with two entries.
   - `test_get_job_returns_404_for_unknown`.
   - `test_get_job_returns_log_tail`: enqueue a job, append 5 log lines, `GET /jobs/{id}` → `log_tail` has 5 entries.
   - `test_cancel_returns_409_when_terminal`: mark a job `SUCCEEDED`; `POST /jobs/{id}/cancel` → 409.
   - `test_events_stream_emits_named_events`: enqueue a job that completes quickly; consume the SSE stream via `TestClient.stream(...)`; assert at least one `event: log\ndata: {...}` line plus a terminal `event: status\ndata: {...,"status":"succeeded"...}`.

**Verification:**
```bash
ruff check .
./scripts/test_sqlite.sh -k web_jobs_routes
```

**Commit:** `feat(web): add /jobs routes with POST/list/get/SSE/cancel + JSON + HTML`

---

## Task 9: MCP server + sub-app mount

**Files:**
- Create: `src/doc3gpp/web/render.py`
- Create: `src/doc3gpp/web/mcp_server.py`
- Extend: `src/doc3gpp/web/app.py` (mount at `/mcp`)
- Create: `tests/integration/test_mcp_end_to_end.py` (skeleton + parity first; T12 finalises end-to-end)

**Interfaces:**
- `render.to_jsonable(obj)` → recursively turn dataclasses / `Enum` / `datetime` / `Mapping` / `Sequence` into JSON-safe Python primitives. Single source of truth for HTTP JSON + MCP tool results.
- `def build_mcp_server(state: WebState) -> FastMCP`:
  - `FastMCP(name="doc3gpp", stateless_http=True)`.
  - Tools (full list per spec §"MCP tool surface", lines 199–260):
    - Meetings: `list_meetings(tsg?, name?, location?, year?, tdoc?, limit?, offset?)`, `get_meeting(meeting_id)`.
    - TDocs: `list_tdocs(tdoc?, meeting?, meeting_id?, source?, spec?, wi?, title?, cr_cat?, status?, type?, revision_of?, revised_to?, ftp_url?, release?, version?, cr_num?, cr_pack?, uploaded_date?, limit?, offset?)`, `get_tdoc(tdoc_id)`, `get_tdoc_content(tdoc_id, format?)`.
    - TSGs: `list_tsgs()`, `get_tsg(short_name)`.
    - WIs: `list_wis(tsg?, name?, id?, limit?, offset?)`.
    - Search: `search(query, filter?)`, `search_semantic(query, fts5_query?, fts5_weight?, limit?)`.
    - Jobs: `list_jobs(status?, limit?)`, `get_job(job_id)`, `create_job(kind, params)`, `cancel_job(job_id)`.
  - Each tool returns `render.to_jsonable(...)` of the same DTO the HTTP route returns (parametrized parity test enforces this).
  - All tool docstrings surface the same filter grammar as the HTTP query-param parser (T5).
- Mount in `app.py`: when `Settings.server.enabled and Settings.mcp.enabled`, `app.mount("/mcp", mcp.streamable_http_app())`.

**Steps:**
1. Implement `render.to_jsonable` with coverage for the dataclasses used across services (Meeting, TDoc, Tsg, Wi, SearchHit, SemanticSearchHit, Job).
2. Implement `build_mcp_server`.
3. Mount in `app.py` (replace T4's `pass` placeholder).
4. Tests:
   - `tests/integration/test_mcp_end_to_end.py` skeleton:
     - `test_mcp_health`: `POST /mcp` initialise handshake (`initialize` request) returns protocol version + server info.
     - `test_list_meetings_parity`: seed two meetings; call `list_meetings` tool; assert response bytes equal `GET /meetings?format=json` bytes (use `render.to_jsonable` for both).
     - Repeat parity check for `list_tdocs`, `get_tdoc`, `search`, `list_jobs`.
   - Mark the test as `online=False` so it runs under `scripts/test_sqlite.sh`.

**Verification:**
```bash
ruff check .
./scripts/test_sqlite.sh -k mcp_end_to_end
```

**Commit:** `feat(web): add FastMCP server with read + job tools mounted at /mcp`

---

## Task 10: Install helper — systemd unit + launchd plist

**Files:**
- Create: `src/doc3gpp/web/install.py`
- Create: `tests/unit/test_web_install.py`

**Interfaces:**
- `def render_systemd_unit(*, exec_start: str, working_dir: str, env: Mapping[str, str], log_file: str, pid_file: str, description: str = "doc3gpp web + MCP server") -> str` — returns a `systemd.service` file body with `X-Doc3gpp-Managed=true` in the `[Unit]` section so `uninstall` can guard on it. Uses `Environment=DOC3GPP_CONFIG=...` lines for every `env` entry.
- `def render_launchd_plist(*, label: str, program_args: list[str], working_dir: str, log_file: str, pid_file: str, env: Mapping[str, str]) -> str` — returns a launchd plist body with the same marker (encoded into the plist's `Label` metadata comment block) and `RunAtLoad=true` + `KeepAlive=true` per spec.
- `def install_systemd(*, scope: Literal["user", "system"], no_start: bool = False, dry_run: bool = False) -> str` — returns the rendered unit path; when `dry_run`, only renders + prints without writing; otherwise writes to `~/.config/systemd/user/doc3gpp.service` (or `/etc/systemd/system/doc3gpp.service`); runs `systemctl --<scope> daemon-reload && enable --now doc3gpp` (or `daemon-reload` only when `no_start`).
- `def install_launchd(*, no_start: bool = False, dry_run: bool = False) -> str` — writes to `~/Library/LaunchAgents/org.doc3gpp.server.plist`; runs `launchctl load -w` unless `no_start`.
- `def uninstall_systemd(*, scope: Literal["user", "system"]) -> None` — refuses unless the unit file contains `X-Doc3gpp-Managed=true`; then disables + stops + removes the file.
- `def uninstall_launchd() -> None` — same marker guard; runs `launchctl unload` then removes the plist.

**Steps:**
1. Implement the helpers.
2. Tests (`tests/unit/test_web_install.py`):
   - `test_render_systemd_unit_includes_marker`: assert `X-Doc3gpp-Managed=true` in `[Unit]` + `ExecStart` contains the resolved path + `Environment=DOC3GPP_CONFIG=...` line.
   - `test_render_launchd_plist_includes_marker` + valid XML (`xml.etree.ElementTree.fromstring`) + `RunAtLoad=true` + `KeepAlive=true`.
   - `test_install_systemd_dry_run_does_not_write`: pass `dry_run=True` to `install_systemd`; no file exists afterwards; returns the would-be path.
   - `test_install_launchd_dry_run_does_not_write`: same for launchd.
   - `test_uninstall_refuses_unmanaged`: create a unit without the marker; `uninstall_systemd` raises `InstallNotManagedError`.
   - `test_uninstall_removes_managed`: create a managed unit; `uninstall_systemd` removes it.

**Verification:**
```bash
ruff check .
./scripts/test_sqlite.sh -k web_install
```

**Commit:** `feat(web): add systemd + launchd install/uninstall helpers`

---

## Task 11: CLI server start/stop real implementation

**Files:**
- Extend: `src/doc3gpp/cli_server.py`
- Create: `tests/integration/test_cli_server.py`

**Interfaces:**
- `server start [--host] [--port] [--open|--no-open] [--reload]`:
  - Refuses unless `Settings.server.enabled` is `True` (use `_require_server_enabled` from T2).
  - Resolves host/port: CLI flag overrides `Settings.server.host/port`; if `--open` is set (default per spec — flip if `--no-open` is given), opens the browser to `http://<host>:<port>/`.
  - Background mode (default, no `--reload`): `subprocess.Popen(["uvicorn", "doc3gpp.web.app:build_app", "--factory", "--host", ..., "--port", ...], env={...}, stdout=open(log_file, "ab"), stderr=STDOUT, start_new_session=True)`, write PID to `Settings.server.pid_file`; poll `/healthz` (T4 adds `GET /healthz` → `{"ok": true}`) until ready or timeout.
  - `--reload` mode: `uvicorn.run(...)` blocks the terminal with `reload=True` + `reload_dirs=["src/doc3gpp/web/"]` (per spec — watches the web subtree only to avoid re-running sync on unrelated file touches) + `log_level="debug"`.
- `server stop`:
  - Read PID file; `os.kill(pid, SIGTERM)`; wait up to 10s; `SIGKILL` if still alive; remove PID file.
- `server status`:
  - Per spec: combine OS service state (`systemctl --user is-active doc3gpp` / `launchctl list | grep doc3gpp`) with in-process state (PID alive?, uptime from PID file mtime, last job from `repo.list(limit=1)`). Print `running|stopped|not-installed` + PID + uptime + last job summary.
- `server logs [--job <id>] [--follow]`:
  - Default: `tail -n 50 <log_file>`. `--follow` switches to `tail -f`. `--job <id>` resolves the job's `log_lines` (last 50) via `JobRepository.get(...)` instead of the file tail.
- `server install systemd [--user|--system] [--no-start] [--dry-run]`:
  - Resolve paths (`sys.executable`, `shutil.which("doc3gpp")`, `Settings.config_path`); render the unit; `--dry-run` prints the rendered unit only; otherwise write + `systemctl --<scope> daemon-reload` + (unless `--no-start`) `enable --now doc3gpp`. Print the matching disable command for the user to remember.
- `server install launchd [--no-start] [--dry-run]`:
  - Same flow for `~/Library/LaunchAgents/org.doc3gpp.server.plist`; uses `launchctl load -w` unless `--no-start`.
- `server uninstall systemd [--user|--system]`:
  - Refuse unless unit contains `X-Doc3gpp-Managed=true`; run `systemctl --<scope> disable --now doc3gpp`; remove the unit file.
- `server uninstall launchd`:
  - Same marker guard; `launchctl unload`; remove the plist.

**Steps:**
1. Implement the six subcommands.
2. Tests (`tests/integration/test_cli_server.py`):
   - Use `tmp_path` for cache dir; bind to a random free port.
   - `test_install_user_dry_run_prints_unit_only`: `doc3gpp server install systemd --user --dry-run` → no file written; unit body printed to stdout.
   - `test_install_user_then_start_then_stop_then_uninstall`: full lifecycle against a sqlite + tempfile cache dir; PID file removed after stop; unit file removed after uninstall.
   - `test_start_refuses_when_disabled`: settings with `server.enabled=False` → `click.UsageError`.
   - `test_status_reports_not_running_when_pidfile_missing`.
   - `test_status_reports_running_when_pidfile_alive`.
   - `test_logs_follows_job_id`: enqueue a job; `doc3gpp server logs --job <id>` returns the `log_tail` (not the file tail).

**Verification:**
```bash
ruff check .
./scripts/test_sqlite.sh -k cli_server
```

**Commit:** `feat(cli): implement server start/stop/status/logs/install/uninstall against the web app`

---

## Task 12: End-to-end integration + docs

**Files:**
- Finalise: `tests/integration/test_web_end_to_end.py`
- Finalise: `tests/integration/test_mcp_end_to_end.py`
- Create: `docs/web-server.md`
- Extend: `docs/cli.md`, `docs/architecture.md`, `docs/code-map.md`, `AGENTS.md`, `README.md`
- Extend: `scripts/dev_run.sh`

**Interfaces:**
- `tests/integration/test_web_end_to_end.py`:
  - `test_full_lifecycle`: install → start (random free port) → `GET /healthz` → seed a meeting + tdoc → `GET /meetings/{id}` (HTML + JSON) → `POST /jobs/sync_meetings` → poll until terminal → `GET /jobs/{id}/events` SSE → stop → uninstall. Asserts every step.
  - `test_cache_miss_returns_hint`: `GET /tdocs/{tdoc_id}/content?format=markdown` on an unparsed tdoc → 404 with the hint message in the body.
- `tests/integration/test_mcp_end_to_end.py` (finalised):
  - Full parity test: for every read tool (`list_meetings`, `get_meeting`, `list_tdocs`, `get_tdoc`, `list_wis`, `search`), assert tool result bytes equal the corresponding `?format=json` HTTP route bytes.
  - Job parity: `create_job` via MCP matches `POST /jobs/{kind}`; `cancel_job` matches `POST /jobs/{id}/cancel`.
- `docs/web-server.md`: end-user guide
  - Prerequisites (`pip install doc3gpp[web]`).
  - Quick start (config init, enable server, install, start, browse, MCP client config snippet).
  - Routes reference (every URL + query-param).
  - Jobs reference (every kind, params, how to cancel).
  - MCP reference (tools list, JSON shape parity note).
  - Logs + retention + cleanup behaviour.
  - Uninstallation.
- `docs/cli.md`: document the `server` sub-app (each command, each flag).
- `docs/architecture.md`: new "Web layer + MCP + Jobs" section + workflow bullets (CLI HTTP, browser → HTTP → services → repo; AI client → /mcp → services → repo; CLI start → uvicorn → lifespan → worker).
- `docs/code-map.md`: every new file row in T1 file-structure table.
- `AGENTS.md`: new "Where to look" rows for web layer, MCP, jobs.
- `README.md`: short "Web server + MCP" section near the top.
- `scripts/dev_run.sh`: optional `doc3gpp server start --dev --foreground` smoke (commented out by default).

**Steps:**
1. Write the final integration tests; run the full sqlite suite end-to-end.
2. Write/update all docs in a single change.
3. Update `scripts/dev_run.sh`.

**Verification:**
```bash
ruff check .
./scripts/test_sqlite.sh
```

**Commit:** `test(web): full HTTP + MCP end-to-end parity` (split into two commits if too big: one for tests, one for docs).

---

## Self-review against spec (acceptance criteria from `docs/superpowers/specs/2026-08-02-web-and-mcp-server-design.md` §"Acceptance criteria")

| Spec AC | Task(s) covering it |
| --- | --- |
| AC1 — `pip install "doc3gpp[all]"` resolves cleanly with the new `[web]` extra | T1 (pyproject `[all]` includes `[web]`) |
| AC2 — `doc3gpp server start` boots + serves `/`, `/tdocs`, `/tdocs/{id}`, `/tdocs/{id}/content`, `/meetings`, `/tsgs`, `/wis`, `/search` | T11 (start) + T6 (every route) + T12 (lifecycle test boots + curls every URL) |
| AC3 — `GET /mcp` (via `mcp.ClientSession`) exposes every tool listed in spec §"MCP tool surface" with descriptions auto-generated from function signatures | T9 (FastMCP tool registration) + T12 (parity test) |
| AC4 — `doc3gpp server install systemd --dry-run` prints the rendered unit without writing | T10 (`render_systemd_unit` + `install_systemd(dry_run=True)`) + T11 (CLI flag) + T12 (CLI integration test) |
| AC5 — `doc3gpp server install systemd --user` writes the unit, runs `daemon-reload`, enables and starts the service | T11 (`install_systemd` wires `daemon-reload && enable --now doc3gpp`) + T12 (lifecycle test) |
| AC6 — `doc3gpp server uninstall systemd --user` removes the unit, stops and disables the service, refuses if not doc3gpp-managed | T10 (`X-Doc3gpp-Managed=true` guard) + T11 (`uninstall_systemd` calls `disable --now` + removes file) + T12 (refusal + happy path) |
| AC7 — `POST /jobs/sync/meetings {"tsg":"SA2"}` returns 202 with job id; `GET /jobs/{id}` polls; `GET /jobs/{id}/events` streams logs | T8 (nested URL paths + spec envelope) + T12 (integration test posts + polls + consumes SSE) |
| AC8 — `POST /jobs/{id}/cancel` flips the job to `cancelled` on the next checkpoint | T7 (cooperative `cancel_event` + `mark_cancelled`) + T8 (route) + T12 (integration test) |
| AC9 — HTTP JSON shape == MCP tool result shape byte-for-byte | T9 (`render.to_jsonable` shared source) + T12 (parametrized parity test) |
| AC10 — CLI filter grammar used in `?filter=...` parses identically to CLI's `--filter` | T5 (web filter parser reuses `cli_filters` + `tdoc_sql` helpers) + T12 (parametrized identity test) |
| AC11 — `GET /tdocs/{id}/content` returns 404 with a parse-hint payload on cache miss | T6 (404 + hint body) + T12 (cache-miss test) |
| AC12 — `doc3gpp db init` provisions the `jobs` table on a fresh DB without error | T3 (ORM model picked up by `Base.metadata.create_all`) + T12 (fresh-DB smoke test) |
| AC13 — `./scripts/test_sqlite.sh` passes | every task (verification step) + T12 (final run) |
| AC14 — `ruff check .` passes | every task (verification step) + T12 (final run) |

Coverage gaps after self-review: none. Every AC has at least one task + at least one test case that asserts it.

## Execution choice

Two ways to run this plan:
1. **subagent-driven-development** — dispatch the 12 tasks to a `general` subagent one at a time; you review the diff between tasks and can iterate per task. Recommended when the user wants tight control + commit-by-commit checkpoints.
2. **executing-plans** — same agent executes every task in sequence with checklist updates; faster, less per-task review.

I'll ask which mode the user prefers before kicking off T1.
