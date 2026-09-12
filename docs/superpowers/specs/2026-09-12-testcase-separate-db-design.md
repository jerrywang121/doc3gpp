# Separate Testcase Database — Design

**Date:** 2026-09-12
**Branch:** `feature/ran5-testcases`
**Status:** approved, pending implementation plan

## Goal

Move the three testcase tables (`testcases`, `testcase_status`,
`testcase_sources`) out of the main database file into a dedicated
sqlite file so the testcase corpus can be reset independently of all
other data.

## Non-goals

- No cross-database joins or foreign keys between the main DB and the
  testcase DB.
- No change to table names, column shapes, or the `(testcase_id,
  group)` / `(testcase_id, group, path)` identity work.
- No change to sync/parse/list/show semantics beyond which engine
  they hit.
- No alembic involvement (bootstrap stays `create_all`-based).

## Architecture

Separate declarative base + second engine:

- New `TestCaseBase(DeclarativeBase)` in
  `src/doc3gpp/storage/db/testcase_base.py`.
- `TestCaseORM`, `TestCaseStatusORM`, `TestCaseSourceORM` re-parent
  from `Base` to `TestCaseBase`. Table names unchanged. The composite
  `ForeignKeyConstraint` on `testcase_status → testcases` stays
  intact *inside* the testcase DB.
- `Base.metadata.create_all` no longer creates testcase tables;
  `TestCaseBase.metadata.create_all` creates exactly the three
  testcase tables.
- New `get_testcase_engine()` / `get_testcase_session_factory()` in
  `src/doc3gpp/storage/db/session.py` (same `lru_cache` + sqlite
  backend-kwargs shape as the main pair).
- `SQLAlchemyTestCaseRepository` defaults to the testcase session
  factory. Its constructor override (explicit `session_factory`) is
  unchanged, so unit tests binding to in-memory sqlite keep working.
- `create_schema(scope: Literal["main", "testcase", "all"] = "all")`:
  `"main"` runs the existing one-shot migrations + `Base.create_all`
  + FTS5/vector schema on the main engine; `"testcase"` runs
  `TestCaseBase.create_all` on the testcase engine; `"all"` does
  both. Default `"all"` preserves today's call-site behavior.

## Settings

- New `Settings.testcase_database_url: str | None = None`.
  - TOML key `testcase_database_url`; env `DOC3GPP_TESTCASE_DATABASE_URL`
    added to `ALLOWED_ENV_VARS`.
  - `None` means "derive a sibling": same directory as
    `database_url`, stem suffixed with `_testcase`
    (`doc3gpp.db` → `doc3gpp_testcase.db`). Non-file URLs:
    `:memory:` main → shared-cache memory DB for testcases;
    non-sqlite testcase URL is accepted at settings level and fails
    only when the testcase engine is first used (mirrors how the
    main URL behaves for non-sqlite backends).
  - Derivation helper lives next to the session module and is
    unit-testable without an engine.

- `db check` prints both URLs (main + resolved testcase) regardless
  of scope; `--scope` selects which engine(s) to actually connect to.

## CLI (`db` sub-app)

- `db init | reset | check` each gain
  `--scope main|testcase|all` (default `"all"`).
  - `init --scope=X` creates only that scope's schema (`init` still
    seeds `tsgs` when main is in scope).
  - `check --scope=X` connects to the selected engine(s) and prints
    the corresponding URL(s).
  - `reset --scope=X` deletes the selected sqlite file(s) + WAL
    sidecars (`-wal`, `-shm`, `-journal`), clears **both** engine
    caches, recreates the selected schema(s), re-seeds `tsgs` when
    main is in scope.
- Guard (user ruling): `reset` requires **all selected scopes** to be
  sqlite URLs; any non-sqlite URL in the selection rejects the whole
  reset with a per-scope message. Mixed main/testcase backends are
  allowed for normal operation — only `reset` demands sqlite.
- The bare `create_schema()` call sites (meeting/tsg/wi/spec/testcase
  sync commands) become `create_schema("all")` — same behavior,
  explicit scope.

## Web / server

- `WebState` gains `testcase_engine: Engine`; `build_state` wires it
  via `get_testcase_engine()`; lifespan disposes both engines.
- No route/handler changes: web + MCP reach testcases through
  `TestCaseService` → repo → testcase engine.
- `build_testcase_service` (factory) constructs the repo with no
  args, so it picks up the testcase factory automatically.

## Data flow

```
testcase sync/list/show (CLI, web, MCP, jobs)
  → TestCaseService
    → SQLAlchemyTestCaseRepository(session_factory=get_testcase_session_factory())
      → get_testcase_engine() → testcase_database_url (or sibling of database_url)
```

Main-DB traffic is untouched: every other repository keeps using
`get_session_factory()` → `get_engine()` → `database_url`.

## Fresh vs existing installs

- Fresh installs: `db init` creates both files; sibling derivation
  needs no configuration.
- Existing installs: the main file still contains the three testcase
  tables as orphans after upgrade (harmless — nothing reads them;
  `Base.create_all` will not drop them). Operators reclaim the space
  with `db reset --scope main` (wipes main!) or leave them. The
  testcase corpus re-syncs from scratch via `testcase sync` into the
  new file. No data migration: the file-identity skip rule means the
  first sync into the empty testcase DB downloads the latest zip
  again.

## Error handling

- Missing testcase file on first use: auto-created by the sqlite
  backend helper (parent dirs via `mkdir -p`), same as the main DB.
- Non-sqlite testcase URL on `reset`: `typer.BadParameter` naming
  the offending scope and URL.
- Testcase engine failure at runtime: propagates as today (no new
  envelope); `db check --scope testcase` is the diagnostic.

## Testing

- `sqlite_env` (tests/conftest.py) also pins the testcase URL to
  `tmp_path/test_testcase.db` and clears both engine + settings
  caches on setup/teardown.
- New unit tests: sibling derivation (file stem, `:memory:`,
  explicit override, non-sqlite passthrough).
- New integration tests: `create_schema("testcase")` creates only
  the 3 tables on the testcase engine and none on main (and vice
  versa); FK containment (status insert without header fails);
  independent reset (wipe testcase keeps main rows; wipe main keeps
  testcase rows); `db reset --scope` CLI end-to-end on tmp files;
  non-sqlite rejection names the scope.
- Full gate: `./scripts/test_sqlite.sh` + `ruff check .`.

## Docs to update in the implementation

- `AGENTS.md` (testcase workflow one-liners: two-DB note).
- `docs/cli.md` (`db init/reset/check --scope`, new setting).
- `docs/architecture.md` (storage section: second base/engine).
- `docs/code-map.md` (new symbols).
- `docs/web-server.md` if lifespan/state shape is user-visible.
- `doc3gpp.toml.example` (`testcase_database_url` commented key).
